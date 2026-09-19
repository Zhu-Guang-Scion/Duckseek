"""Query → schema hybrid retrieval (milestone 2, T7).

Channels: BM25 over card markdown (always available) plus an optional
LanceDB vector channel (OpenAI-compatible embeddings, goals.md decision 8).
Fusion is Reciprocal Rank Fusion; glossary term hits force their tables to
the head of the candidate list; the prompt block is packed greedily under
the token budget.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from catalog.glossary import GlossaryStore, load_glossary
from nl2data.config import Nl2DataConfig
from retrieval.embedding import EmbeddingUnavailableError, client_from_env
from retrieval.index import VectorIndex, build_bm25
from retrieval.tokens import estimate_tokens

logger = logging.getLogger(__name__)

_TERM_CHANNEL_HINT = "术语直通"


class RetrievalError(RuntimeError):
    """Raised when retrieval cannot run (e.g. no cards have been built)."""


@dataclass(frozen=True)
class RetrievedItem:
    """One candidate table with its channel evidence."""

    table: str
    score: float
    vector_rank: int | None
    bm25_rank: int | None
    via_term: str | None
    card_json: dict[str, Any]


@dataclass(frozen=True)
class RetrievalResult:
    """The retrieval outcome consumed by milestone 3."""

    items: list[RetrievedItem] = field(default_factory=list)
    prompt_block: str = ""
    total_tokens: int = 0
    dropped_tables: list[dict[str, Any]] = field(default_factory=list)
    channels_used: list[str] = field(default_factory=list)


def _load_cards(cfg: Nl2DataConfig) -> dict[str, dict[str, Any]]:
    """Load card JSON files keyed by table name; missing dir means none."""
    cards: dict[str, dict[str, Any]] = {}
    if cfg.paths.cards_dir.is_dir():
        for path in sorted(cfg.paths.cards_dir.glob("*.json")):
            try:
                card: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("corrupt card %s skipped: %s", path, exc)
                continue
            cards[path.stem] = card
    return cards


def _match_terms(query: str, store: GlossaryStore) -> list[tuple[str, str]]:
    """Return (table, term) pairs whose term or a synonym appears in query."""
    hits: list[tuple[str, str]] = []
    lowered = query.casefold()
    for entry in store.entries:
        names = [entry.term, *entry.synonyms]
        if any(name.casefold() in lowered for name in names):
            hits.append((entry.maps_to.table, entry.term))
    return hits


def _rrf_scores(
    vector: list[tuple[str, int, float]],
    bm25: list[tuple[str, int, float]],
    *,
    rrf_k: int,
    weight_vector: float,
    weight_bm25: float,
) -> dict[str, float]:
    """Reciprocal Rank Fusion over the two ranked channel results."""
    scores: dict[str, float] = {}
    for table, rank, _score in vector:
        scores[table] = scores.get(table, 0.0) + weight_vector / (rrf_k + rank)
    for table, rank, _score in bm25:
        scores[table] = scores.get(table, 0.0) + weight_bm25 / (rrf_k + rank)
    return scores


def _pack_prompt(
    ordered: list[RetrievedItem],
    card_texts: dict[str, str],
    budget: int,
    cfg: Nl2DataConfig,
) -> tuple[str, list[RetrievedItem], list[dict[str, Any]]]:
    """Greedily pack card markdown into the budget.

    The first card is always included so the prompt is never empty; later
    cards that would overflow the budget are dropped with a reason.
    """
    included: list[RetrievedItem] = []
    dropped: list[dict[str, Any]] = []
    total = 0
    for item in ordered:
        text = card_texts.get(item.table, "")
        cost = estimate_tokens(text, cfg.tokens)
        if included and total + cost > budget:
            dropped.append(
                {
                    "table": item.table,
                    "reason": f"token budget ({total}+{cost} > {budget})",
                }
            )
            continue
        included.append(item)
        total += cost
    prompt = "\n\n---\n\n".join(card_texts.get(i.table, "") for i in included)
    return prompt, included, dropped


def retrieve(
    query: str,
    cfg: Nl2DataConfig,
    *,
    k: int | None = None,
    token_budget: int | None = None,
    vector_index: VectorIndex | None = None,
    embedder: Any | None = None,
    glossary: GlossaryStore | None = None,
) -> RetrievalResult:
    """Hybrid-retrieve the Top-K relevant table cards for ``query``.

    Args:
        query: Natural-language question.
        cfg: Active configuration (defaults for k and token budget).
        k: Override for the number of tables to return.
        token_budget: Override for the prompt token budget.
        vector_index: Pre-built vector index (skips LanceDB open).
        embedder: Pre-built embedding client (defaults to env-based).
        glossary: Pre-loaded glossary store (defaults to the configured file;
            structural load only, reference validation stays with
            ``nl2data glossary check``).

    Returns:
        The retrieval result with packed prompt block.

    Raises:
        RetrievalError: When no cards exist (user must ingest/profile/build).
    """
    top_k = k if k is not None else cfg.retrieval.top_k
    budget = token_budget if token_budget is not None else cfg.retrieval.token_budget

    cards = _load_cards(cfg)
    if not cards:
        msg = (
            "no table cards found; run `nl2data ingest ...`, "
            "`nl2data profile --all` and `nl2data cards build --all` first"
        )
        raise RetrievalError(msg)
    card_texts = {name: _card_markdown(cfg, name) for name in cards}
    bm25 = build_bm25(cfg)
    bm25_hits = bm25.search(query, max(top_k * 4, 20))

    channels: list[str] = ["bm25"]
    vector_hits: list[tuple[str, int, float]] = []
    client = embedder if embedder is not None else client_from_env(cfg)
    if client is not None:
        index = vector_index if vector_index is not None else VectorIndex(cfg)
        try:
            query_vector = client.embed_texts([query])[0]
            vector_hits = index.search(query_vector, max(top_k * 4, 20))
            if vector_hits:
                channels.append("vector")
        except EmbeddingUnavailableError as exc:
            logger.warning("vector channel unavailable, BM25 only: %s", exc)
    else:
        logger.warning("EMB_* env not configured; BM25-only retrieval")

    vector_rank = {table: rank for table, rank, _ in vector_hits}
    bm25_rank = {table: rank for table, rank, _ in bm25_hits}
    scores = _rrf_scores(
        vector_hits,
        bm25_hits,
        rrf_k=cfg.retrieval.rrf_k,
        weight_vector=cfg.retrieval.weight_vector,
        weight_bm25=cfg.retrieval.weight_bm25,
    )

    if glossary is None:
        glossary = load_glossary(cfg.paths.glossary, cfg)
    term_hits = _match_terms(query, glossary)
    term_tables = {table: term for table, term in term_hits}

    ordered_names = sorted(scores, key=lambda t: scores[t], reverse=True)
    term_first = [t for t in ordered_names if t in term_tables]
    rest = [t for t in ordered_names if t not in term_tables]
    ordered = (term_first + rest)[: max(top_k, len(term_first))]

    items = [
        RetrievedItem(
            table=table,
            score=round(scores.get(table, 0.0), 6),
            vector_rank=vector_rank.get(table),
            bm25_rank=bm25_rank.get(table),
            via_term=term_tables.get(table),
            card_json=cards[table],
        )
        for table in ordered
        if table in cards
    ]

    prompt, included, dropped = _pack_prompt(items, card_texts, budget, cfg)
    return RetrievalResult(
        items=included,
        prompt_block=prompt,
        total_tokens=estimate_tokens(prompt, cfg.tokens),
        dropped_tables=dropped,
        channels_used=channels,
    )


def _card_markdown(cfg: Nl2DataConfig, table: str) -> str:
    """Read the human-readable markdown card for ``table``."""
    path = cfg.paths.cards_md_dir / f"{table}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("markdown card missing for %s; falling back to json", table)
        return json.dumps({}, ensure_ascii=False)
