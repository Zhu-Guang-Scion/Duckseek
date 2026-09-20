"""OpenAI-compatible chat client: the only module allowed to call LLM APIs (T8).

Credentials come from the environment (``LLM_BASE_URL`` / ``LLM_API_KEY`` /
``LLM_MODEL``, goals.md decision 3) and are read at call time. The API key is
never logged, never placed in exception messages, and never recorded in audit
events; every free-text error summary passes through a redaction filter.

Structured output follows a three-layer degradation strategy:

1. preferred: ``response_format={"type": "json_object"}`` -- the structured
   mode actually supported by GLM / DeepSeek / Qwen / Kimi compatible
   endpoints. The ``json_schema`` argument is never sent to the server; it
   only marks the request as "expect JSON" for client-side parsing.
2. fallback: when the server rejects ``json_object`` (400/422 and the error
   text mentions ``response_format``/``json``), retry once without any
   ``response_format``.
3. parsing: strip a markdown code fence, ``json.loads`` the content; on
   failure ``parsed`` is ``None`` and no exception is raised.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import openai

from nl2data.config import Nl2DataConfig

logger = logging.getLogger(__name__)

ENV_BASE_URL = "LLM_BASE_URL"
ENV_API_KEY = "LLM_API_KEY"
ENV_MODEL = "LLM_MODEL"

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_JSON_UNSUPPORTED_STATUS_CODES: frozenset[int] = frozenset({400, 422})
_JSON_UNSUPPORTED_KEYWORDS: tuple[str, ...] = ("response_format", "json")

_BACKOFF_BASE_SECONDS: float = 1.0
_JITTER_MAX_SECONDS: float = 1.0
_PREVIEW_CHARS: int = 80
_SUMMARY_MAX_CHARS: int = 200

_FENCE_PATTERN: re.Pattern[str] = re.compile(
    r"\A```[^\n]*\n(.*?)\n?```\s*\Z", re.DOTALL
)
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9._-]+"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+"),
)

_FRIENDLY_MESSAGES: dict[str, str] = {
    "auth": "认证失败:请检查 LLM_API_KEY 是否有效",
    "quota": "配额或限流不足:稍后重试或检查账户额度",
    "timeout": "请求超时:请检查网络或增大 llm.timeout_seconds",
    "network": "网络连接失败:请检查 LLM_BASE_URL 与本机网络",
    "api": "LLM API 返回错误",
    "parse": "LLM 返回内容无法按 JSON 解析",
}

_EVENTS_LOCK = threading.Lock()
_EVENTS: list[dict[str, Any]] = []
_ZERO_TOTALS: dict[str, int] = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}
_TOTALS: dict[str, int] = dict(_ZERO_TOTALS)


@dataclass(frozen=True)
class Usage:
    """Token accounting reported by the API (all zeros when unavailable)."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ChatResult:
    """One successful chat completion plus audit metadata."""

    content: str
    parsed: dict[str, Any] | None
    usage: Usage
    model: str
    latency_ms: float


class LlmError(RuntimeError):
    """Chat failure; ``category`` in {auth, quota, timeout, network, api, parse}.

    Messages are user-facing Chinese text containing only the status code,
    the category, and a redacted error summary -- never the API key or any
    HTTP headers.
    """

    def __init__(self, message: str, category: str) -> None:
        super().__init__(message)
        self.category = category


