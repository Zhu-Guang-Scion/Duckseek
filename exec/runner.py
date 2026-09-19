"""Sandboxed SQL execution and result compaction against DuckDB (milestone 3, T11).

The runner is the second line of defence behind the T10 guard: even a
``ValidatedSQL`` that smuggled a write past the parse-tree checks is refused
by DuckDB itself, because the warehouse is opened ``read_only=True``.

Timeout mechanism: DuckDB's Python API has no statement-level timeout
(``SET statement_timeout`` does not exist), so the query runs on a daemon
thread and the main thread waits at most ``cfg.exec.timeout_seconds``. On
timeout ``conn.interrupt()`` cancels the server-side execution and the
connection is closed once the worker observed the interrupt; the caller gets
a ``timeout`` error result, never an exception.

Result compaction keeps the LLM context small: at most
``cfg.exec.sample_rows`` rows are inlined, aggregates (one single-pass SQL
for all numeric columns plus per-column top-N frequency queries over the
already LIMIT-bounded result set) summarise large results, and the full row
set (at most ``vsql.limit`` rows) is spilled to a Parquet file under the
scratch directory reported via ``detail_ref``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from guard.validate import ValidatedSQL
from nl2data.config import Nl2DataConfig

logger = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 300
_INTERRUPT_GRACE_SECONDS = 5.0
# DuckDB type-string prefixes treated as numeric profiling targets.
_NUMERIC_TYPE_PREFIXES: tuple[str, ...] = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
)
_TEXT_TYPE_PREFIXES: tuple[str, ...] = ("VARCHAR", "CHAR", "TEXT", "STRING")


class ExecutionError(RuntimeError):
    """Raised on API misuse, e.g. when ``run`` receives a non-ValidatedSQL."""


@dataclass(frozen=True)
class ExecutionResult:
    """Compacted outcome of one sandboxed query execution.

    ``status`` is ``"ok"`` or ``"error"``; on error every data field is empty
    and ``error`` carries ``{"category", "message"}``. ``rows`` holds at most
    ``cfg.exec.sample_rows`` JSON-safe rows. ``profile`` appears only when
    ``rowcount`` exceeds ``cfg.exec.profile_over_rows``. ``detail_ref`` is the
    scratch Parquet path when rows overflowed the inline sample.
    """

    status: str
    rowcount: int
    columns: list[str]
    rows: list[dict[str, Any]]
    profile: dict[str, Any] | None
    detail_ref: str | None
    error: dict[str, str] | None
    latency_ms: float


@dataclass(frozen=True)
class _ColumnKind:
    """One result column with its profiling kind."""

    name: str
    numeric: bool


def _quote(identifier: str) -> str:
    """Quote a DuckDB identifier (double-quote style)."""
    return '"' + identifier.replace('"', '""') + '"'


def _jsonable(value: Any) -> Any:
    """Convert a DuckDB scalar into a JSON-safe counterpart."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _trim(message: str) -> str:
    """Truncate an error message so LLM context stays bounded."""
    if len(message) <= _MAX_MESSAGE_CHARS:
        return message
    return message[:_MAX_MESSAGE_CHARS] + "…"


def _classify_error(exc: BaseException) -> tuple[str, str]:
    """Map a DuckDB exception to a ``(category, message)`` pair.

    Categories: ``permission`` (IO/access denied, read-only violations),
    ``syntax`` (parse errors), ``resource`` (out of memory / thread limits),
    ``timeout`` (interrupted queries) and the ``execution`` fallback. Messages
    are truncated and never contain connection secrets (the warehouse path may
    remain; it is not sensitive).
    """
    text = str(exc)
    lowered = text.lower()
    permission_types = (
        duckdb.IOException,
        getattr(duckdb, "PermissionException", ()),
    )
    if isinstance(exc, permission_types):
        return "permission", _trim(text)
    if isinstance(exc, duckdb.OutOfMemoryException):
        return "resource", _trim(text)
    syntax_types = (duckdb.ParserException, getattr(duckdb, "SyntaxException", ()))
    if isinstance(exc, syntax_types):
        return "syntax", _trim(text)
    if isinstance(exc, duckdb.InterruptException):
        return "timeout", _trim(text)
    # Keyword sweep for builds where the exception hierarchy differs (e.g.
    # writing to a read-only database surfaces as InvalidInputException in
    # duckdb 1.5.5 with "read-only mode" in the message).
    if "read-only" in lowered or "read only" in lowered:
        return "permission", _trim(text)
    if "permission" in lowered or "denied" in lowered:
        return "permission", _trim(text)
    if "out of memory" in lowered or "memory limit" in lowered or "thread" in lowered:
        return "resource", _trim(text)
    if "interrupted" in lowered:
        return "timeout", _trim(text)
    return "execution", _trim(text)


