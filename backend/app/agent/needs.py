"""Realism P1-10: three intrinsic needs (energy, satiety, social) that give
resident behavior a "because" instead of only "by the schedule". Pure rule, no
LLM. Needs live in ``resident.meta_json["needs"]`` and are metabolized each tick.
"""
from __future__ import annotations

from app.config import settings

NEEDS = ("energy", "satiety", "social")


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def get_needs(resident) -> dict:
    """Current needs dict, defaulting every need to the initial value."""
    meta = resident.meta_json or {}
    stored = meta.get("needs") if isinstance(meta, dict) else None
    init = settings.realism_needs_initial
    needs = {k: init for k in NEEDS}
    if isinstance(stored, dict):
        for k in NEEDS:
            if isinstance(stored.get(k), (int, float)):
                needs[k] = _clamp01(float(stored[k]))
    return needs


def write_needs(resident, needs: dict) -> None:
    """Persist needs into meta_json (copy-on-write, mirrors evolution.py idiom)."""
    meta = dict(resident.meta_json or {})
    meta["needs"] = {k: round(_clamp01(float(needs.get(k, settings.realism_needs_initial))), 4)
                     for k in NEEDS}
    resident.meta_json = meta


def _social_rate(sbti: dict | None) -> float:
    """Solitude social decay rate by extraversion (So1): introverts drain slower."""
    so1 = ((sbti or {}).get("dimensions") or {}).get("So1")
    if so1 == "L":
        return settings.realism_social_introvert
    if so1 == "H":
        return settings.realism_social_extravert
    return settings.realism_social_default


def metabolize(needs: dict, *, status: str, sbti: dict | None) -> dict:
    """Return the needs after one tick of metabolism.

    energy: sleeping → +sleep; walking → walking drain; else awake drain.
    satiety: constant drain. social: solitude drain by extraversion.
    """
    out = dict(needs)
    if status == "sleeping":
        out["energy"] = _clamp01(needs["energy"] + settings.realism_energy_sleep)
    elif status == "walking":
        out["energy"] = _clamp01(needs["energy"] + settings.realism_energy_walking)
    else:
        out["energy"] = _clamp01(needs["energy"] + settings.realism_energy_awake)
    out["satiety"] = _clamp01(needs["satiety"] + settings.realism_satiety_decay)
    out["social"] = _clamp01(needs["social"] + _social_rate(sbti))
    return out


def most_critical(needs: dict) -> str | None:
    """The lowest need if it is below the critical threshold, else None."""
    if not needs:
        return None
    key = min(NEEDS, key=lambda k: needs.get(k, 1.0))
    return key if needs.get(key, 1.0) < settings.realism_needs_critical else None
