"""LLM price table + cost computation (P1-1).

Rates are **USD per 1,000,000 tokens**, matched to the model family by longest
prefix. ``claude-*`` families use Anthropic list prices. DashScope pay-as-you-go
models (deepseek-v4-flash, qwen3.7-plus) are converted from the published CNY
tariff at 7.2 CNY/USD (2026-07-15; the official pages also quote the same USD
figures). Unlike the old Coding-Plan subscription, the按量 endpoint is genuinely
metered, so a deepseek ``cost_usd`` is now reconcilable against the provider bill
(F-02) rather than a pure estimate.

Unknown / unpriced models (e.g. the kimi-k2.5 video path) fall back to the Haiku
rate so their spend is still counted rather than silently zeroed.
"""
from __future__ import annotations

# (input, output, cache_read, cache_creation) USD per 1M tokens.
_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku": (1.00, 5.00, 0.10, 1.25),
    "claude-3-5-haiku": (1.00, 5.00, 0.10, 1.25),
    "claude-sonnet": (3.00, 15.00, 0.30, 3.75),
    "claude-opus": (15.00, 75.00, 1.50, 18.75),
    # ---- DashScope 按量计费（Anthropic 兼容端点 /apps/anthropic）：非 Anthropic 模型 ----
    # deepseek-v4-flash：输入 ¥1(cache-miss) / 输出 ¥2 / 输入缓存命中 ¥0.02
    #   → @7.2 = $0.14 / $0.28 / $0.0028（官方 USD 同值）。deepseek 无独立 cache-write
    #   加价，cache_creation 按输入价 ¥1 = $0.14 计。不补则回落 haiku $1/$5，虚高 ~7x
    #   → 预算熔断误触发（Kickoff V6 任务 2.1）。
    "deepseek-v4-flash": (0.14, 0.28, 0.0028, 0.14),
    # qwen3.7-plus：百炼 Coding Plan（订阅制，实际成本已摊销固定）；此价仅供 burn-in
    #   阶段 1/2 历史 llm_usage 行重算口径，用 qwen-plus 档按量价 输入 ¥0.8 / 输出 ¥2 /
    #   缓存命中 ~¥0.16 → @7.2 = $0.11 / $0.28 / $0.022。
    "qwen3.7-plus": (0.11, 0.28, 0.022, 0.11),
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
