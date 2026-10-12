"""Content fingerprints for ingestion lineage (JE feedback #3/#5).

Two levels:

- :func:`file_sha256` — SHA-256 of the source file bytes. Anchors the
  evidence chain ("this exact file was ingested"); recorded per source in
  catalog.yaml and enforced by the same-name re-registration gate.
- :func:`canonical_sha256_frame` / :func:`canonical_sha256_parquet` —
  a row-order-independent multiset hash of the TABLE content. Identical
  content in a different row order yields the same digest, so table-version
  comparisons do not misjudge reordered exports as changes. Implementation:
  per-row line digests are aggregated with a commutative sum (streaming,
  O(1) memory — the sorted-join formulation would blow up on million-row
  tables), then folded with the row count and sorted column names.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

_CHUNK = 1 << 20
_MOD = 1 << 64


def file_sha256(path: Path) -> str:
    """Stream the file through SHA-256 in 1 MiB chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cell(value: Any) -> str:
    """Canonical text for one cell: NULL/NaN labelled, floats stabilised."""
    if value is None:
        return "∅"
    if isinstance(value, float):
        if math.isnan(value):
            return "∅"
        if value.is_integer():
            return str(int(value))  # 5.0 == 5 for content comparison
        return repr(value)
    return str(value)


def _fold(rows: Iterable[str], columns: Iterable[str]) -> str:
    """Commutative multiset fold: order-independent, streaming."""
    aggregate = 0
    count = 0
    for line in rows:
        row_digest = hashlib.blake2b(line.encode("utf-8"), digest_size=8).digest()
        aggregate = (aggregate + int.from_bytes(row_digest, "big")) % _MOD
        count += 1
    header = f"rows={count}|cols={','.join(sorted(columns))}|"
    return hashlib.sha256(header.encode("utf-8") + str(aggregate).encode()).hexdigest()


def canonical_sha256_frame(frame: pd.DataFrame) -> str:
    """Row-order-independent digest of a DataFrame's content."""
    columns = [str(col) for col in frame.columns]

    def _lines() -> Iterable[str]:
        for row in frame.itertuples(index=False, name=None):
            yield "|".join(f"{col}={_cell(value)}" for col, value in zip(columns, row, strict=True))

    return _fold(_lines(), columns)


def canonical_sha256_parquet(path: Path) -> str:
    """Row-order-independent digest of a Parquet file (batch streaming)."""
    table = pq.read_table(Path(path))
    columns = list(table.column_names)

    def _lines() -> Iterable[str]:
        for batch in table.to_batches():
            for record in batch.to_pylist():
                yield "|".join(f"{col}={_cell(record.get(col))}" for col in columns)

    return _fold(_lines(), columns)
