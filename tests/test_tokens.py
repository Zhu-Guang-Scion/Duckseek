"""Tests for the shared token estimation module."""

from __future__ import annotations

from dataclasses import replace

import pytest

from nl2data.config import TokensConfig
from retrieval.tokens import TokenEstimatorError, estimate_tokens


def test_default_strategy_is_conservative_quarter() -> None:
    """len_div_4 floors the character count divided by four."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_estimate_is_monotonic_in_length() -> None:
    """Longer content never yields a smaller estimate."""
    previous = -1
    for size in range(0, 1000, 37):
        value = estimate_tokens("x" * size)
        assert value >= previous
        previous = value


def test_unknown_strategy_raises() -> None:
    """An unknown configured strategy is a clear error."""
    config = TokensConfig(estimator="tiktoken")
    with pytest.raises(TokenEstimatorError, match="tiktoken"):
        estimate_tokens("hello", config)


def test_config_override_changes_strategy_selection() -> None:
    """Only len_div_4 is implemented; config flows through the dispatcher."""
    config = replace(TokensConfig(), estimator="len_div_4")
    assert estimate_tokens("abcd", config) == 1
