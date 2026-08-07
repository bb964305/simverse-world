"""Capability guardrails for real sandbox runs (spec §5.3, §11).

Pure, dependency-free policy: which tools a scope grants, which actions must
pause for human approval, which are hard-denied (all financial), a budget
breaker, and payload/summary redaction before anything is persisted or streamed.
The runner enforces these around the adapter; nothing here does I/O.
"""
from __future__ import annotations

import re
from typing import Any

# scope → allowed tool-name prefixes (a tool is permitted iff its prefix maps to
# a granted scope). Real adapters must only expose tools within granted scopes;
# this is the belt-and-suspenders backstop.
SCOPE_TOOL_PREFIXES: dict[str, tuple[str, ...]] = {
    "web_search": ("web.search",),
    "browse": ("browser.", "web.open", "page."),
    "code": ("code.", "python.", "shell.", "fs."),
    "http": ("http.", "web.fetch"),
}

# Sensitive actions: pause the run for human approval (spec §5.3).
SENSITIVE_PATTERNS = (
    "login", "signin", "sign_in", "auth", "submit", "post", "publish",
    "send", "email", "upload", "delete", "purchase", "checkout", "pay",
)

# Financial actions: hard-denied, never executed on the user's behalf (global
# financial red line — no placing orders / transfers / payments).
FINANCIAL_PATTERNS = (
    "pay", "payment", "checkout", "purchase", "order", "transfer", "wallet",
    "bank", "card", "wire", "withdraw", "deposit", "invoice",
)

# Secret / PII redaction. Applied to every summary + payload string before it is
# written to lab_run_steps or streamed over WS.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._\-]{12,}|AKIA[0-9A-Z]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{32,}(?![A-Fa-f0-9])|"
    r"(?=[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_-]))"
    r"(?=[A-Za-z0-9+/_-]*[A-Z])(?=[A-Za-z0-9+/_-]*[a-z])"
    r"(?=[A-Za-z0-9+/_-]*[0-9])[A-Za-z0-9+/_-]{40,}={0,2}|"
    r"(?i:api[_-]?key|token|password|secret)[\"']?\s*[:=]\s*[\"']?\S+)"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_REDACTED = "[REDACTED]"


class ScopeViolation(Exception):
    """A tool call fell outside the run's granted scopes."""


def tool_scope(tool: str) -> str | None:
    """Return the scope a tool requires, or None if it maps to no known scope."""
    for scope, prefixes in SCOPE_TOOL_PREFIXES.items():
        if any(tool.startswith(p) for p in prefixes):
            return scope
    return None


def is_tool_allowed(tool: str | None, scopes: list[str]) -> bool:
    """Whitelist check: a tool is allowed only if its required scope was granted.
    Unknown/None tools (pure think/message steps) are always allowed."""
    if not tool:
        return True
    required = tool_scope(tool)
    if required is None:
        return False  # unrecognized tool → deny by default
    return required in (scopes or [])


def classify_action(tool: str | None, payload: dict[str, Any] | None = None) -> str:
    """Return "allow" | "approval" | "deny" for a tool call.

    Financial actions are always denied; other sensitive actions (login/submit/
    publish/send/…) require human approval; everything else runs.
    """
    if not tool:
        return "allow"
    name = tool.lower()
    if any(p in name for p in FINANCIAL_PATTERNS):
        return "deny"
    if any(p in name for p in SENSITIVE_PATTERNS):
        return "approval"
    return "allow"


def check_budget(cost_usd_cents: int, budget_usd_cents: int) -> bool:
    """True if still within budget. A 0 budget means "no cap"."""
    if not budget_usd_cents:
        return True
    return cost_usd_cents <= budget_usd_cents


def redact_text(s: str | None) -> str | None:
    if not s:
        return s
    s = _SECRET_RE.sub(_REDACTED, s)
    s = _EMAIL_RE.sub(_REDACTED, s)
    return s


def redact_payload(payload: Any) -> Any:
    """Recursively redact secrets/PII from a JSON-ish payload."""
    if isinstance(payload, str):
        return redact_text(payload)
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if isinstance(k, str) and any(t in k.lower() for t in ("key", "token", "password", "secret", "authorization")):
                out[k] = _REDACTED
            else:
                out[k] = redact_payload(v)
        return out
    if isinstance(payload, list):
        return [redact_payload(v) for v in payload]
    return payload
