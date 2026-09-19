"""Golden recall-case loading and validation (milestone 2, G2; reused by T7).

Schema per case (``eval/recall_golden.yaml``)::

    question: str            # required, unique
    expected_tables: [str]   # required, non-empty, clean table names
    note: str | null         # optional

``validate_golden_schema`` is a pure structural check; table existence is a
separate check (``validate_golden_tables``) so the T7 runner can apply it
against a live catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class GoldenError(RuntimeError):
    """Raised when the golden file cannot be loaded or fails validation."""


@dataclass(frozen=True)
class GoldenCase:
    """One retrieval recall case: a question and its expected table set."""

    question: str
    expected_tables: list[str] = field(default_factory=list)
    note: str | None = None
    section: str | None = None
    # M4 optional keys (absent = recall-layer-only judgement, back-compat).
    expected_sql: str | None = None
    expected_result: dict[str, Any] | None = None
    result_tolerance: str | None = None  # "exact" | "float_rel_1e_4"


def load_golden(path: Path) -> list[GoldenCase]:
    """Load golden cases from YAML without structural validation.

    Args:
        path: Path to ``recall_golden.yaml``; a missing file means no cases.

    Returns:
        Cases in file order.

    Raises:
        GoldenError: On unreadable or non-conforming YAML containers.
    """
    if not path.is_file():
        return []
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"invalid YAML in {path}: {exc}"
        raise GoldenError(msg) from exc
    if raw is None:
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        msg = f"{path}: expected a top-level 'cases:' list"
        raise GoldenError(msg)
    def _optional_sql(value: Any) -> str | None:
        """Coerce an optional expected_sql entry to a stripped string."""
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _optional_result(value: Any) -> dict[str, Any] | None:
        """Pass through mapping expected_result entries, else None."""
        return value if isinstance(value, dict) else None

    cases: list[GoldenCase] = []
    for index, item in enumerate(raw["cases"], start=1):
        if not isinstance(item, dict):
            msg = f"{path}: 第 {index} 条用例不是 mapping"
            raise GoldenError(msg)
        cases.append(
            GoldenCase(
                question=str(item.get("question", "")),
                expected_tables=[str(t) for t in item.get("expected_tables") or []],
                note=item.get("note"),
                section=item.get("section"),
                expected_sql=_optional_sql(item.get("expected_sql")),
                expected_result=_optional_result(item.get("expected_result")),
                result_tolerance=item.get("result_tolerance"),
            )
        )
    return cases


def validate_golden_schema(cases: list[GoldenCase]) -> list[str]:
    """Structurally validate loaded cases.

    Returns:
        Problems (empty list = valid): blank questions, empty or non-string
        table lists, duplicate questions, and malformed M4 optional keys
        (expected_sql / expected_result / result_tolerance).
    """
    problems: list[str] = []
    seen: dict[str, int] = {}
    for index, case in enumerate(cases, start=1):
        label = f"第 {index} 条"
        if not case.question.strip():
            problems.append(f"{label}: question 为空")
        if not case.expected_tables:
            problems.append(f"{label}: expected_tables 为空列表")
        if case.note is not None and not isinstance(case.note, str):
            problems.append(f"{label}: note 必须是字符串")
        if case.result_tolerance is not None and case.result_tolerance not in {
            "exact",
            "float_rel_1e_4",
        }:
            problems.append(
                f"{label}: result_tolerance 只能是 exact 或 float_rel_1e_4"
            )
        if (case.expected_result is not None) != (case.expected_sql is not None):
            problems.append(f"{label}: expected_sql 与 expected_result 必须成对出现")
        if case.expected_result is not None:
            problems.extend(_check_result_shape(label, case.expected_result))
        if case.question in seen:
            problems.append(
                f"{label}: question 与第 {seen[case.question]} 条重复:"
                f" {case.question}"
            )
        else:
            seen[case.question] = index
    return problems


def validate_golden_tables(
    cases: list[GoldenCase], known_tables: set[str] | frozenset[str]
) -> list[str]:
    """Check every expected table against the set of known clean table names.

    Returns:
        Problems (empty list = valid), one line per case listing its missing
        tables.
    """
    problems: list[str] = []
    for index, case in enumerate(cases, start=1):
        missing = [t for t in case.expected_tables if t not in known_tables]
        if missing:
            problems.append(
                f"第 {index} 条 ({case.question}): 表 {', '.join(missing)} "
                "不在 catalog 中"
            )
    return problems


def load_and_validate(path: Path) -> list[GoldenCase]:
    """Load the golden file and enforce the schema, raising on any problem.

    Raises:
        GoldenError: With all schema problems joined by newlines.
    """
    cases = load_golden(path)
    problems = validate_golden_schema(cases)
    if problems:
        raise GoldenError("\n".join(problems))
    return cases


def _check_result_shape(label: str, result: dict[str, Any]) -> list[str]:
    """Check the expected_result structure: columns/rows alignment."""
    problems: list[str] = []
    columns = result.get("columns")
    rows = result.get("rows")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        problems.append(f"{label}: expected_result.columns 必须是字符串列表")
        return problems
    if not isinstance(rows, list):
        problems.append(f"{label}: expected_result.rows 必须是列表")
        return problems
    for row_index, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) != len(columns):
            problems.append(
                f"{label}: expected_result 第 {row_index} 行长度与 columns 不对齐"
            )
    rowcount = result.get("rowcount")
    if rowcount is not None and rowcount != len(rows):
        problems.append(f"{label}: expected_result.rowcount 与 rows 数量不一致")
    return problems


def validate_expected_sql(
    cases: list[GoldenCase], known_tables: set[str] | frozenset[str]
) -> list[str]:
    """Validate every expected_sql: single SELECT statement, whitelisted tables.

    Column-existence against the catalog is left to ingestion-time checks;
    this layer pins statement shape and table whitelisting.

    Returns:
        Problems (empty list = valid).
    """
    import sqlglot
    from sqlglot import exp as sqlglot_exp

    problems: list[str] = []
    for index, case in enumerate(cases, start=1):
        if case.expected_sql is None:
            continue
        label = f"第 {index} 条"
        try:
            statements = sqlglot.parse(case.expected_sql, dialect="duckdb")
        except Exception as exc:  # sqlglot parse/tokenize errors
            problems.append(f"{label}: expected_sql 解析失败:{exc}")
            continue
        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            problems.append(f"{label}: expected_sql 必须是单条语句")
            continue
        root = statements[0]
        node = root
        while isinstance(node, sqlglot_exp.Subquery):
            node = node.this
        if not isinstance(node, (sqlglot_exp.Select, sqlglot_exp.SetOperation)):
            problems.append(f"{label}: expected_sql 必须是 SELECT 查询")
            continue
        cte_names = {
            cte.alias_or_name.lower()
            for cte in root.find_all(sqlglot_exp.CTE)
        }
        for table in root.find_all(sqlglot_exp.Table):
            name = table.name.lower()
            if name in cte_names:
                continue
            if name not in {t.lower() for t in known_tables}:
                problems.append(
                    f"{label}: expected_sql 引用未知表 {name}(白名单:"
                    f"{', '.join(sorted(known_tables))})"
                )
    return problems
