"""Business glossary (glossary.yaml): load, validate and map terms to tables.

The frozen YAML contract (consumed by milestone 3 cards and the CLI)::

    terms:
      - term: 毛利                    # required, non-empty string, globally unique
        synonyms: [毛利润, GM]        # optional, defaults to []
        maps_to:                      # required
          table: sales_2024           # required, must exist in the catalog
          column: gross_profit        # optional, must exist in that table
          expression: "amount - cost" # optional, DuckDB SQL, parsed via sqlglot
          filter: "status = 'done'"   # optional, DuckDB SQL, parsed via sqlglot
        description: "..."            # optional

Design decisions:

- plain ``dataclass`` plus hand-written validation instead of pydantic: no new
  dependency, and error messages can speak directly to Chinese users with
  in-file positions ("第 N 个术语 ...");
- structural checks run in :func:`load_glossary` and raise on the first bad
  term, while reference checks (dangling tables/columns, SQL parseability) run
  in :func:`validate_glossary` and collect *all* problems; ``glossary check``
  reports the collected list, whereas :func:`terms_by_table_map` fails fast so
  a broken glossary can never pollute generated cards;
- ``filter`` is expected to be a boolean expression, but the MVP only verifies
  that it parses as SQL — it deliberately does not judge boolean-ness.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
import yaml

from catalog.store import CatalogStore
from nl2data.config import Nl2DataConfig

_SQL_DIALECT = "duckdb"
_CANDIDATE_COUNT = 3
_CANDIDATE_CUTOFF = 0.4
_NO_TABLE_CANDIDATE = "(无相似表)"
_NO_COLUMN_CANDIDATE = "(无相似列)"


class GlossaryError(RuntimeError):
    """Raised when glossary.yaml cannot be parsed or fails reference checks."""


@dataclass(frozen=True)
class MapsTo:
    """Where a term lives: a catalog table plus optional column/expression/filter."""

    table: str
    column: str | None = None
    expression: str | None = None
    filter: str | None = None


@dataclass(frozen=True)
class GlossaryEntry:
    """One glossary term with its mapping and metadata.

    ``metric_filter`` is a term-level metric calibre (T14): unlike the
    table-local ``maps_to.filter`` it applies globally, on every table listed
    in ``applies_to``. The two filter keys are mutually exclusive.
    """

    term: str
    maps_to: MapsTo
    synonyms: list[str] = field(default_factory=list)
    description: str | None = None
    metric_filter: str | None = None
    applies_to: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the card format; ``None`` fields stay ``null``.

        ``metric_filter`` / ``applies_to`` are only emitted when set, so
        legacy glossaries render exactly as before.
        """
        out: dict[str, Any] = {
            "term": self.term,
            "synonyms": list(self.synonyms),
            "description": self.description,
            "maps_to": {
                "table": self.maps_to.table,
                "column": self.maps_to.column,
                "expression": self.maps_to.expression,
                "filter": self.maps_to.filter,
            },
        }
        if self.metric_filter is not None:
            out["metric_filter"] = self.metric_filter
        if self.applies_to:
            out["applies_to"] = list(self.applies_to)
        return out


class GlossaryStore:
    """Loaded and structurally valid glossary; reference validity is a separate step."""

    def __init__(self, entries: list[GlossaryEntry] | None = None) -> None:
        """Build a store from already validated entries (default: empty)."""
        self._entries: tuple[GlossaryEntry, ...] = tuple(entries or ())
        self._by_name: dict[str, GlossaryEntry] = {}
        for entry in self._entries:
            for name in (entry.term, *entry.synonyms):
                self._by_name.setdefault(_normalize_name(name), entry)

    @property
    def entries(self) -> list[GlossaryEntry]:
        """All entries in glossary file order."""
        return list(self._entries)

    def lookup_term(self, name: str) -> GlossaryEntry | None:
        """Exact-match ``name`` against terms and synonyms (strip + case-insensitive)."""
        return self._by_name.get(_normalize_name(name))

    def terms_for_table(self, table: str) -> list[GlossaryEntry]:
        """Entries attached to ``table``, in file order.

        Membership is ``maps_to.table == table`` or ``table in applies_to``
        (metric-calibre terms attach to every applicable table).
        """
        return [
            entry
            for entry in self._entries
            if entry.maps_to.table == table or table in entry.applies_to
        ]


