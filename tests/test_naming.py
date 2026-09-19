"""Tests for the §5.4 clean_name naming contract."""

from __future__ import annotations

import pytest

from catalog.naming import clean_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("订单明细", "ding_dan_ming_xi"),
        ("客户ID", "ke_hu_id"),
        ("Sheet1", "sheet1"),
        ("Sales 2024 (final)!", "sales_2024_final"),
        ("金额（元）", "jin_e_yuan"),
        ("1月销量", "_1_yue_xiao_liang"),
    ],
)
def test_clean_name_transliteration(raw: str, expected: str) -> None:
    """Chinese, symbols, digit-leading and plain names map to clean slugs."""
    assert clean_name(raw) == expected


def test_output_is_always_lowercase_slug() -> None:
    """Every produced name matches the locked character set."""
    result = clean_name("ABC 测试_2024")
    assert result == result.lower()
    assert all(ch.isalnum() or ch == "_" for ch in result)


def test_length_truncation_to_max() -> None:
    """Names longer than the limit are truncated to exactly max_length."""
    raw = "非" * 40
    assert len(clean_name(raw)) == 63
    assert len(clean_name(raw, max_length=10)) == 10


def test_conflict_gets_suffix() -> None:
    """Same-pinyin names collide and receive _1, _2 suffixes."""
    existing: set[str] = set()
    first = clean_name("订单", existing)
    second = clean_name("訂單", existing)
    third = clean_name("订单 ", existing)
    assert first == "ding_dan"
    assert second == "ding_dan_1"
    assert third == "ding_dan_2"
    assert existing == {"ding_dan", "ding_dan_1", "ding_dan_2"}


def test_conflict_suffix_respects_max_length() -> None:
    """Even suffixed names never exceed max_length."""
    raw = "字" * 100
    existing: set[str] = set()
    first = clean_name(raw, existing)
    second = clean_name(raw, existing)
    assert first != second
    assert len(first) == 63
    assert len(second) == 63


def test_fallback_for_symbol_only_name() -> None:
    """Names with no usable characters fall back to the documented default."""
    assert clean_name("———") == "tbl"
    assert clean_name("") == "tbl"


def test_fallback_resolves_conflicts() -> None:
    """Fallback names participate in conflict resolution."""
    existing: set[str] = {"tbl"}
    assert clean_name("———", existing) == "tbl_1"
    assert existing == {"tbl", "tbl_1"}


def test_existing_set_is_not_required() -> None:
    """Without an existing set, the plain slug is returned."""
    assert clean_name("订单") == "ding_dan"


def test_invalid_max_length_raises() -> None:
    """A max_length too small for _N suffixes is rejected."""
    with pytest.raises(ValueError, match="max_length"):
        clean_name("x", max_length=3)
