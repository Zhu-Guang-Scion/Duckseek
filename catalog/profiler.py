"""Table profiling via aggregate SQL (milestone 1, T4).

For each catalog table one aggregate query computes all column statistics in a
single pass (no per-column full scans); enum value lists and the row sample
use two small follow-up queries. Tables above ``profile.sampled_over_rows``
switch to ``USING SAMPLE`` and record ``"sampled": true``. A failing column
degrades to an ``error`` key instead of aborting the table. Output JSON goes
to ``data/catalog/profiles/<table>.json`` per the T4 task brief.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from catalog.store import CatalogStore, TableEntry
from nl2data.config import Nl2DataConfig

logger = logging.getLogger(__name__)

_NUMERIC_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
)
_TEMPORAL_TYPES = ("DATE", "TIME", "TIMESTAMP")
_ENUM_TYPES = ("VARCHAR", "BOOLEAN")


class ProfileError(RuntimeError):
    """Raised when a table cannot be profiled."""


def _quote(identifier: str) -> str:
    """Quote a DuckDB identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _category(dtype: str) -> str:
    """Map a DuckDB type string to numeric/temporal/other."""
    upper = dtype.upper()
    if upper.startswith(_NUMERIC_TYPES):
        return "numeric"
    if upper.startswith(_TEMPORAL_TYPES):
        return "temporal"
    return "other"


