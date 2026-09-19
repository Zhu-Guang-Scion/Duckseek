"""Parse-tree SQL guardrails for LLM-generated DuckDB queries (milestone 3, T10).

Every decision is made on the sqlglot parse tree (``dialect="duckdb"``), never
on the raw SQL text: comments, casing, whitespace and nesting cannot smuggle
anything past the checks. The module is deliberately conservative — when in
doubt a query is rejected with a human-readable reason instead of being let
through.

Pipeline (each step raises :class:`GuardError` on violation):

1. parse the whole input; reject multi-statement input;
2. the single statement must be a read query (``SELECT`` / set operation,
   optionally wrapped in ``WITH``); anything else is mapped to a readable
   keyword or rejected as not-a-select;
3. every table reference must resolve to the whitelist after CTE shadowing;
   function calls in FROM/JOIN relation position are rejected outright;
4. every function anywhere in the tree is checked against the configured
   blocked prefixes;
5. column references are checked against the per-table column map when the
   target table is statically known;
6. the outermost LIMIT is injected (default) or capped.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

import sqlglot
import sqlglot.expressions as exp

from nl2data.config import Nl2DataConfig

_CANDIDATE_COUNT = 3
_CANDIDATE_CUTOFF = 0.4
_NO_COLUMN_CANDIDATE = "(无相似列)"

# Root expression types that mean "statement kind a read-only session forbids".
# Missing names are skipped so the mapping survives sqlglot minor versions.
_FORBIDDEN_ROOT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("Insert", "INSERT"),
    ("Update", "UPDATE"),
    ("Delete", "DELETE"),
    ("Create", "CREATE"),
    ("Drop", "DROP"),
    ("Alter", "ALTER"),
    ("AlterTable", "ALTER"),
    ("Attach", "ATTACH"),
    ("Detach", "DETACH"),
    ("Copy", "COPY"),
    ("Install", "INSTALL"),
    ("Set", "SET"),
    ("Pragma", "PRAGMA"),
    ("Call", "CALL"),
    ("Use", "USE"),
    ("Grant", "GRANT"),
    ("Revoke", "REVOKE"),
    ("TruncateTable", "TRUNCATE"),
)

_FORBIDDEN_ROOTS: tuple[tuple[type[exp.Expression], str], ...] = tuple(
    (root_type, keyword)
    for name, keyword in _FORBIDDEN_ROOT_KEYWORDS
    if (root_type := getattr(exp, name, None)) is not None
)


class GuardError(Exception):
    """A rejected query: ``category`` is machine-readable, the message is not.

    ``category`` is one of ``not_select``, ``multi_statement``,
    ``forbidden_statement``, ``forbidden_function``, ``unknown_table``,
    ``unknown_column``, ``parse_error``.
    """

    def __init__(self, reason: str, category: str) -> None:
        super().__init__(reason)
        self.category = category


@dataclass(frozen=True)
class ValidatedSQL:
    """A query that passed validation, possibly rewritten.

    ``sql`` may differ from the input (LIMIT injected/capped); ``tables``
    lists the whitelisted tables the statement actually references, in
    sqlglot traversal order; ``limit`` is the effective outermost LIMIT.
    """

    sql: str
    tables: list[str]
    limit: int


def validate(
    sql: str,
    allowed_tables: set[str],
    column_map: dict[str, set[str]],
    cfg: Nl2DataConfig,
) -> ValidatedSQL:
    """Validate one SQL statement against the whitelist and repair its LIMIT.

    Args:
        sql: Candidate SQL produced by the LLM.
        allowed_tables: Whitelisted physical table names.
        column_map: Mapping of whitelisted table name to its safe column set.
        cfg: Active configuration (guard limits and blocked function lists).

    Returns:
        The validated, possibly rewritten query.

    Raises:
        GuardError: With a readable Chinese reason and a machine-readable
            ``category`` when any rule is violated.
    """
    root = _parse_single_statement(sql)
    _check_root(root)

    allowed_lower = {table.lower() for table in allowed_tables}
    columns_lower = {
        table.lower(): {column.lower() for column in columns}
        for table, columns in column_map.items()
    }
    tables = _check_tables(root, allowed_lower, cfg)
    _check_system_functions(root, cfg)
    _check_columns(root, columns_lower, tables)

    limit = _enforce_outer_limit(root, cfg)
    return ValidatedSQL(sql=root.sql(dialect="duckdb"), tables=tables, limit=limit)


def _parse_single_statement(sql: str) -> exp.Expression:
    """Parse ``sql`` and return its single statement, rejecting anything else."""
    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except Exception as exc:  # sqlglot raises ParseError and a few tokenizer types
        reason = f"SQL 解析失败:{exc}"
        raise GuardError(reason, "parse_error") from exc
    # Trailing semicolons surface as standalone Semicolon nodes (optionally
    # carrying a trailing comment); they are not second statements.
    statements = [
        statement
        for statement in statements
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    if not statements:
        raise GuardError("SQL 为空或无法解析", "parse_error")
    if len(statements) > 1:
        reason = f"仅允许单条 SQL 语句,实际解析出 {len(statements)} 条语句"
        raise GuardError(reason, "multi_statement")
    return statements[0]


def _check_root(root: exp.Expression) -> None:
    """Ensure the statement is a read query (SELECT / set operation / WITH)."""
    node = root
    while isinstance(node, exp.Subquery):
        # `((SELECT 1))` parses with nested Subquery roots; unwrap to the query.
        node = node.this
    if isinstance(node, (exp.Select, exp.SetOperation)):
        _check_no_write_nodes(root)
        return
    for root_type, keyword in _FORBIDDEN_ROOTS:
        if isinstance(node, root_type):
            reason = f"仅允许只读 SELECT 查询,检测到 {keyword} 语句"
            raise GuardError(reason, "forbidden_statement")
    if isinstance(node, exp.Command):
        # sqlglot falls back to Command for syntax it cannot fully parse
        # (e.g. LOAD/EXPORT variants); treat every such command as forbidden.
        head = str(node.this or node.name or "").split()[0] if node.this else node.name
        reason = f"仅允许只读 SELECT 查询,检测到命令语句 {head or type(node).__name__}"
        raise GuardError(reason, "forbidden_statement")
    if isinstance(node, exp.Column) and node.name.upper() == "CHECKPOINT":
        # sqlglot parses a bare `CHECKPOINT` as a column identifier quirk;
        # decide on the parse tree node, not on raw text.
        raise GuardError("仅允许只读 SELECT 查询,检测到 CHECKPOINT 语句", "forbidden_statement")
    reason = f"仅允许只读 SELECT 查询,检测到 {type(node).__name__} 语句"
    raise GuardError(reason, "not_select")


def _check_no_write_nodes(root: exp.Expression) -> None:
    """Reject write/DDL nodes anywhere in the tree (writable-CTE bypass)."""
    for root_type, keyword in _FORBIDDEN_ROOTS:
        for _match in root.find_all(root_type):
            reason = f"仅允许只读 SELECT 查询,检测到嵌套的 {keyword} 语句"
            raise GuardError(reason, "forbidden_statement")
    for select in root.find_all(exp.Select):
        if select.args.get("into") is not None:
            raise GuardError(
                "仅允许只读 SELECT 查询,检测到 SELECT INTO 写入语句",
                "forbidden_statement",
            )


def _scoped_cte_names(node: exp.Expression) -> set[str]:
    """Names of the CTEs visible to ``node`` (its scope chain up to the root).

    A table name shadowed by a CTE in scope resolves to the CTE (local
    priority, matching DuckDB), so it is exempt from the whitelist check.
    CTEs from sibling scopes are intentionally *not* collected, so a CTE name
    cannot be used to reach a same-named non-whitelisted physical table
    outside its scope.
    """
    names: set[str] = set()
    current: exp.Expression | None = node
    while current is not None:
        with_clause = current.args.get("with_")
        if with_clause is not None:
            names.update(cte.alias.lower() for cte in with_clause.expressions)
        current = current.parent
    return names


def _function_name(func: exp.Expression) -> str:
    """Best-effort function name of a ``Func``/``Anonymous`` node (unlowered)."""
    if isinstance(func, exp.Anonymous):
        return func.name
    return func.sql_name()


def _check_tables(
    root: exp.Expression,
    allowed_lower: set[str],
    cfg: Nl2DataConfig,
) -> list[str]:
    """Validate every table reference; return whitelisted tables in order.

    Table nodes whose ``this`` is not an identifier are functions used as
    relations (``read_csv('f')``, ``glob('*.c')``, ``range(10)`` ...). They can
    read arbitrary files or attach external systems, so all of them are
    rejected — even ones outside ``blocked_table_functions`` — because a
    function call is never a whitelisted physical table (better a false
    rejection than a leak).
    """
    tables: list[str] = []
    for table in root.find_all(exp.Table):
        inner = table.this
        if not isinstance(inner, exp.Identifier):
            function_name = (
                _function_name(inner).lower() if isinstance(inner, exp.Func) else ""
            )
            if function_name in cfg.guard.blocked_table_functions:
                reason = f"FROM/JOIN 位置禁止使用表函数 {function_name}"
            else:
                source = function_name or type(inner).__name__
                reason = (
                    f"FROM/JOIN 位置出现不受支持的表源 {source},"
                    "仅允许白名单中的物理表"
                )
            raise GuardError(reason, "forbidden_function")
        name = table.name.lower()
        if not name or name in _scoped_cte_names(table):
            continue
        if name not in allowed_lower:
            listing = ", ".join(sorted(allowed_lower)) if allowed_lower else "(无)"
            reason = f"表 {name} 不在允许的表清单中,允许的表:{listing}"
            raise GuardError(reason, "unknown_table")
        if name not in tables:
            tables.append(name)
    return tables


def _check_system_functions(root: exp.Expression, cfg: Nl2DataConfig) -> None:
    """Reject any function whose lowercase name hits a blocked prefix.

    Matching is substring containment rather than strict startswith: sqlglot
    normalizes ``version()`` to ``CURRENT_VERSION``, so a plain prefix match
    on "version" would miss it. Containment is strictly more conservative,
    which is the desired failure direction.
    """
    for node in root.find_all(exp.Func):
        name = _function_name(node).lower()
        if not name:
            continue
        for prefix in cfg.guard.blocked_function_prefixes:
            if prefix in name:
                reason = f"禁止使用系统函数 {name}(命中屏蔽前缀 {prefix})"
                raise GuardError(reason, "forbidden_function")


def _check_columns(
    root: exp.Expression,
    columns_lower: dict[str, set[str]],
    tables: list[str],
) -> None:
    """Validate column references against the catalog-derived column map.

    Qualified columns are resolved through the statement's alias map. Columns
    of CTEs and subquery aliases are statically unknowable and therefore
    skipped. Unqualified columns are only checked when the statement references
    exactly one whitelisted table; with several tables (JOINs) the target table
    of a bare column cannot be determined without full name resolution, so the
    check is skipped. This is knowingly permissive, but a JOIN is not an
    escalation path (all its tables are already whitelisted), while name
    resolution here would cause mass false rejections of valid joins.
    """
    aliases: dict[str, str | None] = {}
    for table in root.find_all(exp.Table):
        if isinstance(table.this, exp.Identifier) and table.name:
            aliases[table.alias_or_name.lower()] = table.name.lower()
    for subquery in root.find_all(exp.Subquery):
        if subquery.alias:
            aliases[subquery.alias.lower()] = None

    single_table = tables[0] if len(tables) == 1 else None
    for column in root.find_all(exp.Column):
        qualifier = (column.table or "").lower()
        name = column.name.lower()
        if qualifier:
            target = aliases.get(qualifier, qualifier)
            if target is None or target not in columns_lower:
                continue  # CTE / subquery alias: columns unknowable, skip
            _require_column(target, name, columns_lower[target])
        elif single_table is not None and single_table in columns_lower:
            _require_column(single_table, name, columns_lower[single_table])
        # A qualifier naming a CTE falls into the same skip: the physical
        # table with that name is shadowed, so there is nothing to verify.


def _require_column(table: str, column: str, known: set[str]) -> None:
    """Raise ``unknown_column`` with fuzzy candidates when ``column`` is unknown."""
    if column in known:
        return
    candidates = difflib.get_close_matches(
        column, sorted(known), n=_CANDIDATE_COUNT, cutoff=_CANDIDATE_CUTOFF
    )
    joined = "、".join(candidates) if candidates else _NO_COLUMN_CANDIDATE
    reason = f"表 {table} 的列 {column} 不存在,候选:{joined}"
    raise GuardError(reason, "unknown_column")


def _set_limit(root: exp.Expression, value: int) -> None:
    """Set the outermost LIMIT of ``root`` to ``value``, keeping any OFFSET."""
    root.set("limit", exp.Limit(expression=exp.Literal.number(value)))


def _enforce_outer_limit(root: exp.Expression, cfg: Nl2DataConfig) -> int:
    """Inject or cap the outermost LIMIT; inner (subquery) LIMITs are ignored.

    A non-constant LIMIT cannot be bounded statically, so it is overwritten
    with the default instead of being trusted.
    """
    limit_node = root.args.get("limit")
    if limit_node is None:
        _set_limit(root, cfg.guard.default_limit)
        return cfg.guard.default_limit
    expression = limit_node.args.get("expression")
    if isinstance(expression, exp.Literal) and expression.is_int:
        value = int(expression.this)
        if value <= cfg.guard.limit_cap:
            return value
        _set_limit(root, cfg.guard.limit_cap)
        return cfg.guard.limit_cap
    _set_limit(root, cfg.guard.default_limit)
    return cfg.guard.default_limit
