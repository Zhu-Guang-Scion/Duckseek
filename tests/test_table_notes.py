"""Tests for the human-editable table-notes parser (catalog/table_notes.py)."""

from __future__ import annotations

from pathlib import Path

from catalog.store import CatalogStore, ColumnEntry, SourceEntry, TableEntry
from catalog.table_notes import load_table_notes
from nl2data.config import Nl2DataConfig


def _register_table(config: Nl2DataConfig, *, safe: str, original: str) -> None:
    """Register one table in the temp catalog so notes can resolve names."""
    store = CatalogStore(config.paths.catalog)
    store.upsert_source(
        SourceEntry(
            name="src",
            type="excel",
            path="src.xlsx",
            ingested_at="2026-01-01T00:00:00+00:00",
            tables=[
                TableEntry(
                    name=safe,
                    original_name=original,
                    parquet=f"data/parquet/src/{safe}.parquet",
                    rows=2,
                    columns=[ColumnEntry(name="id", original_name="ID")],
                )
            ],
        )
    )
    store.save()


def _write_notes(path: Path, text: str) -> None:
    """Write a notes markdown file (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_missing_file_returns_empty(config: Nl2DataConfig) -> None:
    """A missing notes file degrades silently to an empty mapping."""
    assert load_table_notes(config.paths.table_notes, config) == {}


def test_hit_by_safe_name(config: Nl2DataConfig) -> None:
    """A `# 表: <safe>` section with a body maps the safe name to its text."""
    _register_table(config, safe="ke_hu", original="客户")
    _write_notes(
        config.paths.table_notes,
        "# 表: ke_hu\n客户维表,一行一位客户。\n\n# 其他小节\n无关正文\n",
    )
    notes = load_table_notes(config.paths.table_notes, config)
    assert notes == {"ke_hu": "客户维表,一行一位客户。"}


def test_hit_by_chinese_original_name(config: Nl2DataConfig) -> None:
    """A `## 表:<原名>` section resolves back to the safe name via catalog."""
    _register_table(config, safe="ke_hu", original="客户")
    _write_notes(config.paths.table_notes, "## 表:客户\n\n客户维表。\n")
    assert load_table_notes(config.paths.table_notes, config) == {"ke_hu": "客户维表。"}


def test_empty_body_counts_as_miss(config: Nl2DataConfig) -> None:
    """A section whose body strips to empty is not a hit."""
    _register_table(config, safe="ke_hu", original="客户")
    _write_notes(config.paths.table_notes, "# 表:ke_hu\n\n# 表:ding_dan\n订单表\n")
    assert load_table_notes(config.paths.table_notes, config) == {"ding_dan": "订单表"}


def test_duplicate_section_last_wins(config: Nl2DataConfig) -> None:
    """Repeated sections with the same name override, without erroring."""
    _register_table(config, safe="ke_hu", original="客户")
    _write_notes(
        config.paths.table_notes,
        "# 表:ke_hu\n旧说明\n\n# 表: ke_hu\n新说明\n",
    )
    assert load_table_notes(config.paths.table_notes, config) == {"ke_hu": "新说明"}


def test_unknown_name_passes_through(config: Nl2DataConfig) -> None:
    """A name matching neither safe nor original names is kept as-is."""
    _register_table(config, safe="ke_hu", original="客户")
    _write_notes(config.paths.table_notes, "# 表:future_tbl\n尚未入库的表\n")
    assert load_table_notes(config.paths.table_notes, config) == {
        "future_tbl": "尚未入库的表"
    }
