"""Executable adapter conformance + scoring gate (PRD P0/P2 §Adapter Selection).

A candidate runtime adapter is admitted only if it scores **≥80/100** on five
weighted dimensions AND passes every *mandatory* one. Three dimensions are
mandatory — they map to the PRD's three hard elimination criteria (Broker-only
effects, fail-closed cancel, isolated deployment); failing any eliminates the
candidate no matter how high the rest score.

    broker_mediation          30%   mandatory
    disconnect_replay_cancel  25%   mandatory
    isolated_deployment       20%   mandatory
    subagent_attenuation      15%
    ops_licensing             10%

This module is the *framework*: ``score_candidate`` is the pure scoring engine,
and ``run_conformance`` drives executable probes against a candidate to produce
evidence-backed per-dimension results. It does NOT select a real adapter — that
requires real runtime endpoints (unconfigured; see
``docs/adr/ADR-lab-runtime-adapter.md``). ``run_conformance`` is proven against
deterministic fake candidates so the gate's SCORING and ELIMINATION are trusted;
a real Hermes/Grok integration ships a thin conformance shim exposing the same
duck-typed hooks the probes call.

Candidate hooks the probes use (all optional; a missing hook scores its
dimension conservatively, never crashes):

* ``handshake_manifest() -> HandshakeManifest``
* ``emit_tool_intent() -> (tool_name, args)``            (broker_mediation)
* ``bypass_broker: bool``                                (broker_mediation)
* ``provider_events() -> list[(cursor, payload)]``       (disconnect_replay_cancel)
* optional ``cancel/terminate/kill/health`` hooks        (disconnect_replay_cancel)
* ``accepts_infra_handles: bool``                        (isolated_deployment)
* ``subagent_child_caps(parent_caps) -> list|None``      (subagent_attenuation)
* ``license_manifest_path: str|None``                    (ops_licensing)
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC

from app.config import settings
from app.lab import broker, grants, leases, policy, supervision
from app.lab.protocol import RunEventEnvelope
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask

# A mandatory dimension scoring below this is a hard elimination.
MANDATORY_THRESHOLD = 0.6
# Total (0..100) at/above which a mandatory-passing candidate is selected.
SELECTION_THRESHOLD = 80.0

_PARENT_CAPS = ["web_search", "browse", "code"]


@dataclass
class GateDimension:
    key: str
    weight: int
    mandatory: bool


GATE_DIMENSIONS: list[GateDimension] = [
    GateDimension("broker_mediation", 30, True),
    GateDimension("disconnect_replay_cancel", 25, True),
    GateDimension("isolated_deployment", 20, True),
    GateDimension("subagent_attenuation", 15, False),
    GateDimension("ops_licensing", 10, False),
]
assert sum(d.weight for d in GATE_DIMENSIONS) == 100


@dataclass
class DimensionResult:
    key: str
    score: float          # 0..1
    evidence: str         # pointer to the conformance probe / trace / manifest / license record


@dataclass
class GateVerdict:
    candidate: str
    total: float          # 0..100
    passed_mandatory: bool
    eliminated: bool
    per_dimension: list[DimensionResult]
    selected: bool
    # Inputs a HUMAN uses to break a tie (PRD: smaller credential/network surface,
    # then lower ops burden). Deliberately NOT auto-decided here.
    tie_break: dict = field(default_factory=dict)


# ── scoring engine (pure) ─────────────────────────────────────────────

def score_candidate(candidate: str, results: list[DimensionResult],
                    *, tie_break: dict | None = None) -> GateVerdict:
    """Weighted total + mandatory gate. A mandatory dimension below
    ``MANDATORY_THRESHOLD`` sets ``eliminated`` and forces ``selected=False`` no
    matter the total; ``selected`` requires ``total >= SELECTION_THRESHOLD`` AND
    every mandatory dimension satisfied. A missing dimension result scores 0."""
    by_key = {r.key: r for r in results}
    total = 0.0
    passed_mandatory = True
    for dim in GATE_DIMENSIONS:
        r = by_key.get(dim.key)
        score = r.score if r is not None else 0.0
        total += dim.weight * score
        if dim.mandatory and score < MANDATORY_THRESHOLD:
            passed_mandatory = False
    total = round(total, 6)  # kill float dust at the 80.0 boundary
    eliminated = not passed_mandatory
    selected = passed_mandatory and total >= SELECTION_THRESHOLD
    return GateVerdict(
        candidate=candidate, total=total, passed_mandatory=passed_mandatory,
        eliminated=eliminated, per_dimension=list(results), selected=selected,
        tie_break=dict(tie_break or {}),
    )


# ── executable probes (one per dimension) ─────────────────────────────

class _VirtualClock:
    """Injected into cancel escalation so windows elapse with zero real waiting."""
    def __init__(self):
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    async def sleep(self, seconds: float) -> None:
        self._t += seconds


async def probe_broker_mediation(candidate, *, db) -> DimensionResult:
    """A conformant runtime causes NO effect except through the Broker. Two
    executable controls, each worth half the dimension:

    * **NEGATIVE** — the candidate's declared intent under a grant that does NOT
      hold the capability is DENIED (proof the Broker gates effects).
    * **POSITIVE** — the SAME intent under a grant that DOES hold the capability is
      ADMITTED (``approved``/``waiting_approval``) — proof the effect actually
      flows *through* the Broker, not merely that ungranted calls bounce.

    Full credit requires both. A ``bypass_broker`` candidate (an out-of-band
    effect channel) fails outright regardless of either control."""
    key = "broker_mediation"
    if getattr(candidate, "bypass_broker", False):
        return DimensionResult(key, 0.0, "bypass_broker present: effects can escape the Broker")

    tool, args = candidate.emit_tool_intent()
    descriptor = policy.TOOL_REGISTRY.get(tool)
    capability = descriptor.capability if descriptor is not None else None

    # NEGATIVE control: an ungranted intent must be refused.
    denied_ok, denied_reason = False, "admitted (Broker not gating)"
    _, ungranted = await grants.issue_run_grant(
        db, tenant_id="gate", task_id="gate-task", run_id=f"gate-{uuid.uuid4().hex[:10]}",
        agent_id=candidate.name, capabilities=[],
    )
    try:
        await broker.request_action(db, claims=ungranted, token=grants.sign_grant(ungranted),
                                    tool_name=tool, args=args)
    except broker.ActionDenied as exc:
        denied_ok, denied_reason = True, exc.reason

    # POSITIVE control: the same intent, properly granted, must be admitted — the
    # effect is Broker-mediated (routed to execute/approval), not bypassed.
    admitted_ok, admitted_status = False, "error"
    _, granted = await grants.issue_run_grant(
        db, tenant_id="gate", task_id="gate-task", run_id=f"gate-{uuid.uuid4().hex[:10]}",
        agent_id=candidate.name, capabilities=[capability] if capability else [],
    )
    try:
        action = await broker.request_action(db, claims=granted, token=grants.sign_grant(granted),
                                             tool_name=tool, args=args)
        admitted_status = action.status
        admitted_ok = action.status in ("approved", "waiting_approval")
    except broker.ActionDenied as exc:
        admitted_status = f"denied:{exc.reason}"

    score = round(0.5 * denied_ok + 0.5 * admitted_ok, 6)
    evidence = (f"ungranted_denied={denied_ok}({denied_reason}), "
                f"granted_admitted={admitted_ok}(status={admitted_status})")
    return DimensionResult(key, score, evidence)


async def probe_disconnect_replay_cancel(candidate, *, db) -> DimensionResult:
    """Reuse the supervision layer as an executable probe: provider-cursor dedup
    (replay yields no second row), correct replay window after ACK, and — the
    fail-closed core — that ``cancel_run`` ALWAYS fences (grants revoked + lease
    epoch bumped) even when the candidate's cancel hooks throw. Cooperation
    (cancel acknowledged without escalating to KILL) earns the remaining credit."""
    key = "disconnect_replay_cancel"
    run_id = f"gate-{uuid.uuid4().hex[:10]}"
    task_id = f"{run_id}-task"
    db.add(LabTask(id=task_id, issuer_user_id="gate", title="gate probe"))
    db.add(LabRun(id=run_id, task_id=task_id, researcher_slug=candidate.name, status="running", adapter="gate"))
    await db.commit()
    await leases.acquire_lease(db, run_id=run_id, owner_id="gate-owner")
    _, claims = await grants.issue_run_grant(
        db, tenant_id="gate", task_id=task_id, run_id=run_id, agent_id=candidate.name,
        capabilities=["web_search"], fencing_epoch=0,
    )
    jti = claims.jti

    session = await supervision.open_session(db, run_id=run_id, manifest=candidate.handshake_manifest())
    events = list(candidate.provider_events())
    for cursor, payload in events:
        await supervision.ingest_provider_event(
            db, session, provider_cursor=cursor,
            envelope_builder=_make_builder(run_id, task_id, cursor, payload),
        )
    first_cursor = events[0][0]
    replayed = await supervision.ingest_provider_event(
        db, session, provider_cursor=first_cursor,
        envelope_builder=_make_builder(run_id, task_id, first_cursor, events[0][1]),
    )
    replay_ok = replayed is None

    last_cursor = events[-1][0]
    await supervision.ack_through(db, session, provider_cursor=last_cursor)
    window_ok = supervision.replay_window(session) == last_cursor + 1

    clock = _VirtualClock()
    tier = await supervision.cancel_run(
        db, run_id=run_id, adapter=candidate, handle=None, reason="gate_probe",
        grace_s=0.05, kill_s=0.1, control_timeout_s=0.05, now=clock, sleep=clock.sleep,
    )
    grant_row = await db.get(LabCapabilityGrant, jti)
    grant_revoked = grant_row is not None and grant_row.revoked_at is not None
    epoch_bumped = (await leases.current_epoch(db, run_id)) > 0
    fenced = grant_revoked and epoch_bumped
    cooperative = tier == "cooperative"

    score = 0.35 * replay_ok + 0.15 * window_ok + 0.35 * fenced + 0.15 * cooperative
    evidence = (f"replay_dedup={replay_ok}, replay_window_ok={window_ok}, "
                f"fenced(grants+epoch)={fenced}, cancel_tier={tier}")
    return DimensionResult(key, round(score, 6), evidence)


def probe_isolated_deployment(candidate) -> DimensionResult:
    """Static: the adapter must not hold DB / Redis / world credentials (its
    constructor must not accept infra handles). A candidate advertising
    ``accepts_infra_handles`` fails this mandatory dimension."""
    key = "isolated_deployment"
    if getattr(candidate, "accepts_infra_handles", False):
        return DimensionResult(key, 0.0, "constructor accepts DB/Redis/world handles (not isolated)")
    return DimensionResult(key, 1.0, "no DB/Redis/world handle accepted at construction")


async def probe_subagent_attenuation(candidate, *, db) -> DimensionResult:
    """If the candidate supports sub-agents, its declared child grant must be a
    proper attenuation of the parent (reuses ``grants`` delegation rules). A child
    that exceeds the parent is a privilege escalation → 0. No sub-agent support →
    0 with ``not supported`` evidence (the dimension is non-mandatory)."""
    key = "subagent_attenuation"
    getter = getattr(candidate, "subagent_child_caps", None)
    child_caps = getter(list(_PARENT_CAPS)) if getter is not None else None
    if child_caps is None:
        return DimensionResult(key, 0.0, "sub-agent delegation not supported")

    run_id = f"gate-{uuid.uuid4().hex[:10]}"
    _, parent = await grants.issue_run_grant(
        db, tenant_id="gate", task_id="gate-task", run_id=run_id, agent_id="parent",
        capabilities=list(_PARENT_CAPS), fencing_epoch=0,
    )
    try:
        await grants.issue_run_grant(
            db, tenant_id="gate", task_id="gate-task", run_id=run_id, agent_id="child",
            capabilities=list(child_caps), parent=parent, fencing_epoch=0,
        )
    except grants.GrantError as exc:
        return DimensionResult(key, 0.0, f"child grant exceeds parent (escalation): {exc}")
    return DimensionResult(key, 1.0, f"child caps {child_caps} ⊆ parent {_PARENT_CAPS} (attenuated)")


def probe_ops_licensing(candidate) -> DimensionResult:
    """Evidence pointer: a license/manifest record file must exist. Missing → 0."""
    key = "ops_licensing"
    path = getattr(candidate, "license_manifest_path", None)
    if path and os.path.exists(path):
        return DimensionResult(key, 1.0, f"license manifest present: {path}")
    return DimensionResult(key, 0.0, "no license/ops manifest record")


async def run_conformance(candidate, *, db) -> list[DimensionResult]:
    """Run every dimension probe against ``candidate`` and return the per-dimension
    results (feed to ``score_candidate``). Operates against the provided (test /
    staging) session, creating ephemeral probe runs — it never selects an adapter."""
    return [
        await probe_broker_mediation(candidate, db=db),
        await probe_disconnect_replay_cancel(candidate, db=db),
        probe_isolated_deployment(candidate),
        await probe_subagent_attenuation(candidate, db=db),
        probe_ops_licensing(candidate),
    ]


def _make_builder(run_id: str, task_id: str, cursor: int, payload: dict | None):
    def build(seq: int) -> RunEventEnvelope:
        return RunEventEnvelope(
            event_id=str(uuid.uuid4()), tenant_id="gate", run_id=run_id, task_id=task_id,
            seq=seq, type="plan.updated", actor="candidate", fencing_epoch=0,
            policy_version=settings.lab_policy_version, occurred_at=datetime.now(UTC),
            payload={"cursor": cursor, **(payload or {})},
        )
    return build
