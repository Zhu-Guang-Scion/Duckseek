"""Tests for BM25 tokenization and lexical card search (milestone 2, T7)."""

from __future__ import annotations

from retrieval.bm25 import Bm25Index, tokenize


def test_tokenize_keeps_chinese_and_english_words() -> None:
    """Mixed CN/EN text keeps words on both sides and drops pure symbols.

    jieba splits ``ehail_fee`` on the underscore into ``ehail`` + ``fee``;
    the standalone ``_`` is a pure symbol and must not survive.
    """
    tokens = tokenize('黄色出租车 Yellow Taxi 的 ehail_fee。')
    assert '黄色' in tokens
    assert '出租车' in tokens
    assert 'yellow' in tokens
    assert 'taxi' in tokens
    assert '的' in tokens
    assert 'ehail' in tokens
    assert 'fee' in tokens
    assert '_' not in tokens
    assert '。' not in tokens
    assert ' ' not in tokens
    for token in tokens:
        assert token.strip() != ''
        assert any(char.isalnum() for char in token)


def test_tokenize_lowercases_and_drops_symbol_only_input() -> None:
    """Input is lowercased; whitespace/punctuation-only input yields no tokens."""
    assert tokenize('YELLOW Taxi!') == ['yellow', 'taxi']
    assert tokenize('。 _  ') == []
    assert tokenize('') == []
    assert tokenize('2024 年') == ['2024', '年']


def test_search_ranks_relevant_table_first() -> None:
    """The only table mentioning the query terms takes rank 1 with score > 0."""
    index = Bm25Index(
        [
            ('taxi_trips', 'yellow taxi ehail fee 车费 支付'),
            ('weather', 'weather 温度 humidity 湿度'),
            ('population', 'population 人口 census'),
        ]
    )
    hits = index.search('ehail fee', 3)
    assert len(hits) == 3
    assert hits[0][0] == 'taxi_trips'
    assert hits[0][1] == 1
    assert hits[0][2] > 0.0
    # Non-matching tables tie at zero and keep their original document order.
    assert [hit[0] for hit in hits[1:]] == ['weather', 'population']
    assert all(hit[2] == 0.0 for hit in hits[1:])


def test_search_empty_index_returns_empty() -> None:
    """An index built from no docs never matches."""
    assert Bm25Index([]).search('anything 出租车', 5) == []


def test_search_all_zero_scores_are_stable() -> None:
    """A fully unmatched query returns every doc in input order with ranks 1..n."""
    docs = [('a', 'foo bar'), ('b', 'baz qux'), ('c', 'quux corge')]
    hits = Bm25Index(docs).search('unmatched-token', 3)
    assert [hit[0] for hit in hits] == ['a', 'b', 'c']
    assert [hit[1] for hit in hits] == [1, 2, 3]
    assert all(hit[2] == 0.0 for hit in hits)


def test_search_respects_top_n_and_non_positive() -> None:
    """``top_n`` caps the result; non-positive limits return nothing.

    The query term appears in exactly one of three docs so its BM25 idf is
    positive (rank-bm25 goes negative when a term covers most of a tiny
    corpus, which would let zero-score docs outrank matches).
    """
    docs = [('a', 'foo foo foo alpha'), ('b', 'bar beta gamma'), ('c', 'bar delta')]
    index = Bm25Index(docs)
    hits = index.search('foo', 1)
    assert len(hits) == 1
    assert hits[0][0] == 'a'
    assert hits[0][2] > 0.0
    all_hits = index.search('foo', 3)
    assert [hit[0] for hit in all_hits] == ['a', 'b', 'c']
    assert index.search('foo', 0) == []
