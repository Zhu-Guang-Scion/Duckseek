"""LLM-decided chart specification for exported xlsx (milestone 6, T20).

One :func:`decide_chart_spec` call sends the result schema (columns, dtypes,
profile hints, sample rows) through :func:`llm.chat.chat` under a JSON
contract and returns a validated :class:`ChartSpec`. The LLM only *proposes*;
a deterministic validator gates every field against the actual result
columns (goals.md decision 10) — invalid proposals degrade honestly to
``(None, reason)`` and the export proceeds chart-less, never blocked.

v1 chart types: ``bar`` / ``line`` / ``pie`` only. ``scatter`` is a reserved
extension slot (dimension+measures shape does not map to x/y) and is
rejected by the validator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from exec.runner import ExecutionResult
from llm.chat import ENV_API_KEY as LLM_KEY_ENV
from llm.chat import ENV_BASE_URL as LLM_URL_ENV
from llm.chat import ENV_MODEL as LLM_MODEL_ENV
from llm.chat import LlmError, chat
from nl2data.config import Nl2DataConfig
from retrieval.embedding import ENV_API_KEY as EMB_KEY_ENV
from retrieval.embedding import ENV_BASE_URL as EMB_URL_ENV
from retrieval.embedding import ENV_MODEL as EMB_MODEL_ENV

#: The six credential env vars (goals.md decisions 3+8), values never persisted.
_ENV_KEYS: tuple[str, ...] = (
    LLM_URL_ENV, LLM_KEY_ENV, LLM_MODEL_ENV, EMB_URL_ENV, EMB_KEY_ENV, EMB_MODEL_ENV,
)

#: v1 supported chart types (decision 10; scatter deliberately excluded).
CHART_TYPES: tuple[str, ...] = ("bar", "line", "pie")

_SAMPLE_ROWS_IN_PROMPT = 5

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "chart_type": {"type": "string", "enum": list(CHART_TYPES)},
        "dimension": {"type": "string"},
        "measures": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"},
        "top_n": {"type": "integer"},
    },
    "required": ["chart_type", "dimension", "measures", "title"],
}

_SYSTEM_PROMPT = """\
你是图表规格判定器。根据查询结果的结构(列名/类型/样本)与用户问题,选择最合适\
的图表规格,输出一个 JSON 对象:
{"chart_type": "bar|line|pie", "dimension": "<分组/时间列名>", \
"measures": ["<数值列名>", ...], "title": "<图表标题>", "top_n": <条数>}

规则:
1. chart_type 只能是 bar、line、pie 三种(没有 scatter,没有其他)。
2. dimension 必须是类别型或时间型列;measures 必须是数值型列。
3. bar/line:measures 至少 1 个,dimension 必填;pie:恰好 1 个 measure。
4. top_n 控制类别数量(长尾取前 N,默认 15 以内);序列完整时不截断可给全量条数。
5. 结果结构不适合画图(如纯文本单值、无类别列)时,输出 \
{"chart_type": "none"},不要硬凑。
6. title 用问题语义命名,不超过 40 字。"""

_FEW_SHOTS: tuple[tuple[str, str], ...] = (
    (
        "用户问题:各行政区的平均车费对比\n"
        "结果列:borough(text) avg_fare(numeric) orders(numeric)\n"
        "样本行:borough=Manhattan avg_fare=19.5 orders=102938",
        '{"chart_type": "bar", "dimension": "borough", "measures": ["avg_fare"], '
        '"title": "各行政区平均车费", "top_n": 15}',
    ),
    (
        "用户问题:月度订单量走势\n"
        "结果列:month(text,形如 2026-03) orders(numeric)\n"
        "样本行:month=2026-01 orders=300120",
        '{"chart_type": "line", "dimension": "month", "measures": ["orders"], '
        '"title": "月度订单量走势", "top_n": 24}',
    ),
)


@dataclass(frozen=True)
class ChartSpec:
    """One validated chart proposal (all fields already checked)."""

    chart_type: str
    dimension: str
    measures: tuple[str, ...]
    title: str
    top_n: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation for the manifest and MCP payload."""
        return {
            "chart_type": self.chart_type,
            "dimension": self.dimension,
            "measures": list(self.measures),
            "title": self.title,
            "top_n": self.top_n,
        }


def _column_role(value: Any) -> str:
    """Classify one sample scalar into numeric / categorical / datetime."""
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return "categorical"
    if isinstance(value, (datetime, date)):
        return "datetime"
    if isinstance(value, (int, float, Decimal)):
        return "numeric"
    return "categorical"


