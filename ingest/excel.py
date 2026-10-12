"""Excel (.xlsx/.xlsm/.xls) ingestion: sheet frames into the shared pipeline.

Reader strategy: the DuckDB ``excel`` extension is preferred for .xlsx/.xlsm
(fast, typed reads); any failure falls back to ``pandas.read_excel`` (openpyxl
engine) with a warning. Legacy binary ``.xls`` workbooks (openpyxl cannot read
them; the dominant legacy format in Chinese enterprises and IDEA exports —
JE feedback #2) go straight to pandas with the ``xlrd`` engine, including a
xlrd-based header probe so report titles never become column names.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import xlrd
from openpyxl import load_workbook

from catalog.naming import clean_name
from catalog.store import SourceEntry
from ingest.common import SOURCE_TYPE_EXCEL, IngestError, PreparedTable, ingest_tables
from nl2data.config import Nl2DataConfig

logger = logging.getLogger(__name__)

_SUPPORTED_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xls"})
_EXCEL_EXTENSION = "excel"
_READ_XLSX_SQL = "SELECT * FROM read_xlsx(?, sheet = ?, header = true, stop_at_empty = false)"

# None until the first attempt; True/False afterwards (never retried).
_extension_available: bool | None = None


def _is_blank_cell(value: Any) -> bool:
    """True for None or whitespace-only cells."""
    return value is None or str(value).strip() == ""


def _is_data_like(value: Any) -> bool:
    """True when a cell reads as data (number/parseable-numeric/date), not prose.

    Numeric-parseable strings count as data (chart-of-account codes like
    '1002' are data, not header words) — jekit feedback #13 R2.
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return True
    if isinstance(value, datetime | date):
        return True
    text = str(value).strip()
    if not text:
        return False
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def _row_profile(row: list[Any]) -> tuple[int, int]:
    """Return ``(non_empty, rightmost_index)`` of one row."""
    non_empty = 0
    rightmost = -1
    for index, value in enumerate(row):
        if not _is_blank_cell(value):
            non_empty += 1
            rightmost = index
    return non_empty, rightmost


def detect_header_row(rows: list[list[Any]], *, max_scan: int = 5) -> int:
    """Return the 0-based header row index inside ``rows`` (JE feedback #13).

    Candidate generation keeps the original signal (≥2 non-empty cells);
    candidates then pass three deterministic gates — jekit's four factors
    minus the domain dictionary, gates instead of tuned weights:

    - coverage: non-empty cells span at least half the window's effective
      width (merged-cell report titles fill 1–2 cells over a wide table);
    - textiness: most non-empty cells are NOT data-like (numbers, numeric
      strings, dates) — headers are words;
    - type contrast: the NEXT non-empty row is mostly data-like (the
      text→data transition marks the boundary; looking only at the row
      itself is the classic mistake).

    Fallback is honest (R6b): when nothing qualifies, row 0 is returned with
    an explicit warning instead of silently skipping ``max_scan`` rows.

    Args:
        rows: Leading sheet rows as raw cell values (one extra row beyond
            ``max_scan`` enables the contrast gate for the last candidate).
        max_scan: Maximum number of rows to consider as header candidates.

    Returns:
        The 0-based header row index, or 0 with a warning when no row
        qualifies.
    """
    window = rows[:max_scan]
    effective_width = max((_row_profile(row)[1] for row in window), default=-1) + 1
    for index, row in enumerate(window):
        non_empty, _ = _row_profile(row)
        if non_empty < 2:
            continue
        if effective_width and non_empty < effective_width * 0.5:
            continue  # sparse merged title over a wide table (R1)
        cells = [value for value in row if not _is_blank_cell(value)]
        textiness = sum(1 for value in cells if not _is_data_like(value)) / len(cells)
        if textiness < 0.6:
            continue  # headers are words, not numbers (R2)
        following = next(
            (
                later
                for later in rows[index + 1 : max_scan + 1]
                if _row_profile(later)[0] >= 2
            ),
            None,
        )
        if following is not None:
            nxt = [value for value in following if not _is_blank_cell(value)]
            nxt_data = sum(1 for value in nxt if _is_data_like(value)) / len(nxt)
            cur_data = sum(1 for value in cells if _is_data_like(value)) / len(cells)
            if nxt_data <= cur_data:
                # No text→data transition below (R4, relative form: the next
                # row must be MORE data-like than the candidate — an absolute
                # ≥0.5 share is ledger-domain tuning and breaks on generic
                # mixed columns like P001|100.0|北京).
                continue
        if index > 0:
            logger.info(
                "header row detected at row %d (the %d row(s) above will be skipped)",
                index + 1,
                index,
            )
        return index
    logger.warning(
        "no row qualifies as header within the first %d rows; "
        "falling back to row 0 — verify the ingested column names",
        max_scan,
    )
    return 0