def _error_result(started: float, category: str, message: str) -> ExecutionResult:
    """Build the uniform error result (rowcount 0, empty data fields)."""
    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    return ExecutionResult(
        status="error",
        rowcount=0,
        columns=[],
        rows=[],
        profile=None,
        detail_ref=None,
        error={"category": category, "message": message},
        latency_ms=latency_ms,
    )


def _close(conn: duckdb.DuckDBPyConnection) -> None:
    """Close a connection, tolerating an already-dead connection."""
    try:
        conn.close()
    except duckdb.Error:
        logger.debug("connection close failed (already closed?)", exc_info=True)


def _handle_timeout(
    conn: duckdb.DuckDBPyConnection,
    worker: threading.Thread,
    cfg: Nl2DataConfig,
    started: float,
) -> ExecutionResult:
    """Interrupt the runaway query and report a ``timeout`` result.

    The connection is closed only after the worker observed the interrupt;
    closing it under a live query would be unsafe. If the worker ignores the
    interrupt past the grace period the connection is intentionally leaked
    (the daemon thread dies with the process) rather than risk undefined
    behaviour.
    """
    try:
        conn.interrupt()
    except duckdb.Error:
        logger.warning("conn.interrupt() unavailable or failed", exc_info=True)
    worker.join(_INTERRUPT_GRACE_SECONDS)
    if not worker.is_alive():
        _close(conn)
    return _error_result(
        started, "timeout", f"查询超时({cfg.exec.timeout_seconds}s),已取消"
    )


def _column_kinds(
    description: Sequence[tuple[Any, ...]],
    sample: list[tuple[Any, ...]],
) -> list[_ColumnKind]:
    """Decide numeric vs text kind for every result column.

    The cursor's DuckDB type code decides; when the type is neither numeric
    nor textual (dates, blobs, lists) the first non-null sample value is probed
    with Python ``isinstance``.
    """
    kinds: list[_ColumnKind] = []
    for index, entry in enumerate(description):
        name = str(entry[0])
        type_text = str(entry[1]).upper()
        numeric = any(type_text.startswith(prefix) for prefix in _NUMERIC_TYPE_PREFIXES)
        if not numeric and not type_text.startswith(_TEXT_TYPE_PREFIXES):
            first = next((row[index] for row in sample if row[index] is not None), None)
            numeric = isinstance(first, (int, float, Decimal)) and not isinstance(
                first, bool
            )
        kinds.append(_ColumnKind(name=name, numeric=numeric))
    return kinds


def _text_top(
    conn: duckdb.DuckDBPyConnection, sql: str, column: str, top_n: int
) -> list[dict[str, Any]]:
    """Top-N most frequent values of one text column over the result set."""
    if top_n <= 0:
        return []
    quoted = _quote(column)
    top_sql = (
        f"SELECT {quoted} AS __v__, count(*) AS __c__ "
        f"FROM ({sql}) AS __exec_inner__ "
        f"WHERE {quoted} IS NOT NULL "
        f"GROUP BY 1 ORDER BY 2 DESC, 1 ASC LIMIT {int(top_n)}"
    )
    try:
        found = conn.execute(top_sql).fetchall()
    except duckdb.Error as exc:
        # Degrade per column: a failed top-N never invalidates the result.
        logger.warning("text top-N degraded for column %s: %s", column, exc)
        return []
    return [{"value": _jsonable(value), "count": int(count)} for value, count in found]