def load_glossary(path: Path, cfg: Nl2DataConfig | None = None) -> GlossaryStore:
    """Load glossary.yaml and run structural validation.

    A missing file, an empty file, or an empty ``terms`` list is a legal empty
    glossary. Structural problems (missing/non-string ``term``, non-list
    ``synonyms``, missing/non-mapping ``maps_to``, duplicate ``term``) raise
    :class:`GlossaryError` with the term's position ("第 N 个术语").

    Args:
        path: Path to glossary.yaml.
        cfg: Optional active configuration; structural loading is
            config-independent, the parameter exists for interface symmetry.

    Returns:
        The structurally validated :class:`GlossaryStore`.

    Raises:
        GlossaryError: If the YAML is unparseable or a term is malformed.
    """
    path = Path(path)
    if not path.is_file():
        return GlossaryStore()
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"glossary {path}: YAML 解析失败:{exc}"
        raise GlossaryError(msg) from exc
    if raw is None:  # empty file
        return GlossaryStore()
    if not isinstance(raw, dict):
        msg = f"glossary {path}:根节点必须是 mapping,实际为 {_type_name(raw)}"
        raise GlossaryError(msg)
    terms = raw.get("terms")
    if terms is None:
        return GlossaryStore()
    if not isinstance(terms, list):
        msg = f"glossary {path}:terms 必须是列表,实际为 {_type_name(terms)}"
        raise GlossaryError(msg)

    seen: set[str] = set()
    entries: list[GlossaryEntry] = []
    for index, item in enumerate(terms, start=1):
        entry = _parse_entry(path, index, item)
        if entry.term in seen:
            msg = f"glossary {path}: 第 {index} 个术语(term={entry.term!r})与之前的术语重复"
            raise GlossaryError(msg)
        seen.add(entry.term)
        entries.append(entry)
    return GlossaryStore(entries)


def validate_glossary(store: GlossaryStore, cfg: Nl2DataConfig) -> list[str]:
    """Collect every reference-level problem; an empty list means the glossary passes.

    Checks, per term in file order:

    1. dangling table: ``maps_to.table`` missing from catalog.yaml (a missing
       catalog file counts as an empty catalog, so every table dangles);
    2. dangling column: ``maps_to.column`` missing from its table, only checked
       when the table itself exists;
    3. ``expression`` / ``filter`` must parse as DuckDB SQL via sqlglot.

    Args:
        store: A structurally valid store from :func:`load_glossary`.
        cfg: Active configuration (supplies the catalog path).

    Returns:
        One human-readable problem message per finding; empty when valid.
    """
    catalog = CatalogStore(cfg.paths.catalog)
    tables: dict[str, list[str]] = {}
    for source in catalog.sources:
        for table in source.tables:
            if table.name not in tables:
                tables[table.name] = [column.name for column in table.columns]

    problems: list[str] = []
    for entry in store.entries:
        if entry.maps_to.table not in tables:
            candidates = _close_matches(entry.maps_to.table, list(tables))
            problems.append(
                f"术语 {entry.term} → 表 {entry.maps_to.table} 不存在,"
                f"候选:{_join_candidates(candidates, _NO_TABLE_CANDIDATE)}"
            )
            continue
        columns = tables[entry.maps_to.table]
        if entry.maps_to.column is not None and entry.maps_to.column not in columns:
            candidates = _close_matches(entry.maps_to.column, columns)
            problems.append(
                f"术语 {entry.term} → 表 {entry.maps_to.table} 的列 "
                f"{entry.maps_to.column} 不存在,"
                f"候选:{_join_candidates(candidates, _NO_COLUMN_CANDIDATE)}"
            )
        for kind, text in (
            ("expression", entry.maps_to.expression),
            ("filter", entry.maps_to.filter),
            ("metric_filter", entry.metric_filter),
        ):
            if text is None:
                continue
            problem = _sql_problem(kind, entry.term, text)
            if problem is not None:
                problems.append(problem)
        for table in entry.applies_to:
            if table not in tables:
                candidates = _close_matches(table, list(tables))
                problems.append(
                    f"术语 {entry.term} → applies_to 表 {table} 不存在,"
                    f"候选:{_join_candidates(candidates, _NO_TABLE_CANDIDATE)}"
                )
    return problems


