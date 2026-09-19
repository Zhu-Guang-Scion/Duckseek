"""Shared ingestion pipeline used by every source type (T2/T3 contract layer).

Source adapters (``ingest.excel``, ``ingest.access``) only read raw frames and
hand them to :func:`ingest_tables`, which owns everything downstream: column
naming, Parquet layout, DuckDB view registration and catalog lineage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from catalog.naming import clean_name
from catalog.store import CatalogStore, ColumnEntry, SourceEntry, TableEntry
from nl2data.config import Nl2DataConfig

logger = logging.getLogger(__name__)

SOURCE_TYPE_EXCEL = "excel"
SOURCE_TYPE_ACCESS = "access"
SOURCE_TYPE_PARQUET = "parquet"


class IngestError(RuntimeError):
    """Raised when a source cannot be ingested."""


@dataclass(frozen=True)
class PreparedTable:
    """One raw source table: its original name plus the unmodified frame."""

    original_name: str
    frame: pd.DataFrame


@dataclass(frozen=True)
class RegisteredTable:
    """An existing Parquet file registered as a view without copying."""

    original_name: str
    parquet_path: Path
    rows: int
    column_names: list[str]


def normalize_columns(
    frame: pd.DataFrame,
    existing: set[str],
    *,
    max_length: int = 63,
) -> tuple[pd.DataFrame, list[ColumnEntry]]:
    """Rename columns to clean names, de-duplicating against ``existing``.

    Args:
        frame: Raw frame whose headers may contain Chinese or symbols.
        existing: Clean names already taken; chosen names are added to it.
        max_length: Maximum clean-name length (§5.4 contract).

    Returns:
        Tuple of the renamed frame and the column lineage entries.
    """
    renames: dict[str, str] = {}
    entries: list[ColumnEntry] = []
    for original in frame.columns:
        clean = clean_name(str(original), existing, max_length=max_length)
        renames[str(original)] = clean
        entries.append(ColumnEntry(name=clean, original_name=str(original)))
    return frame.rename(columns=renames), entries


def _normalize_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize dtypes for Parquet: nullable ints/bools/strings, no objects."""
    out = frame.convert_dtypes()
    for column in out.columns:
        if pd.api.types.is_object_dtype(out[column]):
            out[column] = out[column].astype("string")
    return out


def register_view(
    conn: duckdb.DuckDBPyConnection,
    view_name: str,
    parquet_path: Path,
    select: str = "*",
) -> None:
    """Register (or replace) a DuckDB view over a Parquet file."""
    escaped = str(parquet_path).replace("'", "''")
    conn.execute(
        f'CREATE OR REPLACE VIEW "{view_name}" AS '
        f"SELECT {select} FROM read_parquet('{escaped}')"
    )


def _quote_ident(identifier: str) -> str:
    """Quote a SQL identifier (double-quote style)."""
    return '"' + identifier.replace('"', '""') + '"'


def _recorded_parquet_path(parquet_path: Path, cfg: Nl2DataConfig) -> str:
    """Record the Parquet path relative to the data root when possible."""
    root = cfg.paths.data_dir.parent
    try:
        return parquet_path.relative_to(root).as_posix()
    except ValueError:
        return str(parquet_path)


def _resolve_recorded_path(cfg: Nl2DataConfig, recorded: str) -> Path:
    """Resolve a catalog-recorded relative path against the data root."""
    path = Path(recorded)
    return path if path.is_absolute() else cfg.paths.data_dir.parent / path


def _drop_previous_artifacts(
    conn: duckdb.DuckDBPyConnection,
    previous: SourceEntry,
    cfg: Nl2DataConfig,
) -> None:
    """Drop old views and managed Parquet files before re-ingesting a source.

    Only files inside the managed ``data/parquet`` area are deleted; paths
    registered from outside (native Parquet sources) belong to the user and
    are never touched.
    """
    managed_root = cfg.paths.parquet_dir.resolve()
    for old_table in previous.tables:
        conn.execute(f'DROP VIEW IF EXISTS "{old_table.name}"')
        old_path = _resolve_recorded_path(cfg, old_table.parquet)
        try:
            managed = old_path.resolve().is_relative_to(managed_root)
        except (OSError, ValueError):
            managed = False
        if managed and old_path.exists():
            old_path.unlink(missing_ok=True)
            logger.info("dropped stale artifacts for table %s", old_table.name)


