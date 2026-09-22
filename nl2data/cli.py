"""nl2data command-line interface (milestone 1)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from nl2data import __version__

app = typer.Typer(
    name="nl2data",
    help="Query large Excel/Access datasets with natural language.",
    no_args_is_help=True,
    add_completion=False,
)
ingest_app = typer.Typer(
    name="ingest",
    help="Ingest source files into the local warehouse.",
    no_args_is_help=True,
)
app.add_typer(ingest_app)
cards_app = typer.Typer(
    name="cards",
    help="Build M-Schema table cards for retrieval and prompting.",
    no_args_is_help=True,
)
app.add_typer(cards_app)
glossary_app = typer.Typer(
    name="glossary",
    help="Business glossary utilities.",
    no_args_is_help=True,
)
app.add_typer(glossary_app)
index_app = typer.Typer(
    name="index",
    help="Build and inspect the retrieval index.",
    no_args_is_help=True,
)
app.add_typer(index_app)
eval_app = typer.Typer(
    name="eval",
    help="Evaluation harness (recall, later milestones).",
    no_args_is_help=True,
)
app.add_typer(eval_app)
mcp_app = typer.Typer(
    name="mcp",
    help="MCP server for AI host integration (stdio).",
    no_args_is_help=True,
)
app.add_typer(mcp_app)
console = Console()

ConfigOption = typer.Option(
    None,
    "--config",
    envvar="NL2DATA_CONFIG",
    help="Path to the config file (default: repository-root config.yaml).",
)


def _print_version(value: bool) -> None:
    """Print the package version and exit."""
    if value:
        console.print(f"nl2data [bold]{__version__}[/bold]")
        raise typer.Exit(code=0)


def _load_cfg(config_path: Path | None) -> Any:
    """Load the config, mapping errors to a clean CLI exit."""
    from nl2data.config import ConfigError, load_config

    try:
        return load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _report_source(entry: Any) -> None:
    """Print a per-table summary of an ingested source."""
    console.print(
        f"[green]ingested[/green] source [bold]{entry.name}[/bold] "
        f"({entry.type}) from {entry.path}"
    )
    for table in entry.tables:
        console.print(
            f"  - [bold]{table.name}[/bold] (原: {table.original_name}) "
            f"rows={table.rows} cols={len(table.columns)} -> {table.parquet}"
        )


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        callback=_print_version,
        is_eager=True,
        help="Show the nl2data version and exit.",
    ),
) -> None:
    """Inspect and query Excel/Access datasets with natural language."""


@ingest_app.command("excel")
def ingest_excel_cmd(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to a .xlsx/.xlsm file."
    ),
    sheet: str | None = typer.Option(
        None, "--sheet", help="Ingest a single sheet by its exact name."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Source alias (cleaned) instead of the file stem."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Ingest a multi-sheet Excel file into Parquet + DuckDB views."""
    from ingest.common import IngestError
    from ingest.excel import ingest_excel

    cfg = _load_cfg(config_path)
    try:
        entry = ingest_excel(file, cfg, sheet=sheet, name=name)
    except IngestError as exc:
        console.print(f"[red]ingest failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    _report_source(entry)


@ingest_app.command("access")
def ingest_access_cmd(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to a .mdb/.accdb file."
    ),
    table: str | None = typer.Option(
        None, "--table", help="Ingest a single table by its exact name."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Ingest an Access database via mdbtools into Parquet + DuckDB views."""
    from ingest.access import ingest_access
    from ingest.common import IngestError

    cfg = _load_cfg(config_path)
    try:
        entry = ingest_access(file, cfg, table=table)
    except IngestError as exc:
        console.print(f"[red]ingest failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    _report_source(entry)


@app.command("profile")
def profile_cmd(
    table: str | None = typer.Option(
        None, "--table", help="Profile a single table by its clean name."
    ),
    all_tables: bool = typer.Option(
        False, "--all", help="Profile every table registered in the catalog."
    ),
    force: bool = typer.Option(
        False, "--force", help="Recompute even when the stored profile is up to date."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Profile ingested tables into data/catalog/profiles/*.json."""
    from catalog.profiler import ProfileError, run_profiles

    if (table is None) == (not all_tables):
        console.print("[red]choose exactly one of --table NAME or --all[/red]")
        raise typer.Exit(code=2)
    cfg = _load_cfg(config_path)
    try:
        profiles = run_profiles(cfg, [table] if table else None, force=force)
    except ProfileError as exc:
        console.print(f"[red]profile failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not profiles:
        console.print("[yellow]all profiles are up to date; nothing to do[/yellow]")
        return
    for profile in profiles:
        sampled = " (sampled)" if profile.get("sampled") else ""
        console.print(
            f"[green]profiled[/green] [bold]{profile['table']}[/bold] "
            f"(原: {profile.get('original_name', '-')}, "
            f"source={profile.get('source', '-')}) rows={profile['rows']}{sampled}"
        )
        for col in profile["columns"]:
            extras = []
            for key in ("enum_values", "quantiles", "avg_len", "error"):
                if key in col:
                    extras.append(f"{key}={col[key]}")
            console.print(
                f"  - {col['name']} ({col['dtype']}) "
                f"null={col['null_rate']} distinct={col['distinct_count']}"
                + (f" {' '.join(extras)}" if extras else "")
            )
    console.print(f"[green]{len(profiles)} profile(s) written.[/green]")


@ingest_app.command("parquet")
def ingest_parquet_cmd(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to a .parquet file."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Source alias (cleaned) instead of the file stem."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Register an existing Parquet file as a view (no data copying)."""
    from ingest.common import IngestError
    from ingest.parquet import ingest_parquet

    cfg = _load_cfg(config_path)
    try:
        entry = ingest_parquet(file, cfg, name=name)
    except IngestError as exc:
        console.print(f"[red]ingest failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    _report_source(entry)


@cards_app.command("build")
def cards_build_cmd(
    table: str | None = typer.Option(
        None, "--table", help="Build one card by its clean table name."
    ),
    all_tables: bool = typer.Option(
        False, "--all", help="Build cards for every catalog table."
    ),
    force: bool = typer.Option(
        False, "--force", help="Rebuild even when the card is up to date."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Assemble table cards from profiles, table notes and the glossary."""
    from catalog.cards import build_cards

    if (table is None) == (not all_tables):
        console.print("[red]choose exactly one of --table NAME or --all[/red]")
        raise typer.Exit(code=2)
    cfg = _load_cfg(config_path)
    from catalog.glossary import GlossaryError, terms_by_table_map

    try:
        terms_by_table = terms_by_table_map(cfg)
    except GlossaryError as exc:
        console.print(f"[red]glossary invalid:[/red] run `nl2data glossary check`.\n{exc}")
        raise typer.Exit(code=1) from exc
    cards = build_cards(
        cfg,
        terms_by_table=terms_by_table,
        force=force,
        tables=[table] if table else None,
    )
    if not cards:
        console.print("[yellow]all cards are up to date; nothing to do[/yellow]")
        return
    for card in cards:
        console.print(
            f"[green]card[/green] [bold]{card['table']}[/bold] "
            f"(原: {card.get('original_name', '-')}) rows={card['rows']} "
            f"cols={len(card['columns'])} terms={len(card['terms'])} "
            f"tokens~{card['token_estimate']}"
        )
    console.print(f"[green]{len(cards)} card(s) written.[/green]")


@glossary_app.command("check")
def glossary_check_cmd(config_path: Path | None = ConfigOption) -> None:
    """Validate the glossary against the catalog and print a report."""
    from catalog.glossary import GlossaryError, load_glossary, validate_glossary

    cfg = _load_cfg(config_path)
    try:
        store = load_glossary(cfg.paths.glossary, cfg)
    except GlossaryError as exc:
        console.print(f"[red]glossary invalid:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not store.entries:
        console.print("[yellow]glossary is empty (this is a valid state)[/yellow]")
        return
    errors = validate_glossary(store, cfg)
    console.print(f"{len(store.entries)} term(s) checked, {len(errors)} error(s)")
    for error in errors:
        console.print(f"[red]{error}[/red]")
    if errors:
        raise typer.Exit(code=1)


@glossary_app.command("list")
def glossary_list_cmd(
    table: str | None = typer.Option(
        None, "--table", help="List only terms that map to this table."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """List glossary terms (optionally for a single table)."""
    from catalog.glossary import GlossaryError, load_glossary

    cfg = _load_cfg(config_path)
    try:
        store = load_glossary(cfg.paths.glossary, cfg)
    except GlossaryError as exc:
        console.print(f"[red]glossary invalid:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    entries = store.terms_for_table(table) if table else store.entries
    for entry in entries:
        maps = entry.maps_to
        target = maps.column or maps.expression or "(table-level)"
        suffix = f" | filter: {maps.filter}" if maps.filter else ""
        note = f" | {entry.description}" if entry.description else ""
        console.print(f"{entry.term} -> {maps.table}.{target}{suffix}{note}")
    console.print(f"{len(entries)} term(s).")


@index_app.command("build")
def index_build_cmd(config_path: Path | None = ConfigOption) -> None:
    """Build the hybrid retrieval index (vector channel needs EMB_* env)."""
    from retrieval.embedding import EmbeddingUnavailableError, client_from_env
    from retrieval.index import VectorIndex, load_card_docs

    cfg = _load_cfg(config_path)
    docs = load_card_docs(cfg)
    if not docs:
        console.print(
            "[red]no cards to index; run `nl2data cards build --all` first[/red]"
        )
        raise typer.Exit(code=1)
    client = client_from_env(cfg)
    if client is None:
        console.print(
            "[yellow]EMB_BASE_URL/EMB_API_KEY/EMB_MODEL not set; "
            "only the BM25 channel will be available.[/yellow]"
        )
        raise typer.Exit(code=0)
    try:
        report = VectorIndex(cfg).sync(docs, client)
    except EmbeddingUnavailableError as exc:
        console.print(f"[red]embedding failed; previous index kept:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]index synced[/green]: embedded={report.embedded} "
        f"upserted={report.upserted} deleted={report.deleted} "
        f"total={report.total}"
    )


@app.command("retrieve")
def retrieve_cmd(
    query: str = typer.Argument(..., help="Natural-language question."),
    k: int | None = typer.Option(None, "-k", help="Top-K tables to return."),
    budget: int | None = typer.Option(
        None, "--budget", help="Prompt token budget override."
    ),
    explain: bool = typer.Option(
        False, "--explain", help="Print per-table channel evidence and drops."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Retrieve the Top-K relevant table cards for a question."""
    from retrieval.retrieve import RetrievalError, retrieve

    cfg = _load_cfg(config_path)
    try:
        result = retrieve(query, cfg, k=k, token_budget=budget)
    except RetrievalError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"channels: [bold]{'+'.join(result.channels_used)}[/bold]")
    for item in result.items:
        term = f" [magenta]via术语:{item.via_term}[/magenta]" if item.via_term else ""
        console.print(
            f"  {item.table}  score={item.score}"
            f"  vector_rank={item.vector_rank}  bm25_rank={item.bm25_rank}{term}"
        )
    console.print(
        f"prompt tokens ~{result.total_tokens}"
        + (
            f"; dropped: {', '.join(d['table'] for d in result.dropped_tables)}"
            if result.dropped_tables
            else ""
        )
    )
    if explain:
        for item in result.items:
            console.print(
                f"[dim] 入选 {item.table}: rrf={item.score} "
                f"(vector_rank={item.vector_rank}, bm25_rank={item.bm25_rank}"
                + (f", 术语直通:{item.via_term}" if item.via_term else "")
                + ")[/dim]"
            )
        for dropped in result.dropped_tables:
            console.print(
                f"[dim] 丢弃 {dropped['table']}: {dropped['reason']}[/dim]"
            )


@eval_app.command("recall")
def eval_recall_cmd(
    golden: Path = typer.Option(
        Path("eval/recall_golden.yaml"), "--golden", help="Golden case file."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Evaluate retrieval recall against the golden case set."""
    import yaml

    from eval.recall import RecallEvalError, run_recall
    from nl2data.reports import print_recall_report

    cfg = _load_cfg(config_path)
    if not golden.is_file():
        console.print(f"[red]golden file not found: {golden}[/red]")
        raise typer.Exit(code=2)
    if not cfg.paths.catalog.is_file():
        console.print("[red]catalog.yaml missing; run ingest first[/red]")
        raise typer.Exit(code=1)
    with cfg.paths.catalog.open(encoding="utf-8") as fh:
        known = {
            table["name"]
            for source in yaml.safe_load(fh)["sources"]
            for table in source["tables"]
        }
    from eval.recall import load_golden_for_eval

    try:
        cases = load_golden_for_eval(golden)
        report = run_recall(cfg, cases, known_tables=known)
    except RecallEvalError as exc:
        console.print(f"[red]eval failed:[/red]\n{exc}")
        raise typer.Exit(code=1) from exc
    print_recall_report(console, report)


def _render_outcome(outcome: object) -> None:
    """Render one QaOutcome: conclusion, SQL, stats, detail location."""
    from nl2data.qa import QaOutcome

    assert isinstance(outcome, QaOutcome)
    if outcome.clarification is not None:
        console.print(f"[yellow]需要补充信息:[/yellow] {outcome.clarification}")
        return
    if not outcome.ok:
        console.print(f"[red]未能回答:[/red] {outcome.failure_reason}")
        if outcome.vsql:
            console.print(f"最近一次 SQL:\n```sql\n{outcome.vsql.sql}\n```")
        return
    execution = outcome.execution
    assert execution is not None
    if outcome.interpretation:
        console.print(f"[bold]{outcome.interpretation}[/bold]")
    elif outcome.interpretation_failed:
        console.print("[dim](解读调用失败,以下仅展示数据)[/dim]")
    console.print(f"\n[dim]检索表:[/dim] {', '.join(outcome.retrieved_tables) or '-'}")
    if outcome.vsql:
        console.print(f"[dim]实际执行的 SQL[/dim]\n```sql\n{outcome.vsql.sql}\n```")
    tokens = outcome.usage_delta.get("total_tokens", 0)
    console.print(
        f"[dim]行数 {execution.rowcount} | 耗时 {execution.latency_ms:.0f}ms | "
        f"tokens {tokens} | 尝试 {outcome.attempts} 次"
        + (
            f" | 明细 {execution.detail_ref}"
            if execution.detail_ref
            else ""
        )
        + "[/dim]"
    )
    if execution.profile is None and execution.rows:
        from rich.table import Table

        table = Table(show_header=True, header_style="bold")
        for column in execution.columns:
            table.add_column(column)
        for row in execution.rows[:10]:
            table.add_row(*[str(row.get(c, "")) for c in execution.columns])
        console.print(table)


@app.command("ask")
def ask_cmd(
    question: list[str] = typer.Argument(
        None, help="Question; omit to enter the interactive loop."
    ),
    no_interpret: bool = typer.Option(
        False, "--no-interpret", help="Skip the narration LLM call."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Ask one question (or enter an interactive loop)."""
    from nl2data.qa import QaOutcome, ask_once

    cfg = _load_cfg(config_path)
    single = " ".join(question).strip() if question else ""

    def _ask(q: str, extra: str | None = None) -> QaOutcome:
        return ask_once(q, cfg, no_interpret=no_interpret, extra_feedback=extra)

    if single:
        _render_outcome(_ask(single))
        return

    console.print(
        "交互模式:直接输入问题;命令 /retry [补充说明] /show sql /export csv <路径> "
        "/tables /exit"
    )
    last: QaOutcome | None = None
    last_question = ""
    while True:
        try:
            line = console.input("[bold cyan]问 >[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见。")
            return
        if not line:
            continue
        if line in {"/exit", "/quit", "exit", "quit"}:
            console.print("再见。")
            return
        if line == "/show sql":
            if last and last.vsql:
                console.print(f"```sql\n{last.vsql.sql}\n```")
            else:
                console.print("[yellow]还没有已执行的 SQL[/yellow]")
            continue
        if line.startswith("/retry"):
            extra = line[len("/retry") :].strip() or None
            if not last_question:
                console.print("[yellow]还没有可重试的问题[/yellow]")
                continue
            last = _ask(last_question, extra_feedback=extra)
            _render_outcome(last)
            continue
        if line.startswith("/export csv"):
            parts = line.split(maxsplit=2)
            if len(parts) < 3:
                console.print("[yellow]用法:/export csv <路径>[/yellow]")
                continue
            if not (last and last.execution and last.execution.detail_ref):
                console.print("[yellow]最近一次结果没有明细文件(小结果集不落盘)[/yellow]")
                continue
            import pandas as pd

            src = Path(last.execution.detail_ref)
            if not src.is_absolute():
                src = cfg.paths.data_dir.parent / src
            pd.read_parquet(src).to_csv(parts[2], index=False, encoding="utf-8-sig")
            console.print(f"[green]已导出[/green] {parts[2]}")
            continue
        if line == "/tables":
            from nl2data.qa import catalog_whitelists

            tables, _ = catalog_whitelists(cfg)
            for name in sorted(tables):
                console.print(f"  {name}")
            continue
        if line.startswith("/"):
            console.print(f"[yellow]未知命令:{line.split()[0]}[/yellow]")
            continue
        last_question = line
        last = _ask(line)
        _render_outcome(last)


@app.command("audit")
def audit_last_cmd(
    count: int = typer.Argument(10, help="Number of recent events."),
    config_path: Path | None = ConfigOption,
) -> None:
    """Show the most recent audit events (alias: nl2data audit last)."""
    from nl2data.audit import load_recent_events

    cfg = _load_cfg(config_path)
    events = load_recent_events(cfg, count)
    if not events:
        console.print("[yellow]还没有审计记录[/yellow]")
        return
    for event in reversed(events):
        status = event.get("status", "-")
        console.print(
            f"{event.get('ts', '-')} | {status} | rows={event.get('rowcount', 0)} | "
            f"attempts={event.get('attempts', '-')} | {event.get('question', '')[:40]}"
        )
    console.print(f"{len(events)} event(s).")


@eval_app.command("e2e")
def eval_e2e_cmd(
    golden: Path = typer.Option(
        Path("eval/recall_golden.yaml"), "--golden", help="Golden case file."
    ),
    save_baseline: bool = typer.Option(
        False, "--save-baseline", help="Overwrite eval/baseline_e2e.json with this run."
    ),
    config_path: Path | None = ConfigOption,
) -> None:
    """Three-layer end-to-end regression over the golden set (no interpretation)."""
    import json

    from eval.e2e import (
        BASELINE_PATH,
        diff_against_baseline,
        report_to_dict,
        run_e2e,
    )
    from eval.recall import RecallEvalError, load_golden_for_eval

    cfg = _load_cfg(config_path)
    if not golden.is_file():
        console.print(f"[red]golden file not found: {golden}[/red]")
        raise typer.Exit(code=2)
    try:
        cases = load_golden_for_eval(golden)
    except RecallEvalError as exc:
        console.print(f"[red]golden invalid:[/red]\n{exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"running {len(cases)} cases (interpretation disabled)...")
    report = run_e2e(cfg, cases)
    metrics = report.layer_metrics
    console.print(
        f"model={report.model} channels={'+'.join(report.channels_used) or '-'}"
        f" | L1 Recall@3 avg = {metrics['l1_recall3_avg']:.3f}  "
        f"L2 pass = {metrics['l2_pass_rate']:.1%}  "
        f"L3 pass(可判) = {metrics['l3_pass_rate']:.1%}  "
        f"L3 pass(总数) = {metrics['l3_pass_of_total']:.1%}"
    )
    for verdict in report.case_verdicts:
        l3 = verdict.l3 if verdict.l3 is not None else "-"
        mark = "[green]✓[/green]" if verdict.l3 == "pass" else (
            "[yellow]?[/yellow]" if verdict.l3 is None else "[red]✗[/red]"
        )
        console.print(
            f"  {mark} #{verdict.index:>2} L1={verdict.l1_recall3:.2f} "
            f"L2={verdict.l2:<5} L3={l3:<5} {verdict.question[:26]}"
            + (f" | {verdict.reason}" if verdict.reason else "")
        )
    console.print("[bold]vs baseline:[/bold]")
    for line in diff_against_baseline(report, BASELINE_PATH):
        console.print(f"  {line}")
    if save_baseline:
        BASELINE_PATH.write_text(
            json.dumps(report_to_dict(report), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        console.print(f"[green]baseline saved to {BASELINE_PATH}[/green]")


@mcp_app.command("serve")
def mcp_serve_cmd(config_path: Path | None = ConfigOption) -> None:
    """Serve the three read-only DuckSeek tools over stdio for MCP hosts."""
    from mcp_server.server import serve

    cfg = _load_cfg(config_path)
    serve(cfg)


def run() -> None:
    """Entry point for the ``nl2data`` console script."""
    logging.basicConfig(
        level=os.environ.get("NL2DATA_LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    app()


if __name__ == "__main__":
    run()