def terms_by_table_map(cfg: Nl2DataConfig) -> dict[str, list[dict[str, Any]]]:
    """Load + validate the glossary and group serialized terms by clean table name.

    This is the single assembly entry point for card generation (T5) and the
    CLI. It fails fast: any structural or reference problem raises
    :class:`GlossaryError` whose message carries every problem on its own line.

    Args:
        cfg: Active configuration (glossary and catalog paths).

    Returns:
        Mapping of clean table name to that table's terms in the frozen card
        format, each group preserving glossary file order; ``{}`` for an
        empty glossary.

    Raises:
        GlossaryError: If the glossary has any structural or reference problem.
    """
    store = load_glossary(cfg.paths.glossary, cfg)
    problems = validate_glossary(store, cfg)
    if problems:
        msg = "\n".join(problems)
        raise GlossaryError(msg)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in store.entries:
        # Metric-calibre terms attach to every applicable table (T14);
        # regular terms attach to their home table only.
        targets = list(entry.applies_to) if entry.metric_filter else []
        if entry.maps_to.table not in targets:
            targets.append(entry.maps_to.table)
        for table in targets:
            grouped.setdefault(table, []).append(entry.to_dict())
    return grouped


def _normalize_name(name: str) -> str:
    """Normalize a term/synonym for exact matching: strip + casefold."""
    return name.strip().casefold()


def _type_name(value: Any) -> str:
    """Return a readable type name for error messages."""
    return type(value).__name__


def _require_non_empty_str(value: Any) -> bool:
    """Whether ``value`` is a string with non-whitespace content."""
    return isinstance(value, str) and bool(value.strip())