def _safe_summary(exc: BaseException) -> str:
    """Return a redacted one-line summary of ``exc`` (secrets stripped)."""
    text = " ".join(str(exc).split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***", text)
    return text[:_SUMMARY_MAX_CHARS]


def _strip_code_fence(text: str) -> str:
    """Return ``text`` with a surrounding markdown code fence removed."""
    stripped = text.strip()
    match = _FENCE_PATTERN.match(stripped)
    if match is not None:
        return match.group(1).strip()
    return stripped


def _parse_json_content(content: str) -> dict[str, Any] | None:
    """Parse ``content`` as JSON; ``None`` (never raises) on any failure."""
    candidate = _strip_code_fence(content)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _resolve_env() -> tuple[str, str, str]:
    """Read ``LLM_*`` env vars; raise an ``auth`` LlmError when missing."""
    base_url = os.environ.get(ENV_BASE_URL, "").strip()
    api_key = os.environ.get(ENV_API_KEY, "").strip()
    model = os.environ.get(ENV_MODEL, "").strip()
    missing = [
        name
        for name, value in (
            (ENV_BASE_URL, base_url),
            (ENV_API_KEY, api_key),
            (ENV_MODEL, model),
        )
        if not value
    ]
    if missing:
        raise LlmError(
            f"缺少环境变量 {', '.join(missing)}:"
            "请检查 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 是否已设置且非空",
            "auth",
        )
    return base_url, api_key, model


def _status_category(status_code: int) -> str:
    """Map an HTTP status code to an LlmError category."""
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "quota"
    return "api"


def _mentions_json_mode(exc: BaseException) -> bool:
    """Return True when the error text indicates response_format rejection."""
    text = str(exc).lower()
    return any(keyword in text for keyword in _JSON_UNSUPPORTED_KEYWORDS)


def _friendly_message(
    category: str,
    exc: BaseException,
    *,
    status_code: int | None = None,
    retries: int,
    max_retries: int,
) -> str:
    """Build a user-facing message; only redacted details are included."""
    parts = [_FRIENDLY_MESSAGES.get(category, "LLM 调用失败")]
    if status_code is not None:
        parts.append(f"HTTP {status_code}")
    if retries > 0:
        parts.append(f"已重试 {retries}/{max_retries} 次")
    summary = _safe_summary(exc)
    if summary:
        parts.append(summary)
    return f"{parts[0]}({'; '.join(parts[1:])})"


def _prompt_preview(messages: list[dict[str, str]]) -> str:
    """Return the first ``_PREVIEW_CHARS`` characters of the first message."""
    if not messages:
        return ""
    return str(messages[0].get("content", ""))[:_PREVIEW_CHARS]


def _extract_content(response: Any) -> str:
    """Extract the assistant text from a chat completion response."""
    choices = getattr(response, "choices", None) or ()
    if not choices:
        raise LlmError("LLM 返回的 choices 为空", "api")
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", "") or "")


def _extract_usage(response: Any) -> Usage:
    """Extract token usage; zeros when the API did not report any."""
    raw = getattr(response, "usage", None)
    if raw is None:
        return Usage(0, 0, 0)
    return Usage(
        prompt_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(raw, "total_tokens", 0) or 0),
    )


def recent_events() -> list[dict[str, Any]]:
    """Return a copy of the recorded audit events (oldest first)."""
    with _EVENTS_LOCK:
        return [dict(event) for event in _EVENTS]


def reset_events() -> None:
    """Clear audit events and usage totals (test isolation helper)."""
    with _EVENTS_LOCK:
        _EVENTS.clear()
        _TOTALS.clear()
        _TOTALS.update(_ZERO_TOTALS)


def usage_totals() -> dict[str, int]:
    """Return cumulative usage counters over all calls since the last reset."""
    with _EVENTS_LOCK:
        return dict(_TOTALS)


def _record_event(
    *,
    model: str,
    usage: Usage | None,
    latency_ms: float,
    prompt_preview: str,
    category: str,
    retries: int,
) -> None:
    """Append one audit event and fold ``usage`` into the running totals."""
    event: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "model": model,
        "usage": usage,
        "latency_ms": round(latency_ms, 3),
        "prompt_preview": prompt_preview,
        "category": category,
        "retries": retries,
    }
    with _EVENTS_LOCK:
        _EVENTS.append(event)
        _TOTALS["calls"] += 1
        if usage is not None:
            _TOTALS["prompt_tokens"] += usage.prompt_tokens
            _TOTALS["completion_tokens"] += usage.completion_tokens
            _TOTALS["total_tokens"] += usage.total_tokens


