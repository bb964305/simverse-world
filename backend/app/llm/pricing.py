"""LLM price table + cost computation (P1-1).

All rates are **Anthropic list prices, USD per 1,000,000 tokens**, matched to
the model family by prefix. The production endpoint is a DashScope-style relay
whose real tariff is unverified (COST_RESEARCH_REPORT §五, "待 Jimmy"), so
``cost_usd`` is an *estimate*: the budget circuit breaker relies on relative
spend within a window, not on absolute-dollar accuracy.

Unknown / non-Anthropic models (e.g. the kimi-k2.5 video path) fall back to the
Haiku rate so their spend is still counted rather than silently zeroed.
"""
from __future__ import annotations

# (input, output, cache_read, cache_creation) USD per 1M tokens.
_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku": (1.00, 5.00, 0.10, 1.25),
    "claude-3-5-haiku": (1.00, 5.00, 0.10, 1.25),
    "claude-sonnet": (3.00, 15.00, 0.30, 3.75),
    "claude-opus": (15.00, 75.00, 1.50, 18.75),
}

_DEFAULT = _PER_MTOK["claude-haiku"]


def _rates(model: str) -> tuple[float, float, float, float]:
    """Longest-prefix match of ``model`` against the price table."""
    if not model:
        return _DEFAULT
    m = model.lower()
    best: tuple[int, tuple[float, float, float, float]] | None = None
    for prefix, rates in _PER_MTOK.items():
        if m.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), rates)
    return best[1] if best else _DEFAULT


def compute_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Return the estimated USD cost of a single call.

    ``input_tokens`` is Anthropic's non-cached input count; cache read/creation
    tokens are billed at their own (lower / higher) rates and added on top.
    """
    in_rate, out_rate, cr_rate, cc_rate = _rates(model)
    cost = (
        (input_tokens or 0) * in_rate
        + (output_tokens or 0) * out_rate
        + (cache_read_tokens or 0) * cr_rate
        + (cache_creation_tokens or 0) * cc_rate
    ) / 1_000_000
    return round(cost, 8)