def _parse_entry(path: Path, index: int, item: Any) -> GlossaryEntry:
    """Structurally validate one ``terms`` item with position-aware errors.

    Raises:
        GlossaryError: On any malformed field.
    """
    if not isinstance(item, dict):
        msg = f"glossary {path}: 第 {index} 个术语必须是 mapping,实际为 {_type_name(item)}"
        raise GlossaryError(msg)
    term_value = item.get("term")
    if not _require_non_empty_str(term_value):
        msg = f"glossary {path}: 第 {index} 个术语缺少必填的 term 或其不是非空字符串"
        raise GlossaryError(msg)
    term = str(term_value)

    synonyms_value = item.get("synonyms")
    if synonyms_value is None:
        synonyms_value = []
    if not isinstance(synonyms_value, list) or not all(
        isinstance(synonym, str) for synonym in synonyms_value
    ):
        msg = f"glossary {path}: 第 {index} 个术语(term={term!r})的 synonyms 必须是字符串列表"
        raise GlossaryError(msg)

    maps_value = item.get("maps_to")
    if maps_value is None:
        msg = f"glossary {path}: 第 {index} 个术语(term={term!r})缺少必填的 maps_to"
        raise GlossaryError(msg)
    if not isinstance(maps_value, dict):
        msg = f"glossary {path}: 第 {index} 个术语(term={term!r})的 maps_to 必须是 mapping"
        raise GlossaryError(msg)
    table_value = maps_value.get("table")
    if not _require_non_empty_str(table_value):
        msg = (
            f"glossary {path}: 第 {index} 个术语(term={term!r})的 "
            "maps_to.table 缺失或不是非空字符串"
        )
        raise GlossaryError(msg)
    for key in ("column", "expression", "filter"):
        value = maps_value.get(key)
        if value is not None and not isinstance(value, str):
            msg = (
                f"glossary {path}: 第 {index} 个术语(term={term!r})的 "
                f"maps_to.{key} 必须是字符串或 null,实际为 {_type_name(value)}"
            )
            raise GlossaryError(msg)

    description_value = item.get("description")
    if description_value is not None and not isinstance(description_value, str):
        msg = (
            f"glossary {path}: 第 {index} 个术语(term={term!r})的 "
            f"description 必须是字符串或 null,实际为 {_type_name(description_value)}"
        )
        raise GlossaryError(msg)

    # T14 metric-calibre keys: optional, mutually exclusive with maps_to.filter,
    # and applies_to only makes sense together with metric_filter.
    metric_filter_value = item.get("metric_filter")
    if metric_filter_value is not None and not isinstance(metric_filter_value, str):
        msg = (
            f"glossary {path}: 第 {index} 个术语(term={term!r})的 "
            f"metric_filter 必须是字符串或 null,实际为 {_type_name(metric_filter_value)}"
        )
        raise GlossaryError(msg)
    applies_to_value = item.get("applies_to")
    if applies_to_value is None:
        applies_to_value = []
    if not isinstance(applies_to_value, list) or not all(
        isinstance(t, str) and t for t in applies_to_value
    ):
        msg = (
            f"glossary {path}: 第 {index} 个术语(term={term!r})的 "
            "applies_to 必须是非空字符串列表"
        )
        raise GlossaryError(msg)
    filter_text: str | None = maps_value.get("filter")
    if metric_filter_value is not None:
        if filter_text is not None:
            msg = (
                f"glossary {path}: 第 {index} 个术语(term={term!r})的 "
                "filter 与 metric_filter 互斥,只能二选一"
            )
            raise GlossaryError(msg)
        if not applies_to_value:
            msg = (
                f"glossary {path}: 第 {index} 个术语(term={term!r})的 "
                "metric_filter 必须同时给出 applies_to(指标口径的作用表)"
            )
            raise GlossaryError(msg)
    elif applies_to_value:
        msg = (
            f"glossary {path}: 第 {index} 个术语(term={term!r})的 "
            "applies_to 仅可与 metric_filter 搭配使用"
        )
        raise GlossaryError(msg)

    column: str | None = maps_value.get("column")
    expression: str | None = maps_value.get("expression")
    return GlossaryEntry(
        term=term,
        synonyms=list(synonyms_value),
        description=description_value,
        maps_to=MapsTo(
            table=str(table_value),
            column=column,
            expression=expression,
            filter=filter_text,
        ),
        metric_filter=metric_filter_value,
        applies_to=list(applies_to_value),
    )


def _close_matches(name: str, choices: list[str]) -> list[str]:
    """Similar-name suggestions for dangling table/column reports."""
    return difflib.get_close_matches(name, choices, n=_CANDIDATE_COUNT, cutoff=_CANDIDATE_CUTOFF)


def _join_candidates(candidates: list[str], fallback: str) -> str:
    """Join candidate names with 、 or return the fallback when none exist."""
    return "、".join(candidates) if candidates else fallback


def _sql_problem(kind: str, term: str, text: str) -> str | None:
    """Return the problem message when ``text`` fails to parse as DuckDB SQL.

    sqlglot raises ``ParseError`` subclasses; the broad catch keeps reference
    validation resilient to any sqlglot-side exception.
    """
    try:
        sqlglot.parse_one(text, dialect=_SQL_DIALECT)
    except Exception as exc:  # any sqlglot-side failure means invalid SQL here
        rendered = str(exc).strip()
        summary = rendered.splitlines()[0] if rendered else type(exc).__name__
        return f"术语 {term} 的 {kind} 不是合法 SQL:{text}({summary})"
    return None
