"""Red-team tests for the SQL guardrails in :mod:`guard.validate` (T10).

Written by an independent reviewer trying to *bypass* the guard: comment
obfuscation, writable CTEs, ``SELECT .. INTO``, alias shadowing, system
function variants, CTE scope escapes, LIMIT tricks, string-literal smuggling
and DuckDB dialect quirks.

Contract under test:

* destructive / unauthorized / external-access samples must raise
  ``GuardError`` (any ``category`` counts as caught);
* legal read queries must pass.

Known bypasses and false rejections found during the red-team run are fixed
here with ``xfail(strict=False)`` so a future guard fix turns them into
XPASS instead of breaking the suite.
"""

from __future__ import annotations

import pytest

from guard.validate import GuardError, ValidatedSQL, validate
from nl2data.config import Nl2DataConfig

ALLOWED_TABLES = {"yellow_tripdata", "green_tripdata", "taxi_zones"}
COLUMN_MAP = {
    "yellow_tripdata": {"vendorid", "trip_distance", "fare_amount", "pulocationid"},
    "green_tripdata": {"vendorid", "trip_distance", "ehail_fee"},
    "taxi_zones": {"locationid", "borough", "zone"},
}


def _validate(sql: str, cfg: Nl2DataConfig) -> ValidatedSQL:
    """Run ``validate`` with the unified red-team whitelist and column map."""
    return validate(sql, ALLOWED_TABLES, COLUMN_MAP, cfg)


def _reject(sql: str, cfg: Nl2DataConfig, category: str | None = None) -> GuardError:
    """Assert ``sql`` is rejected (optionally pinning the category); return it."""
    with pytest.raises(GuardError) as excinfo:
        validate(sql, ALLOWED_TABLES, COLUMN_MAP, cfg)
    error = excinfo.value
    assert str(error)  # a readable reason is part of the contract
    if category is not None:
        assert error.category == category
    return error


# ---------------------------------------------------------------------------
# Bypass family 1: writable CTE bodies (known bypass, xfail)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="bypass: _check_root only inspects the root node; a DELETE/INSERT/"
    "UPDATE/CREATE hidden in a WITH body validates clean and is returned",
    strict=False,
)
@pytest.mark.parametrize(
    "sql",
    [
        "WITH t AS (DELETE FROM yellow_tripdata RETURNING *) SELECT * FROM t",
        "WITH t AS (INSERT INTO yellow_tripdata VALUES (1)) SELECT * FROM t",
        "WITH t AS (UPDATE yellow_tripdata SET vendorid = 1 RETURNING *) SELECT * FROM t",
        "WITH t AS (CREATE TABLE yellow_tripdata AS SELECT 1) SELECT * FROM t",
    ],
)
def test_writable_cte_body_is_rejected(sql: str, config: Nl2DataConfig) -> None:
    """A data-modifying statement nested in a WITH body must be rejected.

    sqlglot parses the CTE body into Delete/Insert/Update/Create nodes while
    the statement root stays a Select, so every current check passes and the
    destructive SQL is handed back inside ValidatedSQL. (DuckDB 1.5.x refuses
    writable CTEs at execution time — 'A CTE needs a SELECT' — which mitigates
    but does not fix the guard-level bypass.)
    """
    _reject(sql, config)


def test_into_ghost_table_is_rejected(config: Nl2DataConfig) -> None:
    """``SELECT .. INTO`` is a write statement; blocked before table checks."""
    _reject("SELECT 1 AS vendorid INTO ghost_table", config, "forbidden_statement")


@pytest.mark.xfail(
    reason="bypass: SELECT..INTO a whitelisted table validates clean and is even "
    "rewritten to 'CREATE TABLE yellow_tripdata AS SELECT ..'",
    strict=False,
)
def test_select_into_whitelisted_table_is_rejected(config: Nl2DataConfig) -> None:
    """``SELECT .. INTO`` a whitelisted table must be rejected as a write.

    Known bypass: the root is a Select carrying an ``into`` arg, so the table
    check passes (the target *is* whitelisted) and the emitted SQL is a CREATE
    TABLE statement — which DuckDB executes.
    """
    _reject("SELECT 1 AS vendorid INTO yellow_tripdata", config)


# ---------------------------------------------------------------------------
# Multi-statement smuggling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE x",
        "SELECT 1;\nDROP TABLE yellow_tripdata",
        "SELECT vendorid FROM yellow_tripdata; SELECT 2",
    ],
)
def test_multi_statement_is_rejected(sql: str, config: Nl2DataConfig) -> None:
    """Stacked statements are rejected even when the first one is harmless."""
    _reject(sql, config, "multi_statement")


