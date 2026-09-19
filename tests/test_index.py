"""Tests for card-doc loading and the LanceDB vector index (milestone 2, T7).

The embedder is a deterministic duck-typed fake (no network, no dependency on
``retrieval.embedding``): a text's vector is a bag-of-tokens one-hot sum, so a
query with the same tokens as a card collides with it exactly (distance 0).
"""

from __future__ import annotations

from dataclasses import replace
from zlib import crc32

import pytest

from nl2data.config import Nl2DataConfig
from retrieval.bm25 import tokenize
from retrieval.index import CardDoc, VectorIndex, load_card_docs

try:
    from retrieval.embedding import EmbeddingUnavailableError
except ImportError:  # pragma: no cover - T7-A may land this module in parallel
    class EmbeddingUnavailableError(RuntimeError):
        """Local stand-in mirroring ``retrieval.embedding``'s failure type."""


DIM = 1024
TAXI_TEXT = 'yellow taxi 出租车 车费'
WEATHER_TEXT = 'weather 温度 humidity'
POPULATION_TEXT = 'population 人口 census'


def _bucket(token: str) -> int:
    """Deterministic per-token bucket (stable across processes)."""
    return crc32(token.encode('utf-8')) % DIM


def embed_text(text: str) -> list[float]:
    """Deterministic fake embedding: identical token sets give identical vectors."""
    vector = [0.0] * DIM
    for token in tokenize(text):
        vector[_bucket(token)] += 1.0
    return vector


class FakeEmbedder:
    """Duck-typed embedding client with deterministic vectors and a tripwire."""

    def __init__(self) -> None:
        """Record every batch so tests can assert what was embedded."""
        self.batches: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Fail on poisoned texts; otherwise hash tokens into 1024-dim vectors."""
        self.batches.append(list(texts))
        if any('POISON' in text for text in texts):
            raise EmbeddingUnavailableError('embedding backend unavailable')
        return [embed_text(text) for text in texts]


def _write_card(cfg: Nl2DataConfig, name: str, text: str) -> None:
    """Write one card markdown file under the configured cards_md dir."""
    cards_md = cfg.paths.cards_md_dir
    cards_md.mkdir(parents=True, exist_ok=True)
    (cards_md / f'{name}.md').write_text(text, encoding='utf-8')


def _sample_docs() -> list[CardDoc]:
    """Three disjoint-topic cards for nearest-neighbour tests."""
    return [
        CardDoc(id='taxi', table='taxi', text=TAXI_TEXT, mtime=100.0),
        CardDoc(id='weather', table='weather', text=WEATHER_TEXT, mtime=200.0),
        CardDoc(id='population', table='population', text=POPULATION_TEXT, mtime=300.0),
    ]


def test_load_card_docs_reads_md_files(config: Nl2DataConfig) -> None:
    """Card files map to docs with id == table == stem and full text."""
    _write_card(config, '出租车表', f'# 出租车表\n{TAXI_TEXT}')
    _write_card(config, 'weather', f'# weather\n{WEATHER_TEXT}')
    docs = load_card_docs(config)
    assert [doc.id for doc in docs] == sorted(['出租车表', 'weather'])
    assert all(doc.table == doc.id for doc in docs)
    by_id = {doc.id: doc for doc in docs}
    assert by_id['weather'].text == f'# weather\n{WEATHER_TEXT}'
    assert by_id['出租车表'].text == f'# 出租车表\n{TAXI_TEXT}'
    assert all(doc.mtime > 0.0 for doc in docs)


def test_load_card_docs_missing_dir_returns_empty(config: Nl2DataConfig) -> None:
    """A missing cards_md directory is an empty corpus, not an error."""
    assert load_card_docs(config) == []
    assert not config.paths.cards_md_dir.exists()


def test_sync_first_run_creates_table_and_is_idempotent(config: Nl2DataConfig) -> None:
    """First sync writes every card; an unchanged re-sync writes nothing."""
    _write_card(config, 'taxi', TAXI_TEXT)
    _write_card(config, 'weather', WEATHER_TEXT)
    docs = load_card_docs(config)
    index = VectorIndex(config)
    assert not index.available()

    report = index.sync(docs, FakeEmbedder())
    assert (report.embedded, report.upserted, report.deleted, report.total) == (2, 2, 0, 2)
    assert index.available()

    again = index.sync(load_card_docs(config), FakeEmbedder())
    assert (again.embedded, again.upserted, again.deleted, again.total) == (0, 0, 0, 2)


def test_sync_reembeds_card_when_mtime_changes(config: Nl2DataConfig) -> None:
    """Rewriting a card (new mtime) embeds and upserts exactly that card."""
    _write_card(config, 'taxi', TAXI_TEXT)
    _write_card(config, 'weather', WEATHER_TEXT)
    index = VectorIndex(config)
    index.sync(load_card_docs(config), FakeEmbedder())

    _write_card(config, 'taxi', f'{TAXI_TEXT} ehail fee')  # touch: new content + mtime
    report = index.sync(load_card_docs(config), FakeEmbedder())
    assert (report.embedded, report.upserted, report.deleted, report.total) == (1, 1, 0, 2)


def test_sync_deletes_cards_removed_from_disk(config: Nl2DataConfig) -> None:
    """A card deleted on disk is removed from the index on the next sync."""
    _write_card(config, 'taxi', TAXI_TEXT)
    _write_card(config, 'weather', WEATHER_TEXT)
    index = VectorIndex(config)
    index.sync(load_card_docs(config), FakeEmbedder())

    (config.paths.cards_md_dir / 'weather.md').unlink()
    report = index.sync(load_card_docs(config), FakeEmbedder())
    assert (report.embedded, report.upserted, report.deleted, report.total) == (0, 0, 1, 1)


def test_sync_embedding_failure_leaves_index_untouched(config: Nl2DataConfig) -> None:
    """Red line: an embedder failure aborts before any write (no half update)."""
    _write_card(config, 'taxi', TAXI_TEXT)
    _write_card(config, 'weather', WEATHER_TEXT)
    index = VectorIndex(config)
    index.sync(load_card_docs(config), FakeEmbedder())
    good_vector = embed_text(TAXI_TEXT)

    _write_card(config, 'poisoned', 'POISON: embedder explodes here')
    embedder = FakeEmbedder()
    with pytest.raises(EmbeddingUnavailableError, match='backend unavailable'):
        index.sync(load_card_docs(config), embedder)

    # Only the changed card was ever sent to the embedder, and nothing changed.
    assert embedder.batches == [['POISON: embedder explodes here']]
    hits = index.search(good_vector, 10)
    assert len(hits) == 2  # old row count unchanged
    assert hits[0][0] == 'taxi'  # old vector still searchable
    assert hits[0][2] == pytest.approx(0.0, abs=1e-6)


def test_search_returns_nearest_neighbour_first(config: Nl2DataConfig) -> None:
    """The card sharing the query's tokens is rank 1 with (near) zero distance."""
    index = VectorIndex(config)
    index.sync(_sample_docs(), FakeEmbedder())

    query = embed_text(TAXI_TEXT)
    hits = index.search(query, 2)
    assert [hit[0] for hit in hits][0] == 'taxi'
    assert hits[0][1] == 1
    assert hits[0][2] == pytest.approx(0.0, abs=1e-6)
    assert len(hits) == 2
    assert hits[1][0] != 'taxi'
    assert hits[1][2] > hits[0][2]


