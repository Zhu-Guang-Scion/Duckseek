"""Tests for the SQL guardrails in :mod:`guard.validate` (milestone 3, T10).

Every rule from the validation checklist gets at least two cases, including
obfuscation variants (comments, casing, nesting, string literals).
"""

from __future__ import annotations

import pytest

from guard.validate import GuardError, ValidatedSQL, validate
from nl2data.config import Nl2DataConfig

ALLOWED_TABLES = {"yellow", "blue"}
COLUMN_MAP = {
    "yellow": {"taxi_id", "pickup_at", "dropoff_at", "fare", "tip"},
    "blue": {"taxi_id", "zone", "borough"},
}


def _validate(sql: str, cfg: Nl2DataConfig) -> ValidatedSQL:
    """Run ``validate`` with the shared whitelist and column map."""
    return validate(sql, ALLOWED_TABLES, COLUMN_MAP, cfg)


def _reject(sql: str, category: str, cfg: Nl2DataConfig) -> GuardError:
    """Assert ``sql`` is rejected with ``category``; return the raised error."""
    with pytest.raises(GuardError) as excinfo:
        validate(sql, ALLOWED_TABLES, COLUMN_MAP, cfg)
    assert excinfo.value.category == category
    assert str(excinfo.value)  # a readable reason is part of the contract
    return excinfo.value


def test_accepts_single_table_select_and_injects_limit(config: Nl2DataConfig) -> None:
    """A plain one-table query passes and gets the default LIMIT injected."""
    result = _validate("SELECT taxi_id FROM yellow", config)
    assert result.limit == 500
    assert result.tables == ["yellow"]
    assert "LIMIT 500" in result.sql.upper()


def test_accepts_join_of_whitelisted_tables(config: Nl2DataConfig) -> None:
    """A two-table JOIN with qualified columns passes."""
    sql = "SELECT y.fare, b.zone FROM yellow y JOIN blue b ON y.taxi_id = b.taxi_id"
    result = _validate(sql, config)
    assert result.tables == ["yellow", "blue"]
    assert result.limit == 500


def test_accepts_cte_querying_whitelisted_table(config: Nl2DataConfig) -> None:
    """A WITH-CTE whose body reads a whitelisted table passes."""
    sql = (
        "WITH recent AS (SELECT taxi_id, fare FROM yellow WHERE fare > 10) "
        "SELECT taxi_id, fare FROM recent"
    )
    result = _validate(sql, config)
    assert result.tables == ["yellow"]  # the CTE alias itself is not a table
    assert result.limit == 500


def test_cte_shadowing_whitelist_name_is_treated_as_cte(config: Nl2DataConfig) -> None:
    """A CTE named like a whitelisted table wins locally (DuckDB semantics)."""
    sql = "WITH yellow AS (SELECT 1 AS taxi_id) SELECT taxi_id FROM yellow"
    result = _validate(sql, config)
    assert result.tables == []  # no physical table is referenced
    assert result.limit == 500


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT fare FROM yellow UNION SELECT fare FROM blue",
        "SELECT fare FROM yellow UNION ALL SELECT fare FROM blue",
    ],
)
def test_accepts_union(sql: str, config: Nl2DataConfig) -> None:
    """Set operations over whitelisted tables pass."""
    result = _validate(sql, config)
    assert result.tables == ["yellow", "blue"]


def test_accepts_nested_subquery(config: Nl2DataConfig) -> None:
    """A derived table over a whitelisted table passes."""
    result = _validate(
        "SELECT * FROM (SELECT taxi_id FROM yellow WHERE tip > 1) sub", config
    )
    assert result.tables == ["yellow"]


def test_cte_name_outside_whitelist_passes(config: Nl2DataConfig) -> None:
    """A CTE name missing from the whitelist is not an unknown-table error."""
    result = _validate("WITH t AS (SELECT 1 AS x) SELECT x FROM t", config)
    assert result.tables == []


def test_accepts_no_table_statement(config: Nl2DataConfig) -> None:
    """A constant SELECT without any table passes."""
    result = _validate("SELECT 1", config)
    assert result.tables == []
    assert result.limit == 500


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO yellow VALUES (1)",
        "UPDATE yellow SET fare = 0",
        "DELETE FROM yellow",
        "CREATE TABLE evil (a INT)",
        "DROP TABLE yellow",
        "ALTER TABLE yellow ADD COLUMN x INT",
        "PRAGMA version",
        "SET memory_limit = '1GB'",
        "TRUNCATE TABLE yellow",
        "ATTACH 'x.db' AS x",
        "INSTALL httpfs",
        "GRANT SELECT ON yellow TO u",
        "CHECKPOINT",
        # V3-A audit: pin the remaining task-brief keywords so a future
        # refactor of the root-type table cannot silently drop them.
        "DETACH DATABASE x",
        "COPY yellow TO 'out.csv'",
        "CALL pragma_table_info('yellow')",
        "USE memory_limit",
        "RESET memory_limit",
        "REVOKE SELECT ON yellow FROM u",
    ],
)
def test_rejects_write_and_admin_statements(sql: str, config: Nl2DataConfig) -> None:
    """Non-read statement kinds are rejected as forbidden_statement."""
    _reject(sql, "forbidden_statement", config)


