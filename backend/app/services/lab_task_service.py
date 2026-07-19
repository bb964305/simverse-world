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
from datetime import datetime, timedelta, UTC

from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.events.bus import on, emit
from app.models.lab_artifact import LabArtifact
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.resident import Resident
from app.services import coin_service

logger = logging.getLogger(__name__)

ALLOWED_SCOPES = ["web_search", "browse", "code", "http"]
ACTIVE_RUN_STATES = ("queued", "running", "needs_approval")


class LabTaskError(Exception):
    """Publish/accept/reject/cancel conflicts (router maps to 400/402/403/409)."""


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
    """Artifact view. Locked (task not released) → content withheld, external
    links never auto-exposed (anti-freeload + anti-injection, spec §5.3).
    V12 integrity/retention fields are read-only metadata, not the withheld
    content itself — they're always present (additive, backward compatible;
    a pre-P2-B row just reports its column defaults)."""
    base = {
        "id": a.id, "run_id": a.run_id, "task_id": a.task_id,
        "kind": a.kind, "title": a.title, "unlocked": unlocked,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "sha256": a.sha256, "byte_size": a.byte_size,
        "producer_action_id": a.producer_action_id, "provenance": a.provenance,
        "scan_status": a.scan_status,
        "verification_status": a.verification_status, "retention_hold": a.retention_hold,
    }
    if unlocked:
        base.update({"uri": a.uri, "text_md": a.text_md, "meta": a.meta_json or {}})
    return base


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
    if reward_sc <= 0:
        raise LabTaskError("reward must be positive")
    scopes = [s for s in (scopes or []) if s in ALLOWED_SCOPES]
    if not scopes:
        raise LabTaskError("at least one valid scope is required")

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

    fee = math.ceil(reward_sc * settings.lab_platform_fee_rate)
    deadline_at = datetime.now(UTC) + timedelta(hours=deadline_hours or settings.lab_task_deadline_hours)

    task = LabTask(
        issuer_user_id=issuer_id, researcher_slug=researcher_slug, title=title[:200],
        brief_md=brief or "", scopes_json=scopes, reward_sc=reward_sc, platform_fee_sc=fee,
        deliverable_kind=deliverable_kind, status="draft", deadline_at=deadline_at,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    hold_id = await coin_service.hold(db, issuer_id, reward_sc + fee, f"lab_task:{task.id}")
    if hold_id is None:
        # Roll the task back to a terminal state; no money moved.
        task.status = "cancelled"
        await db.commit()
        raise LabTaskError("insufficient balance")
    task.hold_id = hold_id
    task.status = "funded"
    await db.commit()

    await _assign_and_start(db, task)
    await db.refresh(task)
    return task


async def _assign_and_start(db, task: LabTask) -> None:
    """Assign a researcher (named or auto) and enqueue the first run. If open
    recruitment finds no idle researcher, the task stays ``funded`` for a later
    dispatch pass (dispatch_open_tasks)."""
    if task.researcher_slug is None:
        picked = await _pick_researcher(db, task)
        if picked is None:
            return  # remain funded; dispatcher retries later
        task.researcher_slug = picked.slug
    task.status = "assigned"
    await db.commit()
    await _start_run(db, task)


async def _start_run(db, task: LabTask) -> LabRun:
    from app.lab import queue as lab_queue

    budget_cents = int(round(settings.lab_default_budget_usd * 100))
    run = LabRun(
        task_id=task.id, researcher_slug=task.researcher_slug, adapter=settings.lab_adapter,
        status="queued", scopes_json=list(task.scopes_json or []), budget_usd_cents=budget_cents,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    task.accepted_run_id = run.id
    await db.commit()
    await lab_queue.enqueue_run(run.id)
    return run


async def dispatch_open_tasks(db) -> int:
    """Assign + start any funded open-recruitment tasks that now have an idle
    researcher. Returns the number dispatched (cron/on-demand)."""
    tasks = (await db.execute(
        select(LabTask).where(LabTask.status == "funded", LabTask.researcher_slug.is_(None))
    )).scalars().all()
    n = 0
    for task in tasks:
        picked = await _pick_researcher(db, task)
        if picked is None:
            continue
        task.researcher_slug = picked.slug
        task.status = "assigned"
        await db.commit()
        await _start_run(db, task)
        n += 1
    return n


# ── review / settle / fail / accept / reject / cancel ─────────────────

async def mark_review(db, task: LabTask, run: LabRun, result_summary: str = "") -> None:
    """Runner success hook: task → review with a 72h auto-release window."""
    task.status = "review"
    task.accepted_run_id = run.id
    task.result_summary_md = result_summary or task.result_summary_md
    task.review_deadline_at = datetime.now(UTC) + timedelta(hours=settings.lab_auto_release_hours)
    task.updated_at = datetime.now(UTC)
    await db.commit()


async def fail_task(db, task: LabTask, reason: str = "") -> None:
    """Terminal failure: refund the escrow in full and mark failed."""
    if task.hold_id:
        try:
            await coin_service.refund(db, task.hold_id, f"lab_refund:{task.id}:{reason}")
        except coin_service.CoinError:
            logger.warning("refund skipped for task %s (hold not held)", task.id)
    task.status = "failed"
    task.updated_at = datetime.now(UTC)
    await db.commit()


async def _settle_and_complete(db, task: LabTask) -> None:
    """Distribute the escrow: researcher creator share + treasury + platform sink,
    then mark the task completed and emit the domain event."""
    researcher = (await db.execute(
        select(Resident).where(Resident.slug == task.researcher_slug)
    )).scalar_one_or_none()

    reward = task.reward_sc
    fee = task.platform_fee_sc
    splits: list[tuple[str, int, str]] = []

    creator_id = researcher.creator_id if researcher else None
    creator_amount = int(reward * settings.lab_creator_share)
    treasury_amount = reward - creator_amount
    if creator_id and creator_id != "system" and creator_amount > 0:
        splits.append((creator_id, creator_amount, f"lab_reward:{task.id}"))
    else:
        # No real creator (or share rounds to 0) — whole reward to the treasury.
        treasury_amount = reward
    if task.researcher_slug and treasury_amount > 0:
        splits.append((f"treasury:{task.researcher_slug}", treasury_amount, f"lab_treasury:{task.id}"))
    if fee > 0:
        splits.append(("sink", fee, f"lab_fee:{task.id}"))

    if task.hold_id:
        await coin_service.settle(db, task.hold_id, splits)

    task.status = "completed"
    task.completed_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    await db.commit()
    await emit(db, "lab_task_completed", task_id=task.id, issuer_user_id=task.issuer_user_id,
               researcher_slug=task.researcher_slug)


async def accept_result(db, task_id: str, user_id: str) -> LabTask:
    task = await _require_own_task(db, task_id, user_id)
    if task.status != "review":
        raise LabTaskError("task is not awaiting acceptance")
    await _settle_and_complete(db, task)
    await db.refresh(task)
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
    # Not-yet-in-review → full refund. (Running runs are left to terminate; the
    # refund still fires here since the hold is untouched until settle.)
    if task.hold_id:
        try:
            await coin_service.refund(db, task.hold_id, f"lab_cancel:{task.id}")
        except coin_service.CoinError:
            logger.warning("cancel refund skipped for task %s", task.id)
    task.status = "cancelled"
    task.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(task)
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
        if task.hold_id:
            try:
                await coin_service.refund(db, task.hold_id, f"lab_expire:{task.id}")
            except coin_service.CoinError:
                pass
        task.status = "expired"
        task.updated_at = now
        await db.commit()
        n += 1

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
