"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from nl2data.config import CONFIG_ENV_VAR, ConfigError, Nl2DataConfig, load_config


def test_explicit_file_resolves_relative_paths(tmp_path: Path) -> None:
    """Relative configured paths must resolve against the config directory."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("paths:\n  data_dir: mydata\n", encoding="utf-8")
    cfg = load_config(config_file)
    assert cfg.paths.data_dir == tmp_path / "mydata"
    assert cfg.paths.parquet_dir == tmp_path / "mydata" / "parquet"
    assert cfg.paths.warehouse == tmp_path / "mydata" / "warehouse.duckdb"
    assert cfg.paths.catalog == tmp_path / "mydata" / "catalog" / "catalog.yaml"
    assert cfg.paths.profiles_dir == tmp_path / "mydata" / "catalog" / "profiles"
    assert cfg.paths.glossary == tmp_path / "mydata" / "catalog" / "glossary.yaml"
    assert cfg.paths.cards_dir == tmp_path / "mydata" / "catalog" / "cards"
    assert cfg.paths.cards_md_dir == tmp_path / "mydata" / "catalog" / "cards_md"
    assert cfg.paths.table_notes == tmp_path / "docs" / "table_notes.md"


def test_env_var_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """NL2DATA_CONFIG must select the config file when no path is given."""
    config_file = tmp_path / "custom.yaml"
    config_file.write_text("paths:\n  data_dir: d\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
    assert load_config().paths.data_dir == tmp_path / "d"


def test_missing_file_raises(tmp_path: Path) -> None:
    """A missing config file must raise ConfigError."""
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    """Malformed YAML must raise ConfigError, not yaml.YAMLError."""
    config_file = tmp_path / "broken.yaml"
    config_file.write_text("paths: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(config_file)


def test_threshold_defaults_apply(config: Nl2DataConfig) -> None:
    """Missing threshold sections fall back to documented defaults."""
    assert config.ingest.max_name_length == 63
    assert config.ingest.header_scan_rows == 5
    assert config.mdbtools.mdb_tables_cmd == "mdb-tables"
    assert config.mdbtools.mdb_export_cmd == "mdb-export"
    assert config.mdbtools.timeout_seconds == 300
    assert config.mdbtools.date_format == "%Y-%m-%d"
    assert config.mdbtools.datetime_format == "%Y-%m-%d %H:%M:%S"
    assert config.profile.enum_max_distinct == 50
    assert config.profile.sample_values_limit == 5
    assert config.profile.sample_rows == 100
    assert config.profile.quantiles == (0.25, 0.5, 0.75)
    assert config.profile.sampled_over_rows == 5_000_000
    assert config.profile.sample_fraction == 0.1
    assert config.cards.enum_value_max_chars == 50
    assert config.cards.markdown_column_fold_limit == 100
    assert config.tokens.estimator == "len_div_4"
    assert config.paths.index_dir.name == "index"
    assert config.retrieval.top_k == 5
    assert config.retrieval.token_budget == 2000
    assert config.retrieval.rrf_k == 60
    assert config.retrieval.embedding_dimensions == 1024
