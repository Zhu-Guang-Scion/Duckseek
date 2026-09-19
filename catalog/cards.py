"""M-Schema table card generation (milestone 2, T5).

For every profile under ``data/catalog/profiles`` two artifacts are produced:

- a machine-readable JSON card at ``data/catalog/cards/<table>.json``
- a human-review Markdown card (M-Schema style) at
  ``data/catalog/cards_md/<table>.md``

Building is incremental by default: a card is rebuilt only when its JSON file
is older than the profile or the table-notes file. Table descriptions come
from :mod:`catalog.table_notes`; business terms are injected by the caller,
so this module stays decoupled from the glossary (T6).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from catalog.store import CatalogStore
from catalog.table_notes import load_table_notes
from nl2data.config import Nl2DataConfig
from retrieval.tokens import estimate_tokens

logger = logging.getLogger(__name__)

# Profile stat keys that collapse into the card column's ``stats`` dict.
_STAT_KEYS: tuple[str, ...] = ("min", "max", "quantiles", "avg_len")


def _profile_ref(cfg: Nl2DataConfig, table: str) -> str:
    """POSIX path of the profile JSON relative to the data root."""
    path = cfg.paths.profiles_dir / f"{table}.json"
    try:
        return path.relative_to(cfg.paths.data_dir.parent).as_posix()
    except ValueError:
        return path.as_posix()


def _truncate(value: Any, max_chars: int) -> Any:
    """Truncate an over-long string enum value; other values pass through."""
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + "…"
    return value


def _card_column(col: dict[str, Any], cfg: Nl2DataConfig) -> dict[str, Any]:
    """Build one card column entry from its profile counterpart."""
    name = str(col.get("name", ""))
    card_col: dict[str, Any] = {
        "name": name,
        "original_name": col.get("original_name", name),
        "dtype": col.get("dtype", ""),
        "description": None,  # reserved for future human/LLM annotations
        "null_rate": col.get("null_rate"),
        "distinct_count": col.get("distinct_count", 0),
    }
    if "enum_values" in col:
        card_col["enum_values"] = [
            _truncate(value, cfg.cards.enum_value_max_chars)
            for value in col["enum_values"]
        ]
    if "sample_values" in col:
        card_col["sample_values"] = list(col["sample_values"])
    # Whichever stat keys the profile carries collapse into one dict; the key
    # itself is always present (an empty dict when the profile has none).
    card_col["stats"] = {key: col[key] for key in _STAT_KEYS if key in col}
    return card_col


def _cell_values(values: list[Any]) -> str:
    """Render a value list as the comma+space markdown cell payload."""
    return ", ".join(str(value) for value in values)


def _enum_sample_cell(col: dict[str, Any]) -> str:
    """The 枚举/样本 cell: enums win over samples; both empty render (空)."""
    enum = col.get("enum_values") or []
    if enum:
        return f"枚举: {_cell_values(enum)}"
    samples = col.get("sample_values") or []
    if samples:
        return f"样本: {_cell_values(samples)}"
    return "(空)"


def _rate_cell(value: Any) -> str:
    """Render the null-rate cell; a missing rate degrades to a dash."""
    return "-" if value is None else str(value)


def _term_fragment(term: dict[str, Any]) -> str:
    """Render one ``term(别名:...)→ target`` fragment with its calibre.

    A metric calibre (``metric_filter``) is marked global and cross-table;
    a table-local ``maps_to.filter`` is marked table-scoped. The two are
    visually distinct so the LLM cannot mistake their scope.
    """
    synonyms = "/".join(str(alias) for alias in term.get("synonyms", []))
    maps_to = term.get("maps_to") or {}
    target = maps_to.get("column") or maps_to.get("expression") or "-"
    fragment = f"{term.get('term', '')}(别名:{synonyms})→ {target}"
    metric_filter = term.get("metric_filter")
    if metric_filter:
        fragment += f" | 指标口径(全局适用,跨表生效): {metric_filter}"
    table_filter = maps_to.get("filter")
    if table_filter:
        fragment += f" | 表级口径(仅本表): {table_filter}"
    return fragment


def _terms_line(terms: list[dict[str, Any]]) -> str:
    """Render the trailing 适用术语 line of the markdown card."""
    if not terms:
        return "适用术语:(无)"
    return "适用术语:" + ";".join(_term_fragment(term) for term in terms)


def _render_markdown(card: dict[str, Any], cfg: Nl2DataConfig) -> str:
    """Render the frozen M-Schema markdown template for a card."""
    limit = max(int(cfg.cards.markdown_column_fold_limit), 0)
    columns: list[dict[str, Any]] = card["columns"]
    shown = columns[:limit] if len(columns) > limit else columns
    description = card["description"] or "(无)"
    lines: list[str] = [
        f"# 表:{card['table']}(原名:{card['original_name']})",
        "",
        f"- 来源:{card['source']} | 行数:{card['rows']} | 说明:{description}",
        "",
        "| 列 | 原名 | 类型 | 空值率 | 基数 | 枚举/样本 |",
        "|---|---|---|---|---|---|",
    ]
    for col in shown:
        lines.append(
            f"| {col['name']} | {col['original_name']} | {col['dtype']} "
            f"| {_rate_cell(col['null_rate'])} | {col['distinct_count']} "
            f"| {_enum_sample_cell(col)} |"
        )
    if len(columns) > limit:
        lines.append("")
        lines.append(
            f"> 共 {len(columns)} 列,已折叠其余 {len(columns) - limit} 列"
            "(完整清单见 JSON 卡片)"
        )
    lines.append("")
    lines.append(_terms_line(card["terms"]))
    return "\n".join(lines) + "\n"


def build_card(
    profile: dict[str, Any],
    description: str | None,
    terms: list[dict[str, Any]],
    cfg: Nl2DataConfig,
) -> tuple[dict[str, Any], str]:
    """Assemble the JSON card dict and markdown text for one profile.

    Args:
        profile: Profile dict as produced by ``catalog.profiler``.
        description: Human table description (from table notes); ``None``
            when no note matched the table.
        terms: Business-term entries to surface in the card; empty when the
            caller has none.
        cfg: Active configuration; supplies card thresholds and the token
            estimator.

    Returns:
        The ``(card, markdown)`` pair; ``card["token_estimate"]`` measures
        the returned markdown text.
    """
    table = str(profile.get("table", ""))
    card: dict[str, Any] = {
        "table": table,
        "original_name": profile.get("original_name"),
        "source": profile.get("source"),
        "description": description,
        "rows": int(profile.get("rows", 0)),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "profile_ref": _profile_ref(cfg, table),
        "columns": [_card_column(col, cfg) for col in profile.get("columns", [])],
        "terms": list(terms),
    }
    markdown = _render_markdown(card, cfg)
    card["token_estimate"] = estimate_tokens(markdown, cfg.tokens)
    return card, markdown


def write_card_files(
    card: dict[str, Any],
    markdown: str,
    cfg: Nl2DataConfig,
) -> tuple[Path, Path]:
    """Write the JSON and Markdown card files for ``card``.

    Returns:
        The ``(json path, markdown path)`` pair that was written.
    """
    cfg.paths.cards_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cards_md_dir.mkdir(parents=True, exist_ok=True)
    json_path = cfg.paths.cards_dir / f"{card['table']}.json"
    md_path = cfg.paths.cards_md_dir / f"{card['table']}.md"
    json_path.write_text(
        json.dumps(card, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path


def build_cards(
    cfg: Nl2DataConfig,
    terms_by_table: dict[str, list[dict[str, Any]]] | None = None,
    force: bool = False,
    tables: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build cards for all (or the selected) profiled tables.

    Incremental by default: a table is skipped when its card JSON is at
    least as new as both its profile JSON and the table-notes file. Profiles
    without a catalog entry are warned about and skipped; corrupt profile
    JSON is logged as an error and skipped without aborting the run.

    Args:
        cfg: Active configuration.
        terms_by_table: Business terms keyed by safe table name (from T6);
            ``None`` means no terms for any table.
        force: Rebuild every card even when it is up to date.
        tables: Safe table names to build; ``None`` means all profiles.

    Returns:
        Card dicts for the tables rebuilt in this run (skips excluded).
    """
    if not cfg.paths.profiles_dir.is_dir():
        logger.warning("profiles directory not found: %s", cfg.paths.profiles_dir)
        return []

    notes_mtime: float | None = None
    if cfg.paths.table_notes.exists():
        notes_mtime = cfg.paths.table_notes.stat().st_mtime
    notes = load_table_notes(cfg.paths.table_notes, cfg)
    term_map = terms_by_table or {}
    store = CatalogStore(cfg.paths.catalog)
    known = {table.name for source in store.sources for table in source.tables}
    requested = set(tables) if tables is not None else None

    cards: list[dict[str, Any]] = []
    for profile_path in sorted(cfg.paths.profiles_dir.glob("*.json")):
        table = profile_path.stem
        if requested is not None and table not in requested:
            continue
        if table not in known:
            logger.warning(
                "profile %s has no matching catalog table; skipping", table
            )
            continue
        card_path = cfg.paths.cards_dir / f"{table}.json"
        if not force and card_path.exists():
            threshold = profile_path.stat().st_mtime
            if notes_mtime is not None:
                threshold = max(threshold, notes_mtime)
            if card_path.stat().st_mtime >= threshold:
                logger.info("card for %s is up to date; skipping", table)
                continue
        try:
            profile: Any = json.loads(profile_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("corrupt profile %s (%s); skipping", profile_path.name, exc)
            continue
        if not isinstance(profile, dict):
            logger.error("profile %s is not a JSON object; skipping", profile_path.name)
            continue
        card, markdown = build_card(
            profile, notes.get(table), term_map.get(table, []), cfg
        )
        write_card_files(card, markdown, cfg)
        cards.append(card)
        logger.info("built card for %s (%d columns)", table, len(card["columns"]))
    return cards