def test_rejects_command_fallback(config: Nl2DataConfig) -> None:
    """Statements sqlglot cannot fully parse become Command and are rejected."""
    _reject("LOAD httpfs", "forbidden_statement", config)


def test_rejects_multiple_statements(config: Nl2DataConfig) -> None:
    """Stacked statements are rejected even when the first one is harmless."""
    error = _reject("SELECT 1; DROP TABLE x", "multi_statement", config)
    assert "2" in str(error)


def test_rejects_unparseable_input(config: Nl2DataConfig) -> None:
    """Broken (or comment-shredded) SQL is a parse_error, never a pass."""
    _reject("SEL/**/ECT 1", "parse_error", config)
    _reject("SELECT FROM WHERE", "parse_error", config)
    _reject("", "parse_error", config)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('f.csv')",
        "SELECT * FROM read_parquet('p.parquet')",
        "SELECT * FROM glob('*.csv')",
    ],
)
def test_rejects_table_functions(sql: str, config: Nl2DataConfig) -> None:
    """Blocked table functions in FROM position are rejected by name."""
    error = _reject(sql, "forbidden_function", config)
    assert "FROM/JOIN" in str(error)


def test_rejects_table_function_nested_in_subquery(config: Nl2DataConfig) -> None:
    """Nesting a table function inside a derived table does not hide it."""
    error = _reject(
        "SELECT * FROM (SELECT * FROM read_json('j.json')) sub",
        "forbidden_function",
        config,
    )
    assert "read_json" in str(error)


def test_rejects_any_function_in_from_position(config: Nl2DataConfig) -> None:
    """Even non-blocked function relations are rejected (false rejects are OK)."""
    _reject("SELECT * FROM range(10)", "forbidden_function", config)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT duckdb_functions()",
        "SELECT current_setting('memory_limit')",
        "SELECT version()",
        "SELECT Current_Setting('x')",  # mixed case must not evade the check
        "SELECT pg_backend_pid()",
        "SELECT list_frobnicate(1)",  # unknown function: Anonymous keeps the raw name
    ],
)
def test_rejects_system_functions(sql: str, config: Nl2DataConfig) -> None:
    """Functions matching the blocked prefixes are rejected wherever they appear."""
    _reject(sql, "forbidden_function", config)


def test_rejects_unknown_table_and_lists_whitelist(config: Nl2DataConfig) -> None:
    """An unknown table names itself and the whole whitelist in the reason."""
    error = _reject("SELECT * FROM ghost_table", "unknown_table", config)
    message = str(error)
    assert "ghost_table" in message
    assert "yellow" in message
    assert "blue" in message


def test_rejects_unknown_table_in_join(config: Nl2DataConfig) -> None:
    """A whitelisted table does not launder an unknown JOIN partner."""
    _reject("SELECT y.taxi_id FROM yellow y JOIN ghost g ON 1 = 1", "unknown_table", config)


def test_rejects_unknown_column_with_candidates(config: Nl2DataConfig) -> None:
    """A typo'd column on the single referenced table gets fuzzy candidates."""
    error = _reject("SELECT faer FROM yellow", "unknown_column", config)
    message = str(error)
    assert "faer" in message
    assert "fare" in message  # difflib candidate from the yellow column set


def test_rejects_unknown_column_without_candidates(config: Nl2DataConfig) -> None:
    """A column with no similar names reports the empty-candidates fallback."""
    error = _reject("SELECT nonexistent FROM yellow", "unknown_column", config)
    assert "(无相似列)" in str(error)


def test_rejects_unknown_qualified_column(config: Nl2DataConfig) -> None:
    """Qualified columns are checked through their table alias."""
    _reject("SELECT y.nonexistent FROM yellow y", "unknown_column", config)
    _reject("SELECT b.borough2 FROM blue b", "unknown_column", config)


def test_unqualified_column_skipped_for_multi_table(config: Nl2DataConfig) -> None:
    """JOINs: bare columns are not verified (no name resolution, no false rejects).

    ``zone`` only exists on ``blue``, yet the statement passes because the
    unqualified-column check is intentionally disabled for multi-table
    statements; the tables themselves are already whitelisted.
    """
    sql = "SELECT zone FROM yellow y JOIN blue b ON y.taxi_id = b.taxi_id"
    result = _validate(sql, config)
    assert result.tables == ["yellow", "blue"]


def test_limit_capped_at_configured_cap(config: Nl2DataConfig) -> None:
    """An outer LIMIT above the cap is rewritten down to the cap."""
    result = _validate("SELECT taxi_id FROM yellow LIMIT 100000", config)
    assert result.limit == 10000
    assert "LIMIT 10000" in result.sql.upper()


