"""Shared byte-stream decoding for CSV-like ingestion (JE feedback #1).

Chinese-Windows ecosystems routinely produce GBK CSV exports (IDEA, domestic
ERP systems, WeChat-transferred files) while well-behaved tools emit UTF-8
(possibly with a BOM). :func:`decode_bytes` tries the realistic chain in
order — ``utf-8-sig`` → ``gbk`` → ``utf-8`` (lossy) — so ingestion survives
either world instead of failing on the first non-ASCII cell.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Decode attempts in order; the final entry is the lossy fallback.
_ENCODING_CHAIN: tuple[str, ...] = ("utf-8-sig", "gbk", "utf-8")


def decode_bytes(data: bytes) -> str:
    """Decode ``data`` with the utf-8-sig → gbk → utf-8 chain.

    Returns the first successful strict decode. When every strict attempt
    fails, falls back to lossy UTF-8 with ``errors="replace"`` and a warning
    (a readable mojibake row beats a crashed ingest; callers keep going).
    """
    for encoding in _ENCODING_CHAIN:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    logger.warning(
        "CSV bytes decode failed for utf-8-sig and gbk; falling back to "
        "lossy utf-8 (replaced characters)"
    )
    return data.decode("utf-8", errors="replace")
