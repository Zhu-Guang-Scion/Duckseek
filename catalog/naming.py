"""Identifier naming rules (goals.md §5.4 contract).

The locked ``clean_name`` contract:

- output contains only ``[a-z0-9_]``
- never starts with a digit (an underscore is prepended when needed)
- at most ``max_length`` characters (default 63)
- Chinese characters are transliterated to pinyin
- conflicts against ``existing`` are resolved with a ``_N`` suffix
- original names stay recorded in catalog.yaml (bidirectional mapping)
"""

from __future__ import annotations

import re

from pypinyin import lazy_pinyin

_DISALLOWED = re.compile(r"[^a-z0-9_]+")
_MULTI_UNDERSCORE = re.compile(r"_+")
_FALLBACK_NAME = "tbl"
_MIN_MAX_LENGTH = 4


def _slugify(raw: str) -> str:
    """Transliterate a raw identifier into a lowercase pinyin-safe slug."""
    pieces = [piece.lower() for piece in lazy_pinyin(raw) if piece]
    slug = _DISALLOWED.sub("_", "_".join(pieces))
    return _MULTI_UNDERSCORE.sub("_", slug).strip("_")


def clean_name(
    raw: str,
    existing: set[str] | None = None,
    *,
    max_length: int = 63,
) -> str:
    """Return the clean identifier for ``raw`` per the §5.4 naming contract.

    Args:
        raw: Original identifier (column name, sheet name, file stem, ...).
        existing: Names already taken; conflicts receive a ``_N`` suffix and
            the returned name is registered into the set (allocator style).
        max_length: Maximum length of the returned identifier.

    Returns:
        An identifier matching the contract, unique against ``existing``
        when it is provided.

    Raises:
        ValueError: If ``max_length`` is too small to hold a ``_N`` suffix.
    """
    if max_length < _MIN_MAX_LENGTH:
        msg = f"max_length must be >= {_MIN_MAX_LENGTH}, got {max_length}"
        raise ValueError(msg)

    slug = _slugify(raw)
    if not slug:
        slug = _FALLBACK_NAME
    if slug[0].isdigit():
        slug = f"_{slug}"
    slug = slug[:max_length]

    if existing is None:
        return slug
    if slug in existing:
        suffix_num = 1
        while True:
            suffix = f"_{suffix_num}"
            candidate = slug[: max_length - len(suffix)] + suffix
            if candidate not in existing:
                slug = candidate
                break
            suffix_num += 1
    existing.add(slug)
    return slug