def _build_profile(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    description: Sequence[tuple[Any, ...]],
    sample: list[tuple[Any, ...]],
    rowcount: int,
    cfg: Nl2DataConfig,
) -> dict[str, Any]:
    """Profile the (already LIMIT-bounded) result set via aggregate SQL.

    All numeric columns are summarised in one single-pass aggregate
    (min/max/avg/quantile_cont p25/p50/p75 plus per-column null counts); text
    columns get per-column top-N frequency queries. The basis is the bounded
    result set, not a full table scan, so per-column queries stay cheap.
    """
    columns = _column_kinds(description, sample)
    parts = ["count(*) AS __rows__"]
    for index, column in enumerate(columns):
        quoted = _quote(column.name)
        parts.append(f"count({quoted}) AS __n_{index}")
        if column.numeric:
            parts.extend(
                (
                    f"min({quoted}) AS __min_{index}",
                    f"max({quoted}) AS __max_{index}",
                    f"avg(CAST({quoted} AS DOUBLE)) AS __avg_{index}",
                    f"quantile_cont(CAST({quoted} AS DOUBLE), [0.25, 0.5, 0.75]) "
                    f"AS __q_{index}",
                )
            )
    aggregate_sql = f"SELECT {', '.join(parts)} FROM ({sql}) AS __exec_inner__"
    try:
        result = conn.execute(aggregate_sql)
        names = [str(entry[0]) for entry in result.description or []]
        row = result.fetchone()
    except duckdb.Error as exc:
        logger.warning("result-set profiling degraded (aggregate failed): %s", exc)
        return {"rowcount": rowcount, "columns": {}}
    values = dict(zip(names, row, strict=True)) if row is not None else {}

    profile: dict[str, Any] = {"rowcount": rowcount, "columns": {}}
    for index, column in enumerate(columns):
        non_null = int(values.get(f"__n_{index}", 0))
        entry: dict[str, Any] = {
            "kind": "numeric" if column.numeric else "text",
            "null_rate": round((rowcount - non_null) / rowcount, 6) if rowcount else 0.0,
        }
        if column.numeric:
            if values.get(f"__min_{index}") is not None:
                entry["min"] = _jsonable(values[f"__min_{index}"])
                entry["max"] = _jsonable(values[f"__max_{index}"])
                entry["avg"] = round(float(values[f"__avg_{index}"]), 6)
                entry["quantiles"] = {
                    label: float(value)
                    for label, value in zip(
                        ("p25", "p50", "p75"), values[f"__q_{index}"], strict=True
                    )
                }
        else:
            entry["top"] = _text_top(conn, sql, column.name, cfg.exec.text_top_n)
        profile["columns"][column.name] = entry
    return profile


def _detail_ref(path: Path, cfg: Nl2DataConfig) -> str:
    """Record the spill path relative to the data root (catalog path style)."""
    try:
        return path.relative_to(cfg.paths.data_dir.parent).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_column_names(columns: Sequence[str]) -> list[str]:
    """Parquet-safe, de-duplicated column names (illegal chars become ``_``)."""
    names: list[str] = []
    used: set[str] = set()
    for index, name in enumerate(columns):
        cleaned = re.sub(r"[^0-9A-Za-z_]", "_", name)
        if not cleaned:
            cleaned = f"col_{index}"
        elif cleaned[0].isdigit():
            cleaned = f"c_{cleaned}"
        candidate = cleaned
        suffix = 1
        while candidate in used:
            candidate = f"{cleaned}_{suffix}"
            suffix += 1
        used.add(candidate)
        names.append(candidate)
    return names


def _spill_parquet(
    rows: list[tuple[Any, ...]], columns: list[str], cfg: Nl2DataConfig
) -> str | None:
    """Write the full row set to a fresh scratch Parquet file; best-effort.

    Returns:
        The recorded ``detail_ref`` path, or None when the spill failed (the
        inline sample rows are unaffected either way).
    """
    safe = _safe_column_names(columns)
    records = [dict(zip(safe, row, strict=True)) for row in rows]
    path = cfg.paths.scratch_dir / f"{uuid.uuid4().hex}.parquet"
    try:
        cfg.paths.scratch_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(records), path)
    except Exception as exc:  # spill is best-effort; pyarrow/OSError variety
        logger.warning("detail spill to %s failed: %s", path, exc)
        return None
    return _detail_ref(path, cfg)