def ingest_tables(
    *,
    source_name: str,
    source_type: str,
    source_path: Path,
    tables: list[PreparedTable],
    cfg: Nl2DataConfig,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> SourceEntry:
    """Persist prepared tables and record lineage (the §5 contract pipeline).

    Per table: clean naming → Parquet under
    ``data/parquet/<source_slug>/<table>.parquet`` → DuckDB view registration;
    then a single catalog upsert for the whole source. Re-ingesting a source
    slug replaces its previous catalog entry.

    Args:
        source_name: Clean source slug (already ``clean_name``-ed by the caller).
        source_type: One of ``excel`` / ``access``.
        source_path: Original file path as given by the user.
        tables: Prepared frames; empty sheets must be filtered out by the caller.
        cfg: Active configuration.
        conn: Optional existing DuckDB connection (one is opened otherwise).

    Returns:
        The catalog entry that was written.

    Raises:
        IngestError: If no tables remain or a Parquet write fails.
    """
    if not tables:
        raise IngestError(f"no non-empty tables found in {source_path}")

    store = CatalogStore(cfg.paths.catalog)
    previous = store.get_source(source_name)
    if previous is not None and previous.path != str(source_path):
        logger.warning(
            "source %s already exists from %s; replacing with %s",
            source_name,
            previous.path,
            source_path,
        )

    taken: set[str] = {
        table.name
        for src in store.sources
        if src.name != source_name
        for table in src.tables
    }
    max_length = cfg.ingest.max_name_length
    parquet_dir = cfg.paths.parquet_dir / source_name
    parquet_dir.mkdir(parents=True, exist_ok=True)

    owned_conn = conn is None
    if conn is None:
        cfg.paths.warehouse.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(cfg.paths.warehouse))
    if previous is not None:
        _drop_previous_artifacts(conn, previous, cfg)
    entries: list[TableEntry] = []
    try:
        for table in tables:
            clean = clean_name(table.original_name, taken, max_length=max_length)
            taken.add(clean)
            frame, columns = normalize_columns(
                table.frame, set(), max_length=max_length
            )
            parquet_path = parquet_dir / f"{clean}.parquet"
            try:
                _normalize_dtypes(frame).to_parquet(parquet_path, index=False)
            except (OSError, ValueError, TypeError) as exc:
                msg = f"failed to write {parquet_path}: {exc}"
                raise IngestError(msg) from exc
            register_view(conn, clean, parquet_path)
            entries.append(
                TableEntry(
                    name=clean,
                    original_name=table.original_name,
                    parquet=_recorded_parquet_path(parquet_path, cfg),
                    rows=int(len(frame)),
                    columns=columns,
                )
            )
    finally:
        if owned_conn:
            conn.close()

    entry = SourceEntry(
        name=source_name,
        type=source_type,
        path=str(source_path),
        ingested_at=datetime.now(UTC).isoformat(timespec="seconds"),
        tables=entries,
    )
    store.upsert_source(entry)
    store.save()
    logger.info(
        "ingested source %s (%s): %d table(s)", source_name, source_type, len(entries)
    )
    return entry


def register_tables(
    *,
    source_name: str,
    source_path: Path,
    tables: list[RegisteredTable],
    cfg: Nl2DataConfig,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> SourceEntry:
    """Register existing Parquet files as views without copying (T-P).

    Mirrors :func:`ingest_tables` naming, idempotency and catalog semantics;
    the only difference is that the Parquet file stays where it is and the
    catalog records its original path.

    Args:
        source_name: Clean source slug (already ``clean_name``-ed by the caller).
        source_path: Original file path as given by the user.
        tables: Registered tables; each carries its own Parquet path and rows.
        cfg: Active configuration.
        conn: Optional existing DuckDB connection (one is opened otherwise).

    Returns:
        The catalog entry that was written.

    Raises:
        IngestError: If no tables are given.
    """
    if not tables:
        raise IngestError(f"no tables to register from {source_path}")

    store = CatalogStore(cfg.paths.catalog)
    previous = store.get_source(source_name)
    if previous is not None and previous.path != str(source_path):
        logger.warning(
            "source %s already exists from %s; replacing with %s",
            source_name,
            previous.path,
            source_path,
        )

    taken: set[str] = {
        table.name
        for src in store.sources
        if src.name != source_name
        for table in src.tables
    }
    max_length = cfg.ingest.max_name_length

    owned_conn = conn is None
    if conn is None:
        cfg.paths.warehouse.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(cfg.paths.warehouse))
    if previous is not None:
        _drop_previous_artifacts(conn, previous, cfg)
    entries: list[TableEntry] = []
    try:
        for table in tables:
            clean = clean_name(table.original_name, taken, max_length=max_length)
            taken.add(clean)
            existing: set[str] = set()
            selects: list[str] = []
            columns: list[ColumnEntry] = []
            for original in table.column_names:
                col = clean_name(str(original), existing, max_length=max_length)
                selects.append(f"{_quote_ident(str(original))} AS {_quote_ident(col)}")
                columns.append(ColumnEntry(name=col, original_name=str(original)))
            register_view(
                conn,
                clean,
                # Absolute path: the persisted VIEW outlives this process and
                # must not depend on the CLI's working directory.
                table.parquet_path.resolve(),
                select=", ".join(selects) or "*",
            )
            entries.append(
                TableEntry(
                    name=clean,
                    original_name=table.original_name,
                    parquet=str(table.parquet_path),
                    rows=int(table.rows),
                    columns=columns,
                )
            )
    finally:
        if owned_conn:
            conn.close()

    entry = SourceEntry(
        name=source_name,
        type=SOURCE_TYPE_PARQUET,
        path=str(source_path),
        ingested_at=datetime.now(UTC).isoformat(timespec="seconds"),
        tables=entries,
    )
    store.upsert_source(entry)
    store.save()
    logger.info(
        "registered source %s (%s): %d table(s)",
        source_name,
        SOURCE_TYPE_PARQUET,
        len(entries),
    )
    return entry