def _load_excel_extension() -> bool:
    """INSTALL and LOAD the DuckDB excel extension once per process.

    Returns:
        True when the extension is usable. Any failure is logged once as a
        warning and never retried; the pandas fallback then serves sheets.
    """
    global _extension_available
    if _extension_available is not None:
        return _extension_available
    try:
        conn = duckdb.connect()
        try:
            conn.execute(f"INSTALL {_EXCEL_EXTENSION}")
            conn.execute(f"LOAD {_EXCEL_EXTENSION}")
        finally:
            conn.close()
    except duckdb.Error as exc:
        logger.warning("duckdb excel extension unavailable; using pandas fallback: %s", exc)
        _extension_available = False
    else:
        _extension_available = True
    return _extension_available


def _sheet_names(source: Path) -> list[str]:
    """Return the workbook sheet names (.xls via xlrd, else openpyxl)."""
    if source.suffix.lower() == ".xls":
        workbook = xlrd.open_workbook(str(source), on_demand=True)
        try:
            return list(workbook.sheet_names())
        finally:
            workbook.release_resources()
    workbook = load_workbook(source, read_only=True)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def _probe_header_row(source: Path, sheet: str, scan_rows: int) -> int:
    """Scan the first ``scan_rows`` rows of ``sheet`` for the header row."""
    if source.suffix.lower() == ".xls":
        workbook = xlrd.open_workbook(str(source), on_demand=True)
        try:
            worksheet = workbook.sheet_by_name(sheet)
            rows = [
                list(worksheet.row_values(row_index))
                for row_index in range(min(scan_rows + 1, worksheet.nrows))
            ]
        finally:
            workbook.release_resources()
        return detect_header_row(rows, max_scan=scan_rows)
    workbook = load_workbook(source, read_only=True)
    try:
        worksheet = workbook[sheet]
        rows = [
            list(row)
            for row in worksheet.iter_rows(min_row=1, max_row=scan_rows + 1, values_only=True)
        ]
    finally:
        workbook.close()
    return detect_header_row(rows, max_scan=scan_rows)


def _read_sheet_via_duckdb(source: Path, sheet: str) -> pd.DataFrame:
    """Read one sheet with the DuckDB excel extension (header on row 1)."""
    conn = duckdb.connect()
    try:
        conn.execute(f"LOAD {_EXCEL_EXTENSION}")
        return conn.sql(_READ_XLSX_SQL, params=[str(source), sheet]).df()
    finally:
        conn.close()


def _is_blank_header(name: object) -> bool:
    """Return True when a column header cell is empty or a pandas placeholder."""
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return True
    text = str(name).strip()
    return text == "" or text.lower().startswith("unnamed")


def _postprocess(frame: pd.DataFrame, *, sheet: str) -> pd.DataFrame:
    """Shared cleanup for both reader paths.

    Drops columns whose header is blank and whose data is entirely empty,
    drops fully empty data rows, and downgrades header-bearing all-NULL
    columns to ``string`` (logged) so they survive the Parquet round-trip.

    Args:
        frame: Raw sheet frame from either reader.
        sheet: Sheet name, for log messages.

    Returns:
        The cleaned frame.
    """
    if frame.empty:
        return frame
    blank = [
        str(col)
        for col in frame.columns
        if _is_blank_header(col) and frame[col].isna().all()
    ]
    if blank:
        frame = frame.drop(columns=blank)
    frame = frame.dropna(how="all").reset_index(drop=True)
    for column in frame.columns:
        if frame[column].isna().all():
            frame[column] = frame[column].astype("string")
            logger.info(
                "column %s in sheet %s has no non-NULL values; downgraded to VARCHAR",
                column,
                sheet,
            )
    return frame


