"""Tests for exec.runner: sandboxed execution, compaction and cleanup (T11)."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import exec.runner as runner_module
from exec.runner import (
    ExecutionError,
    _build_profile,
    _classify_error,
    _close,
    _detail_ref,
    _jsonable,
    _safe_column_names,
    _text_top,
    _trim,
    cleanup_scratch,
    run,
)
from guard.validate import ValidatedSQL
from nl2data.config import ExecConfig, Nl2DataConfig


class _FakeValidated:
    """Duck-typed ValidatedSQL stand-in that the signature guard must reject."""

    def __init__(self) -> None:
        self.sql = "SELECT 1"
        self.tables: list[str] = []
        self.limit = 1


@pytest.fixture()
def runner_config(config: Nl2DataConfig) -> Nl2DataConfig:
    """A config whose tmp warehouse holds small, profile and 100k fixtures."""
    config.paths.warehouse.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(config.paths.warehouse))
    conn.execute(
        "CREATE TABLE t1 AS SELECT i, "
        "TIMESTAMP '2024-01-01 00:00:00' + to_days(i) AS ts, "
        "i::VARCHAR::BLOB AS payload FROM range(10) t(i)"
    )
    conn.execute(
        "CREATE TABLE c2 AS SELECT i + 1 AS amount, "
        "['a', 'b', 'c', 'd', 'e'][(i % 5) + 1] AS cat, "
        "TIMESTAMP '2020-01-01 00:00:00' + to_seconds(i) AS created_at "
        "FROM range(200) t(i)"
    )
    conn.execute("CREATE TABLE big AS SELECT * FROM range(100000) t(i)")
    conn.close()
    return config


def test_signature_guard_rejects_non_validated(runner_config: Nl2DataConfig) -> None:
    """Both raw strings and duck-typed fakes must raise ExecutionError."""
    with pytest.raises(ExecutionError):
        run("SELECT 1", runner_config)  # type: ignore[arg-type]
    with pytest.raises(ExecutionError):
        run(_FakeValidated(), runner_config)  # type: ignore[arg-type]


def test_missing_warehouse_reports_permission(config: Nl2DataConfig) -> None:
    """A warehouse that does not exist degrades to a permission error result."""
    result = run(ValidatedSQL(sql="SELECT 1", tables=[], limit=1), config)
    assert result.status == "error"
    assert result.rowcount == 0
    assert result.error is not None
    assert result.error["category"] == "permission"
    assert "warehouse 不存在" in result.error["message"]


def test_read_only_connection_rejects_write(runner_config: Nl2DataConfig) -> None:
    """Even a guard-bypassing CREATE TABLE is refused by the read-only DB."""
    vsql = ValidatedSQL(sql="CREATE TABLE evil(i INT)", tables=[], limit=1)
    result = run(vsql, runner_config)
    assert result.status == "error"
    assert result.rowcount == 0
    assert result.error is not None
    assert result.error["category"] == "permission"
    conn = duckdb.connect(str(runner_config.paths.warehouse), read_only=True)
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    conn.close()
    assert "evil" not in tables


def test_small_result_no_profile_no_detail(runner_config: Nl2DataConfig) -> None:
    """10 rows: full inline sample, no spill, no profile."""
    vsql = ValidatedSQL(sql="SELECT * FROM t1", tables=["t1"], limit=500)
    result = run(vsql, runner_config)
    assert result.status == "ok"
    assert result.error is None
    assert result.columns == ["i", "ts", "payload"]
    assert result.rowcount == 10
    assert len(result.rows) == 10
    assert isinstance(result.rows[0]["ts"], str)  # datetime -> isoformat
    assert result.rows[0]["ts"].startswith("2024-01-01T")
    assert isinstance(result.rows[0]["payload"], str)  # bytes decoded
    assert result.detail_ref is None
    assert result.profile is None
    assert result.latency_ms >= 0.0


def test_zero_limit_returns_empty_ok(runner_config: Nl2DataConfig) -> None:
    """A zero limit yields an empty but successful result."""
    vsql = ValidatedSQL(sql="SELECT i FROM big", tables=["big"], limit=0)
    result = run(vsql, runner_config)
    assert result.status == "ok"
    assert result.rowcount == 0
    assert result.rows == []
    assert result.profile is None
    assert result.detail_ref is None


def test_medium_result_profile_and_detail_spill(runner_config: Nl2DataConfig) -> None:
    """200 rows: profiled (numeric + text top-N) and spilled to Parquet."""
    vsql = ValidatedSQL(sql="SELECT amount, cat, created_at FROM c2", tables=["c2"], limit=500)
    result = run(vsql, runner_config)
    assert result.status == "ok"
    assert result.rowcount == 200
    assert len(result.rows) == 20

    profile = result.profile
    assert profile is not None
    assert profile["rowcount"] == 200
    amount = profile["columns"]["amount"]
    assert amount["kind"] == "numeric"
    assert amount["min"] == 1
    assert amount["max"] == 200
    assert amount["avg"] == pytest.approx(100.5)
    assert amount["quantiles"]["p25"] == pytest.approx(50.75)
    assert amount["quantiles"]["p50"] == pytest.approx(100.5)
    assert amount["quantiles"]["p75"] == pytest.approx(150.25)
    assert amount["null_rate"] == 0.0

    cat = profile["columns"]["cat"]
    assert cat["kind"] == "text"
    top = {entry["value"]: entry["count"] for entry in cat["top"]}
    assert top == {"a": 40, "b": 40, "c": 40, "d": 40, "e": 40}
    assert cat["null_rate"] == 0.0

    # TIMESTAMP is neither numeric nor textual by type code: the sample probe
    # (first non-null value isinstance check) must classify it as text.
    created = profile["columns"]["created_at"]
    assert created["kind"] == "text"
    assert len(created["top"]) == 5
    assert all(entry["count"] == 1 for entry in created["top"])

    assert result.detail_ref is not None
    detail = runner_config.paths.data_dir.parent / result.detail_ref
    assert detail.is_file()
    assert detail.suffix == ".parquet"
    assert pq.read_table(detail).num_rows == 200


def test_limit_truncates_without_error(runner_config: Nl2DataConfig) -> None:
    """fetchmany(limit+1) caps the row count; truncation is not an error."""
    vsql = ValidatedSQL(sql="SELECT i FROM big", tables=["big"], limit=100)
    result = run(vsql, runner_config)
    assert result.status == "ok"
    assert result.error is None
    assert result.rowcount == 100
    assert len(result.rows) == 20
    assert result.detail_ref is not None
    detail = runner_config.paths.data_dir.parent / result.detail_ref
    assert pq.read_table(detail).num_rows == 100


def test_timeout_interrupts_long_query(
    runner_config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query exceeding the (shortened) budget is interrupted, not raised."""
    cfg = replace(runner_config, exec=ExecConfig(timeout_seconds=1))
    slow = "SELECT sum(length(md5(i::VARCHAR))) FROM range(50000000) t(i)"
    result = run(ValidatedSQL(sql=slow, tables=[], limit=1), cfg)
    assert result.status == "error"
    assert result.rowcount == 0
    assert result.error is not None
    assert result.error["category"] == "timeout"
    assert "已取消" in result.error["message"]
    assert result.latency_ms < 15000.0


