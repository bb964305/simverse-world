"""LabTask state machine + escrow settlement (spec §4.1, §6).

    draft → funded → assigned → running → review
          → completed | rejected | failed | expired | cancelled

Borrows Commission's optimistic patterns. Money flows through coin_service
holds; settlement splits reward into the researcher's creator share + treasury,
and the platform fee into the sink. Open-recruitment tasks are auto-dispatched
by backend rules here (not the tick), else the open pool would never drain.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, UTC

from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.events.bus import on, emit
from app.lab import transitions
from app.models.lab_artifact import LabArtifact
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.resident import Resident
from app.models.user import User
from app.services import coin_service
from app.services import lab_terminalization_service

logger = logging.getLogger(__name__)

ALLOWED_SCOPES = ["web_search", "browse", "code", "http"]
ACTIVE_RUN_STATES = ("queued", "running", "needs_approval")


class LabTaskError(Exception):
    """Publish/accept/reject/cancel conflicts (router maps to 400/402/403/409)."""


def supported_scopes_for_adapter(adapter: str | None = None) -> list[str]:
    """Return only capabilities the configured runtime can actually execute."""
    selected = settings.lab_adapter if adapter is None else adapter
    if selected == "codex":
        # The ARM Codex runtime is deliberately no-egress and requires code.
        return ["code"]
    return list(ALLOWED_SCOPES)


def _require_execution_consumer(protocol_version: int | None = None) -> int:
    """Freeze and validate the execution protocol before any domain mutation."""
    from app.lab import runner

    selected = (
        runner.configured_protocol_version()
        if protocol_version is None
        else protocol_version
    )
    try:
        runner.require_protocol_handler(selected)
    except runner.ProtocolConsumerUnavailable as exc:
        raise LabTaskError(str(exc)) from exc
    if settings.lab_adapter == "codex" and settings.lab_terminalizer_v2_enabled:
        raise LabTaskError(
            "Codex requires the cost-aware v1 escrow terminalizer; "
            "the PostgreSQL v2 refund kernel is not yet cost-aware"
        )
    return selected


def _require_v2_tenant_admitted(protocol_version: int, tenant_id: str) -> None:
    """Keep canary admission closed unless the tenant is explicitly listed."""
    if protocol_version != 2 or settings.lab_global_admission_enabled:
        return
    allowlist = {
        value.strip()
        for value in settings.lab_runtime_v2_canary_tenants
        if value.strip()
    }
    if not allowlist or tenant_id not in allowlist:
        raise LabTaskError("tenant is not admitted to the Lab protocol-v2 canary")


# ── serialization ─────────────────────────────────────────────────────

def serialize(t: LabTask) -> dict:
    return {
        "id": t.id,
        "issuer_user_id": t.issuer_user_id,
        "researcher_slug": t.researcher_slug,
        "title": t.title,
        "brief_md": t.brief_md,
        "scopes": t.scopes_json or [],
        "reward_sc": t.reward_sc,
        "platform_fee_sc": t.platform_fee_sc,
        "deliverable_kind": t.deliverable_kind,
        "status": t.status,
        "accepted_run_id": t.accepted_run_id,
        "reject_count": t.reject_count,
        "result_summary_md": t.result_summary_md,
        "deadline_at": t.deadline_at.isoformat() if t.deadline_at else None,
        "review_deadline_at": t.review_deadline_at.isoformat() if t.review_deadline_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def serialize_run(r: LabRun) -> dict:
    return {
        "id": r.id,
        "task_id": r.task_id,
        "researcher_slug": r.researcher_slug,
        "adapter": r.adapter,
        "status": r.status,
        "scopes": r.scopes_json or [],
        "model_tier": r.model_tier,
        "model_name": r.model_name,
        "model_policy_version": r.model_policy_version,
        "resource_cpu_cores": r.resource_cpu_cores,
        "resource_memory_mb": r.resource_memory_mb,
        "budget_usd_cents": r.budget_usd_cents,
        "cost_usd_cents": r.cost_usd_cents,
        "approvals": r.approvals_json or [],
        "error": r.error,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
    }


def serialize_step(s) -> dict:
    return {
        "id": s.id, "run_id": s.run_id, "seq": s.seq, "phase": s.phase,
        "tool": s.tool, "summary": s.summary, "payload": s.payload_json or {},
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def serialize_artifact(a: LabArtifact, unlocked: bool) -> dict:
    """Return metadata only; artifact content leaves through /download."""
    from app.services.lab_artifact_service import is_releasable
    releasable = bool(unlocked) and is_releasable(a)
    return {
        "id": a.id, "run_id": a.run_id, "task_id": a.task_id,
        "kind": a.kind, "title": a.title, "unlocked": releasable,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "sha256": a.sha256, "byte_size": a.byte_size,
        "producer_action_id": a.producer_action_id, "provenance": a.provenance,
        "scan_status": a.scan_status,
        "verification_status": a.verification_status, "retention_hold": a.retention_hold,
        "provider_artifact_id": a.provider_artifact_id,
        "producer_epoch": a.producer_epoch,
        "required": a.required,
        "content_type": a.content_type,
        "original_filename": a.original_filename,
        "expected_sha256": a.expected_sha256,
        "declared_byte_size": a.declared_byte_size,
        "storage_status": a.storage_status,
        "scanned_at": a.scanned_at.isoformat() if a.scanned_at else None,
        "released_at": a.released_at.isoformat() if a.released_at else None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
    }


async def _require_required_artifacts_releasable(db, task: LabTask) -> None:
    """Fence v2 settlement until every required production artifact is ready."""
    if not task.accepted_run_id:
        return
    run = await db.get(LabRun, task.accepted_run_id)
    if run is None or run.protocol_version != 2:
        return
    if not settings.lab_artifact_pipeline_enabled:
        raise LabTaskError("protocol-v2 artifact pipeline is disabled")

    from app.services.lab_artifact_service import is_releasable

    artifacts = (
        await db.execute(
            select(LabArtifact).where(
                LabArtifact.run_id == task.accepted_run_id,
                LabArtifact.required.is_(True),
            )
        )
    ).scalars().all()
    if not artifacts:
        raise LabTaskError("required artifacts are missing")
    blocked = [artifact for artifact in artifacts if not is_releasable(artifact)]
    if blocked:
        states = sorted(
            {
                f"{artifact.storage_status}/{artifact.scan_status}/"
                f"{artifact.verification_status}"
                for artifact in blocked
            }
        )
        raise LabTaskError(
            "required artifacts are not releasable: " + ", ".join(states)
        )


# ── researcher selection (auto-dispatch) ──────────────────────────────

def _is_researcher(r: Resident) -> bool:
    return bool(((r.meta_json or {}).get("lab") or {}).get("access"))


async def list_researchers(db) -> list[Resident]:
    rows = (await db.execute(select(Resident).where(Resident.meta_json.isnot(None)))).scalars().all()
    return [r for r in rows if _is_researcher(r)]


async def _busy_slugs(db) -> set[str]:
    rows = (await db.execute(
        select(LabRun.researcher_slug).where(LabRun.status.in_(ACTIVE_RUN_STATES))
    )).scalars().all()
    return set(rows)


async def _pick_researcher(db, task: LabTask) -> Resident | None:
    """Rule-based dispatch for open recruitment: an idle researcher, preferring
    a skills match with the task scopes, then least-recently used (by slug)."""
    researchers = await list_researchers(db)
    if not researchers:
        return None
    busy = await _busy_slugs(db)
    idle = [r for r in researchers if r.slug not in busy]
    if not idle:
        return None
    wanted = set(task.scopes_json or [])

    def score(r: Resident) -> tuple:
        skills = set(((r.meta_json or {}).get("lab") or {}).get("skills") or [])
        return (-len(skills & wanted), r.slug)  # more overlap first, then stable

    idle.sort(key=score)
    return idle[0]


# ── publish / fund / assign / start ───────────────────────────────────

async def create_task(
    db, *, issuer_id: str, title: str, brief: str, scopes: list[str], reward_sc: int,
    deliverable_kind: str = "report", researcher_slug: str | None = None,
    deadline_hours: int | None = None,
) -> LabTask:
    """Publish → fund (escrow hold) → assign (given or auto) → enqueue run.

    Raises LabTaskError on bad input, daily cap, insufficient balance, or an
    invalid/unavailable researcher.
    """
    protocol_version = _require_execution_consumer()
    _require_v2_tenant_admitted(protocol_version, issuer_id)
    if reward_sc <= 0:
        raise LabTaskError("reward must be positive")
    scopes = [s for s in (scopes or []) if s in ALLOWED_SCOPES]
    if not scopes:
        raise LabTaskError("at least one valid scope is required")
    unsupported_adapter_scopes = sorted(
        set(scopes) - set(supported_scopes_for_adapter())
    )
    if unsupported_adapter_scopes:
        raise LabTaskError(
            f"adapter {settings.lab_adapter} does not support scopes: "
            + ", ".join(unsupported_adapter_scopes)
        )
    if protocol_version == 2:
        unsupported_scopes = sorted(set(scopes) - {"code"})
        if unsupported_scopes:
            raise LabTaskError(
                "protocol-v2 does not provide production handlers for scopes: "
                + ", ".join(unsupported_scopes)
            )

    # Content moderation: reject a disallowed title/brief BEFORE any hold, and
    # record only a stable content-free CODE in telemetry (recovery plan Phase 4,
    # gap #6). The gate is structural + an operator-supplied blocklist.
    from app.lab import moderation, telemetry
    reject_code = moderation.moderate_task(title, brief)
    if reject_code is not None:
        telemetry.emit_alert(
            telemetry.LabAlert.TASK_MODERATION_REJECTED, issuer_user_id=issuer_id, reason=reject_code,
        )
        raise LabTaskError(f"task rejected by content policy: {reject_code}")

    # Minimum price: the reward must cover the compute the scopes authorize
    # (effective_budget_usd * lab_sc_per_usd), rejected BEFORE any hold so an
    # underpriced task never charges the issuer (recovery plan Phase 4, gap #6).
    from app.lab import pricing
    minimum_reward = pricing.minimum_reward_sc(scopes)
    if reward_sc < minimum_reward:
        raise LabTaskError(
            f"reward {reward_sc} below minimum {minimum_reward} SC for the requested scopes"
        )

    # Per-player daily publish cap.
    since = datetime.now(UTC) - timedelta(days=1)
    today = (await db.execute(
        select(func.count()).select_from(LabTask).where(
            LabTask.issuer_user_id == issuer_id, LabTask.created_at >= since,
        )
    )).scalar() or 0
    if today >= settings.lab_daily_tasks_per_user:
        raise LabTaskError("daily task limit reached")

    # Validate a named researcher up front (before touching money).
    if researcher_slug is not None:
        res = (await db.execute(select(Resident).where(Resident.slug == researcher_slug))).scalar_one_or_none()
        if res is None or not _is_researcher(res):
            raise LabTaskError("researcher not found or not authorized")

    terminalization_version = (
        "v2" if settings.lab_terminalizer_v2_enabled else "v1"
    )
    if terminalization_version == "v2":
        try:
            lab_terminalization_service.require_v2_consumer_ready()
        except lab_terminalization_service.LabTerminalizationError as exc:
            raise LabTaskError(str(exc)) from exc

    fee = math.ceil(reward_sc * settings.lab_platform_fee_rate)
    deadline_at = datetime.now(UTC) + timedelta(hours=deadline_hours or settings.lab_task_deadline_hours)

    task = LabTask(
        issuer_user_id=issuer_id, researcher_slug=researcher_slug, title=title[:200],
        brief_md=brief or "", scopes_json=scopes, reward_sc=reward_sc, platform_fee_sc=fee,
        terminal_creator_share_bps=int(round(settings.lab_creator_share * 10_000)),
        deliverable_kind=deliverable_kind, status="funded", deadline_at=deadline_at,
    )
    db.add(task)
    await db.flush()  # populate task.id for the hold reason, without committing

    # Transactional funding (recovery plan Phase 2, gap #9): the task, its escrow
    # hold, the debit, and the ledger row commit TOGETHER, so a crash can never
    # leave a funded task without a hold or a hold without its task link. On
    # insufficient balance nothing persists — the whole funding transaction is
    # abandoned before any charge.
    hold = await coin_service.hold_pending(
        db,
        issuer_id,
        reward_sc + fee,
        f"lab_task:{task.id}",
        terminalization_version=terminalization_version,
    )
    if hold is None:
        await db.rollback()
        raise LabTaskError("insufficient balance")
    task.hold_id = hold.id
    await db.commit()  # task(funded) + hold + debit + ledger row, one transaction
    await db.refresh(task)

    await _assign_and_start(db, task, protocol_version=protocol_version)
    await db.refresh(task)
    return task


async def _assign_and_start(
    db, task: LabTask, *, protocol_version: int | None = None
) -> None:
    """Assign a researcher (named or auto) and enqueue the first run. If open
    recruitment finds no idle researcher, the task stays ``funded`` for a later
    dispatch pass (dispatch_open_tasks)."""
    protocol_version = _require_execution_consumer(protocol_version)
    _require_v2_tenant_admitted(protocol_version, task.issuer_user_id)
    if task.researcher_slug is None:
        picked = await _pick_researcher(db, task)
        if picked is None:
            return  # remain funded; dispatcher retries later
        task.researcher_slug = picked.slug
    task.status = "assigned"
    await db.commit()
    await _start_run(db, task, protocol_version=protocol_version)


async def _start_run(
    db, task: LabTask, *, protocol_version: int | None = None
) -> LabRun:
    from app.lab import queue as lab_queue
    from app.models.lab_event import OutboxEvent

    protocol_version = _require_execution_consumer(protocol_version)
    _require_v2_tenant_admitted(protocol_version, task.issuer_user_id)
    from app.lab.model_policy import assignment_for_reward

    model = assignment_for_reward(task.reward_sc)
    run = LabRun(
        task_id=task.id, researcher_slug=task.researcher_slug, adapter=settings.lab_adapter,
        status="queued", scopes_json=list(task.scopes_json or []),
        model_tier=model.tier, model_name=model.model,
        model_policy_version=model.policy_version,
        resource_cpu_cores=model.cpu_cores,
        resource_memory_mb=model.memory_mb,
        budget_usd_cents=model.budget_usd_cents,
        protocol_version=protocol_version,
    )
    db.add(run)
    await db.flush()  # populate run.id without committing
    task.accepted_run_id = run.id
    # Durable dispatch (recovery plan Phase 2, gap #9): the run, its accepted-run
    # link, and a ``lab.run.enqueue`` outbox event commit in ONE transaction, so a
    # crash between the commit and the Redis LPUSH cannot lose the run — the outbox
    # dispatcher replays the enqueue. The inline enqueue below is the fast path;
    # duplicate delivery is idempotent (the runner's queued-guard + the run lease
    # skip a run already picked up).
    db.add(OutboxEvent(
        event_id=str(uuid.uuid4()), tenant_id=task.issuer_user_id, run_id=run.id,
        topic="lab.run.enqueue",
        payload_json={"run_id": run.id, "protocol_version": protocol_version},
    ))
    await db.commit()  # run + accepted_run_id + enqueue outbox, one transaction
    await db.refresh(run)
    await lab_queue.enqueue_run(run.id, protocol_version=protocol_version)
    return run


async def dispatch_open_tasks(db) -> int:
    """Assign + start any funded open-recruitment tasks that now have an idle
    researcher. Returns the number dispatched (cron/on-demand)."""
    protocol_version = _require_execution_consumer()
    tasks = (await db.execute(
        select(LabTask).where(LabTask.status == "funded", LabTask.researcher_slug.is_(None))
    )).scalars().all()
    n = 0
    for task in tasks:
        try:
            _require_v2_tenant_admitted(protocol_version, task.issuer_user_id)
        except LabTaskError:
            continue
        picked = await _pick_researcher(db, task)
        if picked is None:
            continue
        task.researcher_slug = picked.slug
        task.status = "assigned"
        await db.commit()
        await _start_run(db, task, protocol_version=protocol_version)
        n += 1
    return n


# ── review / settle / fail / accept / reject / cancel ─────────────────

async def mark_review(
    db,
    task: LabTask,
    run: LabRun,
    result_summary: str = "",
    *,
    commit: bool = True,
) -> bool:
    """Runner success hook: task → review with a 72h auto-release window.

    Guarded by a compare-and-set from a live (``assigned``/``running``) state so a
    stale runner/orchestrator that finished AFTER the task was cancelled/failed/
    expired can never revive it (status report gap #2). Returns True iff the task
    actually moved to review; False (no-op) means it was already terminal — the
    caller owns no completion for it. Belt-and-braces with the orchestrator's
    epoch fence."""
    moved = await transitions.cas_task_status(
        db, task_id=task.id, expected=("assigned", "running"), new="review",
        accepted_run_id=run.id,
        result_summary_md=result_summary or task.result_summary_md,
        review_deadline_at=datetime.now(UTC) + timedelta(hours=settings.lab_auto_release_hours),
    )
    if commit:
        await db.commit()
    else:
        await db.flush()
    if moved:
        await db.refresh(task)
    else:
        logger.info("mark_review skipped for task %s (status=%s, not revivable)",
                    task.id, task.status)
    return moved


async def fail_task(db, task: LabTask, reason: str = "") -> None:
    """Runner caller: durably request failure/refund; terminalizer owns effects."""
    if not task.hold_id:
        # Legacy/inconsistent cohorts are frozen for reconciliation. The caller may
        # still fence the run, but there is no escrow mutation to authorize here.
        logger.warning("terminal failure frozen for task %s without a hold", task.id)
        return
    if not task.accepted_run_id:
        raise LabTaskError("failure task has no accepted run binding")
    actor = f"runner:{task.accepted_run_id}"
    try:
        await lab_terminalization_service.submit_for_caller(
            db, task=task, operation="fail", actor=actor
        )
    except lab_terminalization_service.LabTerminalizationError as exc:
        raise LabTaskError(str(exc)) from exc


async def _settle_and_complete(db, task: LabTask) -> None:
    """Scheduler caller: enqueue auto-release without performing financial DML."""
    await _require_required_artifacts_releasable(db, task)
    try:
        await lab_terminalization_service.submit_for_caller(
            db,
            task=task,
            operation="auto_release",
            actor="scheduler:auto-release",
        )
    except lab_terminalization_service.LabTerminalizationError as exc:
        raise LabTaskError(str(exc)) from exc


async def accept_result(db, task_id: str, user_id: str) -> LabTask:
    task = await _require_own_task(db, task_id, user_id)
    if task.status != "review":
        raise LabTaskError("task is not awaiting acceptance")
    await _require_required_artifacts_releasable(db, task)
    try:
        await lab_terminalization_service.submit_for_caller(
            db, task=task, operation="accept", actor=user_id
        )
    except lab_terminalization_service.LabTerminalizationError as exc:
        raise LabTaskError(str(exc)) from exc
    return task


async def reject_result(db, task_id: str, user_id: str) -> LabTask:
    task = await _require_own_task(db, task_id, user_id)
    if task.status != "review":
        raise LabTaskError("task is not awaiting acceptance")
    if task.reject_count >= 1:
        raise LabTaskError("already rejected once; awaiting admin arbitration")
    task.reject_count += 1
    task.status = "rejected"  # awaiting admin arbitration (settle or refund)
    task.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(task)
    return task


async def cancel_task(db, task_id: str, user_id: str) -> LabTask:
    task = await _require_own_task(db, task_id, user_id)
    if task.status in ("completed", "cancelled", "failed", "expired"):
        raise LabTaskError("task already finalized")

    await _sync_codex_terminal_cost(db, task)

    try:
        await lab_terminalization_service.submit_for_caller(
            db, task=task, operation="cancel", actor=user_id
        )
    except lab_terminalization_service.LabTerminalizationError as exc:
        raise LabTaskError(str(exc)) from exc
    return task


async def _sync_codex_terminal_cost(db, task: LabTask) -> None:
    """Fence Codex and persist trusted usage before refund terminalization."""
    if not task.accepted_run_id:
        return
    run = await db.get(LabRun, task.accepted_run_id)
    if run is None or run.adapter != "codex" or run.status not in ACTIVE_RUN_STATES:
        return
    from app.lab.sandbox.codex import CodexAdapter

    try:
        run.cost_usd_cents = await CodexAdapter().cancel_and_collect_usage(run.id)
        await db.commit()
        await db.refresh(task)
    except Exception as exc:
        await db.rollback()
        raise LabTaskError(
            "Codex usage is unavailable; cancellation settlement is blocked"
        ) from exc


async def arbitrate_result(
    db,
    *,
    task_id: str,
    admin_id: str,
    decision: str,
) -> LabTask:
    """Submit the only terminal decisions allowed for a rejected task."""
    if decision not in {"settle", "refund"}:
        raise LabTaskError("arbitration decision must be settle or refund")
    task = await db.get(LabTask, task_id)
    if task is None:
        raise LabTaskError("task not found")
    if task.status != "rejected":
        raise LabTaskError("task is not awaiting arbitration")
    admin = await db.get(User, admin_id)
    if admin is None or admin.is_admin is not True:
        raise LabTaskError("arbitration actor is not an admin")
    operation = f"arbitrate_{decision}"
    try:
        await lab_terminalization_service.submit_for_caller(
            db, task=task, operation=operation, actor=admin_id
        )
    except lab_terminalization_service.LabTerminalizationError as exc:
        raise LabTaskError(str(exc)) from exc
    return task


async def _require_own_task(db, task_id: str, user_id: str) -> LabTask:
    task = await db.get(LabTask, task_id)
    if task is None:
        raise LabTaskError("task not found")
    if task.issuer_user_id != user_id:
        raise LabTaskError("not your task")
    return task


# ── cron: expiry + auto-release ───────────────────────────────────────

async def expire_lab_tasks(db) -> int:
    """Refund tasks past their deadline that never reached review, and
    auto-release tasks whose 72h review window elapsed. Returns count touched."""
    now = datetime.now(UTC)
    n = 0

    stale = (await db.execute(
        select(LabTask).where(
            LabTask.status.in_(["funded", "assigned", "running"]),
            LabTask.deadline_at <= now,
        )
    )).scalars().all()
    for task in stale:
        try:
            await _sync_codex_terminal_cost(db, task)
            await lab_terminalization_service.submit_for_caller(
                db,
                task=task,
                operation="expire",
                actor="scheduler:expire",
            )
            n += 1
        except lab_terminalization_service.LabTerminalizationError:
            logger.warning("expiry command rejected for task %s", task.id, exc_info=True)

    due = (await db.execute(
        select(LabTask).where(
            LabTask.status == "review", LabTask.review_deadline_at.isnot(None),
            LabTask.review_deadline_at <= now,
        )
    )).scalars().all()
    for task in due:
        try:
            await _settle_and_complete(db, task)
            n += 1
        except Exception:
            logger.warning("auto-release failed for task %s", task.id, exc_info=True)

    return n


# ── domain event: write memory + notify on completion ─────────────────

@on("lab_task_completed")
async def _on_lab_task_completed(db, task_id: str = "", issuer_user_id: str = "",
                                 researcher_slug: str | None = None, **kw) -> None:
    """Researcher remembers the win; issuer is notified. Runs in its own session
    so a handler hiccup can't poison the settle transaction."""
    from app.services.notification_service import notify
    async with async_session() as s:
        task = await s.get(LabTask, task_id)
        if task is None:
            return
        if researcher_slug:
            res = (await s.execute(select(Resident).where(Resident.slug == researcher_slug))).scalar_one_or_none()
            if res is not None:
                try:
                    from app.memory.service import MemoryService
                    await MemoryService(s).add_memory(
                        res.id, "event",
                        f"在实验楼完成了一项真实委托「{task.title}」，赚到了报酬",
                        importance=0.75, source="lab_task",
                    )
                except Exception:
                    logger.warning("lab completion memory failed for %s", researcher_slug, exc_info=True)
        if issuer_user_id:
            try:
                await notify(s, issuer_user_id, "lab", "委托完成",
                             f"你的委托「{task.title}」已完成，可领取产物", {"task_id": task.id})
            except Exception:
                logger.warning("lab completion notify failed for %s", issuer_user_id, exc_info=True)
