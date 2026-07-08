"""Per-attempt LLM usage metering (P1-1, E-19/E-23).

Records one ``llm_usage`` row per LLM *attempt*. Design invariants:

* **Never breaks the caller.** Every public coroutine here swallows its own
  exceptions — a metering failure must never bubble into an LLM call or the
  business transaction that spawned it.
* **Own session.** Rows are written through a dedicated short-lived session
  (``_session_factory``, defaulting to ``app.database.async_session``) so a
  caller rollback can't drop telemetry and a metering write can't poison the
  caller's transaction. Tests inject a sqlite factory via ``set_session_factory``.
* **usage → estimated fallback.** If the endpoint omits ``response.usage``
  (the relay might, COST_RESEARCH_REPORT 修正-2) we shadow-meter from a char
  heuristic and tag ``source="estimated"`` so the schema stays uniform.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.llm.pricing import compute_cost
# Imported eagerly so the table is registered on Base.metadata (tests'
# create_all) whenever the LLM client is loaded. The runtime insert below
# re-imports locally to keep the write path independent of import order.
from app.models.llm_usage import LLMUsage  # noqa: F401

logger = logging.getLogger(__name__)

# Well-known scenario tags (String column, not a DB enum — kept flexible).
SCENARIOS = frozenset({
    "plan", "decide", "chat_turn", "summary", "extract", "update_rel", "reflect",
    "evolution_shift", "evolution_drift", "evolution_sync",
    "player_chat", "player_wrapup", "video",
    "sbti", "sprite", "skill_import",
    "forge_ability", "forge_persona", "forge_soul", "forge_score", "forge_district",
    "forge_quick", "forge_router", "forge_build", "forge_extract",
    "forge_validate", "forge_refine",
})

_session_factory = None  # lazily bound to app.database.async_session


def set_session_factory(factory) -> None:
    """Point metering writes at a specific async-session factory (tests)."""
    global _session_factory
    _session_factory = factory


def _factory():
    global _session_factory
    if _session_factory is None:
        from app.database import async_session
        _session_factory = async_session
    return _session_factory


@dataclass
class Meter:
    """Lightweight metering context threaded into ``chat()`` / ``stream_chat()``.

    Holds only the who/what of a call; the how-much (tokens, latency, cost) is
    filled in by the client wrapper from the actual response.
    """
    scenario: str
    resident_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    attempt_no: int = 1


def estimate_tokens(text: str | None) -> int:
    """Rough token estimate for the usage-missing fallback path.

    CJK is denser per character than latin, so weight the two ranges
    separately. Absolute accuracy is not required — this only feeds the
    shadow (``source="estimated"``) path when the endpoint returns no usage.
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    other = len(text) - cjk
    return max(1, round(cjk * 0.6 + other * 0.25))


def usage_from_response(response) -> dict | None:
    """Pull token counts from an Anthropic response ``usage`` block, or None."""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


async def record_usage(
    scenario: str,
    *,
    model: str,
    owner: str = "system",
    response=None,
    est_input_tokens: int = 0,
    est_output_tokens: int = 0,
    resident_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    attempt_no: int = 1,
    parse_ok: bool | None = None,
    latency_ms: int | None = None,
) -> None:
    """Append one telemetry row for a single LLM attempt. Never raises.

    If ``response`` carries a ``usage`` block it is authoritative
    (``source="usage"``); otherwise ``est_*_tokens`` are used
    (``source="estimated"``).
    """
    if not settings.llm_metering_enabled:
        return
    try:
        usage = usage_from_response(response) if response is not None else None
        if usage is not None:
            source = "usage"
            in_tok = usage["input_tokens"]
            out_tok = usage["output_tokens"]
            cache_read = usage["cache_read_tokens"]
            cache_creation = usage["cache_creation_tokens"]
        else:
            source = "estimated"
            in_tok = max(0, int(est_input_tokens))
            out_tok = max(0, int(est_output_tokens))
            cache_read = 0
            cache_creation = 0

        cost = compute_cost(model, in_tok, out_tok, cache_read, cache_creation)

        from app.models.llm_usage import LLMUsage

        row = LLMUsage(
            scenario=scenario,
            model=model or "",
            owner=owner,
            resident_id=resident_id,
            user_id=user_id,
            conversation_id=conversation_id,
            attempt_no=attempt_no,
            parse_ok=parse_ok,
            latency_ms=latency_ms,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            source=source,
            cost_usd=cost,
        )
        factory = _factory()
        async with factory() as session:
            session.add(row)
            await session.commit()
    except Exception as e:  # metering must never break the caller
        logger.debug("llm_usage metering skipped (%s): %s", scenario, e)


async def record_from_meter(
    meter: "Meter | None",
    *,
    model: str,
    owner: str,
    response=None,
    est_input_tokens: int = 0,
    est_output_tokens: int = 0,
    parse_ok: bool | None = None,
    latency_ms: int | None = None,
) -> None:
    """Record a row from a ``Meter`` context (no-op if meter is None)."""
    if meter is None:
        return
    await record_usage(
        meter.scenario,
        model=model,
        owner=owner,
        response=response,
        est_input_tokens=est_input_tokens,
        est_output_tokens=est_output_tokens,
        resident_id=meter.resident_id,
        user_id=meter.user_id,
        conversation_id=meter.conversation_id,
        attempt_no=meter.attempt_no,
        parse_ok=parse_ok,
        latency_ms=latency_ms,
    )
