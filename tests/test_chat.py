"""Offline tests for the LLM chat client (T8).

``openai.OpenAI`` is monkeypatched to return an in-process stub; no test
performs real network I/O and ``time.sleep`` is patched where retries run.
A dedicated test asserts the API key never leaks into exception messages,
audit events, or logs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import httpx2
import openai
import pytest

from llm import chat as chat_module
from llm.chat import (
    ChatResult,
    LlmError,
    Usage,
    chat,
    recent_events,
    reset_events,
    usage_totals,
)
from nl2data.config import Nl2DataConfig

_REQUEST_URL = "https://llm.invalid/v1/chat/completions"
_KEY = "sk-TEST-secret-value"
_MESSAGES: list[dict[str, str]] = [
    {"role": "user", "content": "List the tables in this workbook"}
]
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}
_EVENT_KEYS = {
    "ts",
    "model",
    "usage",
    "latency_ms",
    "prompt_preview",
    "category",
    "retries",
}



@dataclass
class _FakeUsage:
    """Stand-in for the SDK usage object."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class _FakeMessage:
    """Stand-in for the SDK assistant message."""

    content: str


@dataclass
class _FakeChoice:
    """Stand-in for the SDK choice object."""

    message: _FakeMessage


@dataclass
class _FakeResponse:
    """Stand-in for ``CreateChatCompletion``."""

    choices: list[_FakeChoice]
    usage: _FakeUsage | None = None


def _ok_response(content: str, *, prompt: int = 11, completion: int = 7) -> _FakeResponse:
    """Build a successful scripted response with fixed token usage."""
    return _FakeResponse(
        choices=[_FakeChoice(message=_FakeMessage(content=content))],
        usage=_FakeUsage(prompt, completion, prompt + completion),
    )


def _status_error(status_code: int, message: str | None = None) -> openai.APIStatusError:
    """Build a real ``APIStatusError`` carrying ``status_code``."""
    text = message if message is not None else f"HTTP {status_code}"
    response = httpx2.Response(
        status_code,
        headers={"x-request-id": "test"},
        request=httpx2.Request("POST", _REQUEST_URL),
    )
    return openai.APIStatusError(text, response=response, body=None)


def _timeout_error() -> openai.APITimeoutError:
    """Build a real timeout error."""
    return openai.APITimeoutError(request=httpx2.Request("POST", _REQUEST_URL))


def _connection_error() -> openai.APIConnectionError:
    """Build a real connection-level error."""
    return openai.APIConnectionError(request=httpx2.Request("POST", _REQUEST_URL))


class _StubCompletions:
    """Stub ``client.chat.completions`` recording calls and scripting outcomes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[Any] = []

    def create(self, **kwargs: Any) -> Any:
        """Record the call and raise/return the next scripted outcome."""
        if not self.script:
            msg = "unexpected extra API call"
            raise AssertionError(msg)
        self.calls.append(kwargs)
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _StubChat:
    """Namespace holder so the stub mirrors ``client.chat.completions``."""

    def __init__(self) -> None:
        self.completions = _StubCompletions()


class _StubClient:
    """Minimal ``openai.OpenAI`` stand-in recording constructor kwargs."""

    def __init__(self) -> None:
        self.chat = _StubChat()
        self.init_kwargs: dict[str, Any] = {}
        self.constructions = 0


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub: _StubClient) -> None:
    """Patch ``openai.OpenAI`` so ``chat`` receives ``stub``."""

    def factory(**kwargs: Any) -> _StubClient:
        stub.constructions += 1
        stub.init_kwargs = dict(kwargs)
        return stub

    monkeypatch.setattr(chat_module.openai, "OpenAI", factory)


def _capture_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace ``time.sleep`` with a recorder and return the delay list."""
    delays: list[float] = []
    monkeypatch.setattr(chat_module.time, "sleep", delays.append)
    return delays
def _json_default(obj: Any) -> Any:
    """JSON serializer hook for frozen dataclasses stored in audit events."""
    return getattr(obj, "__dict__", str(obj))


@pytest.fixture(autouse=True)
def _clean_audit() -> Iterator[None]:
    """Isolate the module-level audit state around each test."""
    reset_events()
    yield
    reset_events()


