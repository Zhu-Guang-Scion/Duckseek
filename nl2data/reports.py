"""Shared report rendering for CLI output (keeps nl2data.cli lean)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from eval.recall import RecallReport


def print_recall_report(console: Console, report: RecallReport) -> None:
    """Print overall metrics, per-section breakdown and failing cases."""
    metrics = report.metrics
    console.print(
        f"channels: [bold]{'+'.join(report.channels_used)}[/bold] | "
        f"cases: {len(report.case_results)}\n"
        f"Recall@1={metrics.get('recall_at_1', 0):.3f}  "
        f"Recall@3={metrics.get('recall_at_3', 0):.3f}  "
        f"Recall@5={metrics.get('recall_at_5', 0):.3f}  "
        f"MRR={metrics.get('mrr', 0):.3f}"
    )
    if report.by_section:
        console.print("[bold]per section:[/bold]")
        for section, values in report.by_section.items():
            console.print(
                f"  {section}: n={int(values['count'])} "
                f"R@3={values['recall_at_3']:.3f} MRR={values['mrr']:.3f}"
            )
    failures = [case for case in report.case_results if case.recall_at_3 < 1.0]
    if failures:
        console.print(f"[yellow]cases below Recall@3=1.0: {len(failures)}[/yellow]")
        for case in failures:
            missing = sorted(set(case.expected) - set(case.got[:3]))
            console.print(
                f"  - {case.question}\n    expected={case.expected} got_top3="
                f"{case.got[:3]} missing_top3={missing}"
            )
    else:
        console.print("[green]all cases at Recall@3 = 1.0[/green]")
