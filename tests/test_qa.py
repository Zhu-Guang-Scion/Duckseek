"""Tests for the T12 ask pipeline (nl2data/qa.py) and audit trail."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from catalog.cards import build_cards
from catalog.profiler import run_profiles
from ingest.common import PreparedTable, ingest_tables
from llm.chat import LlmError
from nl2data.audit import load_recent_events, write_audit_event
from nl2data.cli import app
from nl2data.config import Nl2DataConfig
from nl2data.config import load_config as _load
from nl2data.qa import ask_once
from retrieval.retrieve import RetrievalResult
from sqlgen.generate import SQLGeneration

runner = CliRunner()
FIXTURE_YAML = """\
paths:
  data_dir: data
"""


@pytest.fixture(scope="module")
def qa_env(tmp_path_factory: pytest.TempPathFactory) -> Nl2DataConfig:
    """Workspace with two ingested tables, profiles and cards."""
    root = tmp_path_factory.mktemp("qa")
    config_file = root / "config.yaml"
    config_file.write_text(FIXTURE_YAML, encoding="utf-8")
    cfg = _load(config_file)
    ingest_tables(
        source_name="shop",
        source_type="excel",
        source_path=root / "shop.xlsx",
        tables=[
            PreparedTable(
                original_name="订单",
                frame=pd.DataFrame(
                    {
                        "订单ID": [1, 2, 3, 4],
                        "金额": [10.0, 20.0, 30.0, 40.0],
                        "城市": ["北京", "上海", "北京", "上海"],
                    }
                ),
            ),
        ],
        cfg=cfg,
    )
    run_profiles(cfg)
    build_cards(cfg)
    return cfg


@pytest.fixture()
def qa_config(qa_env: Nl2DataConfig, tmp_path: Path) -> Nl2DataConfig:
    """Per-test config copy sharing the workspace (isolated audit dir)."""
    from dataclasses import replace

    return replace(qa_env, paths=replace(qa_env.paths, audit_dir=tmp_path / "audit"))


class _FakeGenerate:
    """Sequenced generate stub: returns queued SQLGenerations in order."""

    def __init__(self, returns: list[SQLGeneration]) -> None:
        self.returns = list(returns)
        self.calls: list[str | None] = []

    def __call__(
        self,
        question: str,
        retrieval: RetrievalResult,
        cfg: Nl2DataConfig,
        feedback: str | None = None,
    ) -> SQLGeneration:
        self.calls.append(feedback)
        return self.returns.pop(0)


def _ok(sql: str) -> SQLGeneration:
    return SQLGeneration(sql=sql, needs_clarification=False, clarification=None,
                         candidates_tried=1)


def _clar(text: str) -> SQLGeneration:
    return SQLGeneration(sql=None, needs_clarification=True, clarification=text,
                         candidates_tried=1)


def test_ask_once_end_to_end_ok(
    qa_config: Nl2DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mocked LLM + real DuckDB: full pipeline asserts on every layer."""
    fake = _FakeGenerate(
        [_ok("SELECT cheng_shi, sum(jin_e) AS total FROM ding_dan GROUP BY cheng_shi")]
    )
    monkeypatch.setattr("nl2data.qa.generate", fake)

    outcome = ask_once("按城市汇总金额", qa_config, no_interpret=True)

    assert outcome.ok
    assert outcome.attempts == 1
    assert outcome.execution.rowcount == 2
    assert outcome.vsql is not None and outcome.vsql.limit == 500
    assert set(outcome.retrieved_tables) == {"ding_dan"}

    events = load_recent_events(qa_config, 5)
    assert len(events) == 1
    event = events[0]
    assert event["question"] == "按城市汇总金额"
    assert event["guard_outcome"] == "passed"
    assert event["status"] == "ok"
    assert event["rowcount"] == 2
    assert event["sql"].startswith("SELECT cheng_shi")
    assert {"ts", "usage", "latency_ms", "attempts"} <= set(event)