def test_error_classification(runner_config: Nl2DataConfig) -> None:
    """Parse errors classify as syntax; catalog misses fall back to execution."""
    syntax = run(ValidatedSQL(sql="SELCT 1", tables=[], limit=1), runner_config)
    assert syntax.status == "error"
    assert syntax.error is not None
    assert syntax.error["category"] == "syntax"
    assert len(syntax.error["message"]) <= 301  # truncated to 300 chars + ellipsis

    missing = run(
        ValidatedSQL(sql="SELECT * FROM missing_tbl", tables=["missing_tbl"], limit=1),
        runner_config,
    )
    assert missing.status == "error"
    assert missing.error is not None
    assert missing.error["category"] == "execution"
    assert missing.error["message"]


def test_100k_rows_profiles_under_10s(runner_config: Nl2DataConfig) -> None:
    """Compaction of a 10k-row slice of a 100k-row table stays under 10s."""
    vsql = ValidatedSQL(sql="SELECT i FROM big", tables=["big"], limit=10000)
    result = run(vsql, runner_config)
    assert result.status == "ok"
    assert result.rowcount == 10000
    profile = result.profile
    assert profile is not None
    assert profile["rowcount"] == 10000
    assert profile["columns"]["i"]["min"] == 0
    assert profile["columns"]["i"]["max"] == 99999
    assert result.detail_ref is not None
    detail = runner_config.paths.data_dir.parent / result.detail_ref
    assert pq.read_table(detail).num_rows == 10000
    assert result.latency_ms < 10000.0


