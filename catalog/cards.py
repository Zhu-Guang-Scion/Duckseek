"""M-Schema table card generation (milestone 2, T5; M7/T22 value injection).

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

import pyarrow.parquet as pq

from catalog.store import CatalogStore
from catalog.table_notes import load_table_notes
from nl2data.config import Nl2DataConfig
from retrieval.tokens import estimate_tokens

logger = logging.getLogger(__name__)

# Profile stat keys that collapse into the card column's ``stats`` dict.
_STAT_KEYS: tuple[str, ...] = ("min", "max", "quantiles", "avg_len")

#: Per-cell character cap inside the injected dimension-value block.
_DIM_VALUE_CELL_CHARS = 40


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


def _dim_cell(value: Any) -> str:
    """One injected cell: NULL keeps its label, long values truncate."""
    if value is None:
        return "NULL"
    text = str(value)
    if len(text) > _DIM_VALUE_CELL_CHARS:
        return text[:_DIM_VALUE_CELL_CHARS] + "…"
    return text


def _dim_values_section(
    profile: dict[str, Any], parquet_path: Path | None, cfg: Nl2DataConfig
) -> tuple[str, dict[str, Any]] | None:
    """Full-value block for small dimension tables (M7/T22), or ``None``.

    Evidence-backed full injection (docs/research-m7-market.md §三): for
    tables at or below ``cards.dim_value_max_rows`` the entire value list
    enters the card verbatim, guarded by an instruction to use these values
    as-is and to ask for clarification instead of inventing one. The block
    is token-capped; overflow truncates with a mandatory disclosure line.
    """
    max_rows = int(cfg.cards.dim_value_max_rows)
    rows_total = int(profile.get("rows", 0))
    if max_rows <= 0 or rows_total <= 0 or rows_total > max_rows:
        return None
    if parquet_path is None or not parquet_path.is_file():
        logger.warning(
            "dimension-value injection skipped for %s: parquet not found",
            profile.get("table"),
        )
        return None
    columns = [str(col.get("name", "")) for col in profile.get("columns", [])]
    if not columns:
        return None
    try:
        table = pq.read_table(parquet_path)
    except Exception as exc:  # noqa: BLE001 -- degrade to no injection
        logger.warning("dim-value read failed for %s: %s", parquet_path, exc)
        return None
    # Parquet column names differ by write path: the zero-copy register
    # (T-P) keeps ORIGINAL headers while converted ingestion (excel/access)
    # writes SAFE names. Resolve per column: original first, safe as fallback.
    column_names = set(table.column_names)

    def _lookup(name: str, original: str) -> str | None:
        if original in column_names:
            return original
        if name in column_names:
            return name
        return None

    resolved: list[tuple[str, str]] = []
    for col in profile.get("columns", []):
        name = str(col.get("name", ""))
        original = str(col.get("original_name") or name)
        found = _lookup(name, original)
        if found is None:
            logger.warning(
                "dim-value injection skipped for %s: parquet lacks %s/%s",
                profile.get("table"), original, name,
            )
            return None
        resolved.append((name, found))
    records = table.to_pylist()
    cap = max(int(cfg.cards.dim_value_token_cap), 1)
    lines: list[str] = ["|".join(columns)]
    used = estimate_tokens(lines[0] + "\n", cfg.tokens)
    truncated = False
    injected = 0
    for record in records:
        line = "|".join(_dim_cell(record.get(found)) for _, found in resolved)
        cost = estimate_tokens(line + "\n", cfg.tokens)
        if used + cost > cap:
            truncated = True
            break
        lines.append(line)
        used += cost
        injected += 1
    if truncated and injected == 0:
        return None  # cap too small to carry any row: block would mislead
    section = "\n".join(
        [
            "",
            f"## 维度表全量值(共 {rows_total} 行)",
            "过滤条件必须使用下列值原文;问句实体找不到对应值时应在 "
            "needs_clarification 中说明,不要臆造或翻译库值。",
            *lines,
        ]
    )
    if truncated:
        section += f"\n(已截断:前 {injected} 行/共 {rows_total} 行,受 token 预算限制)"
    meta = {
        "rows_total": rows_total,
        "rows_injected": injected,
        "truncated": truncated,
        "token_estimate": used,
    }
    return section, meta


def _render_markdown(
    card: dict[str, Any], cfg: Nl2DataConfig, dim_section: str | None = None
) -> str:
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
    if dim_section:
        lines.append(dim_section)
    lines.append("")
    lines.append(_terms_line(card["terms"]))
    return "\n".join(lines) + "\n"


def build_card(
    profile: dict[str, Any],
    description: str | None,
    terms: list[dict[str, Any]],
    cfg: Nl2DataConfig,
    parquet_path: Path | None = None,
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
        parquet_path: Data file for M7/T22 dimension-value injection; when
            the profile is a small dimension table the full value list is
            injected (token-capped). ``None`` skips injection.

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
    dim = _dim_values_section(profile, parquet_path, cfg)
    if dim is not None:
        section, meta = dim
        card["dim_values"] = meta
    else:
        section = None
    markdown = _render_markdown(card, cfg, dim_section=section)
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
        parquet_path = _resolve_parquet(store, table, cfg)
        card, markdown = build_card(
            profile, notes.get(table), term_map.get(table, []), cfg,
            parquet_path=parquet_path,
        )
        write_card_files(card, markdown, cfg)
        cards.append(card)
        logger.info("built card for %s (%d columns)", table, len(card["columns"]))
    return cards


def _resolve_parquet(store: CatalogStore, table: str, cfg: Nl2DataConfig) -> Path | None:
    """Absolute parquet path for ``table`` from the catalog lineage."""
    for source in store.sources:
        for entry in source.tables:
            if entry.name == table:
                path = Path(entry.parquet)
                if not path.is_absolute():
                    path = cfg.paths.data_dir.parent / path
                return path
    return None
