"""Human-editable table notes (milestone 2, T5).

``docs/table_notes.md`` carries free-form per-table descriptions written and
maintained by humans. A section starts at a ``# 表:<name>`` or
``## 表:<name>`` heading (optional spaces around the colon); the stripped
body up to the next heading is that table's description. ``<name>`` is
matched as a safe table name first, then resolved through the catalog as an
``original_name`` — which is how Chinese table names match. A missing notes
file is legal and yields an empty mapping (silent degradation).
"""

from __future__ import annotations

import re
from pathlib import Path

from catalog.store import CatalogStore
from nl2data.config import Nl2DataConfig

# A table section heading: "# 表: name" / "## 表:name" (ASCII or full-width
# colon, optional spaces).
_TABLE_HEADING_RE = re.compile(r"^#{1,2}\s*表\s*[:：]\s*(.*?)\s*$")
# Any ATX heading terminates the current section body.
_ANY_HEADING_RE = re.compile(r"^#{1,6}(?:\s|$)")


def _catalog_name_maps(cfg: Nl2DataConfig) -> tuple[set[str], dict[str, str]]:
    """Return (safe table names, original_name -> safe name) from the catalog."""
    store = CatalogStore(cfg.paths.catalog)
    safe_names: set[str] = set()
    by_original: dict[str, str] = {}
    for source in store.sources:
        for table in source.tables:
            safe_names.add(table.name)
            by_original.setdefault(table.original_name, table.name)
    return safe_names, by_original


def load_table_notes(notes_path: Path, cfg: Nl2DataConfig) -> dict[str, str]:
    """Parse the notes file into ``{safe table name: description}``.

    Args:
        notes_path: Path of the human-editable markdown file.
        cfg: Active configuration; the catalog resolves original names.

    Returns:
        Mapping of safe table names to stripped descriptions. Sections with
        an empty body count as misses and are dropped; a missing file yields
        ``{}``; later sections override earlier ones with the same name.
    """
    if not notes_path.exists():
        return {}
    safe_names, by_original = _catalog_name_maps(cfg)

    sections: list[tuple[str, str]] = []
    current: str | None = None
    body: list[str] = []

    def _flush() -> None:
        if current is not None:
            sections.append((current, "\n".join(body).strip()))

    for line in notes_path.read_text(encoding="utf-8-sig").splitlines():
        heading = _TABLE_HEADING_RE.match(line)
        if heading is not None:
            _flush()
            current = heading.group(1).strip()
            body = []
            continue
        if current is None:
            continue
        if _ANY_HEADING_RE.match(line):
            _flush()
            current = None
            body = []
        else:
            body.append(line)
    _flush()

    notes: dict[str, str] = {}
    for name, text in sections:
        if not text:
            continue
        key = name if name in safe_names else by_original.get(name, name)
        notes[key] = text
    return notes
