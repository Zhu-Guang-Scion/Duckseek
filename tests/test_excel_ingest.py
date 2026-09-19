"""Tests for the Excel ingestion adapter (ingest.excel)."""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pytest

from catalog.store import CatalogStore, TableEntry
from ingest.common import IngestError
from ingest.excel import detect_header_row, ingest_excel
from nl2data.config import Nl2DataConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _column_lineage(table: TableEntry) -> dict[str, str]:
    """Map clean column names to their original names for one table."""
    return {column.name: column.original_name for column in table.columns}


def test_ingests_basic_workbook(config: Nl2DataConfig) -> None:
    """Two sheets become two tables with lineage, Parquet, views and catalog."""
    source = FIXTURES / "excel_basic.xlsx"
    entry = ingest_excel(source, config)

    assert entry.type == "excel"
    assert [table.name for table in entry.tables] == ["ding_dan_ming_xi", "ke_hu"]
    assert [table.rows for table in entry.tables] == [20, 8]
    lineage = _column_lineage(entry.tables[0])
    assert lineage["ding_dan_id"] == "订单ID"

    parquet = config.paths.parquet_dir / "excel_basic" / "ding_dan_ming_xi.parquet"
    assert parquet.is_file()

    with duckdb.connect(str(config.paths.warehouse)) as conn:
        rows = conn.execute('SELECT count(*) FROM "ding_dan_ming_xi"').fetchone()
    assert rows == (20,)

    assert CatalogStore(config.paths.catalog).get_source("excel_basic") is not None


def test_sheet_filter_and_unknown_sheet(config: Nl2DataConfig) -> None:
    """sheet= picks exactly one sheet; unknown names list the available ones."""
    entry = ingest_excel(FIXTURES / "excel_basic.xlsx", config, sheet="客户")
    assert [(table.name, table.rows) for table in entry.tables] == [("ke_hu", 8)]

    with pytest.raises(IngestError, match="不存在") as exc_info:
        ingest_excel(FIXTURES / "excel_basic.xlsx", config, sheet="不存在")
    message = str(exc_info.value)
    assert "订单明细" in message
    assert "客户" in message


def test_rejects_unsupported_and_missing_files(config: Nl2DataConfig) -> None:
    """Legacy suffixes and missing paths raise IngestError."""
    with pytest.raises(IngestError, match=r"\.xls"):
        ingest_excel(FIXTURES / "legacy.xls", config)
    with pytest.raises(IngestError, match="not found"):
        ingest_excel(FIXTURES / "does_not_exist.xlsx", config)