def chat(
    messages: list[dict[str, str]],
    *,
    json_schema: dict[str, Any] | None = None,
    cfg: Nl2DataConfig,
) -> ChatResult:
    """Call the OpenAI-compatible ``/chat/completions`` endpoint.

    Args:
        messages: Chat messages in OpenAI format.
        json_schema: When given, request JSON output via ``json_object``
            mode, fall back to a bare call if the server rejects it, and try
            to parse the reply into ``parsed``. The schema itself is never
            sent to the server.
        cfg: Root configuration; ``cfg.llm`` supplies temperature, token
            limit, timeout, and the retry budget.

    Returns:
        A :class:`ChatResult`; ``parsed`` is non-``None`` only when the
        content parses as a JSON object.

    Raises:
        LlmError: On missing environment configuration or when the API call
            ultimately fails (after retries). Parse failures never raise.
    """
    base_url, api_key, model = _resolve_env()
    started = time.perf_counter()

    def _elapsed_ms() -> float:
        return (time.perf_counter() - started) * 1000.0

    client: openai.OpenAI = openai.OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=cfg.llm.timeout_seconds,
        max_retries=0,  # this module owns the retry policy.
    )
    retries = 0
    json_mode = json_schema is not None
    degraded = False
    try:
        while True:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": list(messages),
                "temperature": cfg.llm.temperature,
                "max_tokens": cfg.llm.max_tokens,
            }
            if cfg.llm.thinking == "disabled":
                # Opt-in only: strict OpenAI-compatible servers reject unknown
                # body fields, so the switch is sent only when configured.
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = client.chat.completions.create(**kwargs)
                break
            except openai.APIStatusError as exc:
                status_code = int(exc.status_code)
                if (
                    json_mode
                    and not degraded
                    and status_code in _JSON_UNSUPPORTED_STATUS_CODES
                    and _mentions_json_mode(exc)
                ):
                    # Layer 2: server rejects json_object -> one bare retry.
                    degraded = True
                    json_mode = False
                    logger.warning(
                        "server rejected json_object mode;"
                        " retrying without response_format"
                    )
                    continue
                category = _status_category(status_code)
                if (
                    status_code not in RETRYABLE_STATUS_CODES
                    or retries >= cfg.llm.max_retries
                ):
                    raise LlmError(
                        _friendly_message(
                            category,
                            exc,
                            status_code=status_code,
                            retries=retries,
                            max_retries=cfg.llm.max_retries,
                        ),
                        category,
                    ) from exc
            except openai.APITimeoutError as exc:
                if retries >= cfg.llm.max_retries:
                    raise LlmError(
                        _friendly_message(
                            "timeout",
                            exc,
                            retries=retries,
                            max_retries=cfg.llm.max_retries,
                        ),
                        "timeout",
                    ) from exc
            except openai.APIConnectionError as exc:
                if retries >= cfg.llm.max_retries:
                    raise LlmError(
                        _friendly_message(
                            "network",
                            exc,
                            retries=retries,
                            max_retries=cfg.llm.max_retries,
                        ),
                        "network",
                    ) from exc
            # APITimeoutError subclasses APIConnectionError, so it is matched
            # first above; both retry with exponential backoff plus jitter.
            delay = _BACKOFF_BASE_SECONDS * (2**retries) + random.uniform(
                0.0, _JITTER_MAX_SECONDS
            )
            logger.warning(
                "LLM call failed (attempt %s/%s); retrying in %.2fs",
                retries + 1,
                cfg.llm.max_retries,
                delay,
            )
            time.sleep(delay)
            retries += 1
        content = _extract_content(response)
    except LlmError as exc:
        _record_event(
            model=model,
            usage=None,
            latency_ms=_elapsed_ms(),
            prompt_preview=_prompt_preview(messages),
            category=exc.category,
            retries=retries,
        )
        raise
    latency_ms = _elapsed_ms()
    usage = _extract_usage(response)
    parsed = _parse_json_content(content) if json_schema is not None else None
    _record_event(
        model=model,
        usage=usage,
        latency_ms=latency_ms,
        prompt_preview=_prompt_preview(messages),
        category="ok",
        retries=retries,
    )
    return ChatResult(
        content=content, parsed=parsed, usage=usage, model=model, latency_ms=latency_ms
    )
