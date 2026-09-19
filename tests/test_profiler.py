"""Tests for the T4 profiler (catalog/profiler.py)."""

from __future__ import annotations

import json
from dataclasses import replace

import duckdb
import pandas as pd
import pytest

from catalog.profiler import ProfileError, profile_table, run_profiles
from nl2data.config import Nl2DataConfig


def _rich_frame() -> pd.DataFrame:
    """A frame exercising numeric, temporal, enum, boolean and null columns."""
    return pd.DataFrame(
        {
            "num_int": pd.array([1, 2, None, 4], dtype="Int64"),
            "num_float": [1.5, 2.5, 3.5, None],
            "cat_low": ["b", "a", "b", "a"],
            "dt": pd.to_datetime(["2026-01-02", "2026-01-01", None, "2026-03-01"]),
            "flag": pd.array([True, False, None, True], dtype="boolean"),
            "all_null": [None, None, None, None],
        }
    )


class _QuantileFailConn:
    """Connection wrapper whose quantile queries fail, for degradation tests."""

    def __init__(self, real: duckdb.DuckDBPyConnection) -> None:
        self._real = real

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        if "quantile_cont" in sql:
            raise duckdb.InvalidInputException("quantile blocked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, item: str) -> object:
        return getattr(self._real, item)


@pytest.fixture()
def rich_table(
    config: Nl2DataConfig,
    ingest_factory: object,
) -> str:
    """Ingest the rich frame and return its clean table name."""
    return ingest_factory("rich", _rich_frame())  # type: ignore[operator]


def _open(cfg: Nl2DataConfig) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(cfg.paths.warehouse), read_only=True)


def test_profile_table_contract_fields(
    config: Nl2DataConfig, rich_table: str
) -> None:
    """The profile dict matches the T4 output contract per column dtype."""
    from catalog.profiler import catalog_tables

    source_name, entry = catalog_tables(config)[rich_table]
    conn = _open(config)
    try:
        profile = profile_table(
            conn, rich_table, cfg=config, source=source_name, entry=entry
        )
    finally:
        conn.close()

    assert profile["table"] == rich_table
    assert profile["source"] == "rich"
    assert profile["original_name"] == "rich"
    assert profile["rows"] == 4
    assert "T" in profile["profiled_at"]  # ISO8601 timestamp
    assert "sampled" not in profile
    cols = {c["name"]: c for c in profile["columns"]}
    assert set(cols) == {"num_int", "num_float", "cat_low", "dt", "flag", "all_null"}

    num_int = cols["num_int"]
    assert num_int["dtype"] == "BIGINT"
    assert num_int["original_name"] == "num_int"
    assert num_int["null_rate"] == 0.25
    assert num_int["distinct_count"] == 3
    assert num_int["min"] == 1
    assert num_int["max"] == 4
    assert num_int["quantiles"] == {
        "p25": pytest.approx(1.5),
        "p50": pytest.approx(2.0),
        "p75": pytest.approx(3.0),
    }
    assert "enum_values" not in num_int
    assert "avg_len" not in num_int
    assert num_int["sample_values"] == [1, 2, 4]

    num_float = cols["num_float"]
    assert num_float["min"] == 1.5
    assert num_float["max"] == 3.5
    assert set(num_float["quantiles"]) == {"p25", "p50", "p75"}

    cat_low = cols["cat_low"]
    assert cat_low["dtype"] == "VARCHAR"
    assert cat_low["enum_values"] == ["a", "b"]
    assert cat_low["avg_len"] == 1.0
    assert cat_low["sample_values"] == ["b", "a"]  # first-seen order

    dt = cols["dt"]
    assert dt["dtype"] == "TIMESTAMP"
    assert dt["min"] == "2026-01-01T00:00:00"
    assert dt["max"] == "2026-03-01T00:00:00"
    assert "quantiles" not in dt  # quantiles are numeric-only per the contract

    flag = cols["flag"]
    assert flag["dtype"] == "BOOLEAN"
    assert flag["enum_values"] == [False, True]
    assert "min" not in flag  # min/max are numeric/temporal-only

    all_null = cols["all_null"]
    assert all_null["distinct_count"] == 0
    assert all_null["null_rate"] == 1.0
    assert all_null["sample_values"] == []
    assert "enum_values" not in all_null
    assert "avg_len" not in all_null
    assert "min" not in all_null


