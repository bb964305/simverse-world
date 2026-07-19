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

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC

from sqlalchemy import func, select

from app.config import settings
from app.lab import grants, guard
from app.lab.policy import TOOL_REGISTRY
from app.lab.protocol import GrantClaims
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_worker_attempt import LabWorkerAttempt
from app.redis_client import get_redis


@dataclass(frozen=True)
class WorkerResult:
    """The joined outcome of a bounded Mock child execution — content-free: a
    role, a status, a SERVER-computed result digest (never the child's
    self-report, so a spoofed 'success' cannot be trusted), and a Verifier
    verdict the parent gates Builder artifacts on."""
    role: str
    agent_id: str
    status: str            # succeeded | failed
    result_digest: str
    verdict: str | None = None  # verifier only: pass | fail


def _slot_key(run_id: str) -> str:
    return f"sv:lab:workers:{run_id}"


async def reserve_worker_slot(run_id: str, cap: int) -> bool:
    """Atomically reserve one concurrent-worker slot for a run via a Redis INCR
    (recovery plan Phase 6 — replaces the count-then-insert admission). A
    non-positive cap is unlimited. The (cap+1)th reserver sees an over-limit value,
    DECRs back, and is refused, so concurrent delegates never admit worker
    (cap+1). Fail-CLOSED on a Redis fault so a broken gate can't over-admit."""
    if not cap or cap <= 0:
        return True
    try:
        n = int(await get_redis().incr(_slot_key(run_id)))
    except Exception:
        return False
    if n > cap:
        try:
            await get_redis().decr(_slot_key(run_id))
        except Exception:
            pass
        return False
    return True


async def release_worker_slot(run_id: str) -> None:
    try:
        await get_redis().decr(_slot_key(run_id))
    except Exception:
        pass


async def reconcile_worker_slots(db, run_id: str) -> int:
    """Re-sync the per-run worker-slot counter to the DB's count of still-running
    attempts — heals a slot leaked by a crashed supervisor."""
    live = int((await db.execute(
        select(func.count()).select_from(LabWorkerAttempt).where(
            LabWorkerAttempt.run_id == run_id, LabWorkerAttempt.status == "running")
    )).scalar_one())
    try:
        await get_redis().set(_slot_key(run_id), live)
    except Exception:
        pass
    return live


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
    sub_goal: str = "", parent_action_id: str | None = None,
    budgets: dict[str, int] | None = None, ttl_s: int | None = None,
) -> tuple[str, GrantClaims]:
    """Issue an attenuated, role-scoped child grant + a durable worker-attempt
    record, after ATOMICALLY reserving a concurrency slot (recovery plan Phase 6 —
    replaces count-then-insert). Fail-closed at the concurrency cap
    (``WorkerLimitError``, non-fatal to the run) and on any attenuation violation
    (``grants.GrantError``). Capabilities are the intersection of the role
    template and the parent's own grant — never an escalation. Returns
    ``(token, child_claims)`` unchanged; the attempt is queryable by run/jti."""
    tmpl = ROLE_TEMPLATES.get(role)
    if tmpl is None:
        raise WorkerRoleError(f"unknown worker role '{role}'")

    # Atomic slot reservation BEFORE issuing — an over-cap reserve refuses the
    # worker without tripping a budget-dimension exhaustion (which would kill the
    # run). limit 0 == unlimited.
    cap = settings.lab_budget_active_workers
    if not await reserve_worker_slot(parent_claims.run_id, cap):
        raise WorkerLimitError(f"active worker cap {cap} reached for run {parent_claims.run_id}")

    try:
        # Capability intersection: role wants these, but only what the parent holds.
        caps = sorted(tmpl.capabilities & set(parent_claims.capabilities))
        # Defensive: a worker may never carry world.apply / financial / secrets.
        assert not ({"world_apply", "financial", "secrets"} & set(caps)), "worker cannot hold a hard-deny capability"

        child_egress = list(parent_claims.egress) if tmpl.egress else []
        child_budgets = dict(budgets) if budgets is not None else dict(parent_claims.budgets)

        token, child = await grants.issue_run_grant(
            db,
            tenant_id=parent_claims.tenant_id, task_id=parent_claims.task_id,
            run_id=parent_claims.run_id, agent_id=agent_id,
            capabilities=caps, egress=child_egress, budgets=child_budgets,
            parent=parent_claims, ttl_s=ttl_s,
        )
    except Exception:
        # Failed to issue — give the reserved slot back so it isn't leaked.
        await release_worker_slot(parent_claims.run_id)
        raise

    db.add(LabWorkerAttempt(
        run_id=parent_claims.run_id, parent_action_id=parent_action_id, role=role,
        agent_id=agent_id, grant_jti=child.jti,
        child_runtime_id=f"mock-child-{uuid.uuid4().hex[:8]}",
        sub_goal_hash=hashlib.sha256((sub_goal or "").encode("utf-8")).hexdigest(),
        status="running",
    ))
    # Commit the durable attempt so a supervisor restart / cross-session
    # finish_worker can find it (the slot release keys off its run_id). The grant
    # is already committed by issue_run_grant; this isolates the attempt row.
    await db.commit()
    return token, child


