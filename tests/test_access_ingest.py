"""Tests for the Access ingestion adapter (ingest.access).

Every case fakes mdbtools via an injected runner or by monkeypatching
``subprocess.run``; the real binaries are never required (the final test is
skipped unless both mdbtools and NL2DATA_TEST_MDB are present).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest

from catalog.store import CatalogStore
from ingest.access import ingest_access
from ingest.common import IngestError
from nl2data.config import Nl2DataConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_text(name: str) -> str:
    """Return one Access fixture file's text."""
    return (FIXTURES / "access" / name).read_text(encoding="utf-8")


def _make_mdb(tmp_path: Path, name: str = "orders_db.mdb") -> Path:
    """Create a placeholder Access file (its bytes are never read directly)."""
    source = tmp_path / name
    source.write_bytes(b"fake access database bytes")
    return source


def _make_runner(
    tables_stdout: str | None = None,
    *,
    export_by_table: dict[str, str] | None = None,
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], list[list[str]]]:
    """Build a fake mdbtools runner plus the list of commands it received.

    Dispatch mirrors the real CLI: ``cmd[0]`` ends with ``mdb-tables`` or
    ``mdb-export``; the exported table name is the last positional argument.
    """
    exports = {
        "orders": _fixture_text("orders_export.csv"),
        "订单明细": _fixture_text("empty_export.csv"),
    }
    if export_by_table:
        exports.update(export_by_table)
    calls: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0].endswith("mdb-tables"):
            stdout = (
                tables_stdout if tables_stdout is not None else _fixture_text("tables_list.txt")
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if cmd[0].endswith("mdb-export"):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=exports.get(cmd[-1], ""), stderr=""
            )
        msg = f"unexpected mdbtools command: {cmd}"
        raise AssertionError(msg)

    return _run, calls


