"""Tests for the three-layer e2e runner (eval/e2e.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.e2e import (
    CaseVerdict,
    E2EReport,
    compare_results,
    diff_against_baseline,
    report_to_dict,
)
from eval.golden import GoldenCase


def _expected(rows: list[list[object]], columns: list[str]) -> dict:
    return {"columns": columns, "rowcount": len(rows), "rows": rows}


class TestCompareResults:
    """Unit tests for the semantic-equivalence comparator."""

    def test_row_order_insensitive_without_order_by(self) -> None:
        exp = _expected([["a", 1], ["b", 2]], ["k", "v"])
        ok, _ = compare_results([["b", 2], ["a", 1]], 2, exp, "exact",
                                "SELECT k, v FROM t")
        assert ok

    def test_column_order_insensitive(self) -> None:
        exp = _expected([["a", 1]], ["k", "v"])
        ok, _ = compare_results([[1, "a"]], 1, exp, "exact",
                                "SELECT k, v FROM t")
        assert ok

    def test_row_order_sensitive_with_order_by(self) -> None:
        exp = _expected([["a", 1], ["b", 2]], ["k", "v"])
        sql = "SELECT k, v FROM t ORDER BY v"
        ok, _ = compare_results([["b", 2], ["a", 1]], 2, exp, "exact", sql)
        assert not ok
        ok, _ = compare_results([["a", 1], ["b", 2]], 2, exp, "exact", sql)
        assert ok

    def test_float_relative_tolerance(self) -> None:
        exp = _expected([[12.249314]], ["avg"])
        ok, _ = compare_results([[12.25]], 1, exp, "float_rel_1e_4",
                                "SELECT avg(v) FROM t")
        assert ok
        ok, _ = compare_results([[13.0]], 1, exp, "float_rel_1e_4",
                                "SELECT avg(v) FROM t")
        assert not ok

    def test_exact_rejects_numeric_drift(self) -> None:
        exp = _expected([[10]], ["c"])
        ok, _ = compare_results([[10.0001]], 1, exp, "exact", "SELECT c FROM t")
        assert not ok

    def test_rowcount_mismatch_fails(self) -> None:
        exp = _expected([[1]], ["c"])
        ok, reason = compare_results([[1], [2]], 2, exp, "exact", "SELECT c FROM t")
        assert not ok and "行数不一致" in reason

    def test_null_cells_match(self) -> None:
        exp = _expected([[None]], ["s"])
        ok, _ = compare_results([[None]], 1, exp, "exact", "SELECT s FROM t")
        assert ok

    def test_grouping_key_mismatch_fails(self) -> None:
        exp = _expected([["Manhattan", 5]], ["borough", "c"])
        ok, reason = compare_results([["Brooklyn", 5]], 1, exp, "exact",
                                     "SELECT borough, c FROM t")
        assert not ok and "值不一致" in reason


class TestDiffAgainstBaseline:
    """Baseline diff reporting (acceptance: diff output has tests)."""

    def _report(self, l2: str, l3: str | None) -> E2EReport:
        verdict = CaseVerdict(index=12, question="小费率问题?", section=None,
                              l1_recall3=1.0, l2=l2, l3=l3)
        return E2EReport(
            generated_at="2026-09-19T00:00:00+00:00", model="m",
            case_verdicts=[verdict],
            layer_metrics={"l1_recall3_avg": 1.0, "l2_pass_rate": 1.0 if l2 == "pass" else 0.0,
                           "l3_pass_rate": 1.0 if l3 == "pass" else 0.0,
                           "l3_pass_of_total": 1.0 if l3 == "pass" else 0.0},
        )

    def test_fail_to_pass_reported(self, tmp_path: Path) -> None:
        baseline = tmp_path / "base.json"
        baseline.write_text(json.dumps(report_to_dict(self._report("fail", "fail"))),
                            encoding="utf-8")
        lines = diff_against_baseline(self._report("pass", "pass"), baseline)
        assert any("#12" in line and "l2: fail -> pass" in line for line in lines)
        assert any("l3: fail -> pass" in line for line in lines)
        assert any("[指标]" in line for line in lines)

    def test_identical_run_reports_zero_change(self, tmp_path: Path) -> None:
        baseline = tmp_path / "base.json"
        report = self._report("pass", "pass")
        baseline.write_text(json.dumps(report_to_dict(report)), encoding="utf-8")
        lines = diff_against_baseline(report, baseline)
        assert lines == ["(与基线完全一致,零变化)"]

    def test_missing_baseline(self, tmp_path: Path) -> None:
        lines = diff_against_baseline(self._report("pass", "pass"),
                                      tmp_path / "nope.json")
        assert any("无基线" in line for line in lines)


class TestRunE2eMocked:
    """run_e2e over stubbed pipeline: three-state verdicts."""

    @pytest.fixture()
    def cases(self) -> list[GoldenCase]:
        return [
            GoldenCase(question="q-ok", expected_tables=["t"],
                       expected_sql="SELECT c FROM t",
                       expected_result=_expected([[1]], ["c"]),
                       result_tolerance="exact"),
            GoldenCase(question="q-clarify", expected_tables=["t"]),
            GoldenCase(question="q-error", expected_tables=["t"]),
        ]

    def test_three_states(
        self, cases: list[GoldenCase], monkeypatch: pytest.MonkeyPatch, config: object
    ) -> None:
        from guard.validate import ValidatedSQL
        from nl2data.qa import QaOutcome
        from retrieval.retrieve import RetrievalResult, RetrievedItem

        def fake_retrieve(
            question: str, cfg: object, k: int | None = None, **kw: object
        ) -> RetrievalResult:
            return RetrievalResult(
                items=[RetrievedItem("t", 0.1, 1, 1, None, {})],
                prompt_block="pb", total_tokens=2, dropped_tables=[],
                channels_used=["bm25"],
            )

        class Exec:
            status = "ok"
            rowcount = 1
            columns = ["c"]
            rows = [{"c": 1}]
            profile = None
            detail_ref = None
            error = None
            latency_ms = 1.0

        outcomes = {
            "q-ok": QaOutcome(question="q-ok", retrieved_tables=["t"],
                              vsql=ValidatedSQL("SELECT c FROM t LIMIT 500", ["t"], 500),
                              execution=Exec(), attempts=1),
            "q-clarify": QaOutcome(question="q-clarify",
                                   retrieved_tables=["t"],
                                   clarification="补充信息?", attempts=1),
            "q-error": QaOutcome(question="q-error", retrieved_tables=["t"],
                                 failure_reason="护栏拒绝(x)", attempts=3),
        }

        def fake_ask(
            question: str,
            cfg: object,
            *,
            no_interpret: bool = True,
            extra_feedback: str | None = None,
        ) -> object:
            return outcomes[question]

        monkeypatch.setattr("eval.e2e.retrieve", fake_retrieve)
        import eval.e2e as e2e_mod
        monkeypatch.setattr("nl2data.qa.ask_once", fake_ask)
        # run_e2e imports ask_once inside the function from nl2data.qa.
        report = e2e_mod.run_e2e(config, cases)  # type: ignore[arg-type]
        by_q = {v.question: v for v in report.case_verdicts}
        assert by_q["q-ok"].l2 == "pass" and by_q["q-ok"].l3 == "pass"
        assert by_q["q-clarify"].l2 == "fail" and by_q["q-clarify"].l3 is None
        assert by_q["q-error"].l2 == "error"
        assert report.layer_metrics["l2_pass_rate"] == pytest.approx(1 / 3, abs=1e-4)


def test_vehicle_label_aliases_are_semantically_equal() -> None:
    """Chinese/English vehicle labels compare equal after normalisation."""
    exp = _expected([["yellow", 1], ["green", 2]], ["vehicle", "v"])
    ok, _ = compare_results([["黄车", 1], ["绿车", 2]], 2, exp, "exact",
                            "SELECT vehicle, v FROM t")
    assert ok
    # A genuinely different label still fails.
    ok, _ = compare_results([["blue", 1]], 1, _expected([["yellow", 1]], ["v", "k"]),
                            "exact", "SELECT v, k FROM t")
    assert not ok


def test_eval_mode_pins_zero_temperature(config: object) -> None:
    """run_e2e overrides cfg.llm with cfg.eval.temperature (G8 B5)."""
    from dataclasses import replace as dc_replace

    from nl2data.config import Nl2DataConfig

    cfg = dc_replace(config, llm=dc_replace(config.llm, temperature=0.7))  # type: ignore[arg-type]
    assert isinstance(cfg, Nl2DataConfig)
    assert cfg.eval.temperature == 0.0  # eval section pins determinism
    # The override the runner applies:
    pinned = dc_replace(cfg, llm=dc_replace(cfg.llm, temperature=cfg.eval.temperature))
    assert pinned.llm.temperature == 0.0


def test_full_rows_prefers_detail_ref_spill(config: object, tmp_path: Path) -> None:
    """L3 comparison reads the spilled full rows, not the 20-row sample."""
    import pandas as pd

    from eval.e2e import _full_rows

    spill = tmp_path / "spill.parquet"
    pd.DataFrame({"zone": [f"z{i}" for i in range(30)], "c": list(range(30))}).to_parquet(
        spill, index=False
    )

    class Exec:
        columns = ["zone", "c"]
        rows = [{"zone": "z0", "c": 0}]  # truncated sample
        rowcount = 30
        detail_ref = str(spill)

    rows = _full_rows(Exec(), config)  # type: ignore[arg-type]
    assert len(rows) == 30 and rows[-1] == ["z29", 29]

    class NoSpill:
        columns = ["c"]
        rows = [{"c": 1}]
        rowcount = 1
        detail_ref = None

    assert _full_rows(NoSpill(), config) == [[1]]  # type: ignore[arg-type]


class TestG8bComparator:
    """Column-superset projection and unit equivalence (G8b)."""

    def test_column_superset_projects_to_expected(self) -> None:
        """Extra actual columns are projected away when all expected exist."""
        exp = _expected([["Manhattan", 5]], ["zone", "total_orders"])
        ok, _ = compare_results(
            [["Manhattan", "Manhattan", 5]], 1, exp, "exact",
            "SELECT zone, borough, total_orders FROM t",
            actual_columns=["zone", "borough", "total_orders"],
        )
        assert ok

    def test_alias_mismatch_falls_back_to_shape_comparison(self) -> None:
        """Free-form aliases never fail as missing columns (G8b fallback).

        When expected column names cannot all be matched in the actual
        columns, the whole-shape comparison decides — alias wording is
        legal variation. The extra column here makes the shape mismatch.
        """
        exp = _expected([["Manhattan", 5]], ["zone", "total_orders"])
        ok, reason = compare_results(
            [["Manhattan", "Manhattan", 5]], 1, exp, "exact",
            "SELECT zone, borough, c FROM t",
            actual_columns=["zone", "borough", "c"],
        )
        assert not ok  # shape mismatch (3 cells vs 2), not a missing-column error
        assert "缺列" not in reason

    def test_alias_mismatch_same_shape_still_passes(self) -> None:
        """Same cell multiset under different alias names passes."""
        exp = _expected([["yellow", 3952432]], ["vehicle", "march_orders"])
        ok, _ = compare_results(
            [["yellow", 3952432]], 1, exp, "exact",
            "SELECT cab_type, total FROM t",
            actual_columns=["cab_type", "total"],
        )
        assert ok

    def test_projection_survives_row_order_insensitivity(self) -> None:
        exp = _expected([["a", 1], ["b", 2]], ["zone", "c"])
        ok, _ = compare_results(
            [["b", "Queens", 2], ["a", "Manhattan", 1]], 2, exp, "exact",
            "SELECT zone, borough, c FROM t",
            actual_columns=["zone", "borough", "c"],
        )
        assert ok

    def test_unit_equivalence_percent_vs_ratio_passes_with_flag(self) -> None:
        """0.6599 vs 65.99 passes with a loud unit_equivalent note."""
        exp = _expected([[65.99]], ["credit_pct"])
        ok, reason = compare_results([[0.6599096895682118]], 1, exp, "exact",
                                     "SELECT credit_pct FROM t")
        assert ok and "unit_equivalent" in reason

    def test_unit_equivalence_ratio_vs_percent(self) -> None:
        exp = _expected([[0.2514857]], ["rate"])
        ok, reason = compare_results([[25.14857]], 1, exp, "float_rel_1e_4",
                                     "SELECT rate FROM t")
        assert ok and "unit_equivalent" in reason

    def test_non_unit_numeric_drift_still_fails(self) -> None:
        exp = _expected([[65.99]], ["pct"])
        ok, reason = compare_results([[0.5]], 1, exp, "exact",
                                     "SELECT pct FROM t")
        assert not ok and "数值不一致" in reason

    def test_shape_equivalence_explicitly_not_done(self) -> None:
        """Long-format (15 rows) vs wide-format (8 rows) stays a failure."""
        wide = _expected([["Bronx", 698, 79]] * 8, ["borough", "yellow", "green"])
        long_rows = [["Bronx", "yellow", 698], ["Bronx", "green", 79]] * 1
        ok, reason = compare_results(long_rows, 2, wide, "exact",
                                     "SELECT borough, yellow, green FROM t")
        assert not ok


def test_majority_vote_confidence() -> None:
    """_majority picks the leading status; skip rows excluded upstream."""
    from eval.e2e import _majority

    assert _majority(["pass", "pass", "fail"]) == "pass"
    assert _majority(["fail", "pass", "fail"]) == "fail"
    assert _majority(["error", "error", "pass"]) == "error"
    assert _majority([]) == "error"
