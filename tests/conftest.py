"""Shared fixtures and helpers for the nl2data test suite."""

from __future__ import annotations

import os

# Must run before pandas is imported anywhere in the test session; see the
# note in nl2data/__init__.py.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from ingest.common import PreparedTable, ingest_tables  # noqa: E402
from nl2data.config import Nl2DataConfig, load_config  # noqa: E402

DEFAULT_CONFIG_YAML = """\
paths:
  data_dir: data
  parquet_dir: data/parquet
  warehouse: data/warehouse.duckdb
  catalog: data/catalog/catalog.yaml
  profiles_dir: data/catalog/profiles
  glossary: data/catalog/glossary.yaml
  cards_dir: data/catalog/cards
  cards_md_dir: data/catalog/cards_md
  table_notes: docs/table_notes.md
  index_dir: data/index
  scratch_dir: data/scratch
  audit_dir: data/audit
ingest:
  max_name_length: 63
  header_scan_rows: 5
mdbtools:
  mdb_tables_cmd: mdb-tables
  mdb_export_cmd: mdb-export
  timeout_seconds: 300
  date_format: "%Y-%m-%d"
  datetime_format: "%Y-%m-%d %H:%M:%S"
profile:
  enum_max_distinct: 50
  sample_values_limit: 5
  sample_rows: 100
  quantiles: [0.25, 0.5, 0.75]
  sampled_over_rows: 5000000
  sample_fraction: 0.1
cards:
  enum_value_max_chars: 50
  markdown_column_fold_limit: 100
tokens:
  estimator: len_div_4
retrieval:
  top_k: 5
  token_budget: 2000
  rrf_k: 60
  weight_vector: 0.5
  weight_bm25: 0.5
  embedding_batch_size: 32
  embedding_timeout_seconds: 30
  embedding_max_retries: 5
  embedding_dimensions: 1024
llm:
  temperature: 0.0
  max_retries: 3
  timeout_s: 60
  max_tokens: 4096
guard:
  default_limit: 500
  limit_cap: 10000
sqlgen:
  few_shot_count: 4
exec:
  timeout_seconds: 60
  sample_rows: 20
  profile_over_rows: 50
  text_top_n: 5
qa:
  max_attempts: 3
eval:
  temperature: 0.0
  runs_per_case: 3
"""


@pytest.fixture()
def config(tmp_path: Path) -> Nl2DataConfig:
    """A default config rooted at a temporary directory."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return load_config(config_path)


@pytest.fixture()
def ingest_factory(
    config: Nl2DataConfig,
) -> Callable[[str, pd.DataFrame], str]:
    """Ingest a frame as a one-table source and return its clean table name."""

    def _ingest(name: str, frame: pd.DataFrame) -> str:
        entry = ingest_tables(
            source_name=name,
            source_type="excel",
            source_path=config.paths.data_dir / f"{name}.source",
            tables=[PreparedTable(original_name=name, frame=frame)],
            cfg=config,
        )
        return entry.tables[0].name

    return _ingest
