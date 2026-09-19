"""Tests for catalog.yaml storage (§5.2 lineage contract)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from catalog.store import CatalogError, CatalogStore, ColumnEntry, SourceEntry, TableEntry


def _entry(name: str = "sales", table_name: str = "ding_dan") -> SourceEntry:
    """Build a representative source entry with one Chinese-named table."""
    return SourceEntry(
        name=name,
        type="excel",
        path="D:/data/销售.xlsx",
        ingested_at="2026-09-18T06:00:00+00:00",
        tables=[
            TableEntry(
                name=table_name,
                original_name="订单",
                parquet=f"data/parquet/{name}/{table_name}.parquet",
                rows=42,
                columns=[
                    ColumnEntry(name="ding_dan_id", original_name="订单ID"),
                    ColumnEntry(name="ke_hu", original_name="客户"),
                ],
            )
        ],
    )


def test_missing_file_means_empty_catalog(tmp_path: Path) -> None:
    """A fresh environment without catalog.yaml starts empty."""
    store = CatalogStore(tmp_path / "nested" / "catalog.yaml")
    assert store.sources == []


def test_save_load_round_trip(tmp_path: Path) -> None:
    """Entries survive a save/load cycle unchanged, including originals."""
    path = tmp_path / "catalog.yaml"
    store = CatalogStore(path)
    entry = _entry()
    store.upsert_source(entry)
    store.save()
    assert CatalogStore(path).sources == [entry]


def test_yaml_matches_contract_schema(tmp_path: Path) -> None:
    """The serialized YAML matches the locked §5.2 field sets exactly."""
    path = tmp_path / "catalog.yaml"
    store = CatalogStore(path)
    store.upsert_source(_entry())
    store.save()

    text = path.read_text(encoding="utf-8")
    assert "订单ID" in text  # original names stored as unicode, not escapes

    raw: dict = yaml.safe_load(text)
    source = raw["sources"][0]
    assert set(source) == {"name", "type", "path", "ingested_at", "tables"}
    table = source["tables"][0]
    assert set(table) == {"name", "original_name", "parquet", "rows", "columns"}
    assert set(table["columns"][0]) == {"name", "original_name"}


def test_upsert_replaces_by_name(tmp_path: Path) -> None:
    """Re-ingesting a source replaces its previous entry in place."""
    path = tmp_path / "catalog.yaml"
    store = CatalogStore(path)
    store.upsert_source(_entry(table_name="ding_dan"))
    updated = _entry(table_name="ding_dan_2")
    store.upsert_source(updated)
    store.save()

    reloaded = CatalogStore(path)
    assert len(reloaded.sources) == 1
    assert reloaded.sources[0] == updated


def test_get_table_across_sources(tmp_path: Path) -> None:
    """Table lookup works across source boundaries by clean name."""
    store = CatalogStore(tmp_path / "catalog.yaml")
    store.upsert_source(_entry(name="sales"))
    store.upsert_source(_entry(name="hr", table_name="yuan_gong"))
    store.save()

    found = CatalogStore(path := tmp_path / "catalog.yaml").get_table("yuan_gong")
    assert found is not None
    source, table = found
    assert source.name == "hr"
    assert table.name == "yuan_gong"
    assert CatalogStore(path).get_table("missing") is None


def test_invalid_yaml_raises_catalog_error(tmp_path: Path) -> None:
    """Malformed catalog files raise CatalogError, not yaml.YAMLError."""
    path = tmp_path / "catalog.yaml"
    path.write_text("sources: [unclosed\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="invalid YAML"):
        CatalogStore(path)
