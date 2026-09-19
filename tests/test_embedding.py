"""Tests for the OpenAI-compatible embedding client and vector disk cache.

All tests are offline: the ``openai.OpenAI`` constructor is monkeypatched to
return an in-process stub, and ``time.sleep`` is patched where retries run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest

from nl2data.config import Nl2DataConfig, RetrievalConfig
from retrieval import embedding
from retrieval.embedding import (
    DiskCache,
    EmbeddingClient,
    EmbeddingUnavailableError,
    client_from_env,
)

_DIM = 1024
_REQUEST = "https://embedding.invalid/v1/embeddings"


def _unit_vector(text: str, dimension: int = _DIM) -> list[float]:
    """Deterministic one-hot unit-norm vector standing in for ``text``."""
    vector = [0.0] * dimension
    vector[sum(text.encode("utf-8")) % dimension] = 1.0
    return vector


def _status_error(status_code: int) -> openai.APIStatusError:
    """Build a real ``APIStatusError`` carrying ``status_code``."""
    response = httpx2.Response(
        status_code,
        headers={"x-request-id": "test"},
        request=httpx2.Request("POST", _REQUEST),
    )
    return openai.APIStatusError(f"HTTP {status_code}", response=response, body=None)


def _connection_error() -> openai.APIConnectionError:
    """Build a real connection-level error."""
    return openai.APIConnectionError(request=httpx2.Request("POST", _REQUEST))


def _timeout_error() -> openai.APITimeoutError:
    """Build a real timeout error (a subclass of ``APIConnectionError``)."""
    return openai.APITimeoutError(request=httpx2.Request("POST", _REQUEST))


@dataclass
class _FakeItem:
    """Stand-in for ``openai.types.Embedding``."""

    index: int
    embedding: list[float]


@dataclass
class _FakeResponse:
    """Stand-in for ``CreateEmbeddingResponse``."""

    data: list[_FakeItem]


class _StubEmbeddings:
    """Stub ``client.embeddings`` recording calls and scripting failures."""

    def __init__(self, dimension: int = _DIM) -> None:
        self.calls: list[list[str]] = []
        self.failures: list[Exception] = []
        self.dimension = dimension
        self.fixed: list[float] | None = None

    def create(self, model: str, input: list[str]) -> _FakeResponse:
        self.calls.append(list(input))
        if self.failures:
            failure = self.failures.pop(0)
            raise failure
        items = [
            _FakeItem(
                index=i,
                embedding=list(self.fixed)
                if self.fixed
                else _unit_vector(text, self.dimension),
            )
            for i, text in enumerate(input)
        ]
        return _FakeResponse(data=items)


class _StubClient:
    """Minimal ``openai.OpenAI`` stand-in recording constructor kwargs."""

    def __init__(self) -> None:
        self.embeddings = _StubEmbeddings()
        self.init_kwargs: dict[str, Any] = {}


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub: _StubClient) -> None:
    """Patch ``openai.OpenAI`` so :class:`EmbeddingClient` receives ``stub``."""

    def factory(**kwargs: Any) -> _StubClient:
        stub.init_kwargs = kwargs
        return stub

    monkeypatch.setattr(embedding.openai, "OpenAI", factory)


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubClient,
    cache_dir: Path,
    *,
    batch_size: int = 32,
    max_retries: int = 5,
) -> EmbeddingClient:
    """Create an :class:`EmbeddingClient` wired to ``stub`` and ``cache_dir``."""
    _install_stub(monkeypatch, stub)
    return EmbeddingClient(
        "https://embedding.invalid/v1",
        "sk-test",
        "emb-model",
        batch_size=batch_size,
        max_retries=max_retries,
        cache_dir=cache_dir,
    )


def _capture_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch ``time.sleep`` and return the list of requested delays."""
    delays: list[float] = []
    monkeypatch.setattr(embedding.time, "sleep", delays.append)
    return delays


# ---------------------------------------------------------------------------
# DiskCache
# ---------------------------------------------------------------------------


