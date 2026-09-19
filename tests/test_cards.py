"""Tests for table card generation (catalog/cards.py)."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pandas as pd
import pytest

from catalog.cards import build_card, build_cards, write_card_files
from catalog.profiler import run_profiles
from nl2data.config import CardsConfig, Nl2DataConfig
from retrieval.tokens import estimate_tokens


def _rich_frame() -> pd.DataFrame:
    """A frame with enum, numeric, temporal, truncated-enum and null columns."""
    return pd.DataFrame(
        {
            "订单ID": [1, 2, 3, 4],
            "城市": ["上海", "北京", "上海", "广州"],
            "金额": [10.5, 20.0, 15.5, None],
            "下单日期": pd.to_datetime(
                ["2026-01-02", "2026-01-01", None, "2026-03-01"]
            ),
            "备注": ["长" * 60, "短", None, "也短"],
        }
    )


@pytest.fixture()
def profiled_table(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> str:
    """Ingest the rich frame, profile it, and return the clean table name."""
    table = ingest_factory("orders", _rich_frame())
    run_profiles(config)
    return table


def _manual_profile() -> dict[str, Any]:
    """A hand-built profile exercising enum truncation and stats variants."""
    return {
        "table": "t1",
        "original_name": "表一",
        "source": "src",
        "rows": 3,
        "profiled_at": "2026-01-01T00:00:00+00:00",
        "columns": [
            {
                "name": "long_enum",
                "original_name": "长枚举",
                "dtype": "VARCHAR",
                "null_rate": 0.0,
                "distinct_count": 1,
                "enum_values": ["x" * 51],
                "sample_values": ["x" * 60],
            },
            {
                "name": "amount",
                "original_name": "金额",
                "dtype": "BIGINT",
                "null_rate": 0.25,
                "distinct_count": 2,
                "min": 1,
                "max": 5,
                "quantiles": {"p25": 1.5, "p50": 2.0, "p75": 4.0},
                "sample_values": [1, 5],
            },
            {
                "name": "note",
                "original_name": "备注",
                "dtype": "VARCHAR",
                "null_rate": 0.0,
                "distinct_count": 60,
                "avg_len": 4.5,
                "sample_values": ["a", "b"],
            },
        ],
    }


def test_build_cards_end_to_end(
    config: Nl2DataConfig, profiled_table: str
) -> None:
    """Ingest -> profile -> build_cards produces both card artifacts."""
    notes_path = config.paths.table_notes
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(
        f"# 表: {profiled_table}\n订单流水表,记录每笔订单。\n",
        encoding="utf-8",
    )

    cards = build_cards(config)
    assert [card["table"] for card in cards] == [profiled_table]
    card = cards[0]
    assert card["original_name"] == "orders"
    assert card["source"] == "orders"
    assert card["rows"] == 4
    assert "T" in card["generated_at"]  # ISO8601 timestamp
    assert card["profile_ref"] == f"data/catalog/profiles/{profiled_table}.json"
    assert card["description"] == "订单流水表,记录每笔订单。"
    assert card["terms"] == []
    assert card["token_estimate"] > 0
    assert "sampled" not in card  # profile-only key stays out of the card

    json_path = config.paths.cards_dir / f"{profiled_table}.json"
    md_path = config.paths.cards_md_dir / f"{profiled_table}.md"
    assert json_path.is_file()
    assert md_path.is_file()
    stored = json.loads(json_path.read_text(encoding="utf-8"))
    assert stored == card  # JSON round-trips the built card exactly

    # Optional-key and stats rules match the profile column by column.
    profile = json.loads(
        (config.paths.profiles_dir / f"{profiled_table}.json").read_text(
            encoding="utf-8"
        )
    )
    profile_cols = {col["name"]: col for col in profile["columns"]}
    card_cols = {col["name"]: col for col in card["columns"]}
    assert set(card_cols) == set(profile_cols)
    for name, pcol in profile_cols.items():
        ccol = card_cols[name]
        assert ("enum_values" in ccol) == ("enum_values" in pcol)
        assert ("sample_values" in ccol) == ("sample_values" in pcol)
        assert set(ccol["stats"]) == set(pcol) & {"min", "max", "quantiles", "avg_len"}
        assert ccol["description"] is None

    # Column-level spot checks against the frozen contract.
    order = card_cols["ding_dan_id"]
    assert order["stats"] == {
        "min": 1,
        "max": 4,
        "quantiles": {
            "p25": pytest.approx(1.75),
            "p50": pytest.approx(2.5),
            "p75": pytest.approx(3.25),
        },
    }
    assert "enum_values" not in order
    city = card_cols["cheng_shi"]
    assert city["enum_values"] == ["上海", "北京", "广州"]
    assert set(city["stats"]) == {"avg_len"}
    remark = card_cols["bei_zhu"]
    assert any(
        value == "长" * 50 + "…" for value in remark["enum_values"]
    )  # over-long enum value truncated
    assert remark["sample_values"][0] == "长" * 60  # samples stay untouched

    markdown = md_path.read_text(encoding="utf-8")
    assert f"# 表:{profiled_table}(原名:orders)" in markdown
    assert "- 来源:orders | 行数:4 | 说明:订单流水表,记录每笔订单。" in markdown
    assert "原名:" in markdown
    assert "城市" in markdown  # Chinese original column names survive
    assert "枚举: 上海, 北京, 广州" in markdown
    assert "样本: 1, 2, 3, 4" in markdown
    assert "适用术语:(无)" in markdown
    assert "已折叠" not in markdown  # 5 columns < default fold limit


def test_build_card_unit_rules(config: Nl2DataConfig) -> None:
    """build_card honours enum truncation, stats collapse and description=None."""
    card, markdown = build_card(_manual_profile(), None, [], config)
    assert card["description"] is None
    assert card["profile_ref"] == "data/catalog/profiles/t1.json"
    cols = {col["name"]: col for col in card["columns"]}
    assert all("stats" in col for col in card["columns"])

    long_enum = cols["long_enum"]
    assert long_enum["enum_values"] == ["x" * 50 + "…"]
    assert long_enum["sample_values"] == ["x" * 60]
    assert long_enum["stats"] == {}  # no stat keys, but the key stays

    amount = cols["amount"]
    assert amount["stats"] == {
        "min": 1,
        "max": 5,
        "quantiles": {"p25": 1.5, "p50": 2.0, "p75": 4.0},
    }
    assert "enum_values" not in amount  # no enum, samples only
    assert "样本: 1, 5" in markdown

    note = cols["note"]
    assert note["stats"] == {"avg_len": 4.5}
    assert "适用术语:(无)" in markdown
    assert "说明:(无)" in markdown


def test_build_card_markdown_fold(config: Nl2DataConfig) -> None:
    """A tight fold limit lists only the first columns plus a fold note."""
    small = replace(
        config,
        cards=CardsConfig(enum_value_max_chars=50, markdown_column_fold_limit=2),
    )
    _, markdown = build_card(_manual_profile(), "说明文本", [], small)
    assert "> 共 3 列,已折叠其余 1 列(完整清单见 JSON 卡片)" in markdown
    rows = [
        line
        for line in markdown.splitlines()
        if line.startswith(("| long_enum", "| amount", "| note"))
    ]
    assert [row.split("|")[1].strip() for row in rows] == ["long_enum", "amount"]
    assert "说明:说明文本" in markdown


def test_build_card_terms_injection(config: Nl2DataConfig) -> None:
    """Passed terms land in the JSON card and the markdown terms line."""
    terms = [
        {
            "term": "gmv",
            "synonyms": ["成交额", "营业额"],
            "description": "成交总额",
            "maps_to": {
                "table": "t1",
                "column": "amount",
                "expression": "",
                "filter": "",
            },
        }
    ]
    card, markdown = build_card(_manual_profile(), None, terms, config)
    assert card["terms"] == terms
    assert "适用术语:gmv(别名:成交额/营业额)→ amount" in markdown


def test_build_card_token_estimate_monotonic(config: Nl2DataConfig) -> None:
    """A longer description never shrinks the token estimate."""
    base_card, _ = build_card(_manual_profile(), None, [], config)
    longer_card, longer_md = build_card(
        _manual_profile(), "很长的补充说明" * 40, [], config
    )
    assert longer_card["token_estimate"] >= base_card["token_estimate"]
    assert longer_card["token_estimate"] > 0
    assert longer_card["token_estimate"] == estimate_tokens(
        longer_md, config.tokens
    )


def test_write_card_files_round_trip(config: Nl2DataConfig) -> None:
    """write_card_files stores the exact card dict and markdown text."""
    card, markdown = build_card(_manual_profile(), "说明", [], config)
    json_path, md_path = write_card_files(card, markdown, config)
    assert json_path == config.paths.cards_dir / "t1.json"
    assert md_path == config.paths.cards_md_dir / "t1.md"
    assert json.loads(json_path.read_text(encoding="utf-8")) == card
    assert md_path.read_text(encoding="utf-8") == markdown


def test_build_cards_incremental_skip(
    config: Nl2DataConfig, profiled_table: str
) -> None:
    """A fresh card is not rebuilt on the next run."""
    first = build_cards(config)
    assert [card["table"] for card in first] == [profiled_table]
    assert build_cards(config) == []


def test_build_cards_rebuilds_touched_profile(
    config: Nl2DataConfig, profiled_table: str
) -> None:
    """Touching the profile file marks only that table stale."""
    build_cards(config)
    profile_path = config.paths.profiles_dir / f"{profiled_table}.json"
    future = time.time() + 30
    os.utime(profile_path, (future, future))
    rebuilt = build_cards(config)
    assert [card["table"] for card in rebuilt] == [profiled_table]


def test_build_cards_force_rebuilds(
    config: Nl2DataConfig, profiled_table: str
) -> None:
    """force=True rebuilds even fresh cards."""
    build_cards(config)
    rebuilt = build_cards(config, force=True)
    assert [card["table"] for card in rebuilt] == [profiled_table]


def test_build_cards_tables_filter(
    config: Nl2DataConfig, profiled_table: str
) -> None:
    """The tables filter selects which profiles get built."""
    built = build_cards(config, tables=[profiled_table])
    assert [card["table"] for card in built] == [profiled_table]
    assert build_cards(config, tables=["not_profiled"]) == []


def test_dangling_profile_skipped_with_warning(
    config: Nl2DataConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A profile without a catalog table is skipped with a warning."""
    config.paths.profiles_dir.mkdir(parents=True, exist_ok=True)
    (config.paths.profiles_dir / "ghost.json").write_text(
        json.dumps({"table": "ghost", "rows": 0, "columns": []}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        cards = build_cards(config)
    assert cards == []
    assert "ghost" in caplog.text
    assert "WARNING" in caplog.text


def test_corrupt_profile_skipped_with_error(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unparsable profile JSON logs an error and does not abort the run."""
    table = ingest_factory("corrupt", pd.DataFrame({"a": [1]}))
    run_profiles(config)
    profile_path = config.paths.profiles_dir / f"{table}.json"
    profile_path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        cards = build_cards(config)
    assert cards == []
    assert "corrupt" in caplog.text
    assert "ERROR" in caplog.text
