"""LanceDB vector index over table-card texts (milestone 2, T7).

Card documents are loaded from ``cfg.paths.cards_md_dir`` and synced into a
single ``cards`` Lance table keyed by card id. Syncs are incremental (mtime
driven) and failure-atomic per red line: the whole changed set is embedded
before any write, so an embedding failure leaves the previous index untouched.

The embedding client is owned by :mod:`retrieval.embedding` (T7-A) and is only
imported for typing; callers inject the client, and embedder failures surface
as ``retrieval.embedding.EmbeddingUnavailableError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import lancedb
import pyarrow as pa
from lancedb.table import Table

from nl2data.config import Nl2DataConfig
from retrieval.bm25 import Bm25Index

if TYPE_CHECKING:
    from retrieval.embedding import EmbeddingClient

_TABLE_NAME = 'cards'


@dataclass(frozen=True)
class CardDoc:
    """One table card prepared for indexing.

    Attributes:
        id: Card markdown file stem (equals the table name).
        table: Table name.
        text: Full ``cards_md/<table>.md`` content; the card itself is the
            schema description, never data rows.
        mtime: Source file mtime; drives incremental re-embedding.
    """

    id: str
    table: str
    text: str
    mtime: float


def load_card_docs(cfg: Nl2DataConfig) -> list[CardDoc]:
    """Load every ``*.md`` card under ``cfg.paths.cards_md_dir``.

    Args:
        cfg: Active configuration.

    Returns:
        Cards sorted by id for deterministic ordering; ``[]`` when the
        directory does not exist.
    """
    cards_dir: Path = cfg.paths.cards_md_dir
    if not cards_dir.is_dir():
        return []
    docs: list[CardDoc] = []
    for path in sorted(cards_dir.glob('*.md')):
        if not path.is_file():
            continue
        docs.append(
            CardDoc(
                id=path.stem,
                table=path.stem,
                text=path.read_text(encoding='utf-8'),
                mtime=path.stat().st_mtime,
            )
        )
    return docs


def build_bm25(cfg: Nl2DataConfig) -> Bm25Index:
    """Build the in-memory BM25 index over all card texts.

    Args:
        cfg: Active configuration.

    Returns:
        A :class:`Bm25Index`; empty (never fails) when no cards exist.
    """
    docs = load_card_docs(cfg)
    return Bm25Index([(doc.table, doc.text) for doc in docs])


@dataclass(frozen=True)
class SyncReport:
    """Outcome of one :meth:`VectorIndex.sync` round.

    Attributes:
        embedded: Texts sent to the embedder in a single call.
        upserted: Rows written (new or updated).
        deleted: Rows removed because their card vanished.
        total: Rows in the table after the sync.
    """

    embedded: int
    upserted: int
    deleted: int
    total: int


def _in_predicate(ids: list[str]) -> str:
    """Build a SQL ``IN`` predicate, defensively escaping single quotes."""
    quoted = ', '.join("'" + card_id.replace("'", "''") + "'" for card_id in ids)
    return f'id IN ({quoted})'


class VectorIndex:
    """LanceDB-backed vector index over card texts."""

    def __init__(self, cfg: Nl2DataConfig) -> None:
        """Resolve location and dimensionality from config; nothing is created.

        Args:
            cfg: Active configuration (``paths.index_dir`` and
                ``retrieval.embedding_dimensions``).
        """
        self._dim = int(cfg.retrieval.embedding_dimensions)
        self._db_path = cfg.paths.index_dir / 'lance'

    def _schema(self) -> pa.Schema:
        """Arrow schema with a fixed-size float32 vector column."""
        return pa.schema(
            [
                ('id', pa.string()),
                ('mtime', pa.float64()),
                ('vector', pa.list_(pa.float32(), self._dim)),
            ]
        )

    def _open_table(self) -> Table | None:
        """Return the cards table, or ``None`` when the index does not exist.

        Never creates directories: read paths stay side-effect free.
        """
        if not self._db_path.exists():
            return None
        db = lancedb.connect(str(self._db_path))
        if _TABLE_NAME not in db.table_names():
            return None
        return db.open_table(_TABLE_NAME)

    def _existing_mtimes(self, table: Table | None) -> dict[str, float]:
        """Read the current id -> mtime mapping (empty when there is no table)."""
        if table is None:
            return {}
        arrow = table.to_arrow()
        return dict(
            zip(
                arrow.column('id').to_pylist(),
                arrow.column('mtime').to_pylist(),
                strict=True,
            )
        )

    def sync(self, docs: list[CardDoc], embedder: EmbeddingClient) -> SyncReport:
        """Incrementally sync the table to ``docs`` (embed -> upsert -> delete).

        Args:
            docs: Current card set; ids absent from here are deleted.
            embedder: Embedding client; the whole changed set is embedded in
                one call before any write.

        Returns:
            Counts of embedded texts, upserted rows, deleted rows and the
            resulting total row count. A missing table with no docs yields
            ``SyncReport(0, 0, 0, 0)`` without creating anything.

        Raises:
            retrieval.embedding.EmbeddingUnavailableError: Propagated from the
                embedder; the on-disk index is left untouched (no half update).
            ValueError: If the embedder returns a wrong-dimension vector.
        """
        table = self._open_table()
        if table is None and not docs:
            return SyncReport(embedded=0, upserted=0, deleted=0, total=0)
        existing = self._existing_mtimes(table)
        doc_ids = {doc.id for doc in docs}
        changed = [doc for doc in docs if existing.get(doc.id) != doc.mtime]
        removed = sorted(card_id for card_id in existing if card_id not in doc_ids)

        if changed:
            # Red line: embed everything first; a failure must abort here so
            # the previous index stays intact.
            vectors = embedder.embed_texts([doc.text for doc in changed])
            for vector in vectors:
                if len(vector) != self._dim:
                    msg = f'embedder returned {len(vector)}-dim vector, expected {self._dim}'
                    raise ValueError(msg)
            arrow = pa.Table.from_pylist(
                [
                    {'id': doc.id, 'mtime': doc.mtime, 'vector': vector}
                    for doc, vector in zip(changed, vectors, strict=True)
                ],
                schema=self._schema(),
            )
            if table is None:
                table = lancedb.connect(str(self._db_path)).create_table(
                    _TABLE_NAME, data=arrow, schema=self._schema(), mode='overwrite'
                )
            else:
                (
                    table.merge_insert('id')
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute(arrow)
                )
        if removed and table is not None:
            table.delete(_in_predicate(removed))
        total = table.count_rows() if table is not None else 0
        return SyncReport(
            embedded=len(changed), upserted=len(changed), deleted=len(removed), total=total
        )

    def search(self, query_vector: list[float], top_n: int) -> list[tuple[str, int, float]]:
        """Return ``(table, rank, distance)`` nearest neighbours.

        Args:
            query_vector: Query embedding whose dimension matches the index.
            top_n: Maximum number of hits; non-positive yields ``[]``.

        Returns:
            LanceDB distances verbatim (lower is better); ``[]`` when the
            table is missing or empty.
        """
        if top_n <= 0:
            return []
        table = self._open_table()
        if table is None or table.count_rows() == 0:
            return []
        rows = table.search(query_vector).limit(top_n).to_list()
        return [
            (str(row['id']), rank, float(row['_distance']))
            for rank, row in enumerate(rows, start=1)
        ]

    def available(self) -> bool:
        """Report whether the index exists and holds at least one row."""
        table = self._open_table()
        return table is not None and table.count_rows() > 0