def test_limit_within_cap_kept(config: Nl2DataConfig) -> None:
    """A LIMIT at or below the cap is kept untouched."""
    assert _validate("SELECT taxi_id FROM yellow LIMIT 10000", config).limit == 10000
    assert _validate("SELECT taxi_id FROM yellow LIMIT 10", config).limit == 10


def test_limit_offset_preserved(config: Nl2DataConfig) -> None:
    """Rewriting the LIMIT keeps a companion OFFSET intact."""
    result = _validate("SELECT taxi_id FROM yellow LIMIT 10 OFFSET 5", config)
    assert result.limit == 10
    assert "OFFSET 5" in result.sql.upper()


def test_outermost_limit_ignores_subquery_limit(config: Nl2DataConfig) -> None:
    """An inner LIMIT does not count as the outer one; it is left untouched."""
    sql = "SELECT * FROM (SELECT taxi_id FROM yellow LIMIT 99999) sub"
    result = _validate(sql, config)
    assert result.limit == 500  # default injected at the outermost level
    assert "99999" in result.sql  # inner LIMIT preserved as written


def test_non_constant_limit_replaced_by_default(config: Nl2DataConfig) -> None:
    """A non-constant LIMIT cannot be bounded, so it falls back to the default."""
    result = _validate("SELECT taxi_id FROM yellow LIMIT 1 + 1", config)
    assert result.limit == 500


def test_obfuscation_case_and_whitespace(config: Nl2DataConfig) -> None:
    """Keyword casing and spacing changes are still parsed and accepted."""
    result = _validate("sElEcT   taxi_id fRoM yellow", config)
    assert result.tables == ["yellow"]


def test_obfuscation_function_name_in_comment_is_inert(config: Nl2DataConfig) -> None:
    """A blocked function name inside a comment must not trigger anything."""
    result = _validate("SELECT /* read_csv('x') */ taxi_id FROM yellow", config)
    assert result.tables == ["yellow"]


def test_obfuscation_nested_system_function_still_rejected(
    config: Nl2DataConfig,
) -> None:
    """Nesting a blocked function in a scalar subquery cannot hide it."""
    error = _reject(
        "SELECT taxi_id FROM yellow WHERE fare > (SELECT version())",
        "forbidden_function",
        config,
    )
    assert "version" in str(error)


def test_obfuscation_drop_in_string_literal_is_inert(config: Nl2DataConfig) -> None:
    """A DROP statement quoted inside a literal is data, not SQL."""
    sql = "SELECT taxi_id FROM yellow WHERE tip = 'DROP TABLE users'"
    result = _validate(sql, config)
    assert result.tables == ["yellow"]
    assert result.limit == 500


def test_validated_sql_contract_single_table(config: Nl2DataConfig) -> None:
    """Contract: single-table query returns rewritten sql, tables and limit."""
    result = _validate("SELECT taxi_id FROM yellow LIMIT 999999", config)
    assert isinstance(result, ValidatedSQL)
    assert result.tables == ["yellow"]
    assert result.limit == 10000
    assert result.sql.upper().startswith("SELECT")
    assert "LIMIT 10000" in result.sql.upper()


def test_validated_sql_contract_join(config: Nl2DataConfig) -> None:
    """Contract: JOIN tables listed in parse-tree traversal (FROM first) order."""
    sql = "SELECT b.zone, y.fare FROM blue b JOIN yellow y ON b.taxi_id = y.taxi_id"
    result = _validate(sql, config)
    assert result.tables == ["blue", "yellow"]


def test_validated_sql_contract_cte(config: Nl2DataConfig) -> None:
    """Contract: CTE aliases never appear in tables; physical tables do."""
    sql = (
        "WITH c AS (SELECT taxi_id, tip FROM blue) "
        "SELECT c.taxi_id FROM c JOIN yellow y ON c.taxi_id = y.taxi_id"
    )
    result = _validate(sql, config)
    assert result.tables == ["yellow", "blue"]  # traversal order: FROM subtree first
    assert result.limit == 500


def test_rejects_non_select_statements_as_not_select(config: Nl2DataConfig) -> None:
    """Read-only statements outside the SELECT family are rejected, not mapped."""
    _reject("DESCRIBE yellow", "not_select", config)
    _reject("ANALYZE yellow", "not_select", config)
    _reject("SUMMARIZE SELECT 1", "not_select", config)


def test_rejects_export_import_database(config: Nl2DataConfig) -> None:
    """EXPORT/IMPORT DATABASE are rejected (parse_error in the duckdb dialect).

    Any rejection category counts: the safety property is "never executes",
    and sqlglot currently refuses to parse these statements at all.
    """
    import pytest as _pytest

    from guard.validate import GuardError

    for sql in ("EXPORT DATABASE 'dir'", "IMPORT DATABASE 'dir'"):
        with _pytest.raises(GuardError) as excinfo:
            validate(sql, {"yellow"}, {"yellow": {"fare"}}, config)
        assert excinfo.value.category in {"forbidden_statement", "parse_error"}
