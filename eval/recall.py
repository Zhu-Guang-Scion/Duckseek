"""Retrieval recall evaluation against the golden set (T7).

Runs :func:`retrieval.retrieve.retrieve` for every golden case and reports
Recall@1/@3/@5 plus MRR, overall and per section. Uses the G2 schema
validator; table-existence problems are surfaced before running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eval.golden import GoldenCase, validate_golden_tables
from nl2data.config import Nl2DataConfig
from retrieval.retrieve import RetrievalError, retrieve


class RecallEvalError(RuntimeError):
    """Raised when the evaluation cannot run (bad golden / missing catalog)."""


@dataclass(frozen=True)
class CaseResult:
    """One golden case's retrieval outcome."""

    question: str
    section: str | None
    expected: list[str]
    got: list[str]
    hit_ranks: list[int] = field(default_factory=list)  # 1-based, per expected table
    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    recall_at_5: float = 0.0
    mrr: float = 0.0


@dataclass(frozen=True)
class RecallReport:
    """Aggregated metrics with per-section breakdown."""

    case_results: list[CaseResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    by_section: dict[str, dict[str, float]] = field(default_factory=dict)
    channels_used: list[str] = field(default_factory=list)


def _recall_at(got: list[str], expected: list[str], k: int) -> float:
    """Fraction of expected tables present in the top-k retrieved tables."""
    if not expected:
        return 0.0
    top = set(got[:k])
    return len(top.intersection(expected)) / len(expected)


def evaluate_case(case: GoldenCase, got: list[str]) -> CaseResult:
    """Score one case against its retrieved table list."""
    ranks: list[int] = []
    for table in case.expected_tables:
        rank = got.index(table) + 1 if table in got else 0
        ranks.append(rank)
    hit_ranks = [r for r in ranks if r]
    return CaseResult(
        question=case.question,
        section=case.section,
        expected=case.expected_tables,
        got=got,
        hit_ranks=hit_ranks,
        recall_at_1=_recall_at(got, case.expected_tables, 1),
        recall_at_3=_recall_at(got, case.expected_tables, 3),
        recall_at_5=_recall_at(got, case.expected_tables, 5),
        mrr=(1.0 / min(hit_ranks) if hit_ranks else 0.0),
    )


def run_recall(
    cfg: Nl2DataConfig,
    cases: list[GoldenCase],
    *,
    known_tables: set[str],
    k: int = 5,
) -> RecallReport:
    """Evaluate all golden cases and aggregate metrics.

    Args:
        cfg: Active configuration.
        cases: Loaded golden cases (schema-validated upstream).
        known_tables: Catalog table names; cases referencing unknown tables
            are rejected before any retrieval runs.
        k: Retrieval depth per query (metrics still cut at 1/3/5).

    Returns:
        The aggregated report.

    Raises:
        RecallEvalError: On unknown tables or when no cards exist.
    """
    problems = validate_golden_tables(cases, known_tables)
    if problems:
        raise RecallEvalError("\n".join(problems))

    results: list[CaseResult] = []
    channels: list[str] = []
    for case in cases:
        try:
            result = retrieve(case.question, cfg, k=k)
        except RetrievalError as exc:
            raise RecallEvalError(str(exc)) from exc
        channels = result.channels_used
        results.append(evaluate_case(case, [item.table for item in result.items]))

    n = len(results) or 1
    metrics: dict[str, float] = {
        key: round(sum(getattr(r, key) for r in results) / n, 4)
        for key in ("recall_at_1", "recall_at_3", "recall_at_5", "mrr")
    }
    sections: dict[str, dict[str, float]] = {}
    section_names: list[str | None] = []
    for result in results:
        if result.section not in section_names:
            section_names.append(result.section)
    for name in section_names:
        group = [r for r in results if r.section == name]
        label = name or "(未分节)"
        sections[label] = {
            "count": float(len(group)),
            **{
                key: round(sum(getattr(r, key) for r in group) / len(group), 4)
                for key in ("recall_at_1", "recall_at_3", "recall_at_5", "mrr")
            },
        }
    return RecallReport(
        case_results=results,
        metrics=metrics,
        by_section=sections,
        channels_used=channels,
    )


def report_to_dict(report: RecallReport) -> dict[str, Any]:
    """Serialize a report into a JSON-friendly dict."""
    return {
        "metrics": report.metrics,
        "by_section": report.by_section,
        "channels_used": report.channels_used,
        "cases": [
            {
                "question": case.question,
                "section": case.section,
                "expected": case.expected,
                "got": case.got,
                "hit_ranks": case.hit_ranks,
                "recall_at_3": case.recall_at_3,
            }
            for case in report.case_results
        ],
    }


def load_golden_for_eval(path: Path) -> list[GoldenCase]:
    """Load and schema-validate the golden file for evaluation.

    Raises:
        RecallEvalError: On schema problems (reusing the G2 validator).
    """
    from eval.golden import load_and_validate

    try:
        return load_and_validate(path)
    except Exception as exc:  # noqa: BLE001 - surfaced to the CLI uniformly
        raise RecallEvalError(str(exc)) from exc
