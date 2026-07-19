"""P4 specialist workers (PRD §Agent Roles, §Delivery Plan P4).

The Principal Researcher (a resident) owns the goal and its grant. It may
delegate bounded work to short-lived *worker* agents — Scout, Builder, Verifier,
Archivist, World Cartographer — that are execution processes, not new town
residents. This module is the role layer on top of the (already-tested)
attenuation primitive in ``app.lab.grants``:

- each role has a fixed tool set → capability template; a delegated child grant
  carries only the intersection of the role template and the parent's own
  capabilities, so a worker can never escalate beyond its parent (V03);
- delegation is depth-1 only (``grants.issue_run_grant`` enforces depth, subset
  capabilities/egress/budgets, shared tenant/task/run, and expiry ≤ parent);
- at most ``settings.lab_budget_active_workers`` (3) workers run concurrently
  per run — the 4th is refused fail-closed WITHOUT terminating the run;
- every worker shares the run's single ``LabRunBudget`` row, so tool/model/
  egress/artifact spend is aggregated and survives resume for free;
- the Verifier is read-only + test execution (no ``fs.write``); the World
  Cartographer can ``world.propose`` but never ``world.apply``;
- the Archivist emits a privacy-safe (redacted) completion summary as a
  long-term memory candidate for the Principal Researcher.

This module never signs tokens or decides policy itself — it composes
``grants`` (attenuation) and ``budgets`` (concurrency gauge) and ``guard``
(redaction).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.config import settings
from app.lab import grants, guard
from app.lab.policy import TOOL_REGISTRY
from app.lab.protocol import GrantClaims
from app.models.lab_grant import LabCapabilityGrant


class WorkerRoleError(Exception):
    """Unknown worker role."""


class WorkerLimitError(Exception):
    """The run already has the maximum number of concurrent workers. Refusing a
    further delegation is fail-closed and does NOT terminate the run (distinct
    from a budget-dimension exhaustion, which does)."""


@dataclass(frozen=True)
class WorkerRole:
    name: str
    tools: frozenset[str]      # tool names this role may invoke (finer than capability)
    read_only: bool
    egress: bool = False       # inherit the parent's egress allowlist?

    @property
    def capabilities(self) -> set[str]:
        # Derive grant capabilities from the role's tools via the policy
        # registry — never hard-code capability tokens here, so a registry
        # change can't silently widen a role.
        return {TOOL_REGISTRY[t].capability for t in self.tools if t in TOOL_REGISTRY}


# PRD role→capability matrix. Verifier gets test-exec tools but NOT fs.write
# (read-only + test execution). World Cartographer proposes, never applies.
ROLE_TEMPLATES: dict[str, WorkerRole] = {
    "scout": WorkerRole("scout", frozenset({"web.search", "web.fetch", "browser.navigate"}), read_only=True, egress=True),
    "builder": WorkerRole("builder", frozenset({"code.run", "shell.exec", "fs.write"}), read_only=False),
    "verifier": WorkerRole("verifier", frozenset({"code.run", "shell.exec"}), read_only=True),
    "archivist": WorkerRole("archivist", frozenset(), read_only=True),
    "world_cartographer": WorkerRole("world_cartographer", frozenset({"world.propose"}), read_only=False),
}


def role_capabilities(role: str) -> set[str]:
    tmpl = ROLE_TEMPLATES.get(role)
    if tmpl is None:
        raise WorkerRoleError(f"unknown worker role '{role}'")
    return tmpl.capabilities


async def active_worker_count(db, run_id: str) -> int:
    """Live (non-revoked) delegated child grants on this run — the concurrency
    gauge. The Principal's own grant has ``parent_jti IS NULL`` and is excluded."""
    return int((await db.execute(
        select(func.count()).select_from(LabCapabilityGrant).where(
            LabCapabilityGrant.run_id == run_id,
            LabCapabilityGrant.parent_jti.isnot(None),
            LabCapabilityGrant.revoked_at.is_(None),
        )
    )).scalar_one())


async def delegate_worker(
    db, *, parent_claims: GrantClaims, role: str, agent_id: str,
    budgets: dict[str, int] | None = None, ttl_s: int | None = None,
) -> tuple[str, GrantClaims]:
    """Issue an attenuated, role-scoped child grant. Fail-closed at the
    concurrency cap (``WorkerLimitError``) and on any attenuation violation
    (``grants.GrantError``). Capabilities are the intersection of the role
    template and the parent's own grant — never an escalation."""
    tmpl = ROLE_TEMPLATES.get(role)
    if tmpl is None:
        raise WorkerRoleError(f"unknown worker role '{role}'")

    # Concurrency cap BEFORE issuing — count-based so hitting it refuses the new
    # worker without tripping a budget-dimension exhaustion (which would kill
    # the run). limit 0 == unlimited.
    cap = settings.lab_budget_active_workers
    if cap and await active_worker_count(db, parent_claims.run_id) >= cap:
        raise WorkerLimitError(f"active worker cap {cap} reached for run {parent_claims.run_id}")

    # Capability intersection: role wants these, but only what the parent holds.
    caps = sorted(tmpl.capabilities & set(parent_claims.capabilities))
    # Defensive: a worker may never carry world.apply / financial / secrets.
    assert not ({"world_apply", "financial", "secrets"} & set(caps)), "worker cannot hold a hard-deny capability"

    child_egress = list(parent_claims.egress) if tmpl.egress else []
    child_budgets = dict(budgets) if budgets is not None else dict(parent_claims.budgets)

    return await grants.issue_run_grant(
        db,
        tenant_id=parent_claims.tenant_id, task_id=parent_claims.task_id,
        run_id=parent_claims.run_id, agent_id=agent_id,
        capabilities=caps, egress=child_egress, budgets=child_budgets,
        parent=parent_claims, ttl_s=ttl_s,
    )


async def finish_worker(db, *, jti: str) -> None:
    """A worker finished or was cancelled — revoke its grant so its slot frees
    (the concurrency gauge counts only non-revoked child grants) and no further
    tool call under that token can be admitted."""
    await grants.revoke_grant(db, jti)


async def archivist_summary(
    db, *, run_id: str, principal_id: str,
    accepted_artifacts: list[dict], redacted_events: list[str],
    importance: float = 0.6,
) -> str:
    """Privacy-safe completion summary + long-term memory candidate for the
    Principal Researcher (PRD: only accepted, redacted summaries and artifact
    references reach resident memory — never raw task data). Every string is run
    through ``guard.redact_text`` before it can be persisted or returned."""
    art_lines = []
    for a in accepted_artifacts:
        title = guard.redact_text(str(a.get("title") or "")) or ""
        kind = str(a.get("kind") or "artifact")
        provenance = str(a.get("provenance") or "runtime")
        digest = str(a.get("sha256") or "")[:12]
        art_lines.append(f"- {kind}“{title}”（{provenance}{'/' + digest if digest else ''}）")

    event_lines = [guard.redact_text(e) or "" for e in redacted_events]
    parts = [f"实验 run {run_id} 完成小结（已脱敏）："]
    if art_lines:
        parts.append("已验收产物：\n" + "\n".join(art_lines))
    if event_lines:
        parts.append("过程要点：\n" + "\n".join(f"- {e}" for e in event_lines))
    summary = "\n".join(parts)

    from app.memory.service import MemoryService
    await MemoryService(db).add_memory(
        principal_id, "event", summary, importance=importance, source="lab_archivist",
    )
    return summary