def test_trailing_semicolon_alone_is_accepted(config: Nl2DataConfig) -> None:
    """A single statement with a trailing semicolon is legal and must pass."""
    result = _validate("SELECT vendorid FROM yellow_tripdata;", config)
    assert result.tables == ["yellow_tripdata"]
    assert result.limit == 500


@pytest.mark.xfail(
    reason="false rejection: 'SELECT 1; -- comment' parses as Select + Semicolon "
    "(counted as two statements) although it is one legal statement",
    strict=False,
)
def test_trailing_semicolon_with_comment_is_accepted(config: Nl2DataConfig) -> None:
    """A semicolon followed by a comment is still a single statement."""
    result = _validate("SELECT 1; -- DROP TABLE yellow_tripdata", config)
    assert result.limit == 500


# ---------------------------------------------------------------------------
# Comment obfuscation
# ---------------------------------------------------------------------------


def test_keyword_split_by_block_comment_is_parse_error(config: Nl2DataConfig) -> None:
    """SEL/**/ECT never reassembles into SELECT: it is a parse error."""
    _reject("SEL/**/ECT 1", config, "parse_error")


def test_blocked_function_in_comment_is_inert(config: Nl2DataConfig) -> None:
    """A read_csv call fully inside a comment must not trigger any check."""
    result = _validate(
        "SELECT /* read_csv('x.csv') */ vendorid FROM yellow_tripdata", config
    )
    assert result.tables == ["yellow_tripdata"]


def test_drop_keyword_in_comment_is_inert(config: Nl2DataConfig) -> None:
    """DROP TABLE spelled inside a comment is data, not SQL: no rejection."""
    result = _validate("SELECT 1 /* DROP TABLE yellow_tripdata */", config)
    assert result.tables == []
    assert result.limit == 500


def test_comment_shredded_table_function_is_rejected(config: Nl2DataConfig) -> None:
    """read/**/_csv cannot reassemble into read_csv: table 'read' is unknown."""
    error = _reject("FROM read/**/_csv('f.csv')", config, "unknown_table")
    assert "read" in str(error)


# ---------------------------------------------------------------------------
# Casing / whitespace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "CrEaTe TABLE evil (a INT)",
        "sEt memory_limit = '10GB'",
        "DeLeTe FROM yellow_tripdata",
    ],
)
def test_mixed_case_write_keywords_are_rejected(
    sql: str, config: Nl2DataConfig
) -> None:
    """Random keyword casing must not evade the statement-kind check."""
    _reject(sql, config, "forbidden_statement")


def test_tabs_and_newlines_are_accepted(config: Nl2DataConfig) -> None:
    """Whitespace noise inside a legal query changes nothing."""
    result = _validate("SELECT\tvendorid\nFROM\tyellow_tripdata", config)
    assert result.tables == ["yellow_tripdata"]
    assert result.limit == 500


# ---------------------------------------------------------------------------
# Table functions in FROM/JOIN (file and external-system access)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('f.csv') AS t",
        "SELECT * FROM read_csv_auto('f.csv')",
        "SELECT * FROM read_parquet('x.parquet') AS p",
        "SELECT * FROM read_json('j.json')",
        "SELECT * FROM read_xlsx('a.xlsx')",
        "SELECT * FROM parquet_scan('x.parquet')",
        "SELECT * FROM glob('*.parquet')",
        "SELECT v.vendorid FROM read_csv('f.csv') v",
        "SELECT * FROM yellow_tripdata y CROSS JOIN read_parquet('x.parquet') r",
        "SELECT * FROM LATERAL (SELECT * FROM read_parquet('x.parquet')) l",
        "SELECT (SELECT count(*) FROM read_csv('f.csv')) AS n",
    ],
)
def test_table_function_variants_are_rejected(sql: str, config: Nl2DataConfig) -> None:
    """File/table functions cannot hide in aliases, JOINs, LATERAL or scalars."""
    _reject(sql, config, "forbidden_function")


