# nl2data

用自然语言查询大体量 Excel/Access 表格数据的开源 CLI 工具，答案永远附带实际执行的 SQL，可人工核验。

> Status: Milestone 1 (data ingestion & profiling) in progress.

## Architecture (placeholder)

```
文件 → Parquet → DuckDB → LLM 生成 SQL → 静态校验 → 沙箱执行 → 结果压缩 → 自然语言解读
```

- Repository layout: `ingest/` (file ingestion), `catalog/` (naming, lineage, profiles),
  `retrieval/` (schema retrieval), `sqlgen/` (LLM SQL generation), `guard/` (SQL safety),
  `exec/` (sandboxed execution), `eval/` (regression evaluation), `tests/`, `docs/`, `data/`.
- All generated artifacts live under `data/` (git-ignored): Parquet files, `warehouse.duckdb`,
  `catalog/catalog.yaml`, and `catalog/profiles/*.json`.

## Development

> **goals.md is the single source of truth** for project goals, locked
> architecture decisions and cross-module contracts. Read it first in every
> session; it outranks any discussion history.

Requires Python >= 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create venv and install dependencies (locked by uv.lock)
uv run pytest           # fast suite (slow tests deselected by default)
uv run pytest -m slow   # run only the long-running tests (100k-row fixture)
uv run pytest -m "slow or not slow"  # everything
uv run pytest --cov     # with coverage (fail-under 80)
uv run ruff check .     # lint (E/F/I/UP/ANN, line-length 100) — must be zero-warning
uv run nl2data --help   # CLI entry point
```

## License

Apache-2.0. See [LICENSE](LICENSE).
