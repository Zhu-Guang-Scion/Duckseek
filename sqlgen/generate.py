"""Natural-language question to DuckDB SQL generation (milestone 3, T9).

One :func:`generate` call performs exactly one LLM attempt. Messages are
assembled statelessly from the system template (plus optional few-shot
examples) and the retrieval prompt block, then sent through
:func:`llm.chat.chat` under a JSON output contract. The reply is parsed
leniently: a JSON object first (client-side ``parsed`` or fence-stripped
``content``), then a `` ```sql `` fence, otherwise :class:`SqlgenError`.

Retries are caller-driven: call :func:`generate` again passing the execution
error as ``feedback``; :attr:`SQLGeneration.candidates_tried` is therefore
always ``1`` per call and the caller accumulates the total.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm.chat import chat
from nl2data.config import Nl2DataConfig
from retrieval.retrieve import RetrievalResult

# Prompt assets live inside the package; module-level so tests can monkeypatch.
_SYSTEM_TEMPLATE_PATH: Path = Path(__file__).parent / "prompts" / "system.md"
_EXAMPLES_DIR: Path = Path(__file__).parent / "examples"

#: Feedback longer than this is truncated before injection into the prompt.
_FEEDBACK_MAX_CHARS: int = 2000
#: Original LLM text attached to parse-failure errors, capped at this length.
_ERROR_SNIPPET_CHARS: int = 200
#: Audit content longer than this is truncated by :func:`last_messages`.
_AUDIT_CONTENT_CHARS: int = 500
_TRUNCATION_SUFFIX: str = "…[truncated]"
_DEFAULT_CLARIFICATION: str = "请补充更多信息。"
_FEEDBACK_INSTRUCTION: str = "——请修正后重新给出完整 SQL。"

#: Output contract marked client-side; never sent to the server (see T8).
_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "needs_clarification": {"type": "boolean"},
        "clarification": {"type": "string"},
    },
}

_WHOLE_FENCE_PATTERN: re.Pattern[str] = re.compile(
    r"\A```[^\n]*\n(.*?)\n?```\s*\Z", re.DOTALL
)
_JSON_FENCE_PATTERN: re.Pattern[str] = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
_SQL_FENCE_PATTERN: re.Pattern[str] = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL)

_LAST_MESSAGES: list[dict[str, str]] | None = None


class SqlgenError(RuntimeError):
    """Raised when the LLM reply cannot be parsed into a SQL result."""


@dataclass(frozen=True)
class SQLGeneration:
    """Outcome of one SQL generation attempt.

    Attributes:
        sql: The generated single SELECT statement, or ``None`` when the
            model asked for clarification or no SQL could be parsed.
        needs_clarification: Whether the model requested more information.
        clarification: The model's follow-up question, or a default Chinese
            prompt when it asked for clarification without giving one.
        candidates_tried: Always ``1``: one :func:`generate` call is exactly
            one attempt. Retries are caller-driven (call again with
            ``feedback``); the caller accumulates the attempt count.
    """

    sql: str | None
    needs_clarification: bool
    clarification: str | None
    candidates_tried: int


def _load_few_shots(count: int) -> list[str]:
    """Return up to ``count`` example file bodies from the examples directory.

    ``README.md`` is always skipped; remaining files are ordered by file name.

    Args:
        count: Maximum number of examples to load; ``<= 0`` loads none.

    Returns:
        Example file contents (stripped); an empty list when the directory
        is missing or contains no usable example files.
    """
    if count <= 0 or not _EXAMPLES_DIR.is_dir():
        return []
    paths = sorted(
        (p for p in _EXAMPLES_DIR.glob("*.md") if p.name != "README.md"),
        key=lambda p: p.name,
    )
    examples: list[str] = []
    for path in paths[:count]:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - unreadable files are skipped
            continue
        if text:
            examples.append(text)
    return examples


def _load_system_prompt(few_shot_count: int) -> str:
    """Build the system message from the template plus optional few-shots.

    Args:
        few_shot_count: Number of few-shot examples to append; the examples
            form a ``## 参考示例`` section at the tail of the template.

    Returns:
        The full system prompt text.

    Raises:
        RuntimeError: If the template file is missing, with guidance to
            restore ``sqlgen/prompts/system.md``.
    """
    if not _SYSTEM_TEMPLATE_PATH.is_file():
        msg = (
            f"SQL 生成系统提示词模板文件缺失: {_SYSTEM_TEMPLATE_PATH};"
            "请恢复 sqlgen/prompts/system.md(可从版本库还原)后重试"
        )
        raise RuntimeError(msg)
    system = _SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")
    if few_shot_count > 0:
        examples = _load_few_shots(few_shot_count)
        if examples:
            system += "\n\n## 参考示例\n\n" + "\n\n".join(examples)
    return system


def _truncate_feedback(feedback: str) -> str:
    """Return ``feedback`` capped at :data:`_FEEDBACK_MAX_CHARS` characters."""
    if len(feedback) <= _FEEDBACK_MAX_CHARS:
        return feedback
    return feedback[:_FEEDBACK_MAX_CHARS] + _TRUNCATION_SUFFIX


def _build_user_message(
    question: str, prompt_block: str, feedback: str | None
) -> str:
    """Assemble the single user message with clearly separated sections.

    Args:
        question: The natural-language question from the user.
        prompt_block: The retrieval prompt block describing candidate tables.
        feedback: Optional prior-round feedback in the caller-composed form
            ``"上一次 SQL: <sql>\n执行错误: <error>"``; truncated before
            injection. The whole section is omitted when ``None``.

    Returns:
        The user message text with ``## `` section headers.
    """
    sections = [
        f"## 任务\n用户问题:{question}",
        f"## 提供的表\n{prompt_block}",
    ]
    if feedback:
        sections.append(
            f"## 上一轮反馈\n{_truncate_feedback(feedback)}\n{_FEEDBACK_INSTRUCTION}"
        )
    return "\n\n".join(sections)


def _remember_messages(messages: list[dict[str, str]]) -> None:
    """Record a deep copy of ``messages`` for :func:`last_messages` audit."""
    global _LAST_MESSAGES
    _LAST_MESSAGES = copy.deepcopy(messages)


def last_messages() -> list[dict[str, str]] | None:
    """Return the most recently assembled messages, truncated for audit.

    Each message content longer than 500 characters is cut and suffixed with
    ``…[truncated]``. Nothing is persisted to disk. Returns ``None`` before
    the first :func:`generate` call in this process.
    """
    if _LAST_MESSAGES is None:
        return None
    audit: list[dict[str, str]] = []
    for message in _LAST_MESSAGES:
        content = message["content"]
        if len(content) > _AUDIT_CONTENT_CHARS:
            content = content[:_AUDIT_CONTENT_CHARS] + _TRUNCATION_SUFFIX
        audit.append({"role": message["role"], "content": content})
    return audit


def _clean_sql(sql: str) -> str:
    """Strip surrounding whitespace and trailing semicolons/newlines.

    No SQL syntax validation happens here; that is the guard module's job.
    """
    return sql.strip().strip(";").strip()


def _strip_fence(content: str) -> str:
    """Return ``content`` without a single surrounding markdown code fence."""
    match = _WHOLE_FENCE_PATTERN.match(content)
    return match.group(1) if match else content


def _extract_json_payload(content: str) -> dict[str, Any] | None:
    """Best-effort extraction of a JSON object from raw LLM text.

    Tries, in order: the fence-stripped whole text, each `` ```json `` fenced
    block, and the span between the first ``{`` and the last ``}``.

    Args:
        content: The raw LLM reply text.

    Returns:
        The first successfully parsed JSON object, or ``None``.
    """
    candidates = [_strip_fence(content)]
    candidates.extend(m.group(1) for m in _JSON_FENCE_PATTERN.finditer(content))
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        candidates.append(content[start : end + 1])
    for candidate in candidates:
        try:
            payload: Any = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _extract_sql_fence(content: str) -> str | None:
    """Return the body of the first `` ```sql `` fenced block, if any."""
    match = _SQL_FENCE_PATTERN.search(content)
    if match is None:
        return None
    sql = match.group(1).strip()
    return sql or None


def _parse_result(content: str, parsed: dict[str, Any] | None) -> SQLGeneration:
    """Interpret one chat reply leniently into a :class:`SQLGeneration`.

    Fallback order: client-side parsed JSON object, then a JSON object
    extracted from the raw text, then a bare `` ```sql `` fence; otherwise
    :class:`SqlgenError` with a truncated excerpt of the original text.
    """
    payload = parsed if parsed is not None else _extract_json_payload(content)
    if payload is not None:
        if payload.get("needs_clarification"):
            clarification = payload.get("clarification") or _DEFAULT_CLARIFICATION
            return SQLGeneration(
                sql=None,
                needs_clarification=True,
                clarification=str(clarification),
                candidates_tried=1,
            )
        sql = payload.get("sql")
        if isinstance(sql, str) and sql.strip():
            return SQLGeneration(
                sql=_clean_sql(sql),
                needs_clarification=False,
                clarification=None,
                candidates_tried=1,
            )
    fenced = _extract_sql_fence(content)
    if fenced is not None:
        return SQLGeneration(
            sql=_clean_sql(fenced),
            needs_clarification=False,
            clarification=None,
            candidates_tried=1,
        )
    snippet = " ".join(content.split())[:_ERROR_SNIPPET_CHARS]
    msg = f"LLM 输出无法解析为 SQL 结果: {snippet}"
    raise SqlgenError(msg)


def generate(
    question: str,
    retrieval: RetrievalResult,
    cfg: Nl2DataConfig,
    feedback: str | None = None,
) -> SQLGeneration:
    """Generate one candidate SQL statement for ``question``.

    Statelessly assembles ``[system, user]`` messages from the prompt
    template (plus few-shot examples when ``cfg.sqlgen.few_shot_count > 0``)
    and the retrieval prompt block, makes exactly one LLM call under the JSON
    output contract, and parses the reply leniently.

    Args:
        question: The natural-language question from the user.
        retrieval: The retrieval outcome supplying the table prompt block.
        cfg: Root configuration; ``cfg.sqlgen.few_shot_count`` controls the
            number of injected examples and ``cfg.llm`` the chat parameters.
        feedback: Optional prior-round feedback in the caller-composed form
            ``"上一次 SQL: <sql>\n执行错误: <error>"``; truncated to 2000
            characters before injection.

    Returns:
        A :class:`SQLGeneration` with ``candidates_tried == 1``: this module
        performs exactly one attempt per call, and retries are driven by the
        caller (call again with ``feedback`` and accumulate counts there).

    Raises:
        RuntimeError: If the system prompt template file is missing.
        llm.chat.LlmError: Propagated untouched; the CLI decides whether to
            retry or abort.
        SqlgenError: If the reply cannot be parsed into SQL or a
            clarification request.
    """
    system = _load_system_prompt(cfg.sqlgen.few_shot_count)
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": _build_user_message(question, retrieval.prompt_block, feedback),
        },
    ]
    _remember_messages(messages)
    result = chat(messages, json_schema=_JSON_SCHEMA, cfg=cfg)
    return _parse_result(result.content, result.parsed)
