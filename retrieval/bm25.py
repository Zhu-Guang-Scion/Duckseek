"""BM25 lexical retrieval over table-card markdown (milestone 2, T7).

The index is small (one document per table card) and cheap to build, so it is
kept in memory and rebuilt per process instead of being persisted.
"""

from __future__ import annotations

import jieba
from rank_bm25 import BM25Okapi

jieba.setLogLevel(60)  # Silence the "Building prefix dict" startup log line.


def _has_word_char(token: str) -> bool:
    """Return whether ``token`` contains at least one letter or digit.

    Pure whitespace and pure symbol fragments (spaces, ``_``, CJK punctuation)
    carry no lexical signal and are dropped by :func:`tokenize`.
    """
    return any(char.isalnum() for char in token)


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text for BM25.

    Lowercases the input, segments it with jieba, and drops pure
    whitespace/symbol tokens so both English words and Chinese words survive.

    Args:
        text: Raw text (card markdown, free-form query, ...).

    Returns:
        Lexical tokens; empty for symbol-only or empty input.
    """
    return [tok for tok in jieba.lcut(text.lower()) if _has_word_char(tok)]


class Bm25Index:
    """BM25 over card markdown texts (in-memory, rebuilt per process)."""

    def __init__(self, docs: list[tuple[str, str]]) -> None:
        """Build the index from ``(table_name, text)`` pairs.

        Args:
            docs: One entry per card. An empty list yields an empty index whose
                :meth:`search` always returns ``[]``.
        """
        self._names: list[str] = [name for name, _ in docs]
        self._bm25: BM25Okapi | None = (
            BM25Okapi([tokenize(text) for _, text in docs]) if docs else None
        )

    def search(self, query: str, top_n: int) -> list[tuple[str, int, float]]:
        """Return ``(table_name, rank, score)`` sorted by score descending.

        Args:
            query: Free-form query text.
            top_n: Maximum number of hits; non-positive yields ``[]``.

        Returns:
            Hits with 1-based ranks. Ties (including all-zero scores, e.g. an
            unmatched query) keep the original document order so results are
            stable. An empty index yields ``[]``.
        """
        if self._bm25 is None or top_n <= 0:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        return [
            (self._names[i], rank, float(scores[i]))
            for rank, i in enumerate(order[:top_n], start=1)
        ]
