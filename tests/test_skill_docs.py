"""Acceptance tests for the T18 nl2data skill package (skills/nl2data/).

The task brief's acceptance criteria are machine-checkable, so they are
pinned here: file set, SKILL.md budget and frontmatter, tool references
restricted to the real T17 surface, the four credential disciplines, and
config-template.sh variable names matching goals.md decision 3/8.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "nl2data"
SKILL_MD = SKILL_DIR / "SKILL.md"
TEMPLATE = SKILL_DIR / "config-template.sh"
README = SKILL_DIR / "README.md"

# goals.md §3 decision 3 (LLM_*) + decision 8 (EMB_*).
GOALS_ENV_VARS = {
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "EMB_BASE_URL",
    "EMB_API_KEY",
    "EMB_MODEL",
}
TOOL_SURFACE = {"nl2data_status", "nl2data_list_tables", "nl2data_ask"}


def test_skill_package_files_exist() -> None:
    """The distributable directory carries exactly the three required files."""
    assert SKILL_DIR.is_dir()
    for path in (SKILL_MD, TEMPLATE, README):
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").strip()


def test_skill_md_frontmatter_and_line_budget() -> None:
    """Frontmatter names the skill; the main file stays under 150 lines."""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---\n", 2)[1]
    assert "name: nl2data" in frontmatter
    assert "description:" in frontmatter and "自然语言查询" in frontmatter
    assert len(text.splitlines()) < 150


def test_skill_references_only_real_tools() -> None:
    """No dangling tool promises: every nl2data_* mention is a real T17 tool,
    and all three tools appear in the workflow."""
    text = SKILL_MD.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"nl2data_[a-z_]+", text))
    assert mentioned == TOOL_SURFACE


def test_skill_contains_four_disciplines() -> None:
    """All four credential/behaviour disciplines are spelled out."""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "绝不把 API key 写入任何文件" in text
    assert "不向 nl2data 传密钥类参数" in text
    assert "一次一问" in text
    assert "词典里未定义" in text


def test_skill_workflow_steps_and_fault_table() -> None:
    """The workflow covers status→list_tables→ask→呈现 plus the mandatory
    fault-table rows (index unbuilt / table missing / timeout / rotated key)."""
    text = SKILL_MD.read_text(encoding="utf-8")
    for step in ("Step a", "Step b", "Step c", "Step d"):
        assert step in text, step
    for keyword in ("index.built=false", "查不到某表", "超时", "已轮换失效"):
        assert keyword in text, keyword
    for command in ("profile --all", "cards build --all", "index build"):
        assert command in text, command
    assert "embedding_degraded=true" in text  # V5-E: rotated-key row


def test_skill_referenced_files_exist() -> None:
    """Files referenced from SKILL.md (progressive disclosure) all exist."""
    text = SKILL_MD.read_text(encoding="utf-8")
    referenced = re.findall(r"\]\(([^)#]+)\)", text)
    assert referenced
    for name in referenced:
        assert (SKILL_DIR / name).is_file(), name


def test_config_template_variables_match_goals() -> None:
    """config-template.sh exports exactly the six goals.md §3 variables,
    all with empty placeholder values (nothing secret is shipped)."""
    text = TEMPLATE.read_text(encoding="utf-8")
    exported = set(re.findall(r"^export ([A-Z_]+)=", text, re.MULTILINE))
    assert exported == GOALS_ENV_VARS
    values = re.findall(r'^export [A-Z_]+="([^"]*)"', text, re.MULTILINE)
    assert values == [""] * len(GOALS_ENV_VARS)
    assert "硅基流动" in text  # worked example comments, as briefed


def test_skill_package_carries_no_secrets() -> None:
    """No credential-looking literals anywhere in the skill package."""
    for path in (SKILL_MD, TEMPLATE, README):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"sk-[A-Za-z0-9]{8,}", text), path
        assert not re.search(r"(?i)api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9]{8,}", text), path
        assert "bigmodel.cn" not in text and "d2d161b3" not in text, path