def _collect(
    conn: duckdb.DuckDBPyConnection,
    cursor: duckdb.DuckDBPyConnection,
    vsql: ValidatedSQL,
    cfg: Nl2DataConfig,
    started: float,
) -> ExecutionResult:
    """Fetch, compact, profile and spill the successful query result."""
    limit = max(int(vsql.limit), 0)
    batch = list(cursor.fetchmany(limit + 1)) if limit > 0 else []
    fetched = batch[:limit]
    rowcount = len(fetched)
    columns = [str(entry[0]) for entry in cursor.description or []]
    rows = [
        {name: _jsonable(row[index]) for index, name in enumerate(columns)}
        for row in fetched[: cfg.exec.sample_rows]
    ]
    profile: dict[str, Any] | None = None
    if rowcount > cfg.exec.profile_over_rows:
        profile = _build_profile(
            conn, vsql.sql, cursor.description or (), fetched, rowcount, cfg
        )
    detail_ref: str | None = None
    if rowcount > cfg.exec.sample_rows:
        detail_ref = _spill_parquet(fetched, columns, cfg)
    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    return ExecutionResult(
        status="ok",
        rowcount=rowcount,
        columns=columns,
        rows=rows,
        profile=profile,
        detail_ref=detail_ref,
        error=None,
        latency_ms=latency_ms,
    )


def run(vsql: ValidatedSQL, cfg: Nl2DataConfig) -> ExecutionResult:
    """Execute a validated query on a read-only warehouse connection.

    Args:
        vsql: Output of ``guard.validate``; anything else (including
            duck-typed stand-ins) raises :class:`ExecutionError`.
        cfg: Active configuration (timeouts, sample sizes, paths).

    Returns:
        The compacted :class:`ExecutionResult`; failures are reported as
        ``status="error"`` with a classified message, never raised.

    Raises:
        ExecutionError: When ``vsql`` is not a :class:`ValidatedSQL` instance
            (runtime guard against bypassing the T10 guardrails).
    """
    if not isinstance(vsql, ValidatedSQL):
        raise ExecutionError("必须传入 ValidatedSQL 实例(需先通过 guard.validate 校验)")
    started = time.perf_counter()
    warehouse = Path(cfg.paths.warehouse)
    if not warehouse.is_file():
        return _error_result(
            started, "permission", f"warehouse 不存在:{warehouse},请先执行数据摄取"
        )
    try:
        conn = duckdb.connect(str(warehouse), read_only=True)
    except duckdb.Error as exc:
        category, message = _classify_error(exc)
        return _error_result(started, category, message)

    done = threading.Event()
    outcome: dict[str, Any] = {}

    def _worker() -> None:
        """Execute the query once; the Python API has no statement timeout."""
        try:
            outcome["cursor"] = conn.execute(vsql.sql)
        except BaseException as exc:  # reclassified by the caller
            outcome["exception"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_worker, daemon=True, name="nl2data-query")
    worker.start()
    if not done.wait(timeout=max(int(cfg.exec.timeout_seconds), 1)):
        return _handle_timeout(conn, worker, cfg, started)

    if "exception" in outcome:
        category, message = _classify_error(outcome["exception"])
        _close(conn)
        return _error_result(started, category, message)
    try:
        return _collect(conn, outcome["cursor"], vsql, cfg, started)
    except duckdb.Error as exc:
        category, message = _classify_error(exc)
        return _error_result(started, category, message)
    finally:
        _close(conn)


def cleanup_scratch(cfg: Nl2DataConfig, max_age_hours: float = 72.0) -> int:
    """Delete stale spill Parquet files from the scratch directory.

    Periodic cleanup policy: run on a schedule (cron / manual invocation is
    recommended, default retention 72h); the docs own the operational note.

    Args:
        cfg: Active configuration providing ``paths.scratch_dir``.
        max_age_hours: Files whose mtime is older than this are removed.

    Returns:
        The number of deleted files (0 when the directory is missing).
    """
    scratch = Path(cfg.paths.scratch_dir)
    if not scratch.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600.0
    removed = 0
    for candidate in sorted(scratch.glob("*.parquet")):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except OSError:
            logger.warning("could not remove stale scratch file %s", candidate)
    return removed