def test_disk_cache_roundtrip_creates_directories(tmp_path: Path) -> None:
    """put creates the cache and bucket dirs; get returns the same vector."""
    cache_dir = tmp_path / "nested" / "cache"
    cache = DiskCache(cache_dir, "emb-model")
    vector = [0.25, -0.5, 0.75]
    cache.put("some text", vector)
    assert cache.get("some text") == pytest.approx(vector)


def test_disk_cache_key_separates_models_and_texts(tmp_path: Path) -> None:
    """The same text under a different model (or text) is a different key."""
    cache_a = DiskCache(tmp_path, "model-a")
    cache_b = DiskCache(tmp_path, "model-b")
    cache_a.put("shared text", [1.0, 0.0])
    assert cache_a.get("shared text") == pytest.approx([1.0, 0.0])
    assert cache_b.get("shared text") is None
    assert cache_a.get("other text") is None


def test_disk_cache_tolerates_corrupted_json(tmp_path: Path) -> None:
    """A corrupted cache file is treated as a miss, not an error."""
    cache = DiskCache(tmp_path, "emb-model")
    cache.put("text", [1.0, 2.0])
    corrupted = next(tmp_path.rglob("*.json"))
    corrupted.write_text("{not json", encoding="utf-8")
    assert cache.get("text") is None


def test_disk_cache_missing_directory_is_all_misses(tmp_path: Path) -> None:
    """A non-existent cache dir yields ``None`` without raising."""
    cache = DiskCache(tmp_path / "does-not-exist", "emb-model")
    assert cache.get("anything") is None


# ---------------------------------------------------------------------------
# EmbeddingClient.embed_texts
# ---------------------------------------------------------------------------


def test_embed_texts_single_batch_returns_order_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One batch: vectors come back in input order and land in the cache."""
    stub = _StubClient()
    client = _make_client(monkeypatch, stub, tmp_path)
    texts = ["delta", "alpha", "charlie"]
    result = client.embed_texts(texts)
    assert result == [_unit_vector(t) for t in texts]
    assert stub.embeddings.calls == [texts]
    assert stub.init_kwargs["base_url"] == "https://embedding.invalid/v1"
    assert stub.init_kwargs["api_key"] == "sk-test"
    assert stub.init_kwargs["timeout"] == 30
    cache = DiskCache(tmp_path, "emb-model")
    assert all(cache.get(t) == pytest.approx(_unit_vector(t)) for t in texts)


def test_embed_texts_splits_batches_over_batch_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More texts than ``batch_size`` are sent as several ordered batches."""
    stub = _StubClient()
    client = _make_client(monkeypatch, stub, tmp_path, batch_size=2)
    texts = ["a", "b", "c", "d", "e"]
    result = client.embed_texts(texts)
    assert stub.embeddings.calls == [["a", "b"], ["c", "d"], ["e"]]
    assert result == [_unit_vector(t) for t in texts]


def test_embed_texts_partial_cache_hit_skips_cached_texts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached texts are not re-sent; results still follow the input order."""
    DiskCache(tmp_path, "emb-model").put("cached", _unit_vector("cached"))
    stub = _StubClient()
    client = _make_client(monkeypatch, stub, tmp_path)
    result = client.embed_texts(["cached", "fresh"])
    assert stub.embeddings.calls == [["fresh"]]
    assert result[0] == pytest.approx(_unit_vector("cached"))
    assert result[1] == pytest.approx(_unit_vector("fresh"))


def test_embed_texts_retries_429_with_backoff_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two 429 responses are retried with growing backoff, then succeed."""
    delays = _capture_sleep(monkeypatch)
    stub = _StubClient()
    stub.embeddings.failures = [_status_error(429), _status_error(429)]
    client = _make_client(monkeypatch, stub, tmp_path)
    result = client.embed_texts(["only"])
    assert result == [_unit_vector("only")]
    assert len(stub.embeddings.calls) == 3
    assert len(delays) == 2
    assert 1.0 <= delays[0] < 2.0 <= delays[1] < 3.0