def column_roles(execution: ExecutionResult) -> dict[str, str]:
    """Role per column probed from the first non-null inline sample value.

    Columns whose samples are all null default to ``categorical``; the
    validator then simply refuses them as measures (no numeric evidence).
    """
    roles: dict[str, str] = {}
    for column in execution.columns:
        first = next(
            (row.get(column) for row in execution.rows if row.get(column) is not None),
            None,
        )
        roles[column] = _column_role(first)
    return roles


def _describe_columns(execution: ExecutionResult) -> str:
    """Human-readable column list with roles for the prompt."""
    roles = column_roles(execution)
    return "\n".join(f"{name}({roles[name]})" for name in execution.columns)


def _sample_lines(execution: ExecutionResult) -> str:
    if not execution.rows:
        return "(无样本行)"
    head = execution.rows[:_SAMPLE_ROWS_IN_PROMPT]
    return "\n".join(str(row) for row in head)


def validate_spec(
    candidate: dict[str, Any], execution: ExecutionResult, cfg: Nl2DataConfig
) -> tuple[ChartSpec | None, str | None]:
    """Deterministically gate an LLM proposal against the actual result.

    Returns ``(spec, None)`` when valid, else ``(None, reason)`` — the caller
    degrades honestly instead of drawing an unfaithful chart.
    """
    if not isinstance(candidate, dict):
        return None, "图表规格不是 JSON 对象"
    chart_type = candidate.get("chart_type")
    if chart_type == "none":
        return None, "模型判定结果结构不适合画图"
    if chart_type not in CHART_TYPES:
        return None, f"不支持的图型:{chart_type!r}(v1 仅 bar/line/pie)"
    dimension = candidate.get("dimension")
    measures = candidate.get("measures")
    columns = set(execution.columns)
    roles = column_roles(execution)
    if not isinstance(dimension, str) or dimension not in columns:
        return None, f"维度列不存在:{dimension!r}"
    if roles.get(dimension) not in ("categorical", "datetime"):
        return None, f"维度列必须是类别/时间型:{dimension} 是 {roles.get(dimension)}"
    if not isinstance(measures, list) or not measures:
        return None, "measures 为空或不是列表"
    for measure in measures:
        if not isinstance(measure, str) or measure not in columns:
            return None, f"度量列不存在:{measure!r}"
        if roles.get(measure) != "numeric":
            return None, f"度量列必须是数值型:{measure} 是 {roles.get(measure)}"
    if chart_type == "pie" and len(measures) != 1:
        return None, f"pie 图恰好 1 个度量,收到 {len(measures)} 个"
    title = candidate.get("title")
    if not isinstance(title, str) or not title.strip():
        title = f"{dimension} × {'+'.join(measures)}"
    top_n = candidate.get("top_n", cfg.export.top_n_default)
    if not isinstance(top_n, int | bool) or isinstance(top_n, bool):
        top_n = cfg.export.top_n_default
    top_n = max(1, min(top_n, cfg.export.top_n_default))
    return (
        ChartSpec(
            chart_type=chart_type,
            dimension=dimension,
            measures=tuple(measures),
            title=title.strip()[:60],
            top_n=top_n,
        ),
        None,
    )


def _scrub(text: str) -> str:
    """Replace any configured credential value occurring in ``text``.

    The LLM layer already redacts its own errors; this is the export
    boundary's sweep so no env value can reach disk via ``chart_error``.
    """
    for key in _ENV_KEYS:
        value = os.environ.get(key, "")
        if value and value in text:
            text = text.replace(value, "***")
    return text


def decide_chart_spec(
    question: str,
    execution: ExecutionResult,
    cfg: Nl2DataConfig,
) -> tuple[ChartSpec | None, str | None]:
    """Ask the LLM for a chart proposal and validate it.

    Returns ``(spec, None)`` on success or ``(None, reason)`` on any
    degradation path (LLM error, unparseable reply, validator rejection,
    chart_llm disabled). Never raises: the export must not be blocked.
    """
    if not cfg.export.chart_llm:
        return None, "chart_llm 已关闭(纯数据导出)"
    if not execution.rows:
        return None, "结果无行,不画图"
    few_shot = "\n\n".join(
        f"示例输入:\n{q}\n示例输出:\n{a}" for q, a in _FEW_SHOTS
    )
    user = "\n".join(
        [
            f"用户问题:{question}",
            f"结果列:\n{_describe_columns(execution)}",
            f"样本行:\n{_sample_lines(execution)}",
            "",
            few_shot,
        ]
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    try:
        result = chat(messages, json_schema=_JSON_SCHEMA, cfg=cfg)
    except LlmError as exc:
        return None, _scrub(f"LLM 判定调用失败({exc.category}):{exc}")
    candidate = result.parsed
    if candidate is None:
        return None, "LLM 返回无法按 JSON 解析"
    return validate_spec(candidate, execution, cfg)