def test_ask_once_retry_after_guard_rejection(qa_config, monkeypatch) -> None:  # noqa: ANN001
    """Bad SQL is fed back once, then a good SQL succeeds."""
    fake = _FakeGenerate([
        _ok("SELECT ghost_column FROM ding_dan"),  # rejected by guard
        _ok("SELECT count(*) AS n FROM ding_dan"),  # fixed
    ])
    monkeypatch.setattr("nl2data.qa.generate", fake)

    outcome = ask_once("多少订单", qa_config, no_interpret=True)

    assert outcome.ok
    assert outcome.attempts == 2
    assert fake.calls[0] is None
    assert fake.calls[1] is not None and "unknown_column" in fake.calls[1]
    assert outcome.execution.rows[0]["n"] == 4


def test_ask_once_retry_exhausted_shows_reason(qa_config, monkeypatch) -> None:  # noqa: ANN001
    """All attempts failing keeps the reason and never executes."""
    fake = _FakeGenerate([_ok("SELECT ghost FROM ding_dan")] * 3)
    monkeypatch.setattr("nl2data.qa.generate", fake)

    outcome = ask_once("多少订单", qa_config, no_interpret=True)

    assert not outcome.ok
    assert outcome.execution is None
    assert "护栏拒绝" in outcome.failure_reason
    assert outcome.attempts == 3
    event = load_recent_events(qa_config, 1)[0]
    assert event["guard_outcome"] == "rejected"


def test_ask_once_needs_clarification(qa_config, monkeypatch) -> None:  # noqa: ANN001
    """Clarification short-circuits before any SQL is validated."""
    monkeypatch.setattr(
        "nl2data.qa.generate", _FakeGenerate([_clar("请问要哪个时间范围?")])
    )
    outcome = ask_once("随便问问", qa_config, no_interpret=True)

    assert outcome.clarification == "请问要哪个时间范围?"
    assert outcome.vsql is None and outcome.execution is None
    event = load_recent_events(qa_config, 1)[0]
    assert event["guard_outcome"] == "clarification"


def test_ask_once_interpret_degrades_on_llm_error(qa_config, monkeypatch) -> None:  # noqa: ANN001
    """Interpretation LLM failure degrades to data-only rendering."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise LlmError("api down", "api")

    monkeypatch.setattr("nl2data.qa.generate", _FakeGenerate([
        _ok("SELECT count(*) AS n FROM ding_dan")
    ]))
    monkeypatch.setattr("nl2data.qa.chat", _boom)

    outcome = ask_once("多少订单", qa_config)

    assert outcome.ok
    assert outcome.interpretation is None
    assert outcome.interpretation_failed


def test_audit_module_roundtrip_and_tolerance(qa_config: Nl2DataConfig) -> None:
    """JSONL write/read roundtrip tolerates corrupt lines and missing dir."""
    assert load_recent_events(qa_config, 10) == []
    write_audit_event(qa_config, {"question": "q1", "status": "ok"})
    write_audit_event(qa_config, {"question": "q2", "status": "ok"})
    day_file = next(iter(qa_config.paths.audit_dir.glob("*.jsonl")))
    day_file.write_text(
        day_file.read_text(encoding="utf-8") + "{corrupt json\n",
        encoding="utf-8",
    )
    events = load_recent_events(qa_config, 10)
    assert [e["question"] for e in events] == ["q2", "q1"]


def test_ask_cli_single_shot(qa_env: Nl2DataConfig, monkeypatch) -> None:  # noqa: ANN001
    """`nl2data ask "<question>"` renders SQL, stats and the audit trail."""
    monkeypatch.setattr("nl2data.qa.generate", _FakeGenerate([
        _ok("SELECT avg(jin_e) AS avg_amount FROM ding_dan")
    ]))
    config_file = qa_env.paths.data_dir.parent / "config.yaml"
    result = runner.invoke(app, ["ask", "平均金额", "--no-interpret", "--config",
                                 str(config_file)])
    assert result.exit_code == 0, result.output
    assert "avg_amount" in result.output
    assert "实际执行的 SQL" in result.output
    assert "行数 1" in result.output

    result = runner.invoke(app, ["audit", "5", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "平均金额" in result.output
    assert "event(s)" in result.output
