"""Tests for the shared ingestion pipeline (ingest/common.py)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from catalog.store import CatalogStore
from ingest.common import IngestError, PreparedTable, ingest_tables, normalize_columns
from nl2data.config import Nl2DataConfig


def _orders_frame() -> pd.DataFrame:
    """A frame with Chinese headers, nullable numbers and datetimes."""
    return pd.DataFrame(
        {
            "订单ID": [1, 2, 3],
            "客户": ["甲", None, "丙"],
            "金额": [10.5, 20.0, None],
            "下单日期": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
        }
    )


def _ingest_orders(config: Nl2DataConfig, source_name: str = "sales") -> Path:
    """Ingest the sample frame, returning the written Parquet path."""
    entry = ingest_tables(
        source_name=source_name,
        source_type="excel",
        source_path=Path("D:/data/销售.xlsx"),
        tables=[PreparedTable(original_name="订单明细", frame=_orders_frame())],
        cfg=config,
    )
    return config.paths.parquet_dir / source_name / f"{entry.tables[0].name}.parquet"


def test_ingest_tables_end_to_end(config: Nl2DataConfig) -> None:
    """Full pipeline: naming, Parquet, DuckDB view and catalog lineage."""
    entry = ingest_tables(
        source_name="sales",
        source_type="excel",
        source_path=Path("D:/data/销售.xlsx"),
        tables=[PreparedTable(original_name="订单明细", frame=_orders_frame())],
        cfg=config,
    )
    assert entry.name == "sales"
    assert entry.type == "excel"
    table = entry.tables[0]
    assert table.name == "ding_dan_ming_xi"
    assert table.original_name == "订单明细"
    assert table.rows == 3
    assert [(c.name, c.original_name) for c in table.columns] == [
        ("ding_dan_id", "订单ID"),
        ("ke_hu", "客户"),
        ("jin_e", "金额"),
        ("xia_dan_ri_qi", "下单日期"),
    ]

    parquet = config.paths.parquet_dir / "sales" / "ding_dan_ming_xi.parquet"
    assert parquet.exists()
    assert table.parquet == parquet.relative_to(config.paths.data_dir.parent).as_posix()

    conn = duckdb.connect(str(config.paths.warehouse))
    try:
        assert conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0] == 3
        assert conn.execute(f'SELECT sum("jin_e") FROM "{table.name}"').fetchone()[0] == 30.5
    finally:
        conn.close()

    store = CatalogStore(config.paths.catalog)
    assert store.get_source("sales") is not None


def test_nulls_and_dtypes_survive_round_trip(config: Nl2DataConfig) -> None:
    """Nullable numbers, strings and datetimes keep nulls after Parquet."""
    table_name = _ingest_orders(config).stem
    conn = duckdb.connect(str(config.paths.warehouse))
    try:
        rows = conn.execute(
            f'SELECT "ding_dan_id", "ke_hu", "jin_e", "xia_dan_ri_qi" '
            f'FROM "{table_name}" ORDER BY "ding_dan_id"'
        ).fetchall()
    finally:
        conn.close()
    assert rows[0] == (1, "甲", 10.5, pd.Timestamp("2026-01-01").to_pydatetime())
    assert rows[1][:2] == (2, None)
    assert rows[2][2] is None


def test_table_names_unique_across_sources(config: Nl2DataConfig) -> None:
    """A second source with the same sheet name gets a suffixed table name."""
    first = ingest_tables(
        source_name="sales",
        source_type="excel",
        source_path=Path("D:/data/a.xlsx"),
        tables=[PreparedTable(original_name="订单", frame=_orders_frame())],
        cfg=config,
    )
    second = ingest_tables(
        source_name="other",
        source_type="access",
        source_path=Path("D:/data/b.mdb"),
        tables=[PreparedTable(original_name="订单", frame=_orders_frame())],
        cfg=config,
    )
    assert first.tables[0].name == "ding_dan"
    assert second.tables[0].name == "ding_dan_1"


def test_reingest_replaces_source(config: Nl2DataConfig, caplog: pytest.LogCaptureFixture) -> None:
    """Re-ingesting the same slug replaces the catalog entry, not appends."""
    _ingest_orders(config, source_name="sales")
    with caplog.at_level("WARNING"):
        _ingest_orders(config, source_name="sales")
    store = CatalogStore(config.paths.catalog)
    assert len(store.sources) == 1


def test_reingest_different_path_warns(
    config: Nl2DataConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """Replacing a source with a different file path logs a warning."""
    ingest_tables(
        source_name="sales",
        source_type="excel",
        source_path=Path("D:/data/old.xlsx"),
        tables=[PreparedTable(original_name="t", frame=_orders_frame())],
        cfg=config,
    )
    with caplog.at_level("WARNING"):
        ingest_tables(
            source_name="sales",
            source_type="excel",
            source_path=Path("D:/data/new.xlsx"),
            tables=[PreparedTable(original_name="t", frame=_orders_frame())],
            cfg=config,
        )
    assert any("replacing" in record.message for record in caplog.records)


def test_reingest_cleans_stale_artifacts(config: Nl2DataConfig) -> None:
    """Re-ingesting with fewer tables drops old views and Parquet files."""
    first = ingest_tables(
        source_name="sales",
        source_type="excel",
        source_path=Path("D:/data/sales.xlsx"),
        tables=[
            PreparedTable(original_name="订单", frame=_orders_frame()),
            PreparedTable(original_name="客户", frame=_orders_frame()),
        ],
        cfg=config,
    )
    dropped = first.tables[1]
    old_parquet = config.paths.parquet_dir / "sales" / f"{dropped.name}.parquet"
    assert old_parquet.exists()

    second = ingest_tables(
        source_name="sales",
        source_type="excel",
        source_path=Path("D:/data/sales.xlsx"),
        tables=[PreparedTable(original_name="订单", frame=_orders_frame())],
        cfg=config,
    )

    assert len(second.tables) == 1
    assert not old_parquet.exists()
    conn = duckdb.connect(str(config.paths.warehouse))
    try:
        views = {
            row[0] for row in conn.execute("SELECT view_name FROM duckdb_views").fetchall()
        }
    finally:
        conn.close()
    assert dropped.name not in views
    assert first.tables[0].name in views


def test_no_tables_raises(config: Nl2DataConfig) -> None:
    """An empty table list is an ingestion error, not a silent success."""
    with pytest.raises(IngestError, match="no non-empty tables"):
        ingest_tables(
            source_name="x",
            source_type="excel",
            source_path=Path("x.xlsx"),
            tables=[],
            cfg=config,
        )


def test_normalize_columns_dedupes() -> None:
    """Distinct raw headers with equal slugs get deterministic suffixes."""
    frame = pd.DataFrame({"金额": [1], "金额 ": [2]})
    existing: set[str] = set()
    renamed, entries = normalize_columns(frame, existing)
    assert [c.name for c in entries] == ["jin_e", "jin_e_1"]
    assert [c.original_name for c in entries] == ["金额", "金额 "]
    assert list(renamed.columns) == ["jin_e", "jin_e_1"]
    assert existing == {"jin_e", "jin_e_1"}
