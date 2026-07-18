"""Lab Agent v1 Policy Engine — tool registry + fail-closed decision function
(PRD §Capability and Approval Model, V01-V03/V14).

Pure decision logic only: no I/O, no grant verification (that lives in
``app.lab.grants``). The Tool Broker (T3) composes ``grants.check_grant_active``
and ``decide`` on a verified ``GrantClaims``. Only registered tools exist;
anything else is denied by default.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.lab import guard
from app.lab.protocol import GrantClaims, ToolDescriptor

RISK_ALLOW = ("R0", "R1")      # runs directly (R1 confined to the sandbox; v1 Mock stage treats it like allow)
RISK_ASK = ("R2",)             # requires task-owner approval
RISK_GOVERN = ("R3",)          # routed through the World Governor flow, never a player approval row
RISK_DENY = ("R4",)


def _tool(name: str, capability: str, risk_class: str, read_only: bool, side_effect: bool) -> ToolDescriptor:
    return ToolDescriptor(
        name=name, capability=capability, risk_class=risk_class,
        read_only=read_only, side_effect=side_effect,
    )


TOOL_REGISTRY: dict[str, ToolDescriptor] = {
    "web.search": _tool("web.search", "web_search", "R0", True, False),
    "web.fetch": _tool("web.fetch", "http", "R1", True, False),
    "browser.navigate": _tool("browser.navigate", "browse", "R1", True, False),
    "code.run": _tool("code.run", "code", "R1", False, True),
    "shell.exec": _tool("shell.exec", "code", "R1", False, True),
    "fs.write": _tool("fs.write", "code", "R1", False, True),
    "http.request": _tool("http.request", "http", "R2", False, True),   # new-domain egress: v1 defaults to ask
    "world.propose": _tool("world.propose", "world_propose", "R3", False, True),
    "world.apply": _tool("world.apply", "world_apply", "R4", False, True),   # always denied
    "payment.charge": _tool("payment.charge", "financial", "R4", False, True),
    "wallet.transfer": _tool("wallet.transfer", "financial", "R4", False, True),
    "credential.store": _tool("credential.store", "secrets", "R4", False, True),
}


@dataclass(frozen=True)
class PolicyDecision:
    effect: str          # "allow" | "ask" | "deny" | "govern"
    risk_class: str      # R0..R4; unknown tool -> "R4"
    reason: str          # "ok" | "unknown_tool" | "capability_not_granted" | "hard_deny" | "unregistered_financial"
    tool: ToolDescriptor | None
    requires_approval: bool     # effect == "ask"
    hard_deny: bool             # effect == "deny" and never approvable (R4 / unregistered)


def decide(tool_name: str, args: dict, claims: GrantClaims) -> PolicyDecision:
    """deny > ask > allow, in this order. ``args`` is accepted for the
    Broker's future per-call checks (e.g. matching an egress target); this
    v1 slice never branches on it. Deliberately excludes any reasoning-mode
    parameter — V14 requires the decision to be identical regardless of how
    the agent reasoned about the call."""
    tool = TOOL_REGISTRY.get(tool_name)

    # 1. Unregistered tool: fail closed. A financial-looking name gets a more
    # specific reason, but the effect is the same hard deny either way.
    if tool is None:
        name = tool_name.lower()
        reason = "unregistered_financial" if any(p in name for p in guard.FINANCIAL_PATTERNS) else "unknown_tool"
        return PolicyDecision(
            effect="deny", risk_class="R4", reason=reason, tool=None,
            requires_approval=False, hard_deny=True,
        )

    # 2. Registered but R4: hard deny regardless of any capability the grant holds.
    if tool.risk_class in RISK_DENY:
        return PolicyDecision(
            effect="deny", risk_class=tool.risk_class, reason="hard_deny", tool=tool,
            requires_approval=False, hard_deny=True,
        )

    # 3. Capability gate: approval cannot substitute for a missing grant.
    if tool.capability not in claims.capabilities:
        return PolicyDecision(
            effect="deny", risk_class=tool.risk_class, reason="capability_not_granted", tool=tool,
            requires_approval=False, hard_deny=False,
        )

    # 4. R3: World Governor flow, not a player approval.
    if tool.risk_class in RISK_GOVERN:
        return PolicyDecision(
            effect="govern", risk_class=tool.risk_class, reason="ok", tool=tool,
            requires_approval=False, hard_deny=False,
        )

    # 5. R2: task-owner approval required.
    if tool.risk_class in RISK_ASK:
        return PolicyDecision(
            effect="ask", risk_class=tool.risk_class, reason="ok", tool=tool,
            requires_approval=True, hard_deny=False,
        )

    # 6. R0/R1: runs directly.
    return PolicyDecision(
        effect="allow", risk_class=tool.risk_class, reason="ok", tool=tool,
        requires_approval=False, hard_deny=False,
    )