def test_cleanup_scratch_removes_only_stale_files(runner_config: Nl2DataConfig) -> None:
    """Only files older than max_age_hours are deleted; the count is returned."""
    scratch = runner_config.paths.scratch_dir
    scratch.mkdir(parents=True, exist_ok=True)
    fresh = scratch / "fresh.parquet"
    stale = scratch / "stale.parquet"
    pq.write_table(pa.table({"x": [1]}), fresh)
    pq.write_table(pa.table({"x": [1]}), stale)
    old = time.time() - 73 * 3600.0
    os.utime(stale, (old, old))
    assert cleanup_scratch(runner_config, max_age_hours=72.0) == 1
    assert fresh.exists()
    assert not stale.exists()
    assert cleanup_scratch(runner_config) == 0


def test_cleanup_scratch_missing_directory(config: Nl2DataConfig) -> None:
    """A missing scratch directory is not created; nothing is removed."""
    assert cleanup_scratch(config) == 0


def test_jsonable_covers_decimal_bytes_and_fallback() -> None:
    """Decimal becomes float, unknown scalars fall back to str()."""
    assert _jsonable(Decimal("1.5")) == 1.5
    sentinel = object()
    assert _jsonable(sentinel) == str(sentinel)


def test_trim_caps_message_length() -> None:
    """Messages over 300 chars are truncated with an ellipsis marker."""
    short = _trim("x" * 10)
    assert short == "x" * 10
    long_trim = _trim("y" * 400)
    assert len(long_trim) == 301
    assert long_trim.endswith("…")


def test_classify_error_maps_all_categories() -> None:
    """Exception types and message keywords map to the documented categories."""
    assert _classify_error(duckdb.IOException("disk issue"))[0] == "permission"
    assert _classify_error(duckdb.OutOfMemoryException("oom"))[0] == "resource"
    assert _classify_error(duckdb.ParserException("bad"))[0] == "syntax"
    assert _classify_error(duckdb.InterruptException("stopped"))[0] == "timeout"
    # Keyword sweep for exception hierarchies that differ across builds.
    read_only = duckdb.InvalidInputException(
        "Cannot execute statement of type CREATE on database w which is "
        "attached in read-only mode!"
    )
    assert _classify_error(read_only)[0] == "permission"
    assert _classify_error(duckdb.InvalidInputException("not enough threads"))[0] == (
        "resource"
    )
    assert _classify_error(duckdb.InvalidInputException("query interrupted!"))[0] == (
        "timeout"
    )
    assert _classify_error(duckdb.InvalidInputException("odd input"))[0] == "execution"


def test_close_tolerates_close_failure() -> None:
    """A connection whose close() blows up must not propagate the error."""

    class _ExplodingConn:
        def close(self) -> None:
            raise duckdb.IOException("already gone")

    _close(_ExplodingConn())


