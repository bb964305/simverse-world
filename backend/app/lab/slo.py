"""Content-free SLO metrics + a point-in-time snapshot for the Lab (recovery
plan Phase 10). Every value is STRUCTURAL — queue depth, ages in seconds, counts
— never content, honoring the same hard invariant as ``telemetry`` (kickoff
hard-#3). Prometheus objects are created lazily so importing this module stays
inert in minimal environments.

``collect_snapshot`` is the safe, periodic read the nightly cron / a health
endpoint can call: it derives queue depth (Redis), active/ orphan-candidate run
counts, the oldest unpublished outbox age, and the outbox dead-letter count from
the DB+Redis ground truth, and mirrors them onto the gauges. The histograms
(run latency, approval age) are observed at their event points when wired.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy import func, select

from app.config import settings
from app.models.lab_event import OutboxEvent
from app.models.lab_run import LabRun
from app.redis_client import get_redis

logger = logging.getLogger("lab.slo")

_ACTIVE_RUN_STATES = ("running", "needs_approval")

_METRICS = None


def _metrics():
    global _METRICS
    if _METRICS is None:
        from prometheus_client import Gauge, Histogram
        _METRICS = {
            "queue_depth": Gauge("sv_lab_queue_depth", "Lab work-queue depth (pending runs)"),
            "active_runs": Gauge("sv_lab_active_runs", "Lab runs currently running or needs_approval"),
            "oldest_unpublished_age_s": Gauge(
                "sv_lab_oldest_unpublished_outbox_seconds", "Age of the oldest unpublished outbox row"),
            "dead_letter": Gauge("sv_lab_outbox_dead_letter", "Outbox rows dead-lettered/quarantined"),
            "orphan_candidates": Gauge(
                "sv_lab_orphan_candidates", "Active runs whose heartbeat is past the TTL"),
            "run_latency_s": Histogram("sv_lab_run_latency_seconds", "Run duration, started->ended"),
            "approval_age_s": Histogram("sv_lab_approval_age_seconds", "Approval wait duration"),
        }
    return _METRICS


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def observe_run_latency(seconds: float) -> None:
    """Record a completed run's duration. Best-effort; never raises into callers."""
    try:
        _metrics()["run_latency_s"].observe(max(0.0, float(seconds)))
    except Exception:  # pragma: no cover — metrics must not break the run path
        logger.debug("run_latency observe failed", exc_info=True)


def observe_approval_age(seconds: float) -> None:
    """Record how long a sensitive-action approval waited. Best-effort."""
    try:
        _metrics()["approval_age_s"].observe(max(0.0, float(seconds)))
    except Exception:  # pragma: no cover
        logger.debug("approval_age observe failed", exc_info=True)


async def collect_snapshot(db, *, now: datetime | None = None) -> dict:
    """Point-in-time content-free SLO snapshot. Reads ground truth (Redis queue +
    DB) and mirrors it onto the gauges. Returns the structural dict."""
    now = now if now is not None else datetime.now(UTC)

    try:
        from app.lab.queue import queue_keys

        redis = get_redis()
        queue_depth_by_protocol = {
            version: int(await redis.llen(queue_keys(version)[0]))
            for version in (1, 2)
        }
        queue_depth = sum(queue_depth_by_protocol.values())
    except Exception:  # pragma: no cover — Redis optional in minimal envs
        queue_depth_by_protocol = {1: 0, 2: 0}
        queue_depth = 0

    active = (await db.execute(
        select(func.count()).select_from(LabRun).where(LabRun.status.in_(_ACTIVE_RUN_STATES))
    )).scalar() or 0

    oldest = (await db.execute(
        select(func.min(OutboxEvent.created_at)).where(
            OutboxEvent.published_at.is_(None), OutboxEvent.dispatch_status == "pending")
    )).scalar()
    oldest_dt = _as_utc(oldest)
    oldest_age = round((now - oldest_dt).total_seconds(), 1) if oldest_dt else 0.0

    dead = (await db.execute(
        select(func.count()).select_from(OutboxEvent).where(OutboxEvent.dispatch_status == "dead")
    )).scalar() or 0

    ttl = int(getattr(settings, "lab_run_heartbeat_ttl_s", 300) or 300)
    cutoff = now - timedelta(seconds=ttl)
    orphans = (await db.execute(
        select(func.count()).select_from(LabRun).where(
            LabRun.status.in_(_ACTIVE_RUN_STATES),
            LabRun.heartbeat_at.isnot(None),
            LabRun.heartbeat_at <= cutoff,
        )
    )).scalar() or 0

    snap = {
        "queue_depth": queue_depth,
        "queue_depth_v1": queue_depth_by_protocol[1],
        "queue_depth_v2": queue_depth_by_protocol[2],
        "active_runs": int(active),
        "oldest_unpublished_age_s": oldest_age,
        "dead_letter": int(dead),
        "orphan_candidates": int(orphans),
    }
    try:
        m = _metrics()
        m["queue_depth"].set(snap["queue_depth"])
        m["active_runs"].set(snap["active_runs"])
        m["oldest_unpublished_age_s"].set(snap["oldest_unpublished_age_s"])
        m["dead_letter"].set(snap["dead_letter"])
        m["orphan_candidates"].set(snap["orphan_candidates"])
    except Exception:  # pragma: no cover
        logger.debug("slo gauge set failed", exc_info=True)
    return snap
