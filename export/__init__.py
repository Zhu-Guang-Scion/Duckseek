"""Query-artifact export orchestration (milestone 6, T20).

One :func:`run_export` call produces the two-file artifact bundle of
decision 10 under ``data/exports/<timestamp>_<hash>/``:
``result.xlsx`` (data + meta + native chart) and ``manifest.json`` (agent
contract surface). The chart decision degrades honestly and never blocks
the export; artifacts are kept until manually removed (unlike the scratch
spill's 72h policy) because they face downstream systems.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from export.chart_spec import ChartSpec, decide_chart_spec
from export.manifest import write_manifest
from export.xlsx import write_xlsx
from nl2data.config import Nl2DataConfig
from nl2data.qa import QaOutcome

__all__ = ["ExportResult", "run_export"]


@dataclass(frozen=True)
class ExportResult:
    """Everything callers need to report one export bundle."""

    dir: Path
    xlsx_path: Path
    manifest_path: Path
    chart_spec: ChartSpec | None
    chart_error: str | None
    exported_rows: int
    total_rows: int
    truncated: bool


def _export_dir(cfg: Nl2DataConfig) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return cfg.export.dir / f"{stamp}_{uuid.uuid4().hex[:8]}"


def run_export(
    question: str,
    outcome: QaOutcome,
    cfg: Nl2DataConfig,
    *,
    target_dir: Path | None = None,
) -> ExportResult:
    """Export one successful ask outcome to xlsx + manifest.

    Args:
        question: The natural-language question that produced the outcome.
        outcome: A successful :class:`QaOutcome` (``outcome.ok`` true).
        cfg: Active configuration (export dir, row caps, chart switch).
        target_dir: Optional explicit bundle directory (CLI ``/export xlsx
            <目录>``); defaults to a fresh timestamped folder under
            ``cfg.export.dir``.

    Raises:
        ValueError: When ``outcome`` carries no successful execution — the
            caller (CLI / MCP) is expected to export only answered questions.
    """
    execution = outcome.execution
    if execution is None or execution.status != "ok" or outcome.vsql is None:
        msg = "导出需要一次成功执行的问答结果"
        raise ValueError(msg)
    spec, chart_error = decide_chart_spec(question, execution, cfg)
    out_dir = target_dir if target_dir is not None else _export_dir(cfg)
    xlsx_path = out_dir / "result.xlsx"
    manifest_path = out_dir / "manifest.json"
    exported_rows, truncated = write_xlsx(
        xlsx_path,
        question=question,
        sql=outcome.vsql.sql,
        source_tables=outcome.retrieved_tables,
        execution=execution,
        chart_spec=spec,
        chart_error=chart_error,
        cfg=cfg,
    )
    write_manifest(
        manifest_path,
        question=question,
        sql=outcome.vsql.sql,
        source_tables=outcome.retrieved_tables,
        execution=execution,
        exported_rows=exported_rows,
        truncated=truncated,
        chart_spec=spec,
        chart_error=chart_error,
        artifacts={
            "xlsx": str(xlsx_path.resolve()),
            "manifest": str(manifest_path.resolve()),
        },
    )
    return ExportResult(
        dir=out_dir,
        xlsx_path=xlsx_path,
        manifest_path=manifest_path,
        chart_spec=spec,
        chart_error=chart_error,
        exported_rows=exported_rows,
        total_rows=execution.rowcount,
        truncated=truncated,
    )
