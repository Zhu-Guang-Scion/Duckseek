"""Audit trail: one JSONL event per question (milestone 3, T12).

Events land in ``data/audit/<YYYY-MM-DD>.jsonl``. Writing is best-effort:
callers warn on failure and never block the question loop.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nl2data.config import Nl2DataConfig


def _audit_files(cfg: Nl2DataConfig) -> list[Path]:
    """Audit day files sorted oldest first."""
    if not cfg.paths.audit_dir.is_dir():
        return []
    return sorted(cfg.paths.audit_dir.glob("*.jsonl"))


def write_audit_event(cfg: Nl2DataConfig, event: dict[str, Any]) -> Path:
    """Append one event to today's JSONL file.

    Returns:
        The file written to.

    Raises:
        OSError: On filesystem failures (caller decides how loud to be).
    """
    cfg.paths.audit_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.paths.audit_dir / f"{datetime.now(UTC).date().isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def load_recent_events(cfg: Nl2DataConfig, limit: int = 10) -> list[dict[str, Any]]:
    """Read the most recent ``limit`` events across all day files."""
    events: list[dict[str, Any]] = []
    for path in reversed(_audit_files(cfg)):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
            if len(events) >= limit:
                return events
    return events
