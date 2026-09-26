"""DuckSeek MCP server: three read-only tools over stdio for AI hosts (T17).

Product surface name is ``duckseek`` (skill + MCP tools); the underlying
engine — CLI and Python package — is ``nl2data`` and stays unchanged.

Tool surface (minimal by goals.md decision 9 — no ingest/eval/glossary tool):

- ``duckseek_status``: which of the six credential env vars are missing
  (names only, never values), registered sources/tables, index state.
- ``duckseek_list_tables``: tables grouped by source with row counts from
  the stored profiles.
- ``duckseek_ask``: one question through the full pipeline
  (retrieve → generate → guard → sandbox run). Interpretation is
  deliberately skipped: the calling host LLM narrates the result itself,
  saving one API call and keeping the conversation context on its side.

The blocking pipeline work runs in a worker thread so a long ``ask`` never
blocks the server's event loop. Config resolution reuses
:func:`nl2data.config.load_config` (``NL2DATA_CONFIG`` / repo default).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import yaml
from mcp.server.mcpserver import MCPServer

from exec.runner import ExecutionResult
from export import run_export
from llm.chat import ENV_API_KEY as LLM_KEY_ENV
from llm.chat import ENV_BASE_URL as LLM_URL_ENV
from llm.chat import ENV_MODEL as LLM_MODEL_ENV
from llm.chat import LlmError
from nl2data.config import Nl2DataConfig
from nl2data.qa import ask_once
from retrieval.embedding import ENV_API_KEY as EMB_KEY_ENV
from retrieval.embedding import ENV_BASE_URL as EMB_URL_ENV
from retrieval.embedding import ENV_MODEL as EMB_MODEL_ENV
from retrieval.index import VectorIndex

logger = logging.getLogger(__name__)

#: The six credential env vars (goals.md decisions 3+8), presence only.
ENV_KEYS: tuple[str, ...] = (
    LLM_URL_ENV,
    LLM_KEY_ENV,
    LLM_MODEL_ENV,
    EMB_URL_ENV,
    EMB_KEY_ENV,
    EMB_MODEL_ENV,
)

_SERVER_INSTRUCTIONS = (
    "DuckSeek answers natural-language questions over registered tabular data "
    "(Excel/Access/Parquet → DuckDB). Workflow: call duckseek_status first; if it "
    "reports missing env vars, ask the user to provide them into the server "
    "environment (mcpServers config) — never pass them as tool arguments. Then "
    "duckseek_list_tables shows the data; duckseek_ask answers each question and "
    "returns the executed SQL alongside a compact markdown result. All tools "
    "are read-only."
)


def _scrub(text: str) -> str:
    """Replace any configured credential value occurring in ``text``.

    The llm/guard layers already redact their own messages; this is the MCP
    boundary's last sweep so no env value can leak through an unexpected
    exception text.
    """
    for key in ENV_KEYS:
        value = os.environ.get(key, "")
        if value and value in text:
            text = text.replace(value, "***")
    return text


def env_presence() -> dict[str, bool]:
    """Presence booleans for the six credential env vars (non-empty values)."""
    return {key: bool(os.environ.get(key, "").strip()) for key in ENV_KEYS}


def _index_state(cfg: Nl2DataConfig) -> dict[str, Any]:
    """Whether the retrieval index exists and when it was last written.

    Build time is the newest file mtime under the Lance directory (no
    dedicated metadata file exists; the directory is the metadata).
    """
    if not VectorIndex(cfg).available():
        return {"built": False, "built_at": None}
    lance_dir = cfg.paths.index_dir / "lance"
    mtimes = [p.stat().st_mtime for p in lance_dir.rglob("*") if p.is_file()]
    newest = max(mtimes) if mtimes else None
    built_at = (
        datetime.fromtimestamp(newest).astimezone().isoformat(timespec="seconds")
        if newest is not None
        else None
    )
    return {"built": True, "built_at": built_at}


def status_payload(cfg: Nl2DataConfig) -> dict[str, Any]:
    """Readiness snapshot for hosts: env presence, catalog size, index state."""
    presence = env_presence()
    missing = [key for key, present in presence.items() if not present]
    catalog_path: Path = cfg.paths.catalog
    sources = tables = 0
    exists = catalog_path.is_file()
    if exists:
        with catalog_path.open(encoding="utf-8") as fh:
            catalog = yaml.safe_load(fh) or {}
        entries = catalog.get("sources", [])
        sources = len(entries)
        tables = sum(len(source.get("tables", [])) for source in entries)
    index = _index_state(cfg)
    return {
        "env": presence,
        "missing_env": missing,
        "llm_ready": all(presence[key] for key in (LLM_URL_ENV, LLM_KEY_ENV, LLM_MODEL_ENV)),
        "embedding_ready": all(
            presence[key] for key in (EMB_URL_ENV, EMB_KEY_ENV, EMB_MODEL_ENV)
        ),
        "catalog": {"exists": exists, "sources": sources, "tables": tables},
        "index": index,
        # ask is usable with BM25-only retrieval when EMB_* is absent.
        "ready": (
            all(presence[key] for key in (LLM_URL_ENV, LLM_KEY_ENV, LLM_MODEL_ENV))
            and exists
            and tables > 0
            and index["built"]
        ),
    }


def _profile_rows(cfg: Nl2DataConfig, table: str) -> int | None:
    """Row count from the stored profile; ``None`` when not profiled yet."""
    path = cfg.paths.profiles_dir / f"{table}.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return int(json.load(fh)["rows"])
    except (OSError, TypeError, ValueError, KeyError):
        logger.warning("profile unreadable for table %s; rows unknown", table)
        return None


def list_tables_payload(cfg: Nl2DataConfig) -> dict[str, Any]:
    """Tables grouped by source with row counts read from profile JSON."""
    catalog_path: Path = cfg.paths.catalog
    if not catalog_path.is_file():
        return {"sources": []}
    with catalog_path.open(encoding="utf-8") as fh:
        catalog = yaml.safe_load(fh) or {}
    sources = [
        {
            "source": str(source.get("name", "")),
            "type": str(source.get("type", "")),
            "tables": [
                {
                    "table": str(table.get("name", "")),
                    "rows": _profile_rows(cfg, str(table.get("name", ""))),
                }
                for table in source.get("tables", [])
            ],
        }
        for source in catalog.get("sources", [])
    ]
    return {"sources": sources}


def _fmt_cell(value: Any) -> str:
    """Render one markdown table cell; NULL keeps its honest label."""
    return "NULL" if value is None else str(value)


def _render_answer(execution: ExecutionResult) -> str:
    """Render the compacted execution result as markdown for the host LLM.

    Small results inline the (already sampled) rows; large results show the
    sample plus the single-pass statistical profile, mirroring the CLI's
    compaction contract (goals.md §2: large result sets stay out of context).
    """
    if not execution.rows:
        return "查询无返回行。"
    lines = [
        "| " + " | ".join(execution.columns) + " |",
        "| " + " | ".join("---" for _ in execution.columns) + " |",
    ]
    for row in execution.rows:
        lines.append("| " + " | ".join(_fmt_cell(row.get(c)) for c in execution.columns) + " |")
    if execution.rowcount > len(execution.rows):
        lines.append(f"共 {execution.rowcount} 行,以上为前 {len(execution.rows)} 行样本。")
    if execution.profile:
        for name, stats in execution.profile.get("columns", {}).items():
            if stats.get("kind") == "numeric":
                lines.append(
                    f"画像 {name}: min={stats.get('min')} max={stats.get('max')} "
                    f"avg={stats.get('avg')}"
                )
            else:
                top = ", ".join(
                    f"{item['value']}×{item['count']}" for item in stats.get("top", [])
                )
                lines.append(f"画像 {name}: top=[{top}]")
    return "\n".join(lines)


def ask_payload(
    question: str, cfg: Nl2DataConfig, export: str = ""
) -> dict[str, Any]:
    """Run one question through the pipeline (no interpretation LLM call).

    With ``export="xlsx"`` a successful answer additionally writes the
    artifact bundle of decision 10 (xlsx + manifest) and attaches an
    ``artifacts`` key; degradation never blocks the answer itself.
    """
    if export and export != "xlsx":
        return {"error": f"不支持的导出格式:{export!r}(当前仅 xlsx)"}
    started = time.perf_counter()
    try:
        outcome = ask_once(question, cfg, no_interpret=True)
    except LlmError as exc:  # generate-time API failure propagates from qa.py
        return {"error": _scrub(f"LLM 调用失败({exc.category}):{exc}")}
    except Exception as exc:  # noqa: BLE001 -- MCP boundary reports, never crashes
        logger.warning("ask pipeline raised unexpectedly", exc_info=True)
        return {"error": _scrub(f"内部错误:{exc}")}
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if outcome.clarification is not None:
        return {"needs_clarification": True, "question": outcome.clarification}
    if not outcome.ok:
        return {"error": _scrub(outcome.failure_reason or "未能回答")}
    assert outcome.execution is not None and outcome.vsql is not None
    payload: dict[str, Any] = {
        "answer": _render_answer(outcome.execution),
        "sql": outcome.vsql.sql,
        "row_count": outcome.execution.rowcount,
        "elapsed_ms": elapsed_ms,
        "source_tables": outcome.retrieved_tables,
        # Truthful per-call disclosure (T19): the vector channel counts as
        # used only when retrieval actually listed it — absent when EMB_* is
        # missing, the embedding call failed mid-query, or it scored no hits.
        "embedding_degraded": "vector" not in outcome.retrieval_channels,
    }
    if export:
        try:
            result = run_export(question, outcome, cfg)
        except Exception as exc:  # noqa: BLE001 -- answer stands, export reported
            logger.warning("export failed after a successful ask", exc_info=True)
            payload["artifacts_error"] = _scrub(f"导出失败:{exc}")
            return payload
        payload["artifacts"] = {
            "xlsx": str(result.xlsx_path.resolve()),
            "manifest": str(result.manifest_path.resolve()),
            "chart": result.chart_spec.to_dict() if result.chart_spec else None,
            "chart_error": result.chart_error,
        }
    return payload


def build_server(cfg: Nl2DataConfig) -> MCPServer:
    """Build the MCP server with the three read-only tools bound to ``cfg``."""
    server: MCPServer = MCPServer("duckseek", instructions=_SERVER_INSTRUCTIONS)

    @server.tool()
    async def duckseek_status() -> dict[str, Any]:
        """Environment + data readiness for DuckSeek.

        Reports which of the six env vars (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL,
        EMB_BASE_URL/EMB_API_KEY/EMB_MODEL) are missing — names only, never
        values — plus registered sources/tables and retrieval-index state.
        Call this first; if variables are missing, ask the user to provide
        them into the server environment.
        """
        return await anyio.to_thread.run_sync(status_payload, cfg)

    @server.tool()
    async def duckseek_list_tables() -> dict[str, Any]:
        """List registered tables grouped by source, with row counts.

        Row counts come from stored profiles; ``rows: null`` means the table
        has not been profiled yet. Read-only.
        """
        return await anyio.to_thread.run_sync(list_tables_payload, cfg)

    @server.tool()
    async def duckseek_ask(question: str, export: str = "") -> dict[str, Any]:
        """Answer a natural-language question over the registered tables.

        Runs retrieve → SQL generation → read-only guard → sandboxed
        execution. Returns ``answer`` (compact markdown of the result),
        ``sql`` (the executed statement, for verification), ``row_count``,
        ``elapsed_ms``, ``source_tables`` and ``embedding_degraded`` (true
        when this call's retrieval fell back to BM25-only — tell the user
        and suggest providing EMB_* for best recall); interpret the answer
        yourself. Ambiguous questions return ``needs_clarification`` with a
        follow-up ``question`` to ask the user. Read-only; the SQL is
        verified to be a single SELECT before it runs.

        Optional ``export="xlsx"`` (opt-in, nothing by default) writes an
        artifact bundle beside the answer: an .xlsx (data + meta +
        LLM-chosen native chart) and a self-describing manifest.json —
        the returned ``artifacts`` key carries both absolute paths plus the
        chart spec; tell the user where the files are.
        """
        return await anyio.to_thread.run_sync(ask_payload, question, cfg, export)

    return server


def serve(cfg: Nl2DataConfig) -> None:
    """Run the stdio MCP server (blocking; protocol on stdin/stdout)."""
    build_server(cfg).run("stdio")