def test_empty_sheet_is_skipped_with_warning(
    config: Nl2DataConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fully blank sheets are dropped; a warning names the skipped sheet."""
    with caplog.at_level(logging.WARNING, logger="ingest.excel"):
        entry = ingest_excel(FIXTURES / "excel_empty_sheet.xlsx", config)
    assert [table.name for table in entry.tables] == ["shu_ju_a"]
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert warnings
    assert any("空表" in record.getMessage() for record in warnings)


def test_pinyin_table_name_conflict(config: Nl2DataConfig) -> None:
    """Sheets whose pinyin slugs collide get a ``_N`` suffix."""
    entry = ingest_excel(FIXTURES / "excel_pinyin_conflict.xlsx", config)
    assert [(table.name, table.rows) for table in entry.tables] == [
        ("ding_dan", 3),
        ("ding_dan_1", 3),
    ]


def test_tricky_column_names(config: Nl2DataConfig) -> None:
    """Digit-leading, full-width, oversized and duplicate headers all resolve."""
    entry = ingest_excel(FIXTURES / "excel_tricky_names.xlsx", config)
    table = entry.tables[0]
    columns = [column.name for column in table.columns]
    assert columns[0] == "_1_yue_xiao_liang"
    assert "jin_e_yuan" in columns
    assert "jin_e" in columns
    assert "jin_e_1" in columns
    assert len(columns) == 5
    assert all(len(column) <= 63 for column in columns)
    assert table.rows == 5


def test_merged_cells_keep_anchor_values(config: Nl2DataConfig) -> None:
    """Merged cells keep only anchor values; the rest stay NULL."""
    entry = ingest_excel(FIXTURES / "excel_merged.xlsx", config)
    assert entry.tables[0].name == "xiao_shou_qu_yu"
    with duckdb.connect(str(config.paths.warehouse)) as conn:
        rows = conn.execute('SELECT qu_yu FROM "xiao_shou_qu_yu"').fetchall()
    assert rows == [("华东",), (None,), (None,), (None,), ("华南",), ("华北",)]


def test_formula_sheet_reads_null_and_values(config: Nl2DataConfig) -> None:
    """Uncached formulas read as NULL while plain numbers survive."""
    entry = ingest_excel(FIXTURES / "excel_formula.xlsx", config)
    assert entry.tables[0].name == "ji_suan"
    with duckdb.connect(str(config.paths.warehouse)) as conn:
        doubled = conn.execute('SELECT count("liang_bei") FROM "ji_suan"').fetchone()
        values = conn.execute('SELECT count("shu_zhi") FROM "ji_suan"').fetchone()
    assert doubled == (0,)
    assert values == (10,)


def test_title_and_blank_first_rows_detected(config: Nl2DataConfig) -> None:
    """Title-only and blank first rows are skipped during header detection."""
    entry = ingest_excel(FIXTURES / "excel_title_row.xlsx", config)
    by_original = {table.original_name: table for table in entry.tables}

    monthly = by_original["月报"]
    assert [column.name for column in monthly.columns] == ["ding_dan_id", "jin_e"]
    assert monthly.rows == 5

    shifted = by_original["空首行"]
    assert [column.name for column in shifted.columns] == ["bian_hao", "shu_liang"]
    assert shifted.rows == 3


def test_all_null_column_is_kept_as_varchar(config: Nl2DataConfig) -> None:
    """Header-bearing all-NULL columns are kept and downgraded to VARCHAR."""
    entry = ingest_excel(FIXTURES / "excel_all_null_column.xlsx", config)
    table = entry.tables[0]
    assert table.original_name == "备注"
    assert [column.name for column in table.columns] == ["bian_hao", "bei_zhu"]

    with duckdb.connect(str(config.paths.warehouse)) as conn:
        dtype = conn.execute('SELECT typeof("bei_zhu") FROM "bei_zhu" LIMIT 1').fetchone()
        nulls = conn.execute('SELECT count(*) FROM "bei_zhu" WHERE "bei_zhu" IS NULL').fetchone()
        total = conn.execute('SELECT count(*) FROM "bei_zhu"').fetchone()
    assert dtype == ("VARCHAR",)
    assert total == (5,)
    assert nulls == (5,)
    assert nulls[0] / total[0] == 1.0


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param([["订单ID", "金额"], [1, 10]], 0, id="header-first"),
        pytest.param([["月报标题"], ["订单ID", "金额"], [1, 10]], 1, id="title-row"),
        pytest.param([[None, None], ["编号", "数量"], [1, 2]], 1, id="blank-first-row"),
        pytest.param([["t1"], ["t2"], ["t3"], ["t4"], ["t5"]], 5, id="consecutive-titles-hit-cap"),
        pytest.param([], 5, id="no-rows"),
    ],
)
def test_detect_header_row(rows: list[list[object]], expected: int) -> None:
    """Header detection skips blank/title rows and caps at max_scan."""
    assert detect_header_row(rows, max_scan=5) == expected


def test_name_alias_sets_source_slug(config: Nl2DataConfig) -> None:
    """``name`` overrides the file stem as the source slug."""
    entry = ingest_excel(FIXTURES / "excel_basic.xlsx", config, name="销售系统")
    assert entry.name == "xiao_shou_xi_tong"
    assert CatalogStore(config.paths.catalog).get_source("xiao_shou_xi_tong") is not None


@pytest.mark.slow
def test_large_workbook_100k(config: Nl2DataConfig) -> None:
    """A 100k-row workbook ingests fully with all five columns."""
    entry = ingest_excel(FIXTURES / "excel_large_100k.xlsx", config)
    table = entry.tables[0]
    assert table.name == "da_biao"
    assert table.rows == 100_000
    assert len(table.columns) == 5

    with duckdb.connect(str(config.paths.warehouse)) as conn:
        rows = conn.execute('SELECT count(*) FROM "da_biao"').fetchone()
    assert rows == (100_000,)


def test_extension_load_failure_falls_back_to_pandas(
    config: Nl2DataConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unavailable excel extension logs a warning and reads via pandas."""
    import ingest.excel as excel_module

    real_connect = excel_module.duckdb.connect

    def _connect(*args: object, **kwargs: object) -> object:
        if args:  # real warehouse connections keep working
            return real_connect(*args, **kwargs)
        raise duckdb.IOException("extension download blocked")

    monkeypatch.setattr(excel_module, "_extension_available", None)
    monkeypatch.setattr(excel_module.duckdb, "connect", _connect)
    with caplog.at_level(logging.WARNING):
        entry = ingest_excel(FIXTURES / "excel_basic.xlsx", config)

    assert [table.name for table in entry.tables] == ["ding_dan_ming_xi", "ke_hu"]
    assert any("pandas fallback" in record.getMessage() for record in caplog.records)


def test_duckdb_read_error_falls_back_to_pandas(
    config: Nl2DataConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A runtime read_xlsx failure logs a warning and reads via pandas."""
    import ingest.excel as excel_module

    def _raise(source: Path, sheet: str) -> object:
        raise duckdb.Error("read_xlsx exploded")

    monkeypatch.setattr(excel_module, "_extension_available", True)
    monkeypatch.setattr(excel_module, "_read_sheet_via_duckdb", _raise)
    with caplog.at_level(logging.WARNING):
        entry = ingest_excel(FIXTURES / "excel_basic.xlsx", config)

    assert [table.name for table in entry.tables] == ["ding_dan_ming_xi", "ke_hu"]
    assert any("falling back to pandas" in record.getMessage() for record in caplog.records)