def test_profile_table_high_cardinality_and_sample_limit(
    config: Nl2DataConfig, ingest_factory: object
) -> None:
    """Strings above the enum threshold have no enum; samples cap at the limit."""
    frame = pd.DataFrame(
        {
            "t": [f"value{i:02d}" for i in range(60)],
            "g": [i % 3 for i in range(60)],
        }
    )
    table = ingest_factory("highcard", frame)  # type: ignore[operator]
    conn = _open(config)
    try:
        profile = profile_table(conn, table, cfg=config)
    finally:
        conn.close()
    col_t = profile["columns"][0]
    assert col_t["distinct_count"] == 60
    assert "enum_values" not in col_t
    assert col_t["avg_len"] == 7.0
    assert len(col_t["sample_values"]) == config.profile.sample_values_limit


def test_profile_table_zero_rows(
    config: Nl2DataConfig, ingest_factory: object
) -> None:
    """Empty tables profile cleanly with null_rate=None and empty samples."""
    frame = pd.DataFrame(
        {"a": pd.Series([], dtype="int64"), "b": pd.Series([], dtype="object")}
    )
    table = ingest_factory("empty_tbl", frame)  # type: ignore[operator]
    conn = _open(config)
    try:
        profile = profile_table(conn, table, cfg=config)
    finally:
        conn.close()
    assert profile["rows"] == 0
    for col in profile["columns"]:
        assert col["null_rate"] is None
        assert col["distinct_count"] == 0
        assert col["sample_values"] == []


def test_profile_table_sampled_mode(config: Nl2DataConfig) -> None:
    """Tables above the sampled_over_rows threshold record ``sampled``."""
    small = replace(
        config,
        profile=replace(
            config.profile, sampled_over_rows=3, sample_fraction=1.0
        ),
    )
    frame = pd.DataFrame({"x": range(4)})
    from ingest.common import PreparedTable, ingest_tables

    entry = ingest_tables(
        source_name="big",
        source_type="excel",
        source_path=small.paths.data_dir / "big.source",
        tables=[PreparedTable(original_name="big", frame=frame)],
        cfg=small,
    )
    conn = _open(small)
    try:
        profile = profile_table(conn, entry.tables[0].name, cfg=small)
    finally:
        conn.close()
    assert profile["sampled"] is True
    assert profile["rows"] == 4  # row count stays exact
    assert profile["columns"][0]["sample_values"] == [0, 1, 2, 3]
    # null_rate is measured on the sampled basis, not mixed with exact rows.
    assert profile["columns"][0]["null_rate"] == 0.0


def test_profile_table_sampled_null_rate_uses_sample_basis(config: Nl2DataConfig) -> None:
    """A null-free table must report null_rate 0.0 even when undersampled."""
    small = replace(
        config,
        profile=replace(
            config.profile, sampled_over_rows=3, sample_fraction=0.5
        ),
    )
    frame = pd.DataFrame({"x": range(40)})  # no nulls at all
    from ingest.common import PreparedTable, ingest_tables

    entry = ingest_tables(
        source_name="frac",
        source_type="excel",
        source_path=small.paths.data_dir / "frac.source",
        tables=[PreparedTable(original_name="frac", frame=frame)],
        cfg=small,
    )
    conn = _open(small)
    try:
        profile = profile_table(conn, entry.tables[0].name, cfg=small)
    finally:
        conn.close()
    assert profile["sampled"] is True
    assert profile["rows"] == 40
    # min/max stay exact in sampled mode (computed outside the sample).
    assert profile["columns"][0]["min"] == 0
    assert profile["columns"][0]["max"] == 39
    # A 50% bernoulli sample of ~40 rows has well under 4M rows, so basis is
    # the sample count; either way a null-free column reports 0.0.
    assert profile["columns"][0]["null_rate"] == 0.0


