"""Smoke and end-to-end tests for the nl2data CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nl2data import __version__
from nl2data.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG_YAML = "paths:\n  data_dir: data\n"


def test_version_flag_prints_version() -> None:
    """`nl2data --version` must exit cleanly and print the package version."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_version_short_flag_prints_version() -> None:
    """`nl2data -V` must behave like `--version`."""
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_exits_cleanly() -> None:
    """`nl2data --help` must exit cleanly and show the app help."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_ingest_missing_config_exits_cleanly(tmp_path: Path) -> None:
    """A bad --config path is a clean CLI error, not a traceback."""
    result = runner.invoke(
        app,
        [
            "ingest",
            "excel",
            str(FIXTURES / "excel_basic.xlsx"),
            "--config",
            str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "config error" in result.output


def test_profile_requires_exactly_one_selector() -> None:
    """`profile` needs exactly one of --table / --all."""
    result = runner.invoke(app, ["profile"])
    assert result.exit_code == 2
    result = runner.invoke(
        app, ["profile", "--table", "t", "--all", "--config", "missing.yaml"]
    )
    assert result.exit_code == 2


def test_ingest_excel_and_profile_e2e(tmp_path: Path) -> None:
    """CLI chain on a temp data root: ingest excel -> profile --all -> no-op."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    xlsx = FIXTURES / "excel_basic.xlsx"

    result = runner.invoke(
        app, ["ingest", "excel", str(xlsx), "--config", str(config_file)]
    )
    assert result.exit_code == 0, result.output
    assert "ding_dan_ming_xi" in result.output
    assert "ke_hu" in result.output

    data_dir = tmp_path / "data"
    assert (data_dir / "warehouse.duckdb").exists()
    assert list((data_dir / "parquet" / "excel_basic").glob("*.parquet"))
    assert (data_dir / "catalog" / "catalog.yaml").exists()

    result = runner.invoke(app, ["profile", "--all", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    profiles = list((data_dir / "catalog" / "profiles").glob("*.json"))
    assert len(profiles) == 2

    result = runner.invoke(app, ["profile", "--all", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_ingest_excel_unknown_sheet_fails(tmp_path: Path) -> None:
    """An unknown --sheet is a clean CLI error listing available sheets."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "ingest",
            "excel",
            str(FIXTURES / "excel_basic.xlsx"),
            "--sheet",
            "不存在",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 1
    assert "ingest failed" in result.output


def test_cards_build_e2e_with_notes(tmp_path: Path) -> None:
    """ingest -> profile -> cards build injects notes and is incremental."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    xlsx = FIXTURES / "excel_basic.xlsx"
    assert runner.invoke(
        app, ["ingest", "excel", str(xlsx), "--config", str(config_file)]
    ).exit_code == 0
    assert runner.invoke(
        app, ["profile", "--all", "--config", str(config_file)]
    ).exit_code == 0

    notes = tmp_path / "docs" / "table_notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("# 表:订单明细\n订单主表,每行一笔订单。\n", encoding="utf-8")

    result = runner.invoke(app, ["cards", "build", "--all", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    data_dir = tmp_path / "data"
    assert len(list((data_dir / "catalog" / "cards").glob("*.json"))) == 2
    assert len(list((data_dir / "catalog" / "cards_md").glob("*.md"))) == 2
    card = json.loads(
        (data_dir / "catalog" / "cards" / "ding_dan_ming_xi.json").read_text(encoding="utf-8")
    )
    assert card["description"] == "订单主表,每行一笔订单。"
    assert card["token_estimate"] > 0
    assert "订单主表" in (
        data_dir / "catalog" / "cards_md" / "ding_dan_ming_xi.md"
    ).read_text(encoding="utf-8")

    result = runner.invoke(app, ["cards", "build", "--all", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_glossary_check_empty_and_dangling(tmp_path: Path) -> None:
    """Empty glossary is valid; a dangling reference fails with exit 1."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")

    result = runner.invoke(app, ["glossary", "check", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "empty" in result.output

    glossary_path = tmp_path / "data" / "catalog" / "glossary.yaml"
    glossary_path.parent.mkdir(parents=True, exist_ok=True)
    glossary_path.write_text(
        "terms:\n  - term: 毛利\n    maps_to:\n      table: sales_2024\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["glossary", "check", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "sales_2024" in result.output

    result = runner.invoke(app, ["cards", "build", "--all", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "glossary invalid" in result.output


def test_glossary_structural_error_is_clean(tmp_path: Path) -> None:
    """A structurally broken glossary yields a clean error, not a traceback."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    glossary_path = tmp_path / "data" / "catalog" / "glossary.yaml"
    glossary_path.parent.mkdir(parents=True, exist_ok=True)
    glossary_path.write_text("terms:\n  - synonyms: [a]\n", encoding="utf-8")
    result = runner.invoke(app, ["glossary", "check", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "glossary invalid" in result.output
    assert "第 1 个术语" in result.output


def test_glossary_list_with_table_filter(tmp_path: Path) -> None:
    """`glossary list` shows all terms; --table filters to one mapping."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    glossary_path = tmp_path / "data" / "catalog" / "glossary.yaml"
    glossary_path.parent.mkdir(parents=True, exist_ok=True)
    glossary_path.write_text(
        "terms:\n"
        "  - term: 毛利\n"
        "    maps_to:\n"
        "      table: alpha\n"
        "      column: jin_e\n"
        "  - term: 活跃用户\n"
        "    maps_to:\n"
        "      table: beta\n"
        "      expression: \"count(*)\"\n"
        "      filter: \"status = 'done'\"\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["glossary", "list", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "毛利 -> alpha.jin_e" in result.output
    assert "活跃用户 -> beta.count(*)" in result.output
    assert "filter: status = 'done'" in result.output
    assert "2 term(s)." in result.output

    result = runner.invoke(
        app, ["glossary", "list", "--table", "alpha", "--config", str(config_file)]
    )
    assert result.exit_code == 0
    assert "毛利" in result.output
    assert "活跃用户" not in result.output
    assert "1 term(s)." in result.output


def test_retrieve_cmd_bm25_only_and_explain(tmp_path: Path) -> None:
    """`nl2data retrieve` runs BM25-only without EMB env; --explain is readable."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    xlsx = FIXTURES / "excel_basic.xlsx"
    assert runner.invoke(
        app, ["ingest", "excel", str(xlsx), "--config", str(config_file)]
    ).exit_code == 0
    assert runner.invoke(
        app, ["profile", "--all", "--config", str(config_file)]
    ).exit_code == 0
    assert runner.invoke(
        app, ["cards", "build", "--all", "--config", str(config_file)]
    ).exit_code == 0

    import os
    from unittest.mock import patch

    monkeypatch_env = {
        k: "" for k in ("EMB_BASE_URL", "EMB_API_KEY", "EMB_MODEL") if k in os.environ
    }
    with patch.dict(os.environ, monkeypatch_env, clear=False):
        result = runner.invoke(
            app,
            [
                "retrieve",
                "订单的金额",
                "-k",
                "2",
                "--explain",
                "--config",
                str(config_file),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "bm25" in result.output
    assert "ding_dan_ming_xi" in result.output
    assert "入选" in result.output
    assert "prompt tokens" in result.output


def test_retrieve_cmd_no_cards_guidance(tmp_path: Path) -> None:
    """Retrieval without cards exits with actionable guidance."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    result = runner.invoke(
        app, ["retrieve", "任意", "--config", str(config_file)]
    )
    assert result.exit_code == 1
    assert "cards build" in result.output


def test_eval_recall_cmd_fixture_golden(tmp_path: Path) -> None:
    """`eval recall` runs the shipped golden file end to end (BM25-only)."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    xlsx = FIXTURES / "excel_basic.xlsx"
    assert runner.invoke(
        app, ["ingest", "excel", str(xlsx), "--config", str(config_file)]
    ).exit_code == 0
    assert runner.invoke(
        app, ["profile", "--all", "--config", str(config_file)]
    ).exit_code == 0
    assert runner.invoke(
        app, ["cards", "build", "--all", "--config", str(config_file)]
    ).exit_code == 0

    golden = tmp_path / "golden.yaml"
    golden.write_text(
        "cases:\n"
        "  - section: \"s\"\n"
        "    question: \"订单明细的金额\"\n"
        "    expected_tables: [ding_dan_ming_xi]\n"
        "  - section: \"s\"\n"
        "    question: \"客户在城市哪里\"\n"
        "    expected_tables: [ke_hu]\n",
        encoding="utf-8",
    )
    import os
    from unittest.mock import patch

    monkeypatch_env = {
        k: "" for k in ("EMB_BASE_URL", "EMB_API_KEY", "EMB_MODEL") if k in os.environ
    }
    with patch.dict(os.environ, monkeypatch_env, clear=False):
        result = runner.invoke(
            app,
            ["eval", "recall", "--golden", str(golden), "--config", str(config_file)],
        )
    assert result.exit_code == 0, result.output
    assert "Recall@3" in result.output
    assert "per section" in result.output


def _cards_ready_workspace(tmp_path: Path) -> Path:
    """Provision ingest+profile+cards in a tmp workspace; return config path."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    for args in (
        ["ingest", "excel", str(FIXTURES / "excel_basic.xlsx")],
        ["profile", "--all"],
        ["cards", "build", "--all"],
    ):
        assert runner.invoke(app, [*args, "--config", str(config_file)]).exit_code == 0
    return config_file


class _StubEmbedder:
    """Deterministic embedder for index-build CLI tests (no network)."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_on and self.fail_on in texts[0]:
            from retrieval.embedding import EmbeddingUnavailableError

            raise EmbeddingUnavailableError("stub failure")
        return [[float(len(t) % 97)] * 1024 for t in texts]


def test_index_build_cmd_success_with_stub_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """index build syncs cards via an injected stub embedder."""
    config_file = _cards_ready_workspace(tmp_path)
    monkeypatch.setattr(
        "retrieval.embedding.client_from_env", lambda cfg: _StubEmbedder()
    )
    result = runner.invoke(app, ["index", "build", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert "index synced" in result.output
    assert "upserted=2" in result.output


def test_index_build_cmd_without_emb_env_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing EMB_* env is a warning-only path (BM25 remains available)."""
    import os
    from unittest.mock import patch

    config_file = _cards_ready_workspace(tmp_path)
    monkeypatch_env = {
        k: "" for k in ("EMB_BASE_URL", "EMB_API_KEY", "EMB_MODEL") if k in os.environ
    }
    with patch.dict(os.environ, monkeypatch_env, clear=False):
        monkeypatch.setattr("retrieval.embedding.client_from_env", lambda cfg: None)
        result = runner.invoke(app, ["index", "build", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "only the BM25 channel" in result.output


def test_index_build_cmd_embedding_failure_keeps_old_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing embedder exits 1 and announces the previous index is kept."""
    config_file = _cards_ready_workspace(tmp_path)
    monkeypatch.setattr(
        "retrieval.embedding.client_from_env", lambda cfg: _StubEmbedder()
    )
    first = runner.invoke(app, ["index", "build", "--config", str(config_file)])
    assert first.exit_code == 0
    monkeypatch.setattr(
        "retrieval.embedding.client_from_env",
        lambda cfg: _StubEmbedder(fail_on="POISON"),
    )
    poison = tmp_path / "data" / "catalog" / "cards_md" / "POISON_table.md"
    poison.write_text("# 表:POISON_table\n", encoding="utf-8")
    result = runner.invoke(app, ["index", "build", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "previous index kept" in result.output


def test_index_build_cmd_no_cards(tmp_path: Path) -> None:
    """index build without cards gives actionable guidance."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    result = runner.invoke(app, ["index", "build", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "cards build" in result.output


def test_eval_recall_cmd_missing_golden(tmp_path: Path) -> None:
    """A missing golden file is a clean exit-2 error."""
    config_file = _cards_ready_workspace(tmp_path)
    result = runner.invoke(
        app,
        [
            "eval",
            "recall",
            "--golden",
            str(tmp_path / "nope.yaml"),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert "not found" in result.output


class _ScriptedConsole:
    """Replaces rich console input with a scripted sequence of lines."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)

    def __call__(self, prompt: str = "") -> str:
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


def test_ask_interactive_loop_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The interactive loop dispatches /tables, /show sql and /exit."""
    import nl2data.qa as qa_mod

    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    catalog = tmp_path / "data" / "catalog" / "catalog.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "sources:"
        "\n  - name: s"
        "\n    type: excel"
        "\n    path: p"
        "\n    ingested_at: t"
        "\n    tables:"
        "\n      - name: my_table"
        "\n        original_name: my_table"
        "\n        parquet: p"
        "\n        rows: 1"
        "\n        columns: []"
        "\n",
        encoding="utf-8",
    )

    calls: list[str] = []

    class _FakeOutcome:
        clarification = None
        ok = False
        vsql = None
        execution = None
        failure_reason = "stub"

    def _fake_ask(
        question: str,
        cfg: object,
        *,
        no_interpret: bool = False,
        extra_feedback: str | None = None,
    ) -> object:
        calls.append(question)
        return _FakeOutcome()

    monkeypatch.setattr(qa_mod, "ask_once", _fake_ask)
    # One shared scripted instance: each Console.input call consumes the
    # next line (a fresh instance per call would loop forever on line one).
    scripted = _ScriptedConsole(["/tables", "/show sql", "/bogus", "exit"])
    monkeypatch.setattr(
        "nl2data.cli.Console.input",
        lambda self, prompt="": scripted(),
    )

    result = runner.invoke(app, ["ask", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert "再见" in result.output
    assert "未知命令:/bogus" in result.output
    # /tables listed nothing (empty catalog) and /show sql noted no SQL yet.
    assert "还没有已执行的 SQL" in result.output
