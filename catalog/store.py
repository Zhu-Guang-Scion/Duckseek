"""catalog.yaml storage: the lineage contract from goals.md §5.2.

Locked schema::

    sources[]:
      name, type, path, ingested_at,
      tables[]: name, original_name, parquet, rows,
                columns[]: name, original_name
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class CatalogError(RuntimeError):
    """Raised when catalog.yaml cannot be parsed."""


@dataclass(frozen=True)
class ColumnEntry:
    """One column: clean name plus its original name."""

    name: str
    original_name: str


@dataclass(frozen=True)
class TableEntry:
    """One ingested table and its Parquet artifact."""

    name: str
    original_name: str
    parquet: str
    rows: int
    columns: list[ColumnEntry] = field(default_factory=list)


@dataclass(frozen=True)
class SourceEntry:
    """One ingested source file and its tables."""

    name: str
    type: str
    path: str
    ingested_at: str
    tables: list[TableEntry] = field(default_factory=list)


class CatalogStore:
    """Read/write access to catalog.yaml with upsert-by-source-name semantics."""

    def __init__(self, path: Path) -> None:
        """Open the catalog backed by ``path``; a missing file means empty."""
        self._path = path
        self._sources: dict[str, SourceEntry] = {}
        self._load()

    @property
    def path(self) -> Path:
        """Filesystem path of the backing catalog.yaml."""
        return self._path

    @property
    def sources(self) -> list[SourceEntry]:
        """All sources in insertion order."""
        return list(self._sources.values())

    def _load(self) -> None:
        """Parse catalog.yaml if it exists, populating the source map."""
        if not self._path.exists():
            return
        try:
            raw: dict[str, Any] = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            msg = f"invalid YAML in {self._path}: {exc}"
            raise CatalogError(msg) from exc
        for source in raw.get("sources", []):
            tables = [
                TableEntry(
                    name=table["name"],
                    original_name=table["original_name"],
                    parquet=table["parquet"],
                    rows=int(table["rows"]),
                    columns=[
                        ColumnEntry(name=col["name"], original_name=col["original_name"])
                        for col in table.get("columns", [])
                    ],
                )
                for table in source.get("tables", [])
            ]
            entry = SourceEntry(
                name=source["name"],
                type=source["type"],
                path=source["path"],
                ingested_at=source["ingested_at"],
                tables=tables,
            )
            self._sources[entry.name] = entry

    def get_source(self, name: str) -> SourceEntry | None:
        """Return the source with ``name``, or None."""
        return self._sources.get(name)

    def get_table(self, name: str) -> tuple[SourceEntry, TableEntry] | None:
        """Find a table by its clean name across all sources."""
        for source in self._sources.values():
            for table in source.tables:
                if table.name == name:
                    return source, table
        return None

    def upsert_source(self, entry: SourceEntry) -> None:
        """Insert or replace the source carrying the same name."""
        self._sources[entry.name] = entry

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the plain dict shape of the §5.2 contract."""
        return {
            "sources": [
                {
                    "name": source.name,
                    "type": source.type,
                    "path": source.path,
                    "ingested_at": source.ingested_at,
                    "tables": [
                        {
                            "name": table.name,
                            "original_name": table.original_name,
                            "parquet": table.parquet,
                            "rows": table.rows,
                            "columns": [
                                {"name": col.name, "original_name": col.original_name}
                                for col in table.columns
                            ],
                        }
                        for table in source.tables
                    ],
                }
                for source in self._sources.values()
            ]
        }

    def save(self) -> None:
        """Write catalog.yaml (UTF-8, key order preserved)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