def test_profile_table_degrades_per_column(
    config: Nl2DataConfig, rich_table: str
) -> None:
    """A failing per-column stat records ``error`` without blocking others."""
    conn = _QuantileFailConn(_open(config))
    try:
        profile = profile_table(conn, rich_table, cfg=config, source="rich")
    finally:
        conn.close()
    cols = {c["name"]: c for c in profile["columns"]}
    assert "error" in cols["num_int"]
    assert cols["cat_low"]["enum_values"] == ["a", "b"]
    assert cols["cat_low"]["distinct_count"] == 2
    assert "error" not in cols["cat_low"]
    for col in profile["columns"]:
        assert "sample_values" in col


def test_profile_table_missing_view_raises(config: Nl2DataConfig) -> None:
    """Profiling a view that does not exist raises ProfileError."""
    conn = duckdb.connect()
    try:
        with pytest.raises(ProfileError, match="not found"):
            profile_table(conn, "ghost", cfg=config)
    finally:
        conn.close()


def test_run_profiles_writes_json_files(
    config: Nl2DataConfig, rich_table: str
) -> None:
    """run_profiles writes one JSON file per table under profiles_dir."""
    profiles = run_profiles(config, [rich_table])
    assert len(profiles) == 1
    path = config.paths.profiles_dir / f"{rich_table}.json"
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert set(loaded) == {
        "table",
        "original_name",
        "source",
        "rows",
        "profiled_at",
        "columns",
    }
    for col in loaded["columns"]:
        assert {"name", "original_name", "dtype", "null_rate",
                "distinct_count", "sample_values"} <= set(col)


def test_run_profiles_incremental_skips_fresh(
    config: Nl2DataConfig, rich_table: str
) -> None:
    """A second run skips fresh profiles; force or deletion triggers recompute."""
    assert len(run_profiles(config, [rich_table])) == 1
    assert run_profiles(config, [rich_table]) == []  # fresh, skipped
    assert len(run_profiles(config, [rich_table], force=True)) == 1
    profile_path = config.paths.profiles_dir / f"{rich_table}.json"
    profile_path.unlink()
    assert len(run_profiles(config, [rich_table])) == 1


def test_run_profiles_all_and_unknown(
    config: Nl2DataConfig, ingest_factory: object
) -> None:
    """run_profiles covers every catalog table and rejects unknown names."""
    ingest_factory("t1", pd.DataFrame({"x": [1, 2]}))  # type: ignore[operator]
    ingest_factory("t2", pd.DataFrame({"y": [3, 4]}))  # type: ignore[operator]
    assert [p["table"] for p in run_profiles(config)] == ["t1", "t2"]
    assert (config.paths.profiles_dir / "t1.json").exists()
    assert (config.paths.profiles_dir / "t2.json").exists()
    with pytest.raises(ProfileError, match="not in the catalog"):
        run_profiles(config, ["nope"])


def test_run_profiles_empty_catalog_returns_empty(config: Nl2DataConfig) -> None:
    """An empty catalog yields an empty profile list without touching DuckDB."""
    assert run_profiles(config) == []
    assert list(config.paths.profiles_dir.glob("*.json")) == []


def test_run_profiles_requires_warehouse(
    config: Nl2DataConfig, ingest_factory: object
) -> None:
    """With catalog entries but no warehouse file, profiling fails clearly."""
    ingest_factory("t1", pd.DataFrame({"x": [1, 2]}))  # type: ignore[operator]
    config.paths.warehouse.unlink()
    with pytest.raises(ProfileError, match="warehouse not found"):
        run_profiles(config)