def test_full_ingest(
    config: Nl2DataConfig,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listed tables are ingested; the empty one is skipped with a warning."""
    source = _make_mdb(tmp_path)
    runner, _ = _make_runner()
    monkeypatch.setattr("ingest.access.subprocess.run", runner)

    with caplog.at_level(logging.INFO):
        entry = ingest_access(source, config)

    assert entry.type == "access"
    assert [t.original_name for t in entry.tables] == ["orders"]
    orders = entry.tables[0]
    assert orders.name == "orders"
    assert orders.rows == 3
    lineage = [(column.name, column.original_name) for column in orders.columns]
    assert lineage == [
        ("ding_dan_id", "订单ID"),
        ("ke_hu", "客户"),
        ("jin_e", "金额"),
    ]

    parquet = config.paths.parquet_dir / entry.name / "orders.parquet"
    assert parquet.is_file()
    with duckdb.connect(str(config.paths.warehouse)) as conn:
        total = conn.execute('SELECT count(*) FROM "orders"').fetchone()
        customers = conn.execute(
            'SELECT "ke_hu" FROM "orders" ORDER BY "ding_dan_id"'
        ).fetchall()
    assert total == (3,)
    # The quoted comma inside the second customer must survive CSV parsing.
    assert customers == [("张三",), ("李四,先生",), ("王五",)]

    stored = CatalogStore(config.paths.catalog).get_source(entry.name)
    assert stored is not None
    assert stored.type == "access"

    skipped = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "订单明细" in record.getMessage()
    ]
    assert skipped


def test_runner_injection_and_cli_contract(config: Nl2DataConfig, tmp_path: Path) -> None:
    """``runner=`` injection works; mdbtools argv keeps options before operands."""
    source = _make_mdb(tmp_path)
    runner, calls = _make_runner()

    entry = ingest_access(source, config, runner=runner)

    assert entry.type == "access"
    assert [t.original_name for t in entry.tables] == ["orders"]
    assert entry.tables[0].rows == 3
    tables_cmd, export_cmd = calls[0], calls[1]
    assert tables_cmd == ["mdb-tables", "-1", str(source)]
    assert export_cmd[:5] == [
        "mdb-export",
        "-D",
        config.mdbtools.date_format,
        "-T",
        config.mdbtools.datetime_format,
    ]
    assert export_cmd[5:] == [str(source), "orders"]


def test_table_selection_and_unknown_table(config: Nl2DataConfig, tmp_path: Path) -> None:
    """table= picks exactly one table; unknown names list the available ones."""
    source = _make_mdb(tmp_path)
    entry = ingest_access(source, config, table="orders", runner=_make_runner()[0])
    assert [t.original_name for t in entry.tables] == ["orders"]
    assert entry.tables[0].rows == 3

    with pytest.raises(IngestError, match="不存在") as exc_info:
        ingest_access(source, config, table="不存在", runner=_make_runner()[0])
    message = str(exc_info.value)
    assert "orders" in message
    assert "订单明细" in message


def test_system_tables_filtered(
    config: Nl2DataConfig,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MSys*/~/# tables are skipped defensively and logged as info."""
    source = _make_mdb(tmp_path)
    tables_stdout = _fixture_text("tables_list.txt") + "MSysObjects\n~TMPCLP\n#TEMP\n"

    with caplog.at_level(logging.INFO, logger="ingest.access"):
        entry = ingest_access(
            source, config, runner=_make_runner(tables_stdout=tables_stdout)[0]
        )

    assert [t.original_name for t in entry.tables] == ["orders"]
    assert entry.tables[0].rows == 3
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert any("MSysObjects" in message for message in messages)
    assert any("~TMPCLP" in message for message in messages)


def test_missing_mdbtools_binary(config: Nl2DataConfig, tmp_path: Path) -> None:
    """A configured binary that does not exist yields an actionable error."""
    source = _make_mdb(tmp_path)
    broken = replace(
        config,
        mdbtools=replace(config.mdbtools, mdb_tables_cmd="nl2data_missing_mdb_tables_bin"),
    )

    with pytest.raises(IngestError, match="mdbtools") as exc_info:
        ingest_access(source, broken)
    message = str(exc_info.value)
    assert "mdbtools.mdb_tables_cmd" in message
    assert "apt" in message
    assert "brew" in message


def test_tables_command_failure_reports_stderr(config: Nl2DataConfig, tmp_path: Path) -> None:
    """A non-zero mdb-tables exit surfaces the stderr tail in the message."""

    def failing_tables(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with pytest.raises(IngestError, match="boom"):
        ingest_access(_make_mdb(tmp_path), config, runner=failing_tables)


def test_encrypted_export_hint(config: Nl2DataConfig, tmp_path: Path) -> None:
    """An encryption marker in mdb-export stderr appends the encrypted-file hint."""

    def encrypted_export(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if cmd[0].endswith("mdb-tables"):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_fixture_text("tables_list.txt"), stderr=""
            )
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Error: file is encrypted")

    with pytest.raises(IngestError, match="加密") as exc_info:
        ingest_access(_make_mdb(tmp_path), config, runner=encrypted_export)
    assert "文件可能已加密或损坏" in str(exc_info.value)


def test_timeout_raises(config: Nl2DataConfig, tmp_path: Path) -> None:
    """A hanging mdbtools call is converted into IngestError."""

    def timing_out(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="x", timeout=5)

    with pytest.raises(IngestError, match="timed out"):
        ingest_access(_make_mdb(tmp_path), config, runner=timing_out)


def test_suffix_and_missing_file_rejected(config: Nl2DataConfig, tmp_path: Path) -> None:
    """Unsupported suffixes and missing files fail before any mdbtools call."""
    runner, calls = _make_runner()

    xlsx = tmp_path / "workbook.xlsx"
    xlsx.write_bytes(b"not an access database")
    with pytest.raises(IngestError, match="unsupported"):
        ingest_access(xlsx, config, runner=runner)
    with pytest.raises(IngestError, match="not found"):
        ingest_access(tmp_path / "missing.mdb", config, runner=runner)
    assert calls == []


@pytest.mark.skipif(
    shutil.which("mdb-tables") is None or not os.environ.get("NL2DATA_TEST_MDB"),
    reason="requires mdbtools and a real .mdb (set NL2DATA_TEST_MDB)",
)
def test_real_mdb_integration(config: Nl2DataConfig) -> None:
    """End-to-end run against a real database; opt-in via NL2DATA_TEST_MDB."""
    entry = ingest_access(Path(os.environ["NL2DATA_TEST_MDB"]), config)
    assert entry.type == "access"
    assert len(entry.tables) >= 1
