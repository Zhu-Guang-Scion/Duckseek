"""Native Parquet registration: views over existing files, nothing copied.

The register mode never duplicates data: the .parquet file stays where it is,
a DuckDB view with cleaned column names is created over it, and catalog.yaml
records the original path (``source.type: parquet``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pyarrow.parquet as pq

from catalog.naming import clean_name
from catalog.store import SourceEntry
from ingest.common import (
    IngestError,
    RegisteredTable,
    register_tables,
)
from nl2data.config import Nl2DataConfig

logger = logging.getLogger(__name__)

_SUPPORTED_SUFFIXES = frozenset({".parquet"})


def ingest_parquet(
    source: Path,
    cfg: Nl2DataConfig,
    name: str | None = None,
) -> SourceEntry:
    """Register an existing ``.parquet`` file as a queryable table.

    Args:
        source: Parquet file path (suffix must be ``.parquet``); the file is
            not copied or modified.
        cfg: Active configuration.
        name: Optional source alias overriding the file stem for the slug.

    Returns:
        The catalog entry that was written.

    Raises:
        IngestError: If the file is missing, the suffix is unsupported, the
            file is not valid Parquet, or it has no columns.
    """
    source = Path(source)
    if not source.is_file():
        msg = f"Parquet file not found: {source}"
        raise IngestError(msg)
    suffix = source.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        msg = (
            f"unsupported Parquet file {source.name!r} (suffix {suffix!r}); "
            "supported: .parquet"
        )
        raise IngestError(msg)
    try:
        parquet_file = pq.ParquetFile(source)
        try:
            rows = int(parquet_file.metadata.num_rows)
            column_names = [field.name for field in parquet_file.schema_arrow]
        finally:
            parquet_file.close()
    except (OSError, ValueError) as exc:
        # ArrowInvalid is a ValueError and ArrowIOError an OSError subclass.
        msg = f"not a valid Parquet file: {source} ({exc})"
        raise IngestError(msg) from exc
    if not column_names:
        msg = f"Parquet file has no columns: {source}"
        raise IngestError(msg)

    table = RegisteredTable(
        original_name=source.stem,
        parquet_path=source,
        rows=rows,
        column_names=column_names,
    )
    slug = clean_name(name or source.stem, max_length=cfg.ingest.max_name_length)
    entry = register_tables(
        source_name=slug,
        source_path=source,
        tables=[table],
        cfg=cfg,
    )
    logger.info(
        "registered %s (%d rows, %d columns) as %s",
        source,
        rows,
        len(column_names),
        entry.tables[0].name,
    )
    return entry
