"""Tests for the golden recall-case loader/validator (eval/golden.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.golden import (
    GoldenCase,
    GoldenError,
    load_and_validate,
    load_golden,
    validate_expected_sql,
    validate_golden_schema,
    validate_golden_tables,
)

REPO_GOLDEN = Path(__file__).resolve().parent.parent / "eval" / "recall_golden.yaml"


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_repo_golden_file_loads_and_validates() -> None:
    """The shipped recall_golden.yaml: 20 cases, schema-valid, no dupes."""
    cases = load_and_validate(REPO_GOLDEN)
    assert len(cases) == 20
    assert validate_golden_schema(cases) == []
    questions = [case.question for case in cases]
    assert len(questions) == len(set(questions))
    for case in cases:
        assert case.expected_tables
        assert all(isinstance(t, str) and t for t in case.expected_tables)


def test_repo_golden_tables_exist_in_main_catalog() -> None:
    """Every expected_tables entry resolves against the live main catalog."""
    import yaml

    from nl2data.config import load_config

    cfg = load_config()
    if not cfg.paths.catalog.is_file():
        pytest.skip("main catalog.yaml not present in this environment")
    with cfg.paths.catalog.open(encoding="utf-8") as fh:
        known = {
            table["name"]
            for source in yaml.safe_load(fh)["sources"]
            for table in source["tables"]
        }
    cases = load_and_validate(REPO_GOLDEN)
    problems = validate_golden_tables(cases, known)
    assert problems == []


def test_missing_file_and_empty_variants(tmp_path: Path) -> None:
    """Missing/empty files and an empty cases list are valid empty sets."""
    assert load_golden(tmp_path / "nope.yaml") == []
    assert load_golden(_write(tmp_path / "a.yaml", "")) == []
    assert load_golden(_write(tmp_path / "b.yaml", "cases: []")) == []


def test_bad_container_shapes_raise(tmp_path: Path) -> None:
    """Non-mapping roots or non-list cases are container errors."""
    with pytest.raises(GoldenError, match="cases"):
        load_golden(_write(tmp_path / "c.yaml", "- one\n"))
    with pytest.raises(GoldenError, match="cases"):
        load_golden(_write(tmp_path / "d.yaml", "other: 1\n"))
    with pytest.raises(GoldenError, match="第 1 条"):
        load_golden(_write(tmp_path / "e.yaml", "cases:\n  - 42\n"))
    with pytest.raises(GoldenError, match="invalid YAML"):
        load_golden(_write(tmp_path / "f.yaml", "cases: [unclosed\n"))


def test_schema_validation_problems(tmp_path: Path) -> None:
    """Blank question, empty tables and duplicate questions are reported."""
    cases = [
        GoldenCase(question="q1", expected_tables=["t1"]),
        GoldenCase(question="  ", expected_tables=["t1"]),
        GoldenCase(question="q3", expected_tables=[]),
        GoldenCase(question="q1", expected_tables=["t2"]),
    ]
    problems = validate_golden_schema(cases)
    assert any("question 为空" in p for p in problems)
    assert any("expected_tables 为空列表" in p for p in problems)
    assert any("与第 1 条重复" in p for p in problems)


def test_note_defaults_to_none_and_passes(tmp_path: Path) -> None:
    """note is optional and defaults to None."""
    path = _write(
        tmp_path / "g.yaml", "cases:\n  - question: q\n    expected_tables: [t]\n"
    )
    cases = load_and_validate(path)
    assert cases == [GoldenCase(question="q", expected_tables=["t"], note=None)]


def test_table_existence_check(tmp_path: Path) -> None:
    """Missing tables are reported per case with the question quoted."""
    cases = [
        GoldenCase(question="黄车?", expected_tables=["yellow_tripdata", "ghost"]),
        GoldenCase(question="维度?", expected_tables=["taxi_zones"]),
    ]
    problems = validate_golden_tables(cases, {"yellow_tripdata", "taxi_zones"})
    assert len(problems) == 1
    assert "ghost" in problems[0]
    assert "黄车?" in problems[0]


def test_load_and_validate_raises_with_all_problems(tmp_path: Path) -> None:
    """load_and_validate joins every schema problem into one error."""
    path = _write(
        tmp_path / "h.yaml",
        "cases:\n"
        "  - question: q\n"
        "    expected_tables: []\n"
        "  - question: q\n"
        "    expected_tables: [t]\n",
    )
    with pytest.raises(GoldenError) as excinfo:
        load_and_validate(path)
    assert "expected_tables 为空列表" in str(excinfo.value)
    assert "重复" in str(excinfo.value)


def test_m4_optional_keys_roundtrip_and_compat(tmp_path: Path) -> None:
    """expected_sql/result/tolerance load through; absent keys stay None."""
    path = _write(
        tmp_path / "m4.yaml",
        "cases:\n"
        "  - question: q1\n"
        "    expected_tables: [t1]\n"
        "  - question: q2\n"
        "    expected_tables: [t1]\n"
        "    expected_sql: \"SELECT c FROM t1\"\n"
        "    expected_result: {columns: [c], rows: [[1]], rowcount: 1}\n"
        "    result_tolerance: exact\n",
    )
    cases = load_golden(path)
    assert cases[0].expected_sql is None
    assert cases[0].expected_result is None
    assert cases[1].expected_sql == "SELECT c FROM t1"
    assert cases[1].expected_result == {"columns": ["c"], "rows": [[1]], "rowcount": 1}
    assert cases[1].result_tolerance == "exact"
    assert validate_golden_schema(cases) == []


def test_m4_schema_problems(tmp_path: Path) -> None:
    """Malformed M4 keys are reported: bad tolerance, lone keys, bad shapes."""
    cases = [
        GoldenCase(
            question="q1",
            expected_tables=["t1"],
            expected_sql="SELECT c FROM t1",
            expected_result=None,  # lone sql without result
        ),
        GoldenCase(
            question="q2",
            expected_tables=["t1"],
            expected_result={"columns": ["c"], "rows": [[1, 2]]},  # misaligned row
        ),
        GoldenCase(
            question="q3",
            expected_tables=["t1"],
            expected_sql="SELECT c FROM t1",
            expected_result={"columns": ["c"], "rows": [[1]]},
            result_tolerance="bogus",
        ),
    ]
    problems = validate_golden_schema(cases)
    assert any("成对出现" in p for p in problems)
    assert any("长度与 columns 不对齐" in p for p in problems)
    assert any("result_tolerance" in p for p in problems)


def test_validate_expected_sql_shapes_and_whitelist() -> None:
    """expected_sql must be a single SELECT over whitelisted tables."""
    known = {"t1", "t2"}
    ok = [
        GoldenCase(question="q", expected_tables=["t1"],
                   expected_sql="SELECT c FROM t1 JOIN t2 ON t1.i = t2.i"),
        GoldenCase(question="w", expected_tables=["t1"],
                   expected_sql="WITH x AS (SELECT 1 AS i) SELECT i FROM x"),
    ]
    assert validate_expected_sql(ok, known) == []

    bad = [
        GoldenCase(question="m", expected_tables=["t1"],
                   expected_sql="SELECT 1; SELECT 2"),
        GoldenCase(question="d", expected_tables=["t1"],
                   expected_sql="DELETE FROM t1"),
        GoldenCase(question="g", expected_tables=["t1"],
                   expected_sql="SELECT c FROM ghost"),
        GoldenCase(question="p", expected_tables=["t1"],
                   expected_sql="SEL/**/ECT c FROM t1"),
    ]
    problems = validate_expected_sql(bad, known)
    assert any("单条语句" in p for p in problems)
    assert any("SELECT 查询" in p for p in problems)
    assert any("未知表 ghost" in p for p in problems)
    assert any("解析失败" in p for p in problems)
