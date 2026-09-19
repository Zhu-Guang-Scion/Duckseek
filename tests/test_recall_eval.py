"""Fixture recall evaluation: the hard Recall@3 = 1.0 gate (T7)."""

from __future__ import annotations

import pandas as pd
import pytest

from catalog.cards import build_cards
from catalog.profiler import run_profiles
from eval.golden import GoldenCase
from eval.recall import run_recall
from ingest.common import PreparedTable, ingest_tables
from nl2data.config import Nl2DataConfig
from nl2data.config import load_config as _load

FIXTURE_YAML = """\
paths:
  data_dir: data
"""

GLOSSARY = """\
terms:
  - term: 商品目录
    synonyms: [SKU]
    maps_to:
      table: chan_pin
  - term: 用户
    synonyms: [会员]
    maps_to:
      table: ke_hu
"""

# Cases cover: exact column names, term hits, synonyms, cross-table joins,
# distractor tables, sample/enum values from profiles.
FIXTURE_CASES = [
    GoldenCase(question="库存还有多少?", expected_tables=["chan_pin"], section="列名"),
    GoldenCase(question="品名列表", expected_tables=["chan_pin"], section="列名"),
    GoldenCase(question="金额总和", expected_tables=["ding_dan"], section="列名"),
    GoldenCase(question="城市分布", expected_tables=["ke_hu"], section="列名"),
    GoldenCase(
        question="SKU 一共有多少个", expected_tables=["chan_pin"], section="术语"
    ),
    GoldenCase(
        question="会员的姓名是什么", expected_tables=["ke_hu"], section="同义词"
    ),
    GoldenCase(
        question="订单里每个客户的金额", expected_tables=["ding_dan", "ke_hu"], section="跨表"
    ),
    GoldenCase(
        question="每个城市卖出的产品品名", expected_tables=["ke_hu", "chan_pin"], section="跨表"
    ),
    GoldenCase(question="键盘还有货吗", expected_tables=["chan_pin"], section="样本值"),
    GoldenCase(
        question="北京的客户有谁", expected_tables=["ke_hu"], section="枚举值"
    ),
]


@pytest.fixture(scope="module")
def eval_env(tmp_path_factory: pytest.TempPathFactory) -> Nl2DataConfig:
    """A workspace with three tables, cards and a small glossary."""
    root = tmp_path_factory.mktemp("recall_eval")
    config_file = root / "config.yaml"
    config_file.write_text(FIXTURE_YAML, encoding="utf-8")
    cfg = _load(config_file)
    ingest_tables(
        source_name="sales",
        source_type="excel",
        source_path=root / "sales.xlsx",
        tables=[
            PreparedTable(
                original_name="订单",
                frame=pd.DataFrame(
                    {
                        "订单ID": [1, 2, 3],
                        "客户": ["甲", "乙", "丙"],
                        "金额": [10.0, 20.0, 30.0],
                    }
                ),
            ),
            PreparedTable(
                original_name="客户",
                frame=pd.DataFrame(
                    {"客户ID": [1, 2], "姓名": ["甲", "乙"], "城市": ["北京", "上海"]}
                ),
            ),
            PreparedTable(
                original_name="产品",
                frame=pd.DataFrame(
                    {"产品ID": [1, 2], "品名": ["键盘", "鼠标"], "库存": [5, 9]}
                ),
            ),
        ],
        cfg=cfg,
    )
    run_profiles(cfg)
    build_cards(cfg)
    cfg.paths.glossary.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.glossary.write_text(GLOSSARY, encoding="utf-8")
    return cfg


def test_fixture_recall_at_3_is_one_hundred(eval_env: Nl2DataConfig) -> None:
    """Hard gate: fixture Recall@3 must be exactly 1.0 (BM25 + terms)."""
    known = {"ding_dan", "ke_hu", "chan_pin"}
    report = run_recall(eval_env, FIXTURE_CASES, known_tables=known)
    assert len(FIXTURE_CASES) >= 8
    assert report.metrics["recall_at_3"] == 1.0, [
        (c.question, c.expected, c.got[:3]) for c in report.case_results
    ]
    assert report.metrics["mrr"] > 0.5
    assert report.channels_used == ["bm25"]


def test_section_breakdown_populated(eval_env: Nl2DataConfig) -> None:
    """Sections aggregate counts and metrics."""
    report = run_recall(
        eval_env, FIXTURE_CASES, known_tables={"ding_dan", "ke_hu", "chan_pin"}
    )
    assert set(report.by_section) == {"列名", "术语", "同义词", "跨表", "样本值", "枚举值"}
    assert sum(int(v["count"]) for v in report.by_section.values()) == len(
        FIXTURE_CASES
    )


def test_unknown_table_fails_before_running(
    eval_env: Nl2DataConfig,
) -> None:
    """Cases referencing unknown tables are rejected up front."""
    from eval.recall import RecallEvalError

    bad = [GoldenCase(question="?", expected_tables=["ghost"])]
    with pytest.raises(RecallEvalError, match="ghost"):
        run_recall(eval_env, bad, known_tables={"ding_dan"})