# ---------------------------------------------------------------------------
# System / introspection functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT duckdb_settings()",
        "SELECT * FROM duckdb_settings()",
        "SELECT pg_typeof(vendorid) FROM yellow_tripdata",
        "SELECT LIST_TABLES()",
        "SELECT current_setting('memory_limit')",
        "SELECT CURRENT_VERSION()",
        "SELECT version()",
        "SELECT vendorid FROM yellow_tripdata WHERE fare_amount > (SELECT version())",
    ],
)
def test_system_function_variants_are_rejected(sql: str, config: Nl2DataConfig) -> None:
    """Blocked system functions are caught at any casing, arity or nesting."""
    _reject(sql, config, "forbidden_function")


# ---------------------------------------------------------------------------
# CTE scope games
# ---------------------------------------------------------------------------


def test_cte_shadowing_whitelisted_name_is_legal(config: Nl2DataConfig) -> None:
    """A CTE may legally shadow a whitelisted table name (DuckDB semantics)."""
    sql = (
        "WITH yellow_tripdata AS (SELECT 1 AS vendorid) "
        "SELECT vendorid FROM yellow_tripdata"
    )
    result = _validate(sql, config)
    assert result.tables == []  # no physical table is referenced
    assert result.limit == 500


def test_read_parquet_hidden_in_cte_body_is_rejected(config: Nl2DataConfig) -> None:
    """A table function inside a CTE body is still found by the tree walk."""
    sql = "WITH c AS (SELECT * FROM read_parquet('x.parquet')) SELECT * FROM c"
    _reject(sql, config, "forbidden_function")


def test_cte_name_escaping_its_scope_is_rejected(config: Nl2DataConfig) -> None:
    """An outer reference to a CTE defined inside a subquery is not laundered.

    The inner WITH belongs to a sibling scope, so the outer ``t`` would bind
    to a non-whitelisted physical table and must be rejected (DuckDB fails the
    same reference at bind time).
    """
    sql = "SELECT * FROM (WITH t AS (SELECT 1 AS x) SELECT x FROM t) s CROSS JOIN t"
    _reject(sql, config, "unknown_table")


# ---------------------------------------------------------------------------
# Column validation games
# ---------------------------------------------------------------------------


def test_unknown_qualified_column_is_rejected(config: Nl2DataConfig) -> None:
    """A bad column behind a table alias is caught through the alias map."""
    _reject("SELECT y.nonexistent FROM yellow_tripdata y", config, "unknown_column")


def test_column_map_is_per_table(config: Nl2DataConfig) -> None:
    """Columns valid on one whitelisted table do not launder onto another."""
    _reject("SELECT fare_amount FROM green_tripdata", config, "unknown_column")
    result = _validate("SELECT ehail_fee FROM green_tripdata", config)
    assert result.tables == ["green_tripdata"]


def test_subquery_alias_column_is_unknowable(config: Nl2DataConfig) -> None:
    """Columns of a subquery alias are statically unknowable and skipped.

    By-design permissive: ``t.nonexistent`` cannot be resolved without full
    name resolution, and the subquery only reads whitelisted tables anyway.
    """
    sql = "SELECT t.nonexistent FROM (SELECT vendorid FROM yellow_tripdata) t"
    result = _validate(sql, config)
    assert result.tables == ["yellow_tripdata"]


# ---------------------------------------------------------------------------
# LIMIT games
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected_limit"),
    [
        ("SELECT vendorid FROM yellow_tripdata LIMIT 1+1", 500),
        ("SELECT vendorid FROM yellow_tripdata LIMIT ALL", 500),
        ("SELECT vendorid FROM yellow_tripdata FETCH FIRST 100000000 ROWS ONLY", 500),
        (
            "SELECT vendorid FROM (SELECT vendorid FROM yellow_tripdata"
            " LIMIT 999999999) s",
            500,
        ),
        ("SELECT vendorid FROM yellow_tripdata LIMIT 999999", 10000),
        ("SELECT vendorid FROM yellow_tripdata LIMIT 10000", 10000),
        ("SELECT vendorid FROM yellow_tripdata LIMIT 10", 10),
    ],
)
def test_limit_tricks_are_bounded(
    sql: str, expected_limit: int, config: Nl2DataConfig
) -> None:
    """Non-literal, FETCH and oversized LIMITs all end up within the cap."""
    result = _validate(sql, config)
    assert result.limit == expected_limit


def test_fetch_first_is_replaced_by_limit(config: Nl2DataConfig) -> None:
    """A huge FETCH FIRST is rewritten away, not smuggled next to LIMIT 500."""
    sql = "SELECT vendorid FROM yellow_tripdata FETCH FIRST 100000000 ROWS ONLY"
    result = _validate(sql, config)
    assert result.limit == 500
    assert "FETCH" not in result.sql.upper()
    assert "LIMIT 500" in result.sql.upper()


