"""OpenAI-compatible embedding client with retry, validation, and disk cache (T7).

Endpoint and credentials come from the environment (``EMB_BASE_URL`` /
``EMB_API_KEY`` / ``EMB_MODEL``, goals.md decision 8); the factory
:func:`client_from_env` returns ``None`` when they are missing so callers can
warn and degrade to keyword-only retrieval instead of crashing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from pathlib import Path

import openai
from openai.types import CreateEmbeddingResponse

from nl2data.config import Nl2DataConfig

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
EXPECTED_DIMENSIONS: int = 1024
_NORM_TOLERANCE: float = 1e-3
_BACKOFF_BASE_SECONDS: float = 1.0
_JITTER_MAX_SECONDS: float = 1.0

ENV_BASE_URL = "EMB_BASE_URL"
ENV_API_KEY = "EMB_API_KEY"
ENV_MODEL = "EMB_MODEL"


class EmbeddingUnavailableError(RuntimeError):
    """Embedding API unreachable/failed after retries."""


def _cache_key(model: str, text: str) -> str:
    """Return the sha256 hex digest key for ``text`` under ``model``."""
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


def _normalized(vector: list[float]) -> list[float]:
    """Return ``vector`` re-scaled to unit norm when it is not already one."""
    norm = math.sqrt(sum(component * component for component in vector))
    if abs(norm - 1.0) <= _NORM_TOLERANCE:
        return vector
    if norm == 0.0:
        msg = "embedding vector has zero norm and cannot be normalized"
        raise EmbeddingUnavailableError(msg)
    return [component / norm for component in vector]


def _default_cache_dir() -> Path:
    """Return the fallback cache location when no explicit directory is given."""
    return Path.home() / ".cache" / "nl2data" / "embeddings"


class DiskCache:
    """Vector disk cache; key = sha256(model + '\n' + text)."""

    def __init__(self, cache_dir: Path, model: str) -> None:
        self._cache_dir = Path(cache_dir)
        self._model = model

    def _path(self, text: str) -> Path:
        """Return the JSON file path bucketed by the first two key characters."""
        digest = _cache_key(self._model, text)
        return self._cache_dir / digest[:2] / f"{digest}.json"

    def get(self, text: str) -> list[float] | None:
        """Return the cached vector for ``text``; ``None`` on miss or corruption."""
        try:
            raw = self._path(text).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        if not all(
            isinstance(item, int | float) and not isinstance(item, bool) for item in data
        ):
            return None
        return [float(item) for item in data]

    def put(self, text: str, vector: list[float]) -> None:
        """Persist ``vector`` for ``text``, creating the bucket directory."""
        path = self._path(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(vector), encoding="utf-8")


class EmbeddingClient:
    """Batched OpenAI-compatible embedding client with retry + cache."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        batch_size: int = 32,
        timeout_seconds: int = 30,
        max_retries: int = 5,
        cache_dir: Path | None = None,
    ) -> None:
        self._model = model
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._client = openai.OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_seconds
        )
        resolved_cache_dir = cache_dir if cache_dir is not None else _default_cache_dir()
        self._cache = DiskCache(resolved_cache_dir, model)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` and return vectors in input order.

        Cached texts never hit the API; freshly computed vectors are written
        to the disk cache only after every batch has succeeded.

        Raises:
            EmbeddingUnavailableError: On non-retryable API errors, when
                retries are exhausted, or on invalid (dimension/norm) vectors.
        """
        if not texts:
            return []
        cached: list[list[float] | None] = [self._cache.get(text) for text in texts]
        missing = list(
            dict.fromkeys(
                text for text, hit in zip(texts, cached, strict=True) if hit is None
            )
        )
        computed: dict[str, list[float]] = {}
        for start in range(0, len(missing), self._batch_size):
            batch = missing[start : start + self._batch_size]
            computed.update(self._embed_batch(batch))
        for text, vector in computed.items():
            self._cache.put(text, vector)
        results: list[list[float]] = []
        for text, hit in zip(texts, cached, strict=True):
            results.append(hit if hit is not None else computed[text])
        return results

    def _embed_batch(self, batch: list[str]) -> dict[str, list[float]]:
        """Call the API once for ``batch`` and return validated vectors."""
        response = self._create_with_retry(batch)
        items = sorted(response.data, key=lambda item: item.index)
        if len(items) != len(batch):
            msg = f"embedding API returned {len(items)} vectors for {len(batch)} inputs"
            raise EmbeddingUnavailableError(msg)
        vectors: dict[str, list[float]] = {}
        for text, item in zip(batch, items, strict=True):
            vector = [float(component) for component in item.embedding]
            if len(vector) != EXPECTED_DIMENSIONS:
                msg = (
                    f"embedding dimension mismatch: got {len(vector)}, "
                    f"expected {EXPECTED_DIMENSIONS}"
                )
                raise EmbeddingUnavailableError(msg)
            vectors[text] = _normalized(vector)
        return vectors

    def _create_with_retry(self, batch: list[str]) -> CreateEmbeddingResponse:
        """Call ``embeddings.create`` with exponential backoff on transient errors."""
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._client.embeddings.create(
                    model=self._model, input=list(batch)
                )
            except openai.APIStatusError as exc:
                if exc.status_code not in RETRYABLE_STATUS_CODES:
                    raise EmbeddingUnavailableError(str(exc)) from exc
                last_error = exc
            except openai.APIConnectionError as exc:  # APITimeoutError subclasses this.
                last_error = exc
            if attempt < self._max_retries:
                backoff = _BACKOFF_BASE_SECONDS * (2**attempt) + random.uniform(
                    0.0, _JITTER_MAX_SECONDS
                )
                time.sleep(backoff)
        msg = f"embedding request failed after {self._max_retries + 1} attempts: {last_error}"
        raise EmbeddingUnavailableError(msg) from last_error


def client_from_env(cfg: Nl2DataConfig) -> EmbeddingClient | None:
    """Build an :class:`EmbeddingClient` from ``EMB_*`` environment variables.

    Returns ``None`` (never raises) when any of ``EMB_BASE_URL`` /
    ``EMB_API_KEY`` / ``EMB_MODEL`` is missing or empty; the caller is
    responsible for warning and degrading to keyword-only retrieval.
    Batch, timeout, and retry settings come from ``cfg.retrieval``.
    """
    base_url = os.environ.get(ENV_BASE_URL, "").strip()
    api_key = os.environ.get(ENV_API_KEY, "").strip()
    model = os.environ.get(ENV_MODEL, "").strip()
    if not (base_url and api_key and model):
        return None
    return EmbeddingClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        batch_size=cfg.retrieval.embedding_batch_size,
        timeout_seconds=cfg.retrieval.embedding_timeout_seconds,
        max_retries=cfg.retrieval.embedding_max_retries,
        cache_dir=cfg.paths.index_dir / "embedding_cache",
    )