def _read_sheet(
    source: Path,
    sheet: str,
    cfg: Nl2DataConfig,
    extension_available: bool,
) -> pd.DataFrame | None:
    """Read and clean one sheet; return None when it ends up empty.

    The DuckDB reader is only tried when the detected header row is the first
    row (its ``header=true`` contract) and never for legacy ``.xls`` (the
    extension reads xlsx only); any ``duckdb.Error`` such as the Binder Error
    raised for empty sheets falls back to pandas with a warning.
    """
    header_row = _probe_header_row(source, sheet, cfg.ingest.header_scan_rows)
    frame: pd.DataFrame | None = None
    is_xls = source.suffix.lower() == ".xls"
    if extension_available and header_row == 0 and not is_xls:
        try:
            frame = _read_sheet_via_duckdb(source, sheet)
        except duckdb.Error as exc:
            logger.warning(
                "duckdb read_xlsx failed for sheet %r in %s; falling back to pandas: %s",
                sheet,
                source,
                exc,
            )
    if frame is None:
        frame = pd.read_excel(
            source,
            sheet_name=sheet,
            engine="xlrd" if is_xls else "openpyxl",
            header=header_row,
        )
    frame = _postprocess(frame, sheet=sheet)
    if frame.empty:
        logger.warning("sheet %r in %s is empty after cleanup; skipped", sheet, source)
        return None
    return frame


def ingest_excel(
    source: Path,
    cfg: Nl2DataConfig,
    sheet: str | None = None,
    name: str | None = None,
) -> SourceEntry:
    """Ingest an ``.xlsx``/``.xlsm``/``.xls`` workbook through the shared pipeline.

    Every non-empty sheet becomes one table: the header row is auto-detected,
    the sheet is read via the DuckDB excel extension when available (pandas
    fallback otherwise; legacy ``.xls`` always uses the pandas/xlrd path),
    then handed to :func:`ingest.common.ingest_tables`, which owns naming,
    Parquet storage, DuckDB views and catalog lineage.

    Args:
        source: Workbook path; suffix must be ``.xlsx``/``.xlsm``/``.xls``.
        cfg: Active configuration.
        sheet: Exact sheet name to ingest; ``None`` ingests every sheet.
        name: Optional source alias overriding the file stem for the slug.

    Returns:
        The catalog entry written for this source.

    Raises:
        IngestError: If the file is missing, its suffix is unsupported
            (``.xlsb``/``.numbers`` are not supported), the requested
            sheet does not exist, or no non-empty sheet remains.
    """
    source = Path(source)
    if not source.is_file():
        msg = f"Excel file not found: {source}"
        raise IngestError(msg)
    suffix = source.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        msg = (
            f"unsupported Excel file {source.name!r} (suffix {suffix!r}); "
            "supported: .xlsx/.xlsm/.xls (.xlsb/.numbers are not supported)"
        )
        raise IngestError(msg)

    available = _sheet_names(source)
    if sheet is not None and sheet not in available:
        msg = (
            f"sheet {sheet!r} not found in {source}; "
            f"available sheets: {', '.join(repr(n) for n in available)}"
        )
        raise IngestError(msg)
    wanted = available if sheet is None else [sheet]

    extension_available = _load_excel_extension()
    tables: list[PreparedTable] = []
    for sheet_name in wanted:
        frame = _read_sheet(source, sheet_name, cfg, extension_available)
        if frame is not None:
            tables.append(PreparedTable(original_name=sheet_name, frame=frame))

    return ingest_tables(
        source_name=clean_name(
            name or source.stem,
            max_length=cfg.ingest.max_name_length,
        ),
        source_type=SOURCE_TYPE_EXCEL,
        source_path=source,
        tables=tables,
        cfg=cfg,
    )