def test_embed_texts_401_fails_immediately_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-retryable 401 raises at once (single API call)."""
    stub = _StubClient()
    stub.embeddings.failures = [_status_error(401)]
    client = _make_client(monkeypatch, stub, tmp_path)
    with pytest.raises(EmbeddingUnavailableError, match="401"):
        client.embed_texts(["x"])
    assert len(stub.embeddings.calls) == 1


def test_embed_texts_retry_exhaustion_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every attempt fails the client raises after the final retry."""
    _capture_sleep(monkeypatch)
    stub = _StubClient()
    stub.embeddings.failures = [
        _status_error(429),
        _connection_error(),
        _timeout_error(),
    ]
    client = _make_client(monkeypatch, stub, tmp_path, max_retries=2)
    with pytest.raises(EmbeddingUnavailableError, match="3 attempts"):
        client.embed_texts(["x"])
    assert len(stub.embeddings.calls) == 3


def test_embed_texts_empty_input_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty input short-circuits without any API call."""
    stub = _StubClient()
    client = _make_client(monkeypatch, stub, tmp_path)
    assert client.embed_texts([]) == []
    assert stub.embeddings.calls == []


def test_embed_texts_wrong_dimension_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vectors that are not 1024-dimensional fail and are not cached."""
    stub = _StubClient()
    stub.embeddings.dimension = 8
    client = _make_client(monkeypatch, stub, tmp_path)
    with pytest.raises(EmbeddingUnavailableError, match="dimension"):
        client.embed_texts(["x"])
    assert DiskCache(tmp_path, "emb-model").get("x") is None


def test_embed_texts_renormalizes_off_norm_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vectors whose norm is off by more than 1e-3 are re-normalized."""
    stub = _StubClient()
    stub.embeddings.fixed = [3.0] + [0.0] * (_DIM - 1)  # norm 5
    client = _make_client(monkeypatch, stub, tmp_path)
    result = client.embed_texts(["scaled"])
    assert result[0][0] == pytest.approx(1.0)
    assert math.sqrt(sum(component**2 for component in result[0])) == pytest.approx(1.0)
    assert DiskCache(tmp_path, "emb-model").get("scaled") == pytest.approx(result[0])


def test_embed_texts_zero_norm_vector_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An all-zero vector cannot be normalized and fails the request."""
    stub = _StubClient()
    stub.embeddings.fixed = [0.0] * _DIM
    client = _make_client(monkeypatch, stub, tmp_path)
    with pytest.raises(EmbeddingUnavailableError, match="zero norm"):
        client.embed_texts(["empty"])


# ---------------------------------------------------------------------------
# client_from_env
# ---------------------------------------------------------------------------


def test_client_from_env_builds_client_from_all_variables(
    monkeypatch: pytest.MonkeyPatch, config: Nl2DataConfig
) -> None:
    """All three env vars set: the client uses them plus cfg.retrieval values."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    monkeypatch.setenv("EMB_BASE_URL", "https://env.invalid/v1")
    monkeypatch.setenv("EMB_API_KEY", "sk-env")
    monkeypatch.setenv("EMB_MODEL", "emb-env")
    retrieval = RetrievalConfig(
        embedding_batch_size=7,
        embedding_timeout_seconds=11,
        embedding_max_retries=3,
    )
    client = client_from_env(replace(config, retrieval=retrieval))
    assert client is not None
    assert stub.init_kwargs == {
        "base_url": "https://env.invalid/v1",
        "api_key": "sk-env",
        "timeout": 11,
    }
    assert client._model == "emb-env"
    assert client._batch_size == 7
    assert client._max_retries == 3
    assert client._cache._cache_dir == config.paths.index_dir / "embedding_cache"


def test_client_from_env_missing_or_empty_key_returns_none(
    monkeypatch: pytest.MonkeyPatch, config: Nl2DataConfig
) -> None:
    """Missing or empty ``EMB_API_KEY`` yields ``None`` without raising."""
    for var in ("EMB_BASE_URL", "EMB_API_KEY", "EMB_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EMB_BASE_URL", "https://env.invalid/v1")
    monkeypatch.setenv("EMB_MODEL", "emb-env")
    assert client_from_env(config) is None
    monkeypatch.setenv("EMB_API_KEY", "   ")
    assert client_from_env(config) is None
