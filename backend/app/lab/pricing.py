"""Task pricing — the minimal capability-profile that turns requested scopes into
an effective USD budget, and that budget into a minimum SC reward (recovery plan
Phase 4, gap #6).

``lab_sc_per_usd`` bridges the two economies: the SC a player pays and the USD
compute a task authorizes. The minimum reward is ``ceil(effective_budget_usd *
lab_sc_per_usd)`` so a task can never be funded below the compute it buys. The
effective budget is the sum of the requested scopes' per-scope budget, capped at
the run's flat ceiling (``lab_default_budget_usd``) — a fuller named-profile
system (budget ceilings, allowed deliverable kinds, UI metadata) is a later
refinement, but this already makes the floor scope-aware rather than flat.
"""
from __future__ import annotations

import math

from app.config import settings

# Per-scope effective USD budget contribution — the compute a scope authorizes.
# An unknown scope falls back to the cheapest tier so it can never price to 0.
_SCOPE_BUDGET_USD = {
    "web_search": 0.10,
    "http": 0.15,
    "browse": 0.20,
    "code": 0.20,
}
_DEFAULT_SCOPE_BUDGET_USD = 0.10


def effective_budget_usd(scopes: list[str] | None) -> float:
    """The USD compute a task's scopes authorize, capped at the run ceiling. An
    empty scope list still floors at one default tier so the price is never 0."""
    scopes = list(scopes or [])
    raw = sum(_SCOPE_BUDGET_USD.get(s, _DEFAULT_SCOPE_BUDGET_USD) for s in scopes) or _DEFAULT_SCOPE_BUDGET_USD
    return min(raw, float(settings.lab_default_budget_usd))


def minimum_reward_sc(scopes: list[str] | None) -> int:
    """The minimum SC reward a task with these scopes may be funded at."""
    return max(1, math.ceil(effective_budget_usd(scopes) * settings.lab_sc_per_usd))
