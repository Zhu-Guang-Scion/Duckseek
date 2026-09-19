"""Three-layer end-to-end regression runner (milestone 4, T15).

Layer 1 (recall): ``retrieve`` against ``expected_tables`` (Recall@3).
Layer 2 (sql): the full ``ask_once`` pipeline must execute without
  clarification, guard rejection or execution error (SQL text is NOT
  compared — different phrasings are legal).
Layer 3 (result): ``ExecutionResult`` vs ``expected_result`` semantic
  equivalence — same row multiset (order-insensitive unless the expected
  SQL carries ORDER BY), per-cell values within ``result_tolerance``.

Every case runs with interpretation disabled (cost control); per-case
failures never abort the round. A saved baseline can be diffed against.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nl2data.config import Nl2DataConfig
from retrieval.retrieve import retrieve

BASELINE_PATH = Path("eval/baseline_e2e.json")
_REL_TOL = 1e-4

# Grouping-key synonyms normalised before cell comparison (T15): the vehicle
# label is the one systematic Chinese/English alias family in this project
# (glossary term 黄色出租车 with synonyms 黄车/yellow taxi/yellow cab, and the
# bare English short forms the model also emits). Real semantic differences
# (zone vs borough, row-count drift) are NOT normalised.
_LABEL_ALIASES = {
    "黄车": "yellow",
    "黄色出租车": "yellow",
    "yellow taxi": "yellow",
    "yellow cab": "yellow",
    "绿车": "green",
    "绿色出租车": "green",
    "green taxi": "green",
    "boro taxi": "green",
}


def _canonical_label(value: str) -> str:
    """Normalise a known grouping-label alias to its canonical short form."""
    return _LABEL_ALIASES.get(value.strip().casefold(), value)

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class CaseVerdict:
    """One golden case judged on all three layers."""

    index: int
    question: str
    section: str | None
    l1_recall3: float = 0.0
    l2: str = STATUS_FAIL  # pass / fail(clarification) / error(guard/exec)
    l3: str | None = None  # pass / fail / error; None when L2 did not pass
    sql: str | None = None
    reason: str | None = None
    actual_rows_preview: list[list[Any]] = field(default_factory=list)
    expected_rowcount: int | None = None
    confidence: str = "1/1"


@dataclass(frozen=True)
class E2EReport:
    """Aggregated three-layer report over the golden set."""

    generated_at: str
    model: str
    case_verdicts: list[CaseVerdict] = field(default_factory=list)
    layer_metrics: dict[str, float] = field(default_factory=dict)
    channels_used: list[str] = field(default_factory=list)


def _num_equal(
    actual: Any, expected: Any, tolerance: str | None
) -> tuple[bool, bool]:
    """Compare two numeric cells honouring the case tolerance.

    Returns ``(equal, unit_equivalent)``: the second flag is set only when
    equality held after a x100 / /100 unit conversion (percent vs ratio) —
    reported loudly, never silently.
    """
    if actual == expected:
        return True, False
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual == expected, False
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        if tolerance == "float_rel_1e_4" and math.isclose(
            actual, expected, rel_tol=_REL_TOL, abs_tol=_REL_TOL
        ):
            return True, False
        # Unit equivalence (G8b): ratio vs percent differ by exactly 100x.
        for converted, divisor in ((actual * 100.0, 100.0), (actual / 100.0, 100.0)):
            if math.isclose(converted, expected, rel_tol=_REL_TOL, abs_tol=_REL_TOL):
                return True, True
        return False, False
    return False, False


def _normalize_cell(value: Any) -> tuple[int, Any]:
    """Type-tag a cell so per-row sorting is type-stable (ints before text)."""
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (2, float(value))
    return (3, _canonical_label(str(value)))


def _normalize_row(row: list[Any]) -> list[tuple[int, Any]]:
    """Column-order-insensitive row shape (cells sorted by type-tag + value)."""
    return sorted(_normalize_cell(v) for v in row)


def _expected_sql_is_ordered(expected_sql: str) -> bool:
    """True when the expected SQL sorts rows (ORDER BY on the outer query)."""
    import sqlglot
    from sqlglot import exp as sqlglot_exp

    try:
        statements = sqlglot.parse(expected_sql, dialect="duckdb")
    except Exception:  # noqa: BLE001 - treat unparsable as unordered
        return False
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return False
    root = statements[0]
    node = root
    while isinstance(node, sqlglot_exp.Subquery):
        node = node.this
    if isinstance(node, sqlglot_exp.SetOperation):
        return bool(root.args.get("order"))
    return isinstance(node, sqlglot_exp.Select) and node.args.get("order") is not None


def compare_results(
    actual_rows: list[list[Any]],
    actual_rowcount: int,
    expected: dict[str, Any],
    tolerance: str | None,
    expected_sql: str,
    actual_columns: list[str] | None = None,
) -> tuple[bool, str]:
    """Semantic equivalence of executed rows against expected_result.

    Rules: row count must match; extra actual columns are projected away
    when the expected columns are all present by name (column-superset
    projection, G8b); rows compare as cell multisets; row order matters
    only when the expected SQL has an outer ORDER BY; numeric cells honour
    ``tolerance`` plus a loudly-flagged x100 unit equivalence. Shape
    equivalence (long vs wide) is explicitly NOT performed.
    """
    expected_rows = expected.get("rows") or []
    expected_rowcount = expected.get("rowcount", len(expected_rows))
    expected_columns = [str(c) for c in expected.get("columns") or []]
    if actual_columns and expected_columns and len(actual_columns) > len(expected_columns):
        col_index = {c.casefold(): i for i, c in enumerate(actual_columns)}
        if all(c.casefold() in col_index for c in expected_columns):
            # Project extra columns away only when every expected column name
            # matches; free-form aliases never trigger a "missing column"
            # failure — we fall back to the whole-shape comparison instead
            # (G8b: alias wording is legal variation, not a semantic gap).
            picks = [col_index[c.casefold()] for c in expected_columns]
            actual_rows = [[row[i] for i in picks] for row in actual_rows]
    if actual_rowcount != expected_rowcount or len(actual_rows) != len(expected_rows):
        return (
            False,
            f"行数不一致:实际 {actual_rowcount} vs 期望 {expected_rowcount}",
        )

    ordered = _expected_sql_is_ordered(expected_sql)
    actual_norm = [_normalize_row(r) for r in actual_rows]
    expected_norm = [_normalize_row(r) for r in expected_rows]
    if not ordered:
        actual_norm = sorted(actual_norm)
        expected_norm = sorted(expected_norm)

    unit_flagged = False
    for row_index, (a_row, e_row) in enumerate(zip(actual_norm, expected_norm)):
        if len(a_row) != len(e_row):
            return False, f"第 {row_index + 1} 行列数不一致"
        for (a_tag, a_val), (e_tag, e_val) in zip(a_row, e_row):
            if a_tag != e_tag:
                return (
                    False,
                    f"第 {row_index + 1} 行单元格类型不一致:{a_tag} vs {e_tag}",
                )
            if a_tag == 2:
                equal, unit_equivalent = _num_equal(a_val, e_val, tolerance)
                if not equal:
                    return (
                        False,
                        f"第 {row_index + 1} 行数值不一致:{a_val} vs {e_val}",
                    )
                unit_flagged = unit_flagged or unit_equivalent
            elif a_val != e_val:
                return (
                    False,
                    f"第 {row_index + 1} 行值不一致:{a_val!r} vs {e_val!r}",
                )
    return True, ("unit_equivalent:数值经 x100//100 单位换算后等价" if unit_flagged else "")


def run_e2e(cfg: Nl2DataConfig, cases: list[Any]) -> E2EReport:
    """Run the three-layer judgement over ``cases`` (golden GoldenCase list).

    Per-case exceptions (LLM errors, timeouts) count as ``error`` and never
    abort the round. Interpretation is always skipped (cost control).
    """
    import os
    from dataclasses import replace

    from eval.recall import evaluate_case
    from nl2data.qa import ask_once

    # Eval-mode determinism (G8): pin the LLM temperature from cfg.eval so
    # interactive and regression sampling stay independently tunable.
    cfg = replace(
        cfg, llm=replace(cfg.llm, temperature=cfg.eval.temperature)
    )

    verdicts: list[CaseVerdict] = []
    channels: list[str] = []
    for index, case in enumerate(cases, start=1):
        verdict = CaseVerdict(index=index, question=case.question, section=case.section)
        # Layer 1: recall (independent retrieve, k=3 view of Recall@3).
        try:
            retrieval = retrieve(case.question, cfg, k=3)
            channels = retrieval.channels_used
            recall = evaluate_case(
                case, [item.table for item in retrieval.items]
            ).recall_at_3
        except Exception as exc:  # noqa: BLE001 - recall failures degrade to 0
            recall = 0.0
            verdict = _replace(verdict, reason=f"L1 检索异常:{exc}")
        verdict = _replace(verdict, l1_recall3=round(recall, 4))

        # Layers 2+3: full pipeline, no interpretation. G8b majority vote:
        # repeat each case ``runs_per_case`` times; L2/L3 verdicts are the
        # majority over the runs, confidence annotated as passes/judged.
        runs = max(1, getattr(cfg.eval, "runs_per_case", 1))
        l2_votes: list[str] = []
        l3_votes: list[str] = []
        last_reason: str | None = None
        last_sql: str | None = None
        for _run in range(runs):
            try:
                outcome = ask_once(case.question, cfg, no_interpret=True)
            except Exception as exc:  # noqa: BLE001 - per-case, per-run errors
                l2_votes.append(STATUS_ERROR)
                l3_votes.append(STATUS_ERROR)
                last_reason = f"管线异常:{exc}"
                continue
            if outcome.clarification is not None:
                l2_votes.append(STATUS_FAIL)
                l3_votes.append("skip")
                last_reason = f"needs_clarification:{outcome.clarification}"
            elif outcome.ok and outcome.execution is not None and outcome.vsql is not None:
                l2_votes.append(STATUS_PASS)
                last_sql = outcome.vsql.sql
                execution = outcome.execution
                if case.expected_result is not None and case.expected_sql:
                    actual_rows = _full_rows(execution, cfg)
                    ok, reason = compare_results(
                        actual_rows,
                        execution.rowcount,
                        case.expected_result,
                        case.result_tolerance,
                        case.expected_sql,
                        actual_columns=list(execution.columns),
                    )
                    l3_votes.append(STATUS_PASS if ok else STATUS_FAIL)
                    if reason:
                        last_reason = reason
                else:
                    l3_votes.append("skip")
            else:
                l2_votes.append(STATUS_ERROR)
                l3_votes.append(STATUS_ERROR)
                last_reason = outcome.failure_reason

        judged = [v for v in l3_votes if v != "skip"]
        passes = sum(1 for v in judged if v == STATUS_PASS)
        confidence = f"{passes}/{len(judged)}" if judged else "0/0"
        verdict = _replace(
            verdict,
            l2=_majority(l2_votes),
            l3=_majority(judged) if judged else None,
            sql=last_sql,
            reason=last_reason,
            confidence=confidence,
        )
        verdicts.append(verdict)

    n = len(verdicts) or 1
    metrics = {
        "l1_recall3_avg": round(sum(v.l1_recall3 for v in verdicts) / n, 4),
        "l2_pass_rate": round(
            sum(1 for v in verdicts if v.l2 == STATUS_PASS) / n, 4
        ),
        "l3_pass_rate": round(
            sum(1 for v in verdicts if v.l3 == STATUS_PASS)
            / (sum(1 for v in verdicts if v.l3 is not None) or 1),
            4,
        ),
        "l3_pass_of_total": round(
            sum(1 for v in verdicts if v.l3 == STATUS_PASS) / n, 4
        ),
    }
    return E2EReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        model=os.environ.get("LLM_MODEL", "unknown"),
        case_verdicts=verdicts,
        layer_metrics=metrics,
        channels_used=channels,
    )


def _full_rows(execution: Any, cfg: Nl2DataConfig) -> list[list[Any]]:
    """All executed rows for L3 comparison, reading detail_ref when spilled.

    ``execution.rows`` is a 20-row sample (T11); result sets larger than the
    sample spill their full rows (bounded by the guard LIMIT) to a scratch
    Parquet referenced by ``detail_ref``. Comparison must use the full rows
    whenever they exist, otherwise big results can never match.
    """
    ref = getattr(execution, "detail_ref", None)
    if ref and getattr(execution, "rowcount", 0) > len(execution.rows):
        import pandas as pd

        path = Path(ref)
        if not path.is_absolute():
            path = cfg.paths.data_dir.parent / path
        if path.is_file():
            frame = pd.read_parquet(path)
            return [list(row) for row in frame.itertuples(index=False, name=None)]
    return [[row.get(c) for c in execution.columns] for row in execution.rows]


def _majority(votes: list[str]) -> str:
    """Leading status over run votes (ties resolved by status order)."""
    if not votes:
        return STATUS_ERROR
    counts: dict[str, int] = {}
    for vote in votes:
        counts[vote] = counts.get(vote, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == STATUS_PASS))[0]


def _replace(verdict: CaseVerdict, **changes: Any) -> CaseVerdict:
    """Frozen-dataclass replace shortcut."""
    from dataclasses import replace

    return replace(verdict, **changes)


def report_to_dict(report: E2EReport) -> dict[str, Any]:
    """JSON-friendly serialization for baselines and diffs."""
    return {
        "generated_at": report.generated_at,
        "model": report.model,
        "channels_used": report.channels_used,
        "layer_metrics": report.layer_metrics,
        "cases": [
            {
                "index": v.index,
                "question": v.question,
                "section": v.section,
                "l1_recall3": v.l1_recall3,
                "l2": v.l2,
                "l3": v.l3,
                "confidence": v.confidence,
                "sql": v.sql,
                "reason": v.reason,
            }
            for v in report.case_verdicts
        ],
    }


def diff_against_baseline(
    report: E2EReport, baseline_path: Path
) -> list[str]:
    """Human-readable per-case changes of ``report`` vs a saved baseline."""
    if not baseline_path.is_file():
        return ["(无基线,本次为首次运行;用 --save-baseline 固化)"]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    base_cases = {c["index"]: c for c in baseline.get("cases", [])}
    lines: list[str] = []
    base_metrics = baseline.get("layer_metrics", {})
    for key, value in report.layer_metrics.items():
        old = base_metrics.get(key)
        if old is not None and old != value:
            lines.append(f"[指标] {key}: {old} -> {value}")
    for verdict in report.case_verdicts:
        old = base_cases.get(verdict.index)
        if old is None:
            lines.append(
                f"[新增] #{verdict.index} {verdict.question[:24]}: l2={verdict.l2} l3={verdict.l3}"
            )
            continue
        changes = []
        for layer in ("l1_recall3", "l2", "l3"):
            if old.get(layer) != getattr(verdict, layer):
                changes.append(f"{layer}: {old.get(layer)} -> {getattr(verdict, layer)}")
        if changes:
            lines.append(
                f"[变化] #{verdict.index} {verdict.question[:24]}: "
                + ";".join(changes)
            )
    if not lines:
        lines.append("(与基线完全一致,零变化)")
    return lines
