"""Tests for the CLI report renderer (nl2data/reports.py)."""

from __future__ import annotations

from rich.console import Console

from eval.recall import CaseResult, RecallReport
from nl2data.reports import print_recall_report


def _report(failing: bool) -> RecallReport:
    """A two-case report with or without a failing case."""
    cases = [
        CaseResult(
            question="黄色的订单量",
            section="一、单表精准区分层",
            expected=["yellow_tripdata"],
            got=["yellow_tripdata"],
            hit_ranks=[1],
            recall_at_1=1.0,
            recall_at_3=1.0,
            recall_at_5=1.0,
            mrr=1.0,
        )
    ]
    if failing:
        cases.append(
            CaseResult(
                question="各行政区的对比",
                section="三、双事实表联合对比跨表层",
                expected=["yellow_tripdata", "taxi_zones"],
                got=["green_tripdata", "ke_hu"],
                hit_ranks=[],
            )
        )
    n = len(cases)
    metrics = {
        "recall_at_1": sum(c.recall_at_1 for c in cases) / n,
        "recall_at_3": sum(c.recall_at_3 for c in cases) / n,
        "recall_at_5": sum(c.recall_at_5 for c in cases) / n,
        "mrr": sum(c.mrr for c in cases) / n,
    }
    return RecallReport(
        case_results=cases,
        metrics=metrics,
        by_section={
            "一、单表精准区分层": {"count": 1, "recall_at_3": 1.0, "mrr": 1.0},
            "三、双事实表联合对比跨表层": {
                "count": 1,
                "recall_at_3": 0.0 if failing else 1.0,
                "mrr": 0.0 if failing else 1.0,
            },
        },
        channels_used=["bm25", "vector"],
    )


def test_all_pass_report(capsys: object) -> None:
    """A perfect report prints metrics, sections and the green summary."""
    console = Console()
    print_recall_report(console, _report(failing=False))
    output = console.file.getvalue() if hasattr(console.file, "getvalue") else ""
    if not output:
        import io

        buf = io.StringIO()
        print_recall_report(Console(file=buf, width=200), _report(failing=False))
        output = buf.getvalue()
    assert "Recall@3=1.000" in output
    assert "per section" in output
    assert "all cases at Recall@3 = 1.0" in output


def test_failing_report_lists_missing(capsys: object) -> None:
    """Failing cases are listed with their missing tables."""
    import io

    buf = io.StringIO()
    print_recall_report(Console(file=buf, width=200), _report(failing=True))
    output = buf.getvalue()
    assert "cases below Recall@3=1.0: 1" in output
    assert "各行政区的对比" in output
    assert "missing_top3=['taxi_zones', 'yellow_tripdata']" in output
