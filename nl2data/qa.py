"""Question-answering orchestration (milestone 3, T12).

Wires the frozen contracts together per question:
retrieve → generate → validate (feed errors back, bounded retries) →
run (feed errors back) → interpret (second LLM call, degradable).
Every question appends one audit event.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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
    # Channels the retrieval actually used (e.g. ["bm25"] on degradation);
    # passthrough of RetrievalResult.channels_used, no behavioural meaning.
    retrieval_channels: list[str] = field(default_factory=list)
    # M7/T21 multi-candidate accounting: total generate calls made, how many
    # candidates executed successfully in the winning round, and whether the
    # successful candidates disagreed (no strict result majority).
    candidates_tried: int = 0
    candidates_ok: int = 0
    candidates_disagree: bool = False
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


def _generate_candidates(
    question: str,
    retrieval: RetrievalResult,
    cfg: Nl2DataConfig,
    feedback: str | None,
    count: int,
) -> list[Any]:
    """Generate up to ``count`` candidates, tolerating per-candidate failure.

    With ``count > 1`` candidates run in parallel threads at
    ``cfg.qa.candidate_temperature`` (M7/T21; see QaConfig docstring) and
    ``executor.map`` keeps results in deterministic submission order. A
    candidate whose LLM call fails is dropped; if all fail the first error
    propagates — preserving the single-shot failure contract.
    """
    if count <= 1:
        return [generate(question, retrieval, cfg, feedback=feedback)]
    cand_cfg = cfg
    if cfg.qa.candidate_temperature != cfg.llm.temperature:
        cand_cfg = replace(
            cfg, llm=replace(cfg.llm, temperature=cfg.qa.candidate_temperature)
        )
    errors: list[BaseException] = []

    def _one(_: int) -> Any:
        try:
            return generate(question, retrieval, cand_cfg, feedback=feedback)
        except Exception as exc:  # noqa: BLE001 -- candidate-level tolerance
            errors.append(exc)
            return None

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(_one, range(count)))
    generations = [g for g in results if g is not None]
    if not generations:
        raise errors[0] if errors else RuntimeError("候选生成全部失败")
    return generations


def _result_signature(execution: ExecutionResult) -> tuple:
    """Hashable value-multiset signature of a successful execution result.

    Column names are deliberately excluded — alias wording is legal
    variation (established by the eval comparator in G8b); voting compares
    the multiset of row values plus the row count.
    """
    rows = tuple(
        sorted(
            tuple(sorted(str(value) for value in row.values()))
            for row in execution.rows
        )
    )
    return (execution.rowcount, rows)


def ask_once(
    question: str,
    cfg: Nl2DataConfig,
    *,
    no_interpret: bool = False,
    extra_feedback: str | None = None,
    chat_fn: Any = None,
) -> QaOutcome:
    """Run one full question through the pipeline with bounded retries.

    With ``cfg.qa.candidate_count > 1`` (M7/T21) each round generates that
    many candidates in parallel, executes every guard-passing SQL, and picks
    the strict-majority result group; without a strict majority the first
    successful candidate wins and ``candidates_disagree`` is flagged for
    honest disclosure. ``candidate_count == 1`` is the legacy single-shot
    path.

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
    candidates_tried = 0
    candidates_ok = 0
    candidates_disagree = False
    candidate_count = max(1, int(cfg.qa.candidate_count))

    try:
        retrieval = retrieve(question, cfg)
        allowed, column_map = catalog_whitelists(cfg)
        feedback = extra_feedback
        for attempt in range(1, cfg.qa.max_attempts + 1):
            attempts = attempt
            generations = _generate_candidates(
                question, retrieval, cfg, feedback, candidate_count
            )
            candidates_tried += len(generations)
            clarifications = [g for g in generations if g.needs_clarification]
            if clarifications and len(clarifications) * 2 >= len(generations):
                clarification = clarifications[0].clarification or "请补充更多信息。"
                break
            guard_errors: list[str] = []
            valid: list[tuple[str, ValidatedSQL]] = []
            for generation in generations:
                try:
                    valid.append(
                        (
                            generation.sql or "",
                            validate(generation.sql or "", allowed, column_map, cfg),
                        )
                    )
                except GuardError as exc:
                    guard_errors.append(f"护栏拒绝({exc.category}):{exc}")
            if not valid:
                failure_reason = guard_errors[0] if guard_errors else "候选均未给出 SQL"
                feedback = (
                    f"上一次 SQL: {generations[0].sql}\n执行错误: {failure_reason}"
                )
                continue
            executed: list[tuple[ValidatedSQL, ExecutionResult]] = []
            exec_errors: list[str] = []
            for _, candidate in valid:
                result = run(candidate, cfg)
                if result.status == "ok":
                    executed.append((candidate, result))
                else:
                    error = result.error or {"category": "unknown", "message": ""}
                    exec_errors.append(
                        f"执行失败({error.get('category')}):{error.get('message', '')}"
                    )
            if not executed:
                failure_reason = exec_errors[0] if exec_errors else "全部候选执行失败"
                feedback = f"上一次 SQL: {valid[0][0]}\n执行错误: {failure_reason}"
                continue
            groups: dict[tuple, list[tuple[ValidatedSQL, ExecutionResult]]] = {}
            for pair in executed:
                groups.setdefault(_result_signature(pair[1]), []).append(pair)
            winner = max(groups.values(), key=len)
            candidates_disagree = len(groups) > 1 and len(winner) * 2 <= len(executed)
            vsql, execution = winner[0]
            candidates_ok = len(executed)
            failure_reason = None
            break
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
            candidates_tried=candidates_tried,
            candidates_ok=candidates_ok,
            candidates_disagree=candidates_disagree,
        )

    return QaOutcome(
        question=question,
        retrieved_tables=[item.table for item in retrieval.items] if retrieval else [],
        retrieval_channels=list(retrieval.channels_used) if retrieval else [],
        candidates_tried=candidates_tried,
        candidates_ok=candidates_ok,
        candidates_disagree=candidates_disagree,
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
    candidates_tried: int = 0,
    candidates_ok: int = 0,
    candidates_disagree: bool = False,
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
        "candidates_tried": candidates_tried,
        "candidates_ok": candidates_ok,
        "candidates_disagree": candidates_disagree,
        "clarification": clarification,
        "failure_reason": failure_reason,
    }
    try:
        write_audit_event(cfg, event)
    except OSError as exc:
        logger.warning("audit write failed (question not blocked): %s", exc)