def _jsonable(value: Any) -> Any:
    """Convert a DuckDB scalar into a JSON-safe counterpart."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _quantile_labels(cfg: Nl2DataConfig) -> list[str]:
    """Labels like ``p25`` for the configured quantiles."""
    return [f"p{int(q * 100)}" for q in cfg.profile.quantiles]


def _sample_clause(cfg: Nl2DataConfig) -> str:
    """FROM-clause suffix applying the configured sampling fraction."""
    return f" USING SAMPLE {cfg.profile.sample_fraction * 100:g} PERCENT (bernoulli)"


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    """Return (name, duckdb_type) pairs for a table, raising when missing."""
    try:
        described = conn.execute(f"DESCRIBE SELECT * FROM {_quote(table)}").fetchall()
    except duckdb.Error as exc:
        msg = f"table {table!r} not found or not queryable: {exc}"
        raise ProfileError(msg) from exc
    return [(str(row[0]), str(row[1])) for row in described]


def _fetch_one(conn: duckdb.DuckDBPyConnection, sql: str) -> dict[str, Any]:
    """Execute a single-row query and return an alias -> value mapping."""
    cursor = conn.execute(sql)
    names = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    if row is None:
        return {}
    return dict(zip(names, row, strict=True))


def _aggregate_sql(
    table: str,
    columns: list[tuple[str, str]],
    cfg: Nl2DataConfig,
    sampled: bool,
) -> str:
    """Build the single-pass aggregate query covering every column.

    In sampled mode min/max are excluded here: they are computed exactly in
    a separate non-sampled aggregate (cheap on columnar storage), because
    approximate bounds would drift between runs.
    """
    parts: list[str] = ["count(*) AS __rows_sampled__"]
    for index, (name, dtype) in enumerate(columns):
        quoted = _quote(name)
        parts.append(f"count({quoted}) AS __n_{index}")
        parts.append(f"count(DISTINCT {quoted}) AS __d_{index}")
        category = _category(dtype)
        if not sampled and category in ("numeric", "temporal"):
            parts.append(f"min({quoted}) AS __min_{index}")
            parts.append(f"max({quoted}) AS __max_{index}")
        if category == "numeric":
            qlist = ", ".join(repr(q) for q in cfg.profile.quantiles)
            parts.append(
                f"quantile_cont(CAST({quoted} AS DOUBLE), [{qlist}]) AS __q_{index}"
            )
        elif dtype.upper() == "VARCHAR":
            parts.append(f"avg(length({quoted})) AS __l_{index}")
    sample = _sample_clause(cfg) if sampled else ""
    return f"SELECT {', '.join(parts)} FROM {_quote(table)}{sample}"


def _exact_minmax_sql(
    table: str,
    columns: list[tuple[str, str]],
    indices: list[int],
) -> str:
    """Build one non-sampled query with exact min/max for the given columns."""
    parts: list[str] = []
    for index in indices:
        quoted = _quote(columns[index][0])
        parts.append(f"min({quoted}) AS __min_{index}")
        parts.append(f"max({quoted}) AS __max_{index}")
    return f"SELECT {', '.join(parts)} FROM {_quote(table)}"


def _quantile_dict(raw: Any, cfg: Nl2DataConfig) -> dict[str, float | None]:
    """Convert a DuckDB quantile list into the ``p25``-style contract dict."""
    if raw is None:
        return {}
    return {
        label: float(value)
        for label, value in zip(_quantile_labels(cfg), raw, strict=True)
    }


def _assemble_column(
    col: dict[str, Any],
    values: dict[str, Any],
    index: int,
    dtype: str,
    cfg: Nl2DataConfig,
    basis_rows: int,
    minmax: tuple[Any, Any] | None,
) -> None:
    """Fill one column's stats from the single-pass aggregate values.

    ``basis_rows`` is the row count the aggregate itself ran on: the exact
    table row count normally, or the sampled row count in sampled mode, so
    null_rate always reflects the same data the other statistics describe.
    ``minmax`` carries the (possibly separately computed, always exact)
    min/max pair for numeric/temporal columns.
    """
    non_null = int(values[f"__n_{index}"])
    col["null_rate"] = (
        round((basis_rows - non_null) / basis_rows, 6) if basis_rows else None
    )
    col["distinct_count"] = int(values[f"__d_{index}"])
    if minmax is not None and minmax[0] is not None:
        col["min"] = _jsonable(minmax[0])
        col["max"] = _jsonable(minmax[1])
    category = _category(dtype)
    if category == "numeric":
        quantiles = _quantile_dict(values[f"__q_{index}"], cfg)
        if quantiles:
            col["quantiles"] = quantiles
    if dtype.upper() == "VARCHAR" and values[f"__l_{index}"] is not None:
        col["avg_len"] = round(float(values[f"__l_{index}"]), 2)


def _optional_stats(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    name: str,
    dtype: str,
    cfg: Nl2DataConfig,
    sampled: bool,
) -> dict[str, Any]:
    """Compute min/max/quantiles/avg_len for one column via small queries.

    min/max are always exact (non-sampled); quantiles/avg_len follow the
    sampling mode.
    """
    stats: dict[str, Any] = {}
    quoted = _quote(name)
    sample = _sample_clause(cfg) if sampled else ""
    from_clause = f" FROM {_quote(table)}{sample}"
    exact_from = f" FROM {_quote(table)}"
    category = _category(dtype)
    if category in ("numeric", "temporal"):
        pair = _fetch_one(
            conn,
            f"SELECT min({quoted}) AS lo, max({quoted}) AS hi{exact_from}",
        )
        if pair["lo"] is not None:
            stats["min"] = _jsonable(pair["lo"])
            stats["max"] = _jsonable(pair["hi"])
    if category == "numeric":
        qlist = ", ".join(repr(q) for q in cfg.profile.quantiles)
        raw = _fetch_one(
            conn,
            f"SELECT quantile_cont(CAST({quoted} AS DOUBLE), [{qlist}]) AS v{from_clause}",
        )["v"]
        quantiles = _quantile_dict(raw, cfg)
        if quantiles:
            stats["quantiles"] = quantiles
    if dtype.upper() == "VARCHAR":
        avg = _fetch_one(conn, f"SELECT avg(length({quoted})) AS v{from_clause}")["v"]
        if avg is not None:
            stats["avg_len"] = round(float(avg), 2)
    return stats


def _add_enum_values(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    out_columns: list[dict[str, Any]],
    cfg: Nl2DataConfig,
    sampled: bool,
) -> None:
    """Attach ``enum_values`` to low-cardinality string/boolean columns."""
    eligible = [
        (index, col)
        for index, col in enumerate(out_columns)
        if col.get("distinct_count", 0) and col["distinct_count"] <= cfg.profile.enum_max_distinct
        and col["dtype"].upper() in _ENUM_TYPES
    ]
    if not eligible:
        return
    sample = _sample_clause(cfg) if sampled else ""
    from_clause = f" FROM {_quote(table)}{sample}"
    try:
        parts = [
            f"list(DISTINCT {_quote(col['name'])}) AS __e_{index}"
            for index, col in eligible
        ]
        values = _fetch_one(conn, f"SELECT {', '.join(parts)}{from_clause}")
        rows: list[Any | None] = [values[f"__e_{index}"] for index, _ in eligible]
    except duckdb.Error:
        rows = []
        for _index, col in eligible:
            try:
                single = _fetch_one(
                    conn,
                    f"SELECT list(DISTINCT {_quote(col['name'])}) AS v{from_clause}",
                )
                rows.append(single["v"])
            except duckdb.Error:
                rows.append("__error__")
    for (index, col), row in zip(eligible, rows, strict=True):
        if isinstance(row, str) and row == "__error__":
            col["error"] = "failed to list distinct values"
            continue
        # DuckDB keeps NULL inside list(DISTINCT ...); drop it before sorting.
        col["enum_values"] = sorted(
            _jsonable(v) for v in (row or []) if v is not None
        )


def _add_sample_values(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    out_columns: list[dict[str, Any]],
    cfg: Nl2DataConfig,
    sampled: bool,
) -> None:
    """Attach first-seen distinct non-null ``sample_values`` per column."""
    sample = _sample_clause(cfg) if sampled else ""
    rows = conn.execute(
        f"SELECT * FROM {_quote(table)}{sample} LIMIT {int(cfg.profile.sample_rows)}"
    ).fetchall()
    limit = cfg.profile.sample_values_limit
    samples: list[list[Any]] = [[] for _ in out_columns]
    seen: list[set[Any]] = [set() for _ in out_columns]
    for row in rows:
        for index, value in enumerate(row):
            if value is None or len(samples[index]) >= limit:
                continue
            key = _jsonable(value)
            if key not in seen[index]:
                seen[index].add(key)
                samples[index].append(key)
    for index, col in enumerate(out_columns):
        col["sample_values"] = samples[index]


def profile_table(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    cfg: Nl2DataConfig,
    *,
    source: str | None = None,
    entry: TableEntry | None = None,
) -> dict[str, Any]:
    """Profile one table and return the T4 profile dict.

    Args:
        conn: Open DuckDB connection whose catalog contains ``table``.
        table: Clean table (view) name.
        cfg: Active configuration; supplies all profiling thresholds.
        source: Source slug recorded in the profile header when given.
        entry: Catalog entry supplying ``original_name`` mappings when given.

    Returns:
        Profile dict: ``table``, ``original_name``/``source`` when known,
        ``rows``, ``profiled_at``, optional ``sampled`` and ``columns``.

    Raises:
        ProfileError: If the table does not exist.
    """
    columns = _table_columns(conn, table)
    total_rows = int(_fetch_one(conn, f"SELECT count(*) AS n FROM {_quote(table)}")["n"])
    sampled = total_rows > cfg.profile.sampled_over_rows
    sample_clause = _sample_clause(cfg) if sampled else ""
    # Row count the per-column statistics are computed on: exact normally,
    # sampled-row count in sampled mode (rows itself stays exact).
    basis_rows = total_rows
    if sampled:
        basis_rows = int(
            _fetch_one(
                conn, f"SELECT count(*) AS n FROM {_quote(table)}{sample_clause}"
            )["n"]
        )

    try:
        values: dict[str, Any] | None = _fetch_one(
            conn, _aggregate_sql(table, columns, cfg, sampled)
        )
    except duckdb.Error as exc:
        logger.warning("aggregate query failed on %s (%s); degrading", table, exc)
        values = None

    # In sampled mode min/max stay exact: one non-sampled aggregate over all
    # numeric/temporal columns (cheap on columnar storage, stable across runs).
    minmax_indices = [
        index
        for index, (_name, dtype) in enumerate(columns)
        if _category(dtype) in ("numeric", "temporal")
    ]
    exact_minmax: dict[str, Any] | None = None
    if sampled and minmax_indices:
        try:
            exact_minmax = _fetch_one(conn, _exact_minmax_sql(table, columns, minmax_indices))
        except duckdb.Error as exc:
            logger.warning("exact min/max query failed on %s: %s", table, exc)

    originals = {col.name: col.original_name for col in entry.columns} if entry else {}
    out_columns: list[dict[str, Any]] = []
    for index, (name, dtype) in enumerate(columns):
        col: dict[str, Any] = {
            "name": name,
            "original_name": originals.get(name, name),
            "dtype": dtype,
        }
        try:
            if values is not None:
                basis = int(values.get("__rows_sampled__", basis_rows)) or basis_rows
                minmax: tuple[Any, Any] | None = None
                if _category(dtype) in ("numeric", "temporal"):
                    if exact_minmax is not None and f"__min_{index}" in exact_minmax:
                        minmax = (exact_minmax[f"__min_{index}"], exact_minmax[f"__max_{index}"])
                    elif not sampled:
                        minmax = (values[f"__min_{index}"], values[f"__max_{index}"])
                _assemble_column(col, values, index, dtype, cfg, basis, minmax)
            else:
                col.update(_optional_stats(conn, table, name, dtype, cfg, sampled))
                single = _fetch_one(
                    conn,
                    f"SELECT count({_quote(name)}) AS n, "
                    f"count(DISTINCT {_quote(name)}) AS d "
                    f"FROM {_quote(table)}{sample_clause}",
                )
                col["null_rate"] = (
                    round((basis_rows - int(single["n"])) / basis_rows, 6)
                    if basis_rows
                    else None
                )
                col["distinct_count"] = int(single["d"])
        except (duckdb.Error, TypeError, ValueError, KeyError) as exc:
            col["error"] = str(exc)
        out_columns.append(col)

    try:
        _add_enum_values(conn, table, out_columns, cfg, sampled)
        _add_sample_values(conn, table, out_columns, cfg, sampled)
    except duckdb.Error as exc:
        for col in out_columns:
            col.setdefault("error", f"post-processing failed: {exc}")

    profile: dict[str, Any] = {"table": table}
    if entry is not None:
        profile["original_name"] = entry.original_name
    if source is not None:
        profile["source"] = source
    profile["rows"] = total_rows
    profile["profiled_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if sampled:
        profile["sampled"] = True
    profile["columns"] = out_columns
    return profile


def write_profile(profile: dict[str, Any], cfg: Nl2DataConfig) -> Path:
    """Write a profile dict to ``data/catalog/profiles/<table>.json``.

    Returns:
        The path of the written JSON file.
    """
    cfg.paths.profiles_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.paths.profiles_dir / f"{profile['table']}.json"
    path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def catalog_tables(cfg: Nl2DataConfig) -> dict[str, tuple[str, TableEntry]]:
    """Map every clean table name to its (source slug, catalog entry)."""
    store = CatalogStore(cfg.paths.catalog)
    return {
        table.name: (source.name, table)
        for source in store.sources
        for table in source.tables
    }


def _is_fresh(parquet_path: Path, profile_path: Path) -> bool:
    """True when the profile is newer than the Parquet artifact."""
    if not parquet_path.exists() or not profile_path.exists():
        return False
    return profile_path.stat().st_mtime >= parquet_path.stat().st_mtime


def _resolve_parquet(cfg: Nl2DataConfig, recorded: str) -> Path:
    """Resolve a catalog-recorded Parquet path against the data root."""
    path = Path(recorded)
    return path if path.is_absolute() else cfg.paths.data_dir.parent / path


def run_profiles(
    cfg: Nl2DataConfig,
    tables: list[str] | None = None,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Profile catalog tables (default: all) and write their profile JSON.

    Incremental by default: a table is skipped when its profile file is
    newer than its Parquet artifact; pass ``force=True`` to recompute.

    Args:
        cfg: Active configuration.
        tables: Clean table names to profile; None means all catalog tables.
        force: Recompute even when the stored profile is up to date.

    Returns:
        Profile dicts for the tables computed in this run (skips excluded).

    Raises:
        ProfileError: If a table is unknown to the catalog or the warehouse
            database is missing while tables exist.
    """
    known = catalog_tables(cfg)
    targets = list(known) if tables is None else list(tables)
    for table in targets:
        if table not in known:
            known_list = ", ".join(sorted(known)) if known else "<none>"
            msg = f"table {table!r} is not in the catalog (known: {known_list})"
            raise ProfileError(msg)
    if not targets:
        logger.warning("no tables in the catalog; nothing to profile")
        return []
    if not cfg.paths.warehouse.exists():
        msg = f"warehouse not found at {cfg.paths.warehouse}; run ingest first"
        raise ProfileError(msg)

    profiles: list[dict[str, Any]] = []
    conn = duckdb.connect(str(cfg.paths.warehouse), read_only=True)
    try:
        for table in targets:
            source_name, entry = known[table]
            if not force and _is_fresh(
                _resolve_parquet(cfg, entry.parquet),
                cfg.paths.profiles_dir / f"{table}.json",
            ):
                logger.info("profile for %s is up to date; skipping", table)
                continue
            profile = profile_table(conn, table, cfg, source=source_name, entry=entry)
            write_profile(profile, cfg)
            profiles.append(profile)
            logger.info("profiled %s (%d rows)", table, profile["rows"])
    finally:
        conn.close()
    return profiles