def test_text_top_n_zero_disables_top_lists(runner_config: Nl2DataConfig) -> None:
    """text_top_n=0 skips the top-N queries entirely (empty top lists)."""
    cfg = replace(runner_config, exec=ExecConfig(text_top_n=0))
    result = run(
        ValidatedSQL(sql="SELECT amount, cat FROM c2", tables=["c2"], limit=500), cfg
    )
    assert result.status == "ok"
    assert result.profile is not None
    assert result.profile["columns"]["cat"]["top"] == []


def test_private_degrade_paths(runner_config: Nl2DataConfig) -> None:
    """Broken column/SQL references degrade instead of raising."""
    conn = duckdb.connect(str(runner_config.paths.warehouse), read_only=True)
    try:
        assert _text_top(conn, "SELECT amount FROM c2", "__nope__", 5) == []
        profile = _build_profile(
            conn,
            "SELECT __nope__ FROM c2",
            description=(("amount", "INTEGER"),),
            sample=[],
            rowcount=200,
            cfg=runner_config,
        )
        assert profile == {"rowcount": 200, "columns": {}}
    finally:
        conn.close()


def test_safe_column_names_and_detail_ref_units(runner_config: Nl2DataConfig) -> None:
    """Name sanitising covers empty/digit/duplicate names; refs may be absolute."""
    assert _safe_column_names(["a b", "1x", "", "a-b", "a b"]) == [
        "a_b",
        "c_1x",
        "col_2",
        "a_b_1",
        "a_b_2",
    ]
    outside = runner_config.paths.data_dir.parent.parent / "outside.parquet"
    assert _detail_ref(outside, runner_config) == outside.as_posix()
    inside = runner_config.paths.scratch_dir / "x.parquet"
    assert _detail_ref(inside, runner_config).endswith("scratch/x.parquet")


def test_spill_failure_degrades_to_none(runner_config: Nl2DataConfig) -> None:
    """A scratch path that is a file makes the spill fail without erroring."""
    runner_config.paths.scratch_dir.parent.mkdir(parents=True, exist_ok=True)
    Path(runner_config.paths.scratch_dir).write_text("blocker", encoding="utf-8")
    result = run(
        ValidatedSQL(sql="SELECT amount FROM c2", tables=["c2"], limit=500),
        runner_config,
    )
    assert result.status == "ok"
    assert result.rowcount == 200
    assert result.detail_ref is None


def test_connect_failure_is_classified(
    runner_config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing duckdb.connect surfaces as a classified error result."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise duckdb.IOException("cannot open warehouse")

    monkeypatch.setattr(runner_module.duckdb, "connect", _boom)
    result = run(ValidatedSQL(sql="SELECT 1", tables=[], limit=1), runner_config)
    assert result.status == "error"
    assert result.error is not None
    assert result.error["category"] == "permission"


def test_collect_error_is_classified(
    runner_config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Errors raised while collecting results degrade to an error result."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise duckdb.IOException("spill exploded")

    monkeypatch.setattr(runner_module, "_spill_parquet", _boom)
    result = run(
        ValidatedSQL(sql="SELECT amount FROM c2", tables=["c2"], limit=500),
        runner_config,
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error["category"] == "permission"


def test_cleanup_skips_undeletable_file(
    runner_config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files that cannot be unlinked are skipped without raising.

    Undeletability is simulated by making ``Path.unlink`` raise for that one
    file: chmod-based tricks are platform-specific (a read-only *file* still
    unlinks on POSIX, where unlink permission depends on the parent directory,
    while a read-only *directory* still allows deletion on Windows).
    """
    scratch = runner_config.paths.scratch_dir
    scratch.mkdir(parents=True, exist_ok=True)
    stuck = scratch / "stuck.parquet"
    pq.write_table(pa.table({"x": [1]}), stuck)
    old = time.time() - 73 * 3600.0
    os.utime(stuck, (old, old))
    real_unlink = Path.unlink

    def _stuck_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self.name == "stuck.parquet":
            raise OSError("simulated undeletable file")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _stuck_unlink)
    assert cleanup_scratch(runner_config) == 0
    assert stuck.exists()