def test_injected_limit_keeps_offset(config: Nl2DataConfig) -> None:
    """OFFSET survives the LIMIT injection; returned rows stay bounded."""
    result = _validate("SELECT vendorid FROM yellow_tripdata OFFSET 10", config)
    assert result.limit == 500
    assert "OFFSET 10" in result.sql.upper()


# ---------------------------------------------------------------------------
# String-literal smuggling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT vendorid FROM yellow_tripdata WHERE vendorid = 'DROP TABLE x'",
        "SELECT zone FROM taxi_zones WHERE zone = 'read_csv(1)'",
    ],
)
def test_destructive_text_in_string_literal_is_inert(
    sql: str, config: Nl2DataConfig
) -> None:
    """Destructive syntax quoted inside a literal is data: pass, never fire."""
    result = _validate(sql, config)
    assert result.limit == 500


# ---------------------------------------------------------------------------
# Set operations wrapping writes
# ---------------------------------------------------------------------------


def test_union_of_constants_is_legal(config: Nl2DataConfig) -> None:
    """A pure constant UNION references no table and gets the default LIMIT."""
    result = _validate("SELECT 1 UNION SELECT 2", config)
    assert result.tables == []
    assert result.limit == 500


def test_union_over_whitelisted_tables_is_legal(config: Nl2DataConfig) -> None:
    """UNION ALL across whitelisted tables passes and lists both tables."""
    sql = (
        "SELECT vendorid FROM yellow_tripdata "
        "UNION ALL SELECT vendorid FROM green_tripdata"
    )
    result = _validate(sql, config)
    assert result.tables == ["yellow_tripdata", "green_tripdata"]


def test_union_all_delete_is_rejected(config: Nl2DataConfig) -> None:
    """A DELETE cannot ride inside a set operation (sqlglot refuses to parse)."""
    _reject("SELECT 1 UNION ALL DELETE FROM yellow_tripdata", config, "parse_error")


# ---------------------------------------------------------------------------
# Dialect / wrapper quirks
# ---------------------------------------------------------------------------


def test_double_quoted_whitelisted_table_is_accepted(config: Nl2DataConfig) -> None:
    """Quoted identifiers resolve to the same whitelisted table."""
    result = _validate('SELECT vendorid FROM "yellow_tripdata"', config)
    assert result.tables == ["yellow_tripdata"]


def test_catalog_qualified_whitelisted_table_is_accepted(config: Nl2DataConfig) -> None:
    """``main.yellow_tripdata`` names the same whitelisted physical table."""
    result = _validate("SELECT vendorid FROM main.yellow_tripdata", config)
    assert result.tables == ["yellow_tripdata"]


def test_bare_from_statement_is_accepted(config: Nl2DataConfig) -> None:
    """DuckDB's 'FROM tbl' sugar is a read query and gets a LIMIT injected."""
    result = _validate("FROM yellow_tripdata", config)
    assert result.tables == ["yellow_tripdata"]
    assert result.limit == 500


def test_unnest_relation_is_accepted(config: Nl2DataConfig) -> None:
    """UNNEST of a constant list reads no table and is harmless.

    Observation: sqlglot models this without a Table node, so the blanket
    'any function in FROM is rejected' rule does not fire — benign because
    the argument is a literal and nothing external is reachable.
    """
    result = _validate("SELECT * FROM UNNEST([1, 2]) AS t(x)", config)
    assert result.tables == []
    assert result.limit == 500


@pytest.mark.xfail(
    reason="false rejection: sqlglot's duckdb dialect cannot parse backtick "
    "identifiers although DuckDB itself accepts them",
    strict=False,
)
def test_backtick_quoted_table_is_accepted(config: Nl2DataConfig) -> None:
    """Backtick identifiers are legal DuckDB and must not be a parse error."""
    result = _validate("SELECT vendorid FROM `yellow_tripdata`", config)
    assert result.tables == ["yellow_tripdata"]


@pytest.mark.xfail(
    reason="false rejection: a parenthesized SELECT parses as a Subquery root "
    "and is rejected as not_select although DuckDB executes it",
    strict=False,
)
def test_parenthesized_select_is_accepted(config: Nl2DataConfig) -> None:
    """``((SELECT 1))`` is a legal single read query."""
    result = _validate("((SELECT 1))", config)
    assert result.limit == 500
