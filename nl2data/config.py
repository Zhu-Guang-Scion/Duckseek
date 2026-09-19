"""Configuration loading: paths and thresholds live in YAML, never in code.

The config file is resolved as:

1. the explicit ``path`` argument, else
2. the ``NL2DATA_CONFIG`` environment variable, else
3. ``config.yaml`` at the repository root (next to the ``nl2data`` package).

All configured paths are resolved relative to the config file's directory
and stored absolute.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV_VAR = "NL2DATA_CONFIG"
DEFAULT_CONFIG_FILENAME = "config.yaml"


class ConfigError(RuntimeError):
    """Raised when the configuration file is missing or malformed."""


@dataclass(frozen=True)
class PathsConfig:
    """Artifact locations, all absolute."""

    data_dir: Path
    parquet_dir: Path
    warehouse: Path
    catalog: Path
    profiles_dir: Path
    glossary: Path
    cards_dir: Path
    cards_md_dir: Path
    table_notes: Path
    index_dir: Path
    scratch_dir: Path
    audit_dir: Path


@dataclass(frozen=True)
class IngestConfig:
    """Ingestion behaviour."""

    max_name_length: int = 63
    header_scan_rows: int = 5


@dataclass(frozen=True)
class MdbToolsConfig:
    """mdbtools binaries used by the Access ingestion path.

    ``date_format`` / ``datetime_format`` are passed to ``mdb-export`` via
    ``-D`` / ``-T`` because its libmdb defaults follow the system locale.
    """

    mdb_tables_cmd: str = "mdb-tables"
    mdb_export_cmd: str = "mdb-export"
    timeout_seconds: int = 300
    date_format: str = "%Y-%m-%d"
    datetime_format: str = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class ProfileConfig:
    """Profiling thresholds (milestone 1, T4)."""

    enum_max_distinct: int = 50
    sample_values_limit: int = 5
    sample_rows: int = 100
    quantiles: tuple[float, ...] = (0.25, 0.5, 0.75)
    sampled_over_rows: int = 5_000_000
    sample_fraction: float = 0.1


@dataclass(frozen=True)
class CardsConfig:
    """Table-card generation thresholds (milestone 2, T5)."""

    enum_value_max_chars: int = 50
    markdown_column_fold_limit: int = 100


@dataclass(frozen=True)
class TokensConfig:
    """Token estimation strategy (milestone 2, shared with retrieval)."""

    estimator: str = "len_div_4"


@dataclass(frozen=True)
class RetrievalConfig:
    """Hybrid retrieval parameters (milestone 2, T7).

    Embedding credentials come from the environment (goals.md decision 8):
    ``EMB_BASE_URL`` / ``EMB_API_KEY`` / ``EMB_MODEL`` — never from this file.
    """

    top_k: int = 5
    token_budget: int = 2000
    rrf_k: int = 60
    weight_vector: float = 0.5
    weight_bm25: float = 0.5
    embedding_batch_size: int = 32
    embedding_timeout_seconds: int = 30
    embedding_max_retries: int = 5
    embedding_dimensions: int = 1024


@dataclass(frozen=True)
class LlmConfig:
    """LLM chat parameters (milestone 3, T8).

    Credentials come from the environment (goals.md decision 3):
    ``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL`` — never from this file.
    """

    temperature: float = 0.0
    max_retries: int = 3
    timeout_seconds: int = 60
    max_tokens: int = 4096


@dataclass(frozen=True)
class GuardConfig:
    """SQL guardrail parameters (milestone 3, T10)."""

    default_limit: int = 500
    limit_cap: int = 10_000
    # Function-name prefixes rejected wherever they appear in the tree.
    blocked_function_prefixes: tuple[str, ...] = (
        "duckdb_",
        "list_",
        "pg_",
        "current_setting",
        "version",
    )
    # Table functions rejected inside FROM/JOIN clauses (external access).
    blocked_table_functions: tuple[str, ...] = (
        "read_csv",
        "read_parquet",
        "read_xlsx",
        "read_json",
        "read_blob",
        "glob",
        "sqlite_scan",
        "sqlite_attach",
        "postgres_scan",
        "mysql_scan",
        "delta_scan",
        "iceberg_scan",
    )


@dataclass(frozen=True)
class SqlgenConfig:
    """SQL generation prompt parameters (milestone 3, T9)."""

    few_shot_count: int = 2


@dataclass(frozen=True)
class QaConfig:
    """Ask-loop parameters (milestone 3, T12)."""

    max_attempts: int = 3


@dataclass(frozen=True)
class EvalConfig:
    """Regression-eval LLM parameters (milestone 4, T15/G8).

    ``temperature`` pins eval-time sampling determinism; the e2e runner
    overrides ``cfg.llm`` with it so interactive and eval behaviour stay
    independently tunable. ``runs_per_case`` repeats each case and takes
    the majority L3 verdict (G8b) to absorb residual model nondeterminism.
    """

    temperature: float = 0.0
    runs_per_case: int = 1


@dataclass(frozen=True)
class ExecConfig:
    """Sandboxed execution parameters (milestone 3, T11)."""

    timeout_seconds: int = 60
    sample_rows: int = 20
    profile_over_rows: int = 50
    text_top_n: int = 5


@dataclass(frozen=True)
class Nl2DataConfig:
    """Root configuration object handed to every module."""

    paths: PathsConfig
    ingest: IngestConfig = IngestConfig()
    mdbtools: MdbToolsConfig = MdbToolsConfig()
    profile: ProfileConfig = ProfileConfig()
    cards: CardsConfig = CardsConfig()
    tokens: TokensConfig = TokensConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    llm: LlmConfig = LlmConfig()
    guard: GuardConfig = GuardConfig()
    sqlgen: SqlgenConfig = SqlgenConfig()
    exec: ExecConfig = ExecConfig()
    qa: QaConfig = QaConfig()
    eval: EvalConfig = EvalConfig()


def default_config_path() -> Path:
    """Return the repository-root ``config.yaml`` path."""
    return Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_FILENAME


def _resolve(base: Path, value: Path) -> Path:
    """Return ``value`` as an absolute path under ``base`` when relative."""
    return value if value.is_absolute() else base / value


def _build_paths(section: dict[str, Any], base: Path) -> PathsConfig:
    """Build :class:`PathsConfig` with contract defaults for missing keys."""
    data_dir = _resolve(base, Path(str(section.get("data_dir", "data"))))
    return PathsConfig(
        data_dir=data_dir,
        parquet_dir=_resolve(base, Path(str(section.get("parquet_dir", data_dir / "parquet")))),
        warehouse=_resolve(
            base, Path(str(section.get("warehouse", data_dir / "warehouse.duckdb")))
        ),
        catalog=_resolve(
            base,
            Path(str(section.get("catalog", data_dir / "catalog" / "catalog.yaml"))),
        ),
        profiles_dir=_resolve(
            base,
            Path(str(section.get("profiles_dir", data_dir / "catalog" / "profiles"))),
        ),
        glossary=_resolve(
            base,
            Path(str(section.get("glossary", data_dir / "catalog" / "glossary.yaml"))),
        ),
        cards_dir=_resolve(
            base,
            Path(str(section.get("cards_dir", data_dir / "catalog" / "cards"))),
        ),
        cards_md_dir=_resolve(
            base,
            Path(str(section.get("cards_md_dir", data_dir / "catalog" / "cards_md"))),
        ),
        table_notes=_resolve(
            base, Path(str(section.get("table_notes", base / "docs" / "table_notes.md")))
        ),
        index_dir=_resolve(
            base, Path(str(section.get("index_dir", data_dir / "index")))
        ),
        scratch_dir=_resolve(
            base, Path(str(section.get("scratch_dir", data_dir / "scratch")))
        ),
        audit_dir=_resolve(
            base, Path(str(section.get("audit_dir", data_dir / "audit")))
        ),
    )


def load_config(path: Path | None = None) -> Nl2DataConfig:
    """Load and validate the nl2data configuration.

    Args:
        path: Explicit config file path; falls back to ``NL2DATA_CONFIG``
            and then to the repository-root ``config.yaml``.

    Returns:
        The fully resolved configuration.

    Raises:
        ConfigError: If the config file is missing or not a mapping.
    """
    if path is None:
        env_value = os.environ.get(CONFIG_ENV_VAR, "")
        path = Path(env_value) if env_value else default_config_path()
    path = Path(path)
    if not path.is_file():
        msg = f"config file not found: {path}"
        raise ConfigError(msg)
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"invalid YAML in {path}: {exc}"
        raise ConfigError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"config root must be a mapping, got {type(raw).__name__}"
        raise ConfigError(msg)

    base = path.resolve().parent
    ingest_section: dict[str, Any] = raw.get("ingest") or {}
    mdb_section: dict[str, Any] = raw.get("mdbtools") or {}
    profile_section: dict[str, Any] = raw.get("profile") or {}
    cards_section: dict[str, Any] = raw.get("cards") or {}
    tokens_section: dict[str, Any] = raw.get("tokens") or {}
    retrieval_section: dict[str, Any] = raw.get("retrieval") or {}
    llm_section: dict[str, Any] = raw.get("llm") or {}
    guard_section: dict[str, Any] = raw.get("guard") or {}
    sqlgen_section: dict[str, Any] = raw.get("sqlgen") or {}
    exec_section: dict[str, Any] = raw.get("exec") or {}
    qa_section: dict[str, Any] = raw.get("qa") or {}
    eval_section: dict[str, Any] = raw.get("eval") or {}
    return Nl2DataConfig(
        paths=_build_paths(raw.get("paths") or {}, base),
        ingest=IngestConfig(
            max_name_length=int(ingest_section.get("max_name_length", 63)),
            header_scan_rows=int(ingest_section.get("header_scan_rows", 5)),
        ),
        mdbtools=MdbToolsConfig(
            mdb_tables_cmd=str(mdb_section.get("mdb_tables_cmd", "mdb-tables")),
            mdb_export_cmd=str(mdb_section.get("mdb_export_cmd", "mdb-export")),
            timeout_seconds=int(mdb_section.get("timeout_seconds", 300)),
            date_format=str(mdb_section.get("date_format", "%Y-%m-%d")),
            datetime_format=str(mdb_section.get("datetime_format", "%Y-%m-%d %H:%M:%S")),
        ),
        profile=ProfileConfig(
            enum_max_distinct=int(profile_section.get("enum_max_distinct", 50)),
            sample_values_limit=int(profile_section.get("sample_values_limit", 5)),
            sample_rows=int(profile_section.get("sample_rows", 100)),
            quantiles=tuple(
                float(q)
                for q in profile_section.get("quantiles", (0.25, 0.5, 0.75))
            ),
            sampled_over_rows=int(profile_section.get("sampled_over_rows", 5_000_000)),
            sample_fraction=float(profile_section.get("sample_fraction", 0.1)),
        ),
        cards=CardsConfig(
            enum_value_max_chars=int(cards_section.get("enum_value_max_chars", 50)),
            markdown_column_fold_limit=int(
                cards_section.get("markdown_column_fold_limit", 100)
            ),
        ),
        tokens=TokensConfig(estimator=str(tokens_section.get("estimator", "len_div_4"))),
        retrieval=RetrievalConfig(
            top_k=int(retrieval_section.get("top_k", 5)),
            token_budget=int(retrieval_section.get("token_budget", 2000)),
            rrf_k=int(retrieval_section.get("rrf_k", 60)),
            weight_vector=float(retrieval_section.get("weight_vector", 0.5)),
            weight_bm25=float(retrieval_section.get("weight_bm25", 0.5)),
            embedding_batch_size=int(retrieval_section.get("embedding_batch_size", 32)),
            embedding_timeout_seconds=int(
                retrieval_section.get("embedding_timeout_seconds", 30)
            ),
            embedding_max_retries=int(
                retrieval_section.get("embedding_max_retries", 5)
            ),
            embedding_dimensions=int(retrieval_section.get("embedding_dimensions", 1024)),
        ),
        llm=LlmConfig(
            temperature=float(llm_section.get("temperature", 0.0)),
            max_retries=int(llm_section.get("max_retries", 3)),
            timeout_seconds=int(llm_section.get("timeout_s", 60)),
            max_tokens=int(llm_section.get("max_tokens", 4096)),
        ),
        guard=GuardConfig(
            default_limit=int(guard_section.get("default_limit", 500)),
            limit_cap=int(guard_section.get("limit_cap", 10_000)),
            blocked_function_prefixes=tuple(
                guard_section.get(
                    "blocked_function_prefixes", GuardConfig.blocked_function_prefixes
                )
            ),
            blocked_table_functions=tuple(
                guard_section.get(
                    "blocked_table_functions", GuardConfig.blocked_table_functions
                )
            ),
        ),
        sqlgen=SqlgenConfig(
            few_shot_count=int(sqlgen_section.get("few_shot_count", 2))
        ),
        exec=ExecConfig(
            timeout_seconds=int(exec_section.get("timeout_seconds", 60)),
            sample_rows=int(exec_section.get("sample_rows", 20)),
            profile_over_rows=int(exec_section.get("profile_over_rows", 50)),
            text_top_n=int(exec_section.get("text_top_n", 5)),
        ),
        qa=QaConfig(
            max_attempts=int(qa_section.get("max_attempts", 3)),
        ),
        eval=EvalConfig(
            temperature=float(eval_section.get("temperature", 0.0)),
            runs_per_case=int(eval_section.get("runs_per_case", 1)),
        ),
    )
