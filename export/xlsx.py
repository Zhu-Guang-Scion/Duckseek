"""Write the exported .xlsx artifact: data sheet + meta sheet + native chart.

The workbook is the human-facing artifact (decision 10): ``data`` carries the
typed rows (frozen header, autofilter, sized columns), ``meta`` carries
provenance (question / SQL / source tables / counts / tool version) plus the
mandatory truncation disclosure row, and a validated :class:`ChartSpec`
renders as a genuine openpyxl chart on a dedicated sheet — editable in Excel
afterwards. Truncation is always visible, never silent.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from exec.runner import ExecutionResult
from export.chart_spec import ChartSpec
from nl2data import __version__
from nl2data.config import Nl2DataConfig

#: Hard cap for auto-computed column widths (characters).
_MAX_COLUMN_WIDTH = 50


def _jsonable(value: Any) -> Any:
    """Convert pandas/numpy scalars into native Python cell values."""
    if value is None or value != value:  # None or NaN
        return None
    if hasattr(value, "item"):  # numpy scalar → python scalar
        return value.item()
    return value


def _resolve_detail(execution: ExecutionResult, cfg: Nl2DataConfig) -> Path | None:
    """Resolve the spill Parquet path of ``detail_ref`` when it exists."""
    if not execution.detail_ref:
        return None
    src = Path(execution.detail_ref)
    if not src.is_absolute():
        src = cfg.paths.data_dir.parent / src
    return src if src.is_file() else None


def load_rows(
    execution: ExecutionResult, cfg: Nl2DataConfig
) -> tuple[list[dict[str, Any]], bool]:
    """Full exportable rows: spill Parquet first, else inline sample.

    Returns ``(rows, truncated)`` — rows are capped at
    ``cfg.export.max_rows`` and ``truncated`` is True exactly when the total
    result had more rows than exported.
    """
    detail = _resolve_detail(execution, cfg)
    if detail is not None:
        frame = pd.read_parquet(detail)
        records: list[dict[str, Any]] = []
        for record in frame.to_dict("records"):
            records.append({key: _jsonable(value) for key, value in record.items()})
    else:
        records = [
            {key: _jsonable(value) for key, value in row.items()} for row in execution.rows
        ]
    exported = records[: max(int(cfg.export.max_rows), 1)]
    return exported, execution.rowcount > len(exported)


def _write_data_sheet(
    wb: Workbook, columns: list[str], rows: list[dict[str, Any]]
) -> Any:
    """The typed table: frozen header, autofilter, sized columns."""
    ws = wb.active
    ws.title = "data"
    header_font = Font(bold=True)
    for index, name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=index, value=name)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for row_index, record in enumerate(rows, start=2):
        for col_index, name in enumerate(columns, start=1):
            ws.cell(row=row_index, column=col_index, value=record.get(name))
    widths: dict[str, int] = {name: len(str(name)) for name in columns}
    for record in rows[:100]:
        for name in columns:
            value = record.get(name)
            if value is not None:
                widths[name] = max(widths[name], len(str(value)))
    for index, name in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(index)].width = (
            min(widths[name] + 2, _MAX_COLUMN_WIDTH)
        )
    if rows:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = (
            f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
        )
    return ws


def _meta_rows(
    *,
    question: str,
    sql: str,
    source_tables: list[str],
    execution: ExecutionResult,
    exported_rows: int,
    truncated: bool,
    chart_spec: ChartSpec | None,
    chart_error: str | None,
) -> list[tuple[str, Any, bool]]:
    """Key/value meta rows; the last entry flag marks the disclosure row."""
    if chart_spec is not None:
        chart_note = (
            f"{chart_spec.chart_type} 图:{chart_spec.dimension} × "
            f"{'+'.join(chart_spec.measures)}(top {chart_spec.top_n})——{chart_spec.title}"
        )
    else:
        chart_note = f"无图表({chart_error or '未启用'})"
    rows: list[tuple[str, Any, bool]] = [
        ("问题", question, False),
        ("实际执行的 SQL", sql, False),
        ("检索表", ", ".join(source_tables) or "-", False),
        ("总行数", execution.rowcount, False),
        ("导出行数", exported_rows, False),
        ("是否截断", "是" if truncated else "否", False),
        ("查询耗时(ms)", round(execution.latency_ms), False),
        ("生成时间", datetime.now().astimezone().isoformat(timespec="seconds"), False),
        ("tool_version", __version__, False),
        ("图表说明", chart_note, False),
    ]
    if truncated:
        rows.append(
            (
                "⚠ 已截断",
                f"导出前 {exported_rows} 行 / 共 {execution.rowcount} 行,"
                f"完整结果见明细位置 {execution.detail_ref}",
                True,
            )
        )
    return rows


def _write_meta_sheet(wb: Workbook, rows: list[tuple[str, Any, bool]]) -> None:
    ws = wb.create_sheet("meta")
    key_font = Font(bold=True)
    warn_font = Font(bold=True, color="FF9C0006")
    for index, (key, value, warn) in enumerate(rows, start=1):
        key_cell = ws.cell(row=index, column=1, value=key)
        key_cell.font = warn_font if warn else key_font
        value_cell = ws.cell(row=index, column=2, value=value)
        if warn:
            value_cell.font = warn_font
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 90
    # SQL stays single-line readable: wrap instead of one endless row.
    ws.cell(row=2, column=2).alignment = Alignment(wrap_text=True, vertical="top")


def _add_chart_sheet(
    wb: Workbook, ws_data: Any, columns: list[str], rows: list[dict[str, Any]],
    spec: ChartSpec,
) -> None:
    """Native openpyxl chart over the exported slice (top_n rows at most)."""
    chart_classes = {"bar": BarChart, "line": LineChart, "pie": PieChart}
    chart = chart_classes[spec.chart_type]()
    chart.title = spec.title
    chart.height = 10
    chart.width = 22
    slice_rows = min(len(rows), max(spec.top_n, 1))
    dim_col = columns.index(spec.dimension) + 1
    for measure in spec.measures:
        col = columns.index(measure) + 1
        data = Reference(ws_data, min_col=col, min_row=1, max_row=slice_rows + 1)
        chart.add_data(data, titles_from_data=True)
    chart.set_categories(
        Reference(ws_data, min_col=dim_col, min_row=2, max_row=slice_rows + 1)
    )
    ws_chart = wb.create_sheet("chart")
    ws_chart.add_chart(chart, "A1")


def write_xlsx(
    out_path: Path,
    *,
    question: str,
    sql: str,
    source_tables: list[str],
    execution: ExecutionResult,
    chart_spec: ChartSpec | None,
    chart_error: str | None,
    cfg: Nl2DataConfig,
) -> tuple[int, bool]:
    """Write ``out_path`` and return ``(exported_rows, truncated)``.

    The chart (if any) is drawn on the exported slice; any truncation is
    disclosed on the meta sheet (advisor-mandated visibility).
    """
    rows, truncated = load_rows(execution, cfg)
    columns = list(execution.columns) if execution.columns else list(rows[0]) if rows else []
    wb = Workbook()
    ws_data = _write_data_sheet(wb, columns, rows)
    meta = _meta_rows(
        question=question,
        sql=sql,
        source_tables=source_tables,
        execution=execution,
        exported_rows=len(rows),
        truncated=truncated,
        chart_spec=chart_spec,
        chart_error=chart_error,
    )
    _write_meta_sheet(wb, meta)
    if chart_spec is not None and rows:
        _add_chart_sheet(wb, ws_data, columns, rows, chart_spec)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return len(rows), truncated
