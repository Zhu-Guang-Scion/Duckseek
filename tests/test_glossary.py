"""Tests for the business glossary (glossary.yaml load/validate/assembly)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from catalog.glossary import (
    GlossaryError,
    GlossaryStore,
    load_glossary,
    terms_by_table_map,
    validate_glossary,
)
from catalog.store import CatalogStore
from nl2data.config import Nl2DataConfig


def _ingest_orders(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> tuple[str, list[str]]:
    """Ingest a two-column orders table; return (clean table name, clean columns)."""
    table = ingest_factory(
        "orders",
        pd.DataFrame({"订单ID": [1, 2, 3], "金额": [100.0, 50.5, 20.0]}),
    )
    found = CatalogStore(config.paths.catalog).get_table(table)
    assert found is not None
    return table, [column.name for column in found[1].columns]


def _write_glossary(config: Nl2DataConfig, text: str) -> Path:
    """Write glossary content to the config's glossary path (tmp-backed)."""
    path = config.paths.glossary
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _term(term: str, table: str, *, column: str | None = None, **extra: Any) -> dict[str, Any]:
    """Build one ``terms[]`` item in the frozen YAML contract shape."""
    maps: dict[str, Any] = {"table": table}
    if column is not None:
        maps["column"] = column
    maps.update(extra)
    return {"term": term, "maps_to": maps}


def _dump(terms: list[dict[str, Any]]) -> str:
    """Dump a contract-shaped terms list to YAML text (unicode kept)."""
    return yaml.safe_dump({"terms": terms}, allow_unicode=True, sort_keys=False)


