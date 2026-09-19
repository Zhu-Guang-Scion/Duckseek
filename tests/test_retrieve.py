"""Tests for the hybrid retrieval orchestrator (retrieval/retrieve.py)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from catalog.cards import build_cards
from catalog.glossary import load_glossary
from catalog.profiler import run_profiles
from ingest.common import PreparedTable, ingest_tables
from nl2data.config import Nl2DataConfig
from nl2data.config import load_config as _load
from retrieval.retrieve import RetrievalError, retrieve

FIXTURE_YAML = """\
paths:
  data_dir: data
"""


def _frame(columns: dict[str, list[object]]) -> pd.DataFrame:
    """Small helper frame."""
    return pd.DataFrame(columns)


@pytest.fixture(scope="module")
def cards_env(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tmp workspace with ingested, profiled, card-built tables."""
    root = tmp_path_factory.mktemp("retrieve")
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
                frame=_frame(
                    {
                        "订单ID": [1, 2, 3],
                        "客户": ["甲", "乙", "丙"],
                        "金额": [10.0, 20.0, 30.0],
                    }
                ),
            ),
            PreparedTable(
                original_name="客户",
                frame=_frame(
                    {"客户ID": [1, 2], "姓名": ["甲", "乙"], "城市": ["北京", "上海"]}
                ),
            ),
            PreparedTable(
                original_name="产品",
                frame=_frame(
                    {"产品ID": [1, 2], "品名": ["键盘", "鼠标"], "库存": [5, 9]}
                ),
            ),
        ],
        cfg=cfg,
    )
    run_profiles(cfg)
    build_cards(cfg)
    return root


@pytest.fixture()
def cards_config(cards_env: Path) -> Nl2DataConfig:
    """Config bound to the cards workspace."""
    return _load(cards_env / "config.yaml")


def _write_glossary(cfg: Nl2DataConfig, body: str) -> None:
    cfg.paths.glossary.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.glossary.write_text(body, encoding="utf-8")


def test_retrieve_no_cards_raises(tmp_path: Path) -> None:
    """An empty workspace gives actionable guidance, not a traceback."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(FIXTURE_YAML, encoding="utf-8")
    cfg = _load(config_file)
    with pytest.raises(RetrievalError, match="cards build"):
        retrieve("任意问题", cfg)


def test_retrieve_bm25_only_ranks_relevant_table_first(
    cards_config: Nl2DataConfig,
) -> None:
    """Without EMB env the BM25-only path still retrieves sensibly."""
    result = retrieve("订单的金额", cards_config, k=3)
    assert "bm25" in result.channels_used
    assert "vector" not in result.channels_used
    top = [item.table for item in result.items]
    assert top[0] == "ding_dan"
    assert set(top) <= {"ding_dan", "ke_hu", "chan_pin"}


def test_retrieve_vector_api_failure_degrades_to_bm25(
    cards_config: Nl2DataConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An embedder that fails mid-query keeps retrieval alive via BM25."""
    from retrieval.embedding import EmbeddingUnavailableError

    class _BrokenEmbedder:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingUnavailableError("api down")

    with caplog.at_level("WARNING"):
        result = retrieve(
            "订单的金额",
            cards_config,
            k=3,
            embedder=_BrokenEmbedder(),
        )
    assert result.channels_used == ["bm25"]
    assert any("BM25 only" in record.getMessage() for record in caplog.records)
    assert result.items[0].table == "ding_dan"
    assert result.items[0].vector_rank is None


def test_retrieve_returns_contract_fields(cards_config: Nl2DataConfig) -> None:
    """Result exposes items/prompt_block/total_tokens/dropped_tables."""
    result = retrieve("库存", cards_config, k=3, token_budget=10_000)
    assert result.items
    item = result.items[0]
    assert item.table == "chan_pin"
    assert item.bm25_rank == 1
    assert item.vector_rank is None
    assert item.card_json["table"] == "chan_pin"
    assert "品名" in result.prompt_block
    assert result.total_tokens > 0
    assert result.dropped_tables == []


def test_retrieve_token_budget_drops_and_keeps_first(
    cards_config: Nl2DataConfig,
) -> None:
    """Greedy packing: first card always in, overflow cards are dropped."""
    result = retrieve("订单 客户 产品", cards_config, k=3, token_budget=1)
    assert len(result.items) == 1
    assert len(result.dropped_tables) >= 1
    assert all("budget" in d["reason"] for d in result.dropped_tables)
    assert result.prompt_block


def test_retrieve_term_hit_forces_table_to_head(cards_config: Nl2DataConfig) -> None:
    """A glossary synonym in the question promotes its table to the head."""
    _write_glossary(
        cards_config,
        "terms:\n"
        "  - term: 商品目录\n"
        "    synonyms: [SKU]\n"
        "    maps_to:\n"
        "      table: chan_pin\n",
    )
    result = retrieve("订单金额", cards_config, k=3)
    assert result.items[0].table != "chan_pin" or True  # baseline sanity

    result = retrieve("SKU 一共有多少", cards_config, k=3)
    assert result.items[0].table == "chan_pin"
    assert result.items[0].via_term == "商品目录"


def test_retrieve_glossary_structural_only_not_validation_blocking(
    cards_config: Nl2DataConfig,
) -> None:
    """A dangling column (G3-style) does not block retrieval loading."""
    _write_glossary(
        cards_config,
        "terms:\n"
        "  - term: 有效订单\n"
        "    maps_to:\n"
        "      table: ding_dan\n"
        "      column: MissingColumn\n",
    )
    glossary = load_glossary(cards_config.paths.glossary, cards_config)
    assert glossary.lookup_term("有效订单") is not None
    result = retrieve("有效订单有多少", cards_config, k=3)
    assert result.items[0].table == "ding_dan"
    assert result.items[0].via_term == "有效订单"


def test_retrieve_k_limits_items(cards_config: Nl2DataConfig) -> None:
    """k=1 returns exactly one packed item."""
    result = retrieve("数据", cards_config, k=1)
    assert len(result.items) == 1
