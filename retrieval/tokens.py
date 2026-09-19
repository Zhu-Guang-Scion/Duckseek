"""Token estimation for prompt budgets (milestone 2; shared with T7).

The default strategy ``len_div_4`` is a deliberately conservative character
count divided by four. The strategy is selected in configuration so it can be
swapped for a real tokenizer (e.g. tiktoken) without touching call sites.
"""

from __future__ import annotations

from nl2data.config import TokensConfig


class TokenEstimatorError(RuntimeError):
    """Raised when the configured token estimation strategy is unknown."""


def estimate_tokens(text: str, config: TokensConfig | None = None) -> int:
    """Estimate the LLM token count of ``text``.

    Args:
        text: Arbitrary text (markdown cards, glossary excerpts, ...).
        config: Token configuration; defaults to the built-in strategy.

    Returns:
        A conservative non-negative token estimate.

    Raises:
        TokenEstimatorError: If the configured strategy is unknown.
    """
    strategy = config.estimator if config is not None else "len_div_4"
    if strategy == "len_div_4":
        return len(text) // 4
    msg = f"unknown token estimator: {strategy!r}"
    raise TokenEstimatorError(msg)