def test_load_glossary_full_entry(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """All contract fields round-trip through load_glossary and validate clean."""
    table, columns = _ingest_orders(config, ingest_factory)
    amount = columns[1]
    _write_glossary(
        config,
        _dump(
            [
                {
                    "term": "毛利",
                    "synonyms": ["毛利润", "GM"],
                    "maps_to": {
                        "table": table,
                        "column": amount,
                        "expression": f"{amount} - 100",
                        "filter": "status = 'done'",
                    },
                    "description": "收入减去成本",
                }
            ]
        ),
    )
    store = load_glossary(config.paths.glossary, config)
    assert len(store.entries) == 1
    entry = store.entries[0]
    assert entry.term == "毛利"
    assert entry.synonyms == ["毛利润", "GM"]
    assert entry.description == "收入减去成本"
    assert entry.maps_to.table == table
    assert entry.maps_to.column == amount
    assert entry.maps_to.expression == f"{amount} - 100"
    assert entry.maps_to.filter == "status = 'done'"
    assert validate_glossary(store, config) == []


def test_load_glossary_defaults(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """Missing optional fields default to [] / None, serialized as null."""
    table, _ = _ingest_orders(config, ingest_factory)
    _write_glossary(config, _dump([_term("毛利", table)]))
    entry = load_glossary(config.paths.glossary, config).entries[0]
    assert entry.synonyms == []
    assert entry.description is None
    assert entry.maps_to.column is None
    assert entry.maps_to.expression is None
    assert entry.maps_to.filter is None
    card = entry.to_dict()
    assert card["synonyms"] == []
    assert card["description"] is None
    assert card["maps_to"]["expression"] is None
    assert card["maps_to"]["filter"] is None


def test_lookup_term_variants(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """lookup_term hits terms and synonyms, ignoring case and surrounding spaces."""
    table, _ = _ingest_orders(config, ingest_factory)
    _write_glossary(
        config,
        _dump(
            [
                {"term": "毛利", "synonyms": ["毛利润", "GM"], "maps_to": {"table": table}},
                _term("营收", table),
            ]
        ),
    )
    store: GlossaryStore = load_glossary(config.paths.glossary)
    first = store.entries[0]
    assert store.lookup_term("毛利") is first
    assert store.lookup_term("毛利润") is first
    assert store.lookup_term("  毛利润  ") is first
    assert store.lookup_term("gm") is first  # case-insensitive synonym hit
    assert store.lookup_term("GM") is first
    assert store.lookup_term("不存在") is None


def test_terms_for_table(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """terms_for_table preserves file order and returns [] for unknown tables."""
    table, _ = _ingest_orders(config, ingest_factory)
    _write_glossary(config, _dump([_term("毛利", table), _term("营收", table)]))
    store = load_glossary(config.paths.glossary, config)
    assert [entry.term for entry in store.terms_for_table(table)] == ["毛利", "营收"]
    assert store.terms_for_table("no_such_table") == []


def test_validate_dangling_table_with_candidates(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """A dangling table yields a difflib candidate from the catalog tables."""
    _ingest_orders(config, ingest_factory)
    ingest_factory("sales_2025", pd.DataFrame({"v": [1]}))
    _write_glossary(
        config,
        _dump([{"term": "毛利", "maps_to": {"table": "sales_2024", "column": "gross_profit"}}]),
    )
    store = load_glossary(config.paths.glossary, config)
    errors = validate_glossary(store, config)
    assert len(errors) == 1  # column check skipped while the table itself dangles
    assert errors[0].startswith("术语 毛利 → 表 sales_2024 不存在,候选:")
    assert "sales_2025" in errors[0]


def test_validate_dangling_table_without_candidates(config: Nl2DataConfig) -> None:
    """With an empty catalog every table dangles and no candidate exists."""
    _write_glossary(config, _dump([_term("毛利", "sales_2024")]))
    store = load_glossary(config.paths.glossary, config)
    errors = validate_glossary(store, config)
    assert errors == ["术语 毛利 → 表 sales_2024 不存在,候选:(无相似表)"]


def test_validate_dangling_column_suggests_columns(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """A dangling column suggests candidates from that table's clean columns."""
    table, columns = _ingest_orders(config, ingest_factory)
    amount = columns[1]
    bad_column = amount + "x"
    _write_glossary(
        config,
        _dump([{"term": "毛利", "maps_to": {"table": table, "column": bad_column}}]),
    )
    store = load_glossary(config.paths.glossary, config)
    errors = validate_glossary(store, config)
    assert len(errors) == 1
    assert errors[0].startswith(f"术语 毛利 → 表 {table} 的列 {bad_column} 不存在,候选:")
    assert amount in errors[0]


@pytest.mark.parametrize(
    ("field", "text"),
    [("expression", "amount -"), ("filter", "SELEC x FROM")],
    ids=["bad-expression", "bad-filter"],
)
def test_validate_rejects_invalid_sql(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
    field: str,
    text: str,
) -> None:
    """sqlglot blocks unparseable expression/filter, naming the term and original text."""
    table, columns = _ingest_orders(config, ingest_factory)
    _write_glossary(
        config,
        _dump(
            [
                {
                    "term": "毛利",
                    "maps_to": {"table": table, "column": columns[1], field: text},
                }
            ]
        ),
    )
    store = load_glossary(config.paths.glossary, config)
    errors = validate_glossary(store, config)
    assert len(errors) == 1
    assert errors[0].startswith(f"术语 毛利 的 {field} 不是合法 SQL:{text}(")
    assert errors[0].endswith(")")


def test_validate_accepts_legal_expression_and_filter(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """A well-formed expression and filter pass even with unknown column names."""
    table, columns = _ingest_orders(config, ingest_factory)
    _write_glossary(
        config,
        _dump(
            [
                {
                    "term": "毛利",
                    "maps_to": {
                        "table": table,
                        "column": columns[1],
                        "expression": "amount - cost",
                        "filter": "status = 'done'",
                    },
                }
            ]
        ),
    )
    store = load_glossary(config.paths.glossary, config)
    assert validate_glossary(store, config) == []


def test_load_rejects_duplicate_term(config: Nl2DataConfig) -> None:
    """Duplicate terms fail structural validation at load time, with position."""
    _write_glossary(
        config,
        _dump([_term("毛利", "t1"), _term("毛利", "t2")]),
    )
    with pytest.raises(GlossaryError) as excinfo:
        load_glossary(config.paths.glossary)
    assert "第 2 个术语" in str(excinfo.value)
    assert "重复" in str(excinfo.value)


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        (["not", "a", "mapping"], "根节点"),
        ({"terms": {"a": 1}}, "terms 必须是列表"),
        ({"terms": ["毛利"]}, "必须是 mapping"),
        ({"terms": [{"maps_to": {"table": "t"}}]}, "term"),
        ({"terms": [{"term": "", "maps_to": {"table": "t"}}]}, "term"),
        ({"terms": [{"term": 3, "maps_to": {"table": "t"}}]}, "term"),
        ({"terms": [{"term": "毛利", "synonyms": "GM", "maps_to": {"table": "t"}}]}, "synonyms"),
        ({"terms": [{"term": "毛利", "synonyms": [1], "maps_to": {"table": "t"}}]}, "synonyms"),
        ({"terms": [{"term": "毛利"}]}, "maps_to"),
        ({"terms": [{"term": "毛利", "maps_to": "t"}]}, "maps_to 必须是 mapping"),
        ({"terms": [{"term": "毛利", "maps_to": {}}]}, "maps_to.table"),
        ({"terms": [{"term": "毛利", "maps_to": {"table": "t", "column": 3}}]}, "maps_to.column"),
        ({"terms": [{"term": "毛利", "maps_to": {"table": "t", "filter": []}}]}, "maps_to.filter"),
    ],
    ids=[
        "root-not-mapping",
        "terms-not-list",
        "item-not-mapping",
        "term-missing",
        "term-empty",
        "term-not-str",
        "synonyms-not-list",
        "synonyms-item-not-str",
        "maps-missing",
        "maps-not-mapping",
        "table-missing",
        "column-not-str",
        "filter-not-str",
    ],
)
def test_load_rejects_structural_errors(
    config: Nl2DataConfig,
    payload: Any,
    needle: str,
) -> None:
    """Each malformed shape raises GlossaryError naming the offending field."""
    _write_glossary(config, yaml.safe_dump(payload, allow_unicode=True))
    with pytest.raises(GlossaryError) as excinfo:
        load_glossary(config.paths.glossary)
    assert needle in str(excinfo.value)


def test_load_error_includes_position(config: Nl2DataConfig) -> None:
    """Structural errors point at the offending term index in file order."""
    payload = {
        "terms": [
            {"term": "毛利", "maps_to": {"table": "t"}},
            {"term": "营收", "synonyms": [1, 2], "maps_to": {"table": "t"}},
        ]
    }
    _write_glossary(config, yaml.safe_dump(payload, allow_unicode=True))
    with pytest.raises(GlossaryError) as excinfo:
        load_glossary(config.paths.glossary)
    assert "第 2 个术语" in str(excinfo.value)


def test_load_rejects_invalid_yaml(config: Nl2DataConfig) -> None:
    """Unparseable YAML raises GlossaryError, not yaml.YAMLError."""
    _write_glossary(config, "terms: [unclosed\n")
    with pytest.raises(GlossaryError, match="YAML"):
        load_glossary(config.paths.glossary)


def test_empty_missing_or_terms_free_means_empty_glossary(config: Nl2DataConfig) -> None:
    """Missing file, empty file and empty terms list are legal empty glossaries."""
    assert load_glossary(config.paths.glossary, config).entries == []  # file missing

    _write_glossary(config, "")
    empty = load_glossary(config.paths.glossary, config)
    assert empty.entries == []

    _write_glossary(config, "terms: []\n")
    none_terms = load_glossary(config.paths.glossary, config)
    assert none_terms.entries == []

    assert validate_glossary(empty, config) == []
    assert terms_by_table_map(config) == {}


def test_terms_by_table_map_shape_and_keys(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """terms_by_table_map groups frozen-format cards per table, in file order."""
    table, columns = _ingest_orders(config, ingest_factory)
    other = ingest_factory("sales_2025", pd.DataFrame({"v": [1]}))
    _write_glossary(
        config,
        _dump(
            [
                {
                    "term": "毛利",
                    "maps_to": {
                        "table": table,
                        "column": columns[1],
                        "expression": f"{columns[1]} - 100",
                        "filter": "status = 'done'",
                    },
                },
                {
                    "term": "营收",
                    "synonyms": ["revenue"],
                    "description": "收入合计",
                    "maps_to": {"table": table},
                },
                _term("客户数", other),
            ]
        ),
    )
    mapping = terms_by_table_map(config)
    assert set(mapping) == {table, other}
    cards = mapping[table]
    assert [card["term"] for card in cards] == ["毛利", "营收"]
    assert set(cards[0]) == {"term", "synonyms", "description", "maps_to"}
    assert set(cards[0]["maps_to"]) == {"table", "column", "expression", "filter"}
    assert cards[0]["maps_to"]["column"] == columns[1]
    assert cards[0]["maps_to"]["filter"] == "status = 'done'"
    assert cards[0]["synonyms"] == []
    assert cards[1]["synonyms"] == ["revenue"]
    assert cards[1]["description"] == "收入合计"
    assert cards[1]["maps_to"]["column"] is None
    assert mapping[other] == [
        {
            "term": "客户数",
            "synonyms": [],
            "description": None,
            "maps_to": {
                "table": other,
                "column": None,
                "expression": None,
                "filter": None,
            },
        }
    ]


def test_terms_by_table_map_fails_fast_with_all_errors(
    config: Nl2DataConfig,
    ingest_factory: Callable[[str, pd.DataFrame], str],
) -> None:
    """Reference problems raise GlossaryError carrying every problem line."""
    table, _ = _ingest_orders(config, ingest_factory)
    _write_glossary(
        config,
        _dump(
            [
                _term("毛利", "missing_table"),
                {"term": "营收", "maps_to": {"table": table, "expression": "amount -"}},
            ]
        ),
    )
    with pytest.raises(GlossaryError) as excinfo:
        terms_by_table_map(config)
    message = str(excinfo.value)
    assert "术语 毛利 → 表 missing_table 不存在" in message
    assert "术语 营收 的 expression 不是合法 SQL:amount -" in message
    assert "\n" in message  # all problems reported, one per line


def test_metric_filter_and_filter_are_mutually_exclusive(
    config: Nl2DataConfig,
) -> None:
    """A term cannot carry both table-level filter and metric_filter (T14)."""
    from catalog.glossary import GlossaryError, load_glossary

    config.paths.glossary.parent.mkdir(parents=True, exist_ok=True)
    config.paths.glossary.write_text(
        "terms:\n"
        "  - term: 冲突词\n"
        "    maps_to:\n"
        "      table: orders\n"
        "      filter: \"a = 1\"\n"
        "    metric_filter: \"b = 2\"\n"
        "    applies_to: [orders]\n",
        encoding="utf-8",
    )
    with pytest.raises(GlossaryError, match="互斥"):
        load_glossary(config.paths.glossary, config)


def test_metric_filter_requires_applies_to(config: Nl2DataConfig) -> None:
    """metric_filter without applies_to (or the reverse) is rejected."""
    from catalog.glossary import GlossaryError, load_glossary

    config.paths.glossary.parent.mkdir(parents=True, exist_ok=True)
    config.paths.glossary.write_text(
        "terms:\n"
        "  - term: 孤儿口径\n"
        "    maps_to:\n"
        "      table: orders\n"
        "    metric_filter: \"b = 2\"\n",
        encoding="utf-8",
    )
    with pytest.raises(GlossaryError, match="applies_to"):
        load_glossary(config.paths.glossary, config)

    config.paths.glossary.write_text(
        "terms:\n"
        "  - term: 孤儿表单\n"
        "    maps_to:\n"
        "      table: orders\n"
        "    applies_to: [orders]\n",
        encoding="utf-8",
    )
    with pytest.raises(GlossaryError, match="搭配"):
        load_glossary(config.paths.glossary, config)


def test_metric_filter_term_loads_and_attaches_to_all_tables(
    config: Nl2DataConfig,
) -> None:
    """A metric-calibre term loads and terms_for_table matches applies_to."""
    from catalog.glossary import load_glossary

    config.paths.glossary.parent.mkdir(parents=True, exist_ok=True)
    config.paths.glossary.write_text(
        "terms:\n"
        "  - term: 小费率\n"
        "    maps_to:\n"
        "      table: orders\n"
        "      column: ding_dan_id\n"
        "    metric_filter: \"payment_type = 1\"\n"
        "    applies_to: [orders, ding_dan]\n",
        encoding="utf-8",
    )
    store = load_glossary(config.paths.glossary, config)
    entry = store.lookup_term("小费率")
    assert entry is not None and entry.metric_filter == "payment_type = 1"
    assert [t.term for t in store.terms_for_table("ding_dan")] == ["小费率"]
    assert [t.term for t in store.terms_for_table("orders")] == ["小费率"]
    dumped = entry.to_dict()
    assert dumped["metric_filter"] == "payment_type = 1"
    assert dumped["applies_to"] == ["orders", "ding_dan"]


def test_dangling_applies_to_table_reported_with_candidates(
    config: Nl2DataConfig,
    ingest_factory: object,
) -> None:
    """applies_to referencing an unknown table reports difflib candidates."""
    from catalog.glossary import load_glossary, validate_glossary

    _ingest_orders(config, ingest_factory)  # type: ignore[arg-type]

    config.paths.glossary.parent.mkdir(parents=True, exist_ok=True)
    config.paths.glossary.write_text(
        "terms:\n"
        "  - term: 跨表词\n"
        "    maps_to:\n"
        "      table: orders\n"
        "    metric_filter: \"a = 1\"\n"
        "    applies_to: [orders, orderz]\n",
        encoding="utf-8",
    )
    problems = validate_glossary(load_glossary(config.paths.glossary, config), config)
    assert any("applies_to 表 orderz 不存在" in p for p in problems)
    assert any("候选:orders" in p or "orders" in p for p in problems)


def test_metric_filter_sql_validated(
    config: Nl2DataConfig, ingest_factory: object
) -> None:
    """Invalid metric_filter SQL is caught by sqlglot (T14)."""
    from catalog.glossary import load_glossary, validate_glossary

    _ingest_orders(config, ingest_factory)  # type: ignore[arg-type]

    config.paths.glossary.parent.mkdir(parents=True, exist_ok=True)
    config.paths.glossary.write_text(
        "terms:\n"
        "  - term: 坏口径\n"
        "    maps_to:\n"
        "      table: orders\n"
        "    metric_filter: \"payment_type =\"\n"
        "    applies_to: [orders]\n",
        encoding="utf-8",
    )
    problems = validate_glossary(load_glossary(config.paths.glossary, config), config)
    assert any("metric_filter 不是合法 SQL" in p for p in problems)
