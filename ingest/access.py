"""Access (.mdb/.accdb) ingestion via mdbtools into the shared pipeline.

mdbtools is invoked twice per source: ``mdb-tables -1`` lists the table names
(one per line) and ``mdb-export -D <fmt> -T <fmt> <file> <table>`` dumps one
table as a UTF-8 CSV with a header row. ``-D``/``-T`` are always passed
explicitly because mdb-export's date rendering otherwise follows the system
locale. The subprocess call is injectable (``runner``) so tests can fake
mdbtools without the real binaries.

Dates deliberately stay strings: mdb-export already renders date/time columns
with the configured formats, so this adapter performs no type promotion.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pyarrow.csv as pyarrow_csv

from catalog.naming import clean_name
from catalog.store import SourceEntry
from ingest.common import SOURCE_TYPE_ACCESS, IngestError, PreparedTable, ingest_tables
from nl2data.config import Nl2DataConfig

logger = logging.getLogger(__name__)

_SUPPORTED_SUFFIXES = frozenset({".mdb", ".accdb"})
_STDERR_TAIL = 500
# mdbtools error markers that mean the file itself cannot be read.
_ENCRYPTED_PATTERN = re.compile(r"not a database|encrypt|crypt|unrecognized|corrupt", re.IGNORECASE)

MdbRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run_mdb(
    cmd: list[str],
    runner: MdbRunner,
    cfg: Nl2DataConfig,
) -> subprocess.CompletedProcess[str]:
    """Run one mdbtools command, mapping binary/timeout failures to IngestError."""
    try:
        return runner(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=cfg.mdbtools.timeout_seconds,
        )
    except FileNotFoundError as exc:
        msg = (
            f"mdbtools executable not found: {cmd[0]!r} ({exc}). Reading Access files "
            "requires mdbtools; install it via 'sudo apt-get install mdbtools' "
            "(Linux/WSL) or 'brew install mdbtools' (macOS), or point the config keys "
            "mdbtools.mdb_tables_cmd / mdbtools.mdb_export_cmd at the binary paths."
        )
        raise IngestError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"mdbtools command {cmd[0]!r} timed out after {cfg.mdbtools.timeout_seconds}s"
        raise IngestError(msg) from exc


def _stderr_tail(proc: subprocess.CompletedProcess[str]) -> str:
    """Return the trimmed tail of a completed process's stderr."""
    return (proc.stderr or "").strip()[-_STDERR_TAIL:]


def ingest_access(
    source: Path,
    cfg: Nl2DataConfig,
    table: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> SourceEntry:
    """Ingest a Microsoft Access database through mdbtools.

    Every user table becomes one table in the shared pipeline
    (:func:`ingest.common.ingest_tables`), which owns naming, Parquet storage,
    DuckDB views and catalog lineage. ``mdb-tables -1`` output is filtered
    defensively against Access system/temp tables (``MSys*``, ``~*``, ``#*``).

    Args:
        source: Database path; suffix must be ``.mdb`` or ``.accdb``.
        cfg: Active configuration (mdbtools command names, timeout, formats).
        table: Exact table name to ingest; ``None`` ingests every user table.
        runner: Subprocess callable used for mdbtools; defaults to
            ``subprocess.run`` (resolved at call time). Tests inject a fake.

    Returns:
        The catalog entry written for this source.

    Raises:
        IngestError: If the file is missing or has an unsupported suffix, the
            mdbtools binaries are missing or fail (including encrypted or
            corrupted databases), the requested table does not exist, or no
            non-empty table remains.
    """
    source = Path(source)
    run: MdbRunner = subprocess.run if runner is None else runner

    if not source.is_file():
        msg = f"Access file not found: {source}"
        raise IngestError(msg)
    suffix = source.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        msg = (
            f"unsupported Access file {source.name!r} (suffix {suffix!r}); "
            "supported: .mdb/.accdb"
        )
        raise IngestError(msg)

    tables_cmd = [*cfg.mdbtools.mdb_tables_cmd.split(), "-1", str(source)]
    proc = _run_mdb(tables_cmd, run, cfg)
    if proc.returncode != 0:
        msg = f"mdb-tables failed for {source} (exit {proc.returncode}): {_stderr_tail(proc)}"
        raise IngestError(msg)
    names = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not names:
        msg = f"mdb-tables listed no tables in {source}"
        raise IngestError(msg)

    available: list[str] = []
    for name in names:
        if name.lower().startswith("msys") or name.startswith(("~", "#")):
            # mdbtools already hides MSys* tables by default; older builds and
            # damaged catalogs can still leak them, so filter defensively.
            logger.info("skipping Access system/temp table %r in %s", name, source)
            continue
        available.append(name)
    if not available:
        msg = f"no user tables remain in {source} after filtering system tables"
        raise IngestError(msg)

    if table is not None and table not in available:
        msg = (
            f"table {table!r} not found in {source}; "
            f"available tables: {', '.join(repr(n) for n in available)}"
        )
        raise IngestError(msg)
    wanted = available if table is None else [table]

    prepared: list[PreparedTable] = []
    for name in wanted:
        export_cmd = [
            *cfg.mdbtools.mdb_export_cmd.split(),
            "-D",
            cfg.mdbtools.date_format,
            "-T",
            cfg.mdbtools.datetime_format,
            str(source),
            name,
        ]
        proc = _run_mdb(export_cmd, run, cfg)
        if proc.returncode != 0:
            msg = (
                f"mdb-export failed for table {name!r} in {source} "
                f"(exit {proc.returncode}): {_stderr_tail(proc)}"
            )
            if _ENCRYPTED_PATTERN.search(proc.stderr or ""):
                msg += ";文件可能已加密或损坏;不支持加密的 .accdb"
            raise IngestError(msg)
        # Dates stay strings on purpose: mdb-export already rendered date/time
        # columns with the -D/-T formats, so no further type promotion happens.
        # newlines_in_values tolerates quoted fields spanning multiple lines.
        arrow_table = pyarrow_csv.read_csv(
            io.BytesIO(proc.stdout.encode("utf-8")),
            parse_options=pyarrow_csv.ParseOptions(newlines_in_values=True),
        )
        frame = arrow_table.to_pandas()
        if frame.empty:
            logger.warning("table %r in %s is empty; skipped", name, source)
            continue
        prepared.append(PreparedTable(original_name=name, frame=frame))

    return ingest_tables(
        source_name=clean_name(source.stem, max_length=cfg.ingest.max_name_length),
        source_type=SOURCE_TYPE_ACCESS,
        source_path=source,
        tables=prepared,
        cfg=cfg,
    )