async def execute_worker_on_mock(
    db, *, child_claims: GrantClaims, role: str, sub_goal: str = "",
) -> WorkerResult:
    """Run a bounded child sub-goal on Mock under the child's attenuated grant and
    return a joined, content-free result (recovery plan Phase 6). Synthetic — no
    real I/O — so it is safe and deterministic: it produces a redacted terminal
    summary and a SERVER-computed ``result_digest`` (never the child's
    self-report, so a spoofed 'success' payload cannot be trusted), and a Verifier
    yields a verdict the parent gates Builder artifacts on. When a REAL adapter
    drives a child, its tool intents route through the same Broker under exactly
    this attenuated grant; the Mock child needs no external call to prove the
    supervised depth-1 lifecycle. Never raises for a child failure — a failed
    child is non-fatal to the parent run."""
    tmpl = ROLE_TEMPLATES.get(role)
    if tmpl is None:
        return WorkerResult(role=role, agent_id=getattr(child_claims, "agent_id", "?"),
                            status="failed", result_digest="", verdict=None)
    # The child must actually hold some capability for a tool-bearing role, else
    # it did no bounded work (an empty intersection = attenuated to nothing).
    has_caps = bool(tmpl.capabilities & set(child_claims.capabilities)) or not tmpl.tools
    status = "succeeded" if has_caps else "failed"
    summary = guard.redact_text(f"worker {role} 完成子目标") or role
    result_digest = hashlib.sha256(
        f"{role}|{child_claims.agent_id}|{child_claims.jti}|{summary}".encode("utf-8")
    ).hexdigest()
    verdict = ("pass" if status == "succeeded" else "fail") if role == "verifier" else None
    return WorkerResult(role=role, agent_id=child_claims.agent_id, status=status,
                        result_digest=result_digest, verdict=verdict)


async def finish_worker(db, *, jti: str, status: str = "succeeded",
                        result_digest: str | None = None) -> None:
    """A worker finished, failed, or was cancelled — drive its full terminal
    lifecycle: mark the durable attempt terminal (with a content-free result
    digest + cleanup evidence), revoke its grant so no further tool call under
    that token can be admitted, and RELEASE its concurrency slot. Idempotent: a
    second call on an already-terminal attempt is a no-op that never
    double-releases the slot (recovery plan Phase 6)."""
    attempt = (await db.execute(
        select(LabWorkerAttempt).where(LabWorkerAttempt.grant_jti == jti)
    )).scalars().first()
    await grants.revoke_grant(db, jti)
    if attempt is None:
        return
    if attempt.status != "running":
        return  # already finalized — do not release a slot twice
    attempt.status = status if status in ("succeeded", "failed", "cancelled") else "succeeded"
    attempt.result_digest = result_digest
    attempt.ended_at = datetime.now(UTC)
    attempt.cleanup_evidence = {"grant_revoked": True, "slot_released": True}
    await db.commit()
    await release_worker_slot(attempt.run_id)


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
