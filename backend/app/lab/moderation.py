"""Task content-entry moderation gate (recovery plan Phase 4, gap #6).

Applied to a task's title/brief BEFORE any escrow hold. The gate is deliberately
two-layer:

* **objective / structural** — emptiness, length bounds, and control characters.
  These are policy-free safety checks that never depend on judgement.
* **operator blocklist** — ``settings.lab_task_blocklist`` (empty by default).
  The substantive content policy is operator-supplied, not invented here; this
  module only provides the enforcement point so a real moderator (a service or a
  curated list) can be dropped in without touching the task flow.

``moderate_task`` returns a STABLE rejection CODE (or ``None`` when clean). The
caller records only that code in content-free telemetry — never the raw title or
brief, which could itself carry the disallowed content.
"""
from __future__ import annotations

from app.config import settings

MAX_TITLE = 200
MAX_BRIEF = 16000
# Printable-ish: allow tab/newline/carriage-return, reject other C0 controls.
_ALLOWED_CONTROLS = {"\t", "\n", "\r"}


def _has_control_chars(s: str) -> bool:
    return any(ord(ch) < 32 and ch not in _ALLOWED_CONTROLS for ch in s)


def moderate_task(title: str, brief: str) -> str | None:
    """Return a stable rejection code if the task content is disallowed, else
    ``None``. Codes are content-free and safe to log/alert on."""
    title = title or ""
    brief = brief or ""

    if not title.strip():
        return "empty_title"
    if len(title) > MAX_TITLE:
        return "title_too_long"
    if len(brief) > MAX_BRIEF:
        return "brief_too_long"
    if _has_control_chars(title) or _has_control_chars(brief):
        return "control_chars"

    blocklist = [t for t in (getattr(settings, "lab_task_blocklist", []) or []) if t]
    if blocklist:
        haystack = f"{title}\n{brief}".lower()
        for term in blocklist:
            if str(term).lower() in haystack:
                return "blocked_term"
    return None