def test_sync_missing_table_with_no_docs_creates_nothing(config: Nl2DataConfig) -> None:
    """Syncing an empty card set against a missing table is a no-op report."""
    report = VectorIndex(config).sync([], FakeEmbedder())
    assert (report.embedded, report.upserted, report.deleted, report.total) == (0, 0, 0, 0)
    assert not (config.paths.index_dir / 'lance').exists()


def test_search_missing_table_returns_empty(config: Nl2DataConfig) -> None:
    """Searching before any sync is empty and creates no directories."""
    index = VectorIndex(config)
    assert index.search([0.0] * DIM, 3) == []
    assert not config.paths.index_dir.exists()


def test_available_reflects_table_existence_and_rows(config: Nl2DataConfig) -> None:
    """available() is False before, True after, and False once drained again."""
    index = VectorIndex(config)
    assert index.available() is False

    _write_card(config, 'taxi', TAXI_TEXT)
    index.sync(load_card_docs(config), FakeEmbedder())
    assert index.available() is True

    (config.paths.cards_md_dir / 'taxi.md').unlink()
    index.sync(load_card_docs(config), FakeEmbedder())
    assert index.available() is False


def test_card_doc_is_frozen_and_replaceable(config: Nl2DataConfig) -> None:
    """CardDoc stays immutable; mtime bumps are expressed via ``replace``."""
    doc = _sample_docs()[0]
    with pytest.raises(AttributeError):
        doc.mtime = 999.0  # type: ignore[misc]
    bumped = replace(doc, mtime=doc.mtime + 1.0)
    assert bumped.mtime == doc.mtime + 1.0
    assert bumped.id == doc.id
