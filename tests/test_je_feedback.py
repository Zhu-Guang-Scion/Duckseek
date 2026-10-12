"""Tests for the JE-feedback hardening package (2026-10-12).

Covers feedback items #1 (CSV encoding chain), #2 (.xls via xlrd),
#3 (same-name content gate, allow/reject policies), #4 (enum values kept
verbatim — the discipline pinned after jekit's type-code swallowing
incident), #5 (row-order-independent canonical hash) and #9 (manifest
input_digest). Evidence chain: JE_Testing docs/21_DuckSeek回馈建议.md.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from catalog.fingerprint import (
    canonical_sha256_frame,
    canonical_sha256_parquet,
    file_sha256,
)
from ingest.common import IngestError, PreparedTable, ingest_tables
from ingest.encoding import decode_bytes
from nl2data.config import Nl2DataConfig


# --------------------------------------------------------------- #1 chain ----
def test_decode_bytes_chain() -> None:
    """utf-8-sig → gbk → lossy utf-8, in that order."""
    assert decode_bytes(b"a,b\n") == "a,b\n"
    bom = "城市,金额\n".encode("utf-8-sig")  # codec prepends the BOM
    decoded_bom = decode_bytes(bom)
    assert decoded_bom.startswith("城市") and "\ufeff" not in decoded_bom
    gbk = "城市,金额\n北京,100\n".encode("gbk")
    decoded = decode_bytes(gbk)
    assert decoded.startswith("城市") and "北京" in decoded
    undecodable = b"\xff\xfe\xfa\xf0binary\xff"
    assert "\ufffd" in decode_bytes(undecodable)  # lossy fallback survives


# ------------------------------------------------------------------ #2 xls ----
def _write_xls(path: Path) -> None:
    """A real legacy .xls via xlwt: title row + Chinese headers + data."""
    import xlwt

    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("订单")
    sheet.write(0, 0, "2025年12月中国报表")
    sheet.write(1, 0, "凭证号")
    sheet.write(1, 1, "金额")
    sheet.write(1, 2, "城市")
    rows = [("P001", 100.0, "北京"), ("P002", 200.0, "上海"), ("P003", 300.0, "北京")]
    for r, row in enumerate(rows, start=2):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    workbook.save(str(path))


def test_ingest_legacy_xls(config: Nl2DataConfig, tmp_path: Path) -> None:
    """Legacy binary .xls ingests: xlrd read + title-row header probe."""
    from ingest.excel import ingest_excel

    source = tmp_path / "china_report.xls"
    _write_xls(source)
    entry = ingest_excel(source, config)
    table = entry.tables[0]
    assert table.name == "ding_dan"
    assert [col.name for col in table.columns] == ["ping_zheng_hao", "jin_e", "cheng_shi"]
    assert table.rows == 3
    with pytest.raises(IngestError, match=r"\.xlsb"):
        ingest_excel(tmp_path / "nope.xlsb", config)


# ------------------------------------------------------- #3 same-name gate ----
def _ingest_file(
    config: Nl2DataConfig, tmp_path: Path, name: str, rows: list[tuple]
) -> Path:
    source = tmp_path / f"{name}.xlsx"
    pd.DataFrame(rows, columns=["k", "v"]).to_excel(source, index=False)
    return source


def test_same_name_gate_policies(
    config: Nl2DataConfig, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """allow (default) warns and replaces; reject refuses; same hash passes."""
    source = _ingest_file(config, tmp_path, "s", [(1, 1)])
    ingest_tables(
        source_name="s",
        source_type="excel",
        source_path=source,
        tables=[PreparedTable("t", pd.read_excel(source))],
        cfg=config,
    )
    # Same file re-ingest: idempotent under both policies, no warning.
    ingest_tables(
        source_name="s",
        source_type="excel",
        source_path=source,
        tables=[PreparedTable("t", pd.read_excel(source))],
        cfg=config,
    )

    different = _ingest_file(config, tmp_path, "s2", [(1, 999)])
    # allow: proceeds with a loud warning; fingerprint updates to the new file.
    with caplog.at_level("WARNING"):
        ingest_tables(
            source_name="s",
            source_type="excel",
            source_path=different,
            tables=[PreparedTable("t", pd.read_excel(different))],
            cfg=config,
        )
    assert any("内容变更" in r.message for r in caplog.records)

    strict = replace(
        config, ingest=replace(config.ingest, same_name_content_policy="reject")
    )
    third = _ingest_file(config, tmp_path, "s3", [(1, 12345)])
    with pytest.raises(IngestError, match="同名源内容变更被拒绝"):
        ingest_tables(
            source_name="s",
            source_type="excel",
            source_path=third,
            tables=[PreparedTable("t", pd.read_excel(third))],
            cfg=strict,
        )


def test_source_fingerprint_recorded(
    config: Nl2DataConfig, tmp_path: Path
) -> None:
    """The catalog records the source SHA-256 and per-table canonical hash."""
    from catalog.store import CatalogStore

    source = _ingest_file(config, tmp_path, "fp", [(1, 1), (2, 2)])
    entry = ingest_tables(
        source_name="fp",
        source_type="excel",
        source_path=source,
        tables=[PreparedTable("t", pd.read_excel(source))],
        cfg=config,
    )
    assert entry.sha256 == file_sha256(source)
    assert entry.tables[0].canonical_sha256 is not None
    # Round-trip through catalog.yaml keeps both additive keys.
    store = CatalogStore(config.paths.catalog)
    loaded = store.get_source("fp")
    assert loaded is not None and loaded.sha256 == entry.sha256
    assert loaded.tables[0].canonical_sha256 == entry.tables[0].canonical_sha256


# ----------------------------------------------------------- #5 canonical ----
def test_canonical_hash_order_independent_and_sensitive() -> None:
    """Row order never changes the digest; values and columns do."""
    a = pd.DataFrame({"k": [1, 2, 3], "v": ["x", "y", "z"]})
    shuffled = pd.DataFrame({"k": [3, 1, 2], "v": ["z", "x", "y"]})
    assert canonical_sha256_frame(a) == canonical_sha256_frame(shuffled)
    changed_value = pd.DataFrame({"k": [1, 2, 3], "v": ["x", "y", "w"]})
    assert canonical_sha256_frame(a) != canonical_sha256_frame(changed_value)
    renamed_column = a.rename(columns={"v": "w"})
    assert canonical_sha256_frame(a) != canonical_sha256_frame(renamed_column)
    # 5.0 == 5 and NULL/NaN share one label.
    assert canonical_sha256_frame(pd.DataFrame({"k": [5.0]})) == canonical_sha256_frame(
        pd.DataFrame({"k": [5]})
    )
    assert canonical_sha256_frame(pd.DataFrame({"k": [None]})) == canonical_sha256_frame(
        pd.DataFrame({"k": [float("nan")]})
    )


def test_canonical_hash_frame_matches_parquet(
    config: Nl2DataConfig, tmp_path: Path
) -> None:
    """Frame and Parquet digests agree for identical logical content."""
    frame = pd.DataFrame({"k": [1, 2, 3], "v": ["x", "y", "z"]})
    path = tmp_path / "t.parquet"
    frame.to_parquet(path, index=False)
    assert canonical_sha256_frame(frame) == canonical_sha256_parquet(path)


# ------------------------------------------------------------- #4 enum pin ----
def test_profiler_enum_values_kept_verbatim(
    config: Nl2DataConfig, tmp_path: Path
) -> None:
    """Enum values are preserved byte-for-byte: no strip/lower/casefold merge.

    The discipline pinned after jekit's incident: 15 voucher-type codes were
    swallowed into a default bucket and 255K rows misfired downstream.
    """
    from catalog.profiler import run_profiles

    source = tmp_path / "codes.xlsx"
    pd.DataFrame(
        {
            "类型": ["CR", "DR", "cr", "Dr ", None, "CR"],
            "金额": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    ).to_excel(source, index=False, sheet_name="类型表")
    from ingest.excel import ingest_excel

    ingest_excel(source, config)
    run_profiles(config)
    profile = json.loads(
        (config.paths.profiles_dir / "lei_xing_biao.json").read_text(encoding="utf-8")
    )
    col = next(c for c in profile["columns"] if c["name"] == "lei_xing")
    # NULL renders as ""; case ("CR" vs "cr") and the trailing space in
    # "Dr " survive byte-for-byte — no strip/lower/casefold merge.
    assert col["enum_values"] == ["", "CR", "DR", "Dr ", "cr"]


# ------------------------------------------------------------ #9 manifest ----
def test_manifest_input_digest(config: Nl2DataConfig, tmp_path: Path) -> None:
    """manifest.json carries per-table canonical digests from the catalog."""
    from exec.runner import ExecutionResult
    from export.manifest import write_manifest

    source = _ingest_file(config, tmp_path, "digest_src", [(1, 1), (2, 2)])
    entry = ingest_tables(
        source_name="digest_src",
        source_type="excel",
        source_path=source,
        tables=[PreparedTable("t", pd.read_excel(source))],
        cfg=config,
    )
    execution = ExecutionResult(
        status="ok", rowcount=1, columns=["n"], rows=[{"n": 1}],
        profile=None, detail_ref=None, error=None, latency_ms=1.0,
    )
    out = tmp_path / "manifest.json"
    write_manifest(
        out,
        question="q",
        sql="SELECT 1",
        source_tables=[entry.tables[0].name],
        execution=execution,
        exported_rows=1,
        truncated=False,
        chart_spec=None,
        chart_error=None,
        artifacts={"xlsx": "x", "manifest": "m"},
        catalog_path=config.paths.catalog,
    )
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["input_digest"] == {
        entry.tables[0].name: entry.tables[0].canonical_sha256
    }
