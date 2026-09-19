"""Question-answering orchestration (milestone 3, T12).

Wires the frozen contracts together per question:
retrieve → generate → validate (feed errors back, bounded retries) →
run (feed errors back) → interpret (second LLM call, degradable).
Every question appends one audit event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from exec.runner import ExecutionResult, run
from guard.validate import GuardError, ValidatedSQL, validate
from llm.chat import LlmError, chat, usage_totals
from nl2data.audit import write_audit_event
from nl2data.config import Nl2DataConfig
from retrieval.retrieve import RetrievalResult, retrieve
from sqlgen.generate import generate

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
INTERPRET_PROMPT_PATH = _REPO_ROOT / "sqlgen" / "prompts" / "interpret.md"


@dataclass(frozen=True)
class QaOutcome:
    """Everything the CLI needs to render one answered question."""

    question: str
    retrieved_tables: list[str] = field(default_factory=list)
    clarification: str | None = None
    vsql: ValidatedSQL | None = None
    execution: ExecutionResult | None = None
    interpretation: str | None = None
    interpretation_failed: bool = False
    attempts: int = 0
    failure_reason: str | None = None
    usage_delta: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the question produced an executed result."""
        return self.execution is not None and self.execution.status == "ok"


def catalog_whitelists(cfg: Nl2DataConfig) -> tuple[set[str], dict[str, set[str]]]:
    """Read table/column whitelists from catalog.yaml."""
    with cfg.paths.catalog.open(encoding="utf-8") as fh:
        catalog: dict[str, Any] = yaml.safe_load(fh) or {}
    tables = {
        table["name"]: {column["name"] for column in table.get("columns", [])}
        for source in catalog.get("sources", [])
        for table in source.get("tables", [])
    }
    return set(tables), tables


def _safe_interpret(
    question: str, vsql: ValidatedSQL, execution: ExecutionResult, cfg: Nl2DataConfig
) -> tuple[str | None, bool]:
    """Interpret with degradation: LLM failure returns (None, True)."""
    system = INTERPRET_PROMPT_PATH.read_text(encoding="utf-8")
    payload: list[str] = [
        f"用户问题:{question}",
        f"实际执行的 SQL:{vsql.sql}",
        f"行数:{execution.rowcount}",
    ]
    if execution.rows:
        sample = "\n".join(str(row) for row in execution.rows[:10])
        payload.append(f"样本行(前 10):\n{sample}")
    if execution.profile:
        payload.append(f"统计画像:{execution.profile}")
    try:
        result = chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n".join(payload)},
            ],
            cfg=cfg,
        )
    except LlmError as exc:
        logger.warning("interpretation failed; showing data only: %s", exc)
        return None, True
    return result.content.strip(), False


def ask_once(
    question: str,
    cfg: Nl2DataConfig,
    *,
    no_interpret: bool = False,
    extra_feedback: str | None = None,
    chat_fn: Any = None,
) -> QaOutcome:
    """Run one full question through the pipeline with bounded retries.

    Args:
        question: The natural-language question.
        cfg: Active configuration.
        no_interpret: Skip the second (narration) LLM call.
        extra_feedback: Optional user hint merged into the first generate call.
        chat_fn: Test hook replacing the interpret-time chat call.

    Returns:
        The outcome for rendering; one audit event is always appended.
    """
    before = usage_totals()
    retrieval: RetrievalResult | None = None
    attempts = 0
    failure_reason: str | None = None
    vsql: ValidatedSQL | None = None
    execution: ExecutionResult | None = None
    clarification: str | None = None

    try:
        retrieval = retrieve(question, cfg)
        allowed, column_map = catalog_whitelists(cfg)
        feedback = extra_feedback
        for attempt in range(1, cfg.qa.max_attempts + 1):
            attempts = attempt
            generation = generate(question, retrieval, cfg, feedback=feedback)
            if generation.needs_clarification:
                clarification = generation.clarification or "请补充更多信息。"
                break
            try:
                vsql = validate(generation.sql or "", allowed, column_map, cfg)
            except GuardError as exc:
                failure_reason = f"护栏拒绝({exc.category}):{exc}"
                feedback = f"上一次 SQL: {generation.sql}\n执行错误: {failure_reason}"
                vsql = None
                continue
            execution = run(vsql, cfg)
            if execution.status == "ok":
                failure_reason = None
                break
            error = execution.error or {"category": "unknown", "message": ""}
            failure_reason = (
                f"执行失败({error.get('category')}):{error.get('message', '')}"
            )
            feedback = f"上一次 SQL: {generation.sql}\n执行错误: {failure_reason}"
            execution = None
            vsql = None
        else:
            failure_reason = failure_reason or "重试次数已用尽"

        interpretation: str | None = None
        interpretation_failed = False
        if execution is not None and execution.status == "ok" and not no_interpret:
            if chat_fn is not None:
                interpretation = str(chat_fn()).strip()
            else:
                interpretation, interpretation_failed = _safe_interpret(
                    question, vsql, execution, cfg  # type: ignore[arg-type]
                )
    finally:
        after = usage_totals()
        usage_delta = {key: after[key] - before.get(key, 0) for key in after}
        _append_audit(
            cfg,
            question=question,
            retrieval=retrieval,
            vsql=vsql,
            execution=execution,
            attempts=attempts,
            clarification=clarification,
            failure_reason=failure_reason,
            usage_delta=usage_delta,
        )

    return QaOutcome(
        question=question,
        retrieved_tables=[item.table for item in retrieval.items] if retrieval else [],
        clarification=clarification,
        vsql=vsql,
        execution=execution,
        interpretation=interpretation,
        interpretation_failed=interpretation_failed,
        attempts=attempts,
        failure_reason=failure_reason,
        usage_delta=usage_delta,
    )


def _append_audit(
    cfg: Nl2DataConfig,
    *,
    question: str,
    retrieval: RetrievalResult | None,
    vsql: ValidatedSQL | None,
    execution: ExecutionResult | None,
    attempts: int,
    clarification: str | None,
    failure_reason: str | None,
    usage_delta: dict[str, int],
) -> None:
    """Append one audit event; write failures warn but never block."""
    if clarification is not None:
        guard_outcome = "clarification"
    elif failure_reason is not None and vsql is None:
        guard_outcome = "rejected"
    elif vsql is not None:
        guard_outcome = "passed"
    else:
        guard_outcome = "unknown"
    event = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "question": question,
        "retrieved_tables": [item.table for item in retrieval.items] if retrieval else [],
        "sql": vsql.sql if vsql else None,
        "guard_outcome": guard_outcome,
        "status": execution.status if execution else "not_executed",
        "rowcount": execution.rowcount if execution else 0,
        "usage": usage_delta,
        "latency_ms": round(execution.latency_ms) if execution else None,
        "attempts": attempts,
        "clarification": clarification,
        "failure_reason": failure_reason,
    }
    try:
        write_audit_event(cfg, event)
    except OSError as exc:
        logger.warning("audit write failed (question not blocked): %s", exc)