@pytest.fixture()
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ``LLM_*`` environment variables at offline test values."""
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", _KEY)
    monkeypatch.setenv("LLM_MODEL", "glm-test")


def _with_retries(cfg: Nl2DataConfig, max_retries: int) -> Nl2DataConfig:
    """Return ``cfg`` with a different ``llm.max_retries`` budget."""
    return replace(cfg, llm=replace(cfg.llm, max_retries=max_retries))


def test_success_basic(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain call returns content, usage, model, latency, parsed=None."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_ok_response("hello world")]
    result = chat(_MESSAGES, cfg=config)
    assert isinstance(result, ChatResult)
    assert result.content == "hello world"
    assert result.parsed is None
    assert result.usage == Usage(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    assert result.model == "glm-test"
    assert result.latency_ms > 0
    assert stub.constructions == 1
    assert stub.init_kwargs["base_url"] == "https://llm.invalid/v1"
    assert stub.init_kwargs["api_key"] == _KEY
    assert stub.init_kwargs["timeout"] == config.llm.timeout_seconds
    call = stub.chat.completions.calls[0]
    assert call["model"] == "glm-test"
    assert call["messages"] == _MESSAGES
    assert call["temperature"] == config.llm.temperature
    assert call["max_tokens"] == config.llm.max_tokens
    assert "response_format" not in call


def test_json_object_response_format_and_fenced_parse(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """json_schema requests json_object mode and parses a fenced JSON reply."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_ok_response('```json\n{"answer": "42"}\n```')]
    result = chat(_MESSAGES, json_schema=_SCHEMA, cfg=config)
    call = stub.chat.completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert result.parsed == {"answer": "42"}
    assert result.content == '```json\n{"answer": "42"}\n```'


