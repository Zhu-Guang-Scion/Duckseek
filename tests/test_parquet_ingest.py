"""Tests for native Parquet registration (ingest/parquet.py)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from catalog.store import CatalogStore
from ingest.common import IngestError, ingest_tables, register_tables
from ingest.parquet import ingest_parquet
from nl2data.config import Nl2DataConfig


def _write_parquet(path: Path) -> Path:
    """Write a small parquet file with Chinese columns and nullable values."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "订单ID": pa.array([1, 2, 3], type=pa.int64()),
            "客户": pa.array(["甲", None, "丙"], type=pa.string()),
            "金额": pa.array([10.5, None, 30.5], type=pa.float64()),
        }
    )
    pq.write_table(table, path)
    return path


def _external_parquet(tmp_path: Path) -> Path:
    """A parquet outside the managed data/ area (user-owned file)."""
    return _write_parquet(tmp_path / "external" / "外部订单.parquet")


def test_ingest_parquet_registers_view_without_copy(
    config: Nl2DataConfig, tmp_path: Path
) -> None:
    """Register mode: view + catalog lineage, no file under data/parquet."""
    source = _external_parquet(tmp_path)
    entry = ingest_parquet(source, config)

    assert entry.type == "parquet"
    assert entry.name == "wai_bu_ding_dan"
    table = entry.tables[0]
    assert table.name == "wai_bu_ding_dan"
    assert table.original_name == "外部订单"
    assert table.rows == 3
    assert table.parquet == str(source)
    assert [(c.name, c.original_name) for c in table.columns] == [
        ("ding_dan_id", "订单ID"),
        ("ke_hu", "客户"),
        ("jin_e", "金额"),
    ]

    conn = duckdb.connect(str(config.paths.warehouse), read_only=True)
    try:
        assert conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0] == 3
        assert conn.execute(
            f'SELECT sum("jin_e") FROM "{table.name}"'
        ).fetchone()[0] == 41.0
    finally:
        conn.close()

    assert not (config.paths.parquet_dir / entry.name).exists()
    assert CatalogStore(config.paths.catalog).get_source(entry.name) is not None


def test_ingest_parquet_name_alias(config: Nl2DataConfig, tmp_path: Path) -> None:
    """``name`` overrides the file stem as the source slug."""
    source = _external_parquet(tmp_path)
    entry = ingest_parquet(source, config, name="销售系统")
    assert entry.name == "xiao_shou_xi_tong"


def test_reingest_preserves_user_file(config: Nl2DataConfig, tmp_path: Path) -> None:
    """Idempotent re-register never deletes the user-owned Parquet file."""
    source = _external_parquet(tmp_path)
    first = ingest_parquet(source, config)
    second = ingest_parquet(source, config)

    assert first.tables[0].name == second.tables[0].name
    assert source.exists()
    store = CatalogStore(config.paths.catalog)
    assert len(store.sources) == 1

    conn = duckdb.connect(str(config.paths.warehouse), read_only=True)
    try:
        assert conn.execute(
            f'SELECT count(*) FROM "{second.tables[0].name}"'
        ).fetchone()[0] == 3
    finally:
        conn.close()


def test_reingest_keeps_external_file_when_replaced_by_managed_source(
    config: Nl2DataConfig, tmp_path: Path
) -> None:
    """A managed re-ingest drops the view but still never deletes user files."""
    import pandas as pd

    from ingest.common import PreparedTable

    source = _external_parquet(tmp_path)
    parquet_entry = ingest_parquet(source, config, name="shared")

    frame = pd.DataFrame({"x": [1, 2]})
    ingest_tables(
        source_name="shared",
        source_type="excel",
        source_path=config.paths.data_dir / "shared.xlsx",
        tables=[PreparedTable(original_name="订单", frame=frame)],
        cfg=config,
    )

    assert source.exists()  # external file untouched
    conn = duckdb.connect(str(config.paths.warehouse), read_only=True)
    try:
        views = {
            row[0] for row in conn.execute("SELECT view_name FROM duckdb_views").fetchall()
        }
    finally:
        conn.close()
    assert parquet_entry.tables[0].name not in views


def test_ingest_parquet_rejects_bad_inputs(
    config: Nl2DataConfig, tmp_path: Path
) -> None:
    """Missing files, wrong suffixes, invalid parquet and zero columns fail."""
    with pytest.raises(IngestError, match="not found"):
        ingest_parquet(tmp_path / "missing.parquet", config)

    fake = tmp_path / "fake.parquet"
    fake.write_text("not parquet", encoding="utf-8")
    with pytest.raises(IngestError, match="not a valid Parquet"):
        ingest_parquet(fake, config)

    wrong = tmp_path / "data.parquet.bak"
    _write_parquet(wrong)
    with pytest.raises(IngestError, match="unsupported"):
        ingest_parquet(wrong, config)

    empty = tmp_path / "empty.parquet"
    pq.write_table(pa.table({}), empty)
    with pytest.raises(IngestError, match="no columns"):
        ingest_parquet(empty, config)


def test_register_tables_requires_tables(config: Nl2DataConfig) -> None:
    """An empty table list is an error, mirroring ingest_tables."""
    with pytest.raises(IngestError, match="no tables"):
        register_tables(
            source_name="x",
            source_path=Path("x.parquet"),
            tables=[],
            cfg=config,
        )
