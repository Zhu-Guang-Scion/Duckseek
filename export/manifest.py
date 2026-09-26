"""Write the machine-readable export manifest (milestone 6, T20).

The manifest is the agent-facing contract surface (decision 10): a stable,
self-describing JSON placed beside the xlsx. ``version`` is mandatory and
only bumps on breaking semantic changes — additive keys keep version 1.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from exec.runner import ExecutionResult
from export.chart_spec import ChartSpec, column_roles
from nl2data import __version__

#: Contract version; additive changes stay at 1, breaking changes bump.
MANIFEST_VERSION = 1


def describe_columns(execution: ExecutionResult) -> list[dict[str, str]]:
    """[{name, dtype, role}] per result column, probed from inline samples."""
    roles = column_roles(execution)
    described: list[dict[str, str]] = []
    for column in execution.columns:
        first = next(
            (row.get(column) for row in execution.rows if row.get(column) is not None),
            None,
        )
        described.append(
            {
                "name": column,
                "dtype": type(first).__name__ if first is not None else "unknown",
                "role": roles.get(column, "categorical"),
            }
        )
    return described


def write_manifest(
    out_path: Path,
    *,
    question: str,
    sql: str,
    source_tables: list[str],
    execution: ExecutionResult,
    exported_rows: int,
    truncated: bool,
    chart_spec: ChartSpec | None,
    chart_error: str | None,
    artifacts: dict[str, str],
) -> Path:
    """Assemble and write the manifest JSON; returns its path."""
    payload: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "question": question,
        "sql": sql,
        "source_tables": source_tables,
        "columns": describe_columns(execution),
        "truncated": truncated,
        "total_rows": execution.rowcount,
        "exported_rows": exported_rows,
        "chart_spec": chart_spec.to_dict() if chart_spec else None,
        "chart_error": chart_error,
        "artifacts": artifacts,
        "tool_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_path