def test_invalid_json_with_schema_returns_parsed_none(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unparseable content keeps the raw text and yields parsed=None."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    raw = "抱歉,我无法给出 JSON。"
    stub.chat.completions.script = [_ok_response(raw)]
    result = chat(_MESSAGES, json_schema=_SCHEMA, cfg=config)
    assert result.parsed is None
    assert result.content == raw


def test_non_dict_json_parsed_none(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid JSON that is not an object also yields parsed=None."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_ok_response("```json\n[1, 2, 3]\n```")]
    result = chat(_MESSAGES, json_schema=_SCHEMA, cfg=config)
    assert result.parsed is None
    assert result.content == "```json\n[1, 2, 3]\n```"


def test_retry_on_429_then_succeeds(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two 429s then success: three calls, two escalating sleeps, retries=2."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    delays = _capture_sleep(monkeypatch)
    stub.chat.completions.script = [
        _status_error(429),
        _status_error(429),
        _ok_response("finally"),
    ]
    result = chat(_MESSAGES, cfg=config)
    assert result.content == "finally"
    assert len(stub.chat.completions.calls) == 3
    assert len(delays) == 2
    assert delays[0] < delays[1]
    event = recent_events()[0]
    assert event["category"] == "ok"
    assert event["retries"] == 2


def test_retry_exhausted_quota(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent 429 with max_retries=2 raises LlmError(quota) after 3 calls."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    _capture_sleep(monkeypatch)
    cfg = _with_retries(config, max_retries=2)
    stub.chat.completions.script = [
        _status_error(429),
        _status_error(429),
        _status_error(429),
    ]
    with pytest.raises(LlmError) as excinfo:
        chat(_MESSAGES, cfg=cfg)
    assert excinfo.value.category == "quota"
    assert "重试" in str(excinfo.value)
    assert len(stub.chat.completions.calls) == 3
    event = recent_events()[0]
    assert event["category"] == "quota"
    assert event["usage"] is None
    assert event["retries"] == 2


def test_auth_error_no_retry(
    llm_env: None,
    config: Nl2DataConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """401 maps to auth, is not retried, and never leaks the key."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_status_error(401)]
    with caplog.at_level(logging.DEBUG, logger="llm.chat"):
        with pytest.raises(LlmError) as excinfo:
            chat(_MESSAGES, cfg=config)
    assert excinfo.value.category == "auth"
    assert "认证失败" in str(excinfo.value)
    assert _KEY not in str(excinfo.value)
    assert len(stub.chat.completions.calls) == 1
    assert _KEY not in caplog.text


def test_timeout_error(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APITimeoutError maps to the timeout category."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    cfg = _with_retries(config, max_retries=0)
    stub.chat.completions.script = [_timeout_error()]
    with pytest.raises(LlmError) as excinfo:
        chat(_MESSAGES, cfg=cfg)
    assert excinfo.value.category == "timeout"
    assert len(stub.chat.completions.calls) == 1


def test_connection_error(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APIConnectionError maps to the network category."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    cfg = _with_retries(config, max_retries=0)
    stub.chat.completions.script = [_connection_error()]
    with pytest.raises(LlmError) as excinfo:
        chat(_MESSAGES, cfg=cfg)
    assert excinfo.value.category == "network"
    assert len(stub.chat.completions.calls) == 1


def test_other_4xx_maps_to_api(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-retryable 4xx (404) maps to api and is not retried."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_status_error(404)]
    with pytest.raises(LlmError) as excinfo:
        chat(_MESSAGES, cfg=config)
    assert excinfo.value.category == "api"
    assert len(stub.chat.completions.calls) == 1


def test_json_object_unsupported_falls_back_to_bare_call(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """400 mentioning response_format triggers one bare retry without it."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [
        _status_error(400, "Invalid parameter: response_format is not supported"),
        _ok_response('{"answer": "hi"}'),
    ]
    result = chat(_MESSAGES, json_schema=_SCHEMA, cfg=config)
    calls = stub.chat.completions.calls
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    assert result.parsed == {"answer": "hi"}
    event = recent_events()[0]
    assert event["category"] == "ok"
    assert event["retries"] == 0


def test_env_missing_raises_auth_without_client(
    config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing LLM_* variables raise auth before any client construction."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    with pytest.raises(LlmError) as excinfo:
        chat(_MESSAGES, cfg=config)
    assert excinfo.value.category == "auth"
    assert "检查" in str(excinfo.value)
    assert stub.constructions == 0
    assert recent_events() == []


def test_missing_usage_defaults_to_zero(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response without usage reporting yields an all-zero Usage."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [
        _FakeResponse(choices=[_FakeChoice(message=_FakeMessage(content="ok"))])
    ]
    result = chat(_MESSAGES, cfg=config)
    assert result.usage == Usage(0, 0, 0)


def test_empty_choices_maps_to_api(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty choices list raises LlmError(api) and records the event."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_FakeResponse(choices=[])]
    with pytest.raises(LlmError) as excinfo:
        chat(_MESSAGES, cfg=config)
    assert excinfo.value.category == "api"
    assert recent_events()[0]["category"] == "api"


def test_audit_event_fields_totals_and_reset(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events carry the full contract shape; totals accumulate; reset clears."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_ok_response("a")]
    chat(_MESSAGES, cfg=config)
    events = recent_events()
    assert len(events) == 1
    event = events[0]
    assert set(event) == _EVENT_KEYS
    datetime.fromisoformat(event["ts"])
    assert event["model"] == "glm-test"
    assert event["usage"] == Usage(11, 7, 18)
    assert event["latency_ms"] > 0
    assert event["prompt_preview"] == _MESSAGES[0]["content"][:80]
    assert event["category"] == "ok"
    assert event["retries"] == 0
    assert usage_totals() == {
        "calls": 1,
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    stub.chat.completions.script = [_ok_response("b", prompt=5, completion=3)]
    chat(_MESSAGES, cfg=config)
    assert usage_totals() == {
        "calls": 2,
        "prompt_tokens": 16,
        "completion_tokens": 10,
        "total_tokens": 26,
    }
    reset_events()
    assert recent_events() == []
    assert usage_totals() == {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_prompt_preview_truncated_to_80(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prompt_preview keeps only the first 80 characters."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_ok_response("ok")]
    long_messages = [{"role": "user", "content": "x" * 200}]
    chat(long_messages, cfg=config)
    assert recent_events()[0]["prompt_preview"] == "x" * 80


def test_no_key_leak_in_error_events_or_logs(
    llm_env: None,
    config: Nl2DataConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key must not appear in exceptions, any event, or any log record."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [
        _status_error(401, f"Invalid API key provided: {_KEY}")
    ]
    with caplog.at_level(logging.DEBUG, logger="llm.chat"):
        with pytest.raises(LlmError) as excinfo:
            chat(_MESSAGES, cfg=config)
    assert stub.init_kwargs["api_key"] == _KEY
    assert _KEY not in str(excinfo.value)
    assert _KEY not in json.dumps(recent_events(), ensure_ascii=False, default=_json_default)
    assert _KEY not in caplog.text
    assert "***" in str(excinfo.value)


def test_timeout_retry_then_succeeds(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout with retry budget left backs off once and then succeeds."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    delays = _capture_sleep(monkeypatch)
    cfg = _with_retries(config, max_retries=1)
    stub.chat.completions.script = [_timeout_error(), _ok_response("recovered")]
    result = chat(_MESSAGES, cfg=cfg)
    assert result.content == "recovered"
    assert len(stub.chat.completions.calls) == 2
    assert len(delays) == 1
    assert recent_events()[0]["retries"] == 1


def test_connection_retry_then_succeeds(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection error with retry budget left backs off and succeeds."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    _capture_sleep(monkeypatch)
    cfg = _with_retries(config, max_retries=1)
    stub.chat.completions.script = [_connection_error(), _ok_response("back online")]
    result = chat(_MESSAGES, cfg=cfg)
    assert result.content == "back online"
    assert len(stub.chat.completions.calls) == 2


def test_empty_messages_preview_is_blank(
    llm_env: None, config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty message list still succeeds with a blank prompt_preview."""
    stub = _StubClient()
    _install_stub(monkeypatch, stub)
    stub.chat.completions.script = [_ok_response("ok")]
    result = chat([], cfg=config)
    assert result.content == "ok"
    assert recent_events()[0]["prompt_preview"] == ""
