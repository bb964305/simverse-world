"""Content-free lab telemetry / alerts (PRD §Observability; deploy spec:
"Alerts fire for orphan heartbeat, stale epoch, blocked unbrokered egress,
cleanup quarantine, approval timeout, budget exhaustion, and world apply/revert
failure").

Hard invariant (kickoff hard-#3): raw thought, tool args/results, artifact
content, and any free text NEVER enter telemetry. This emitter is content-free
by construction — it accepts only an allowlist of *structural* fields (ids,
dimension names, reason codes, counts, hashes). A content-bearing field name is
a programming error and raises ``TelemetryLeak`` rather than silently shipping
it, so the guarantee is enforced at the call site, not trusted.

Emission is best-effort: the Prometheus counter and structured log line never
raise into the caller (an alert must not be able to break a security path).
"""
from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger("lab.alert")


class TelemetryLeak(Exception):
    """A content-bearing field was passed to a content-free telemetry call."""


class LabAlert(str, Enum):
    ORPHAN_HEARTBEAT = "orphan_heartbeat"        # a run's lease lapsed (no heartbeat past TTL) → reap+refund
    STALE_EPOCH = "stale_epoch"                  # a fenced (pre-takeover) writer was rejected
    BLOCKED_EGRESS = "blocked_egress"            # an egress target outside the allowlist was denied
    APPROVAL_TIMEOUT = "approval_timeout"        # a sensitive-action approval expired → default deny
    BUDGET_EXHAUSTED = "budget_exhausted"        # a budget dimension hit its limit → grant revoked, run stopped
    WORLD_APPLY_FAILED = "world_apply_failed"    # a world proposal apply/revert failed
    CLEANUP_QUARANTINE = "cleanup_quarantine"    # a workspace/artifact could not be cleaned → quarantined
    TASK_MODERATION_REJECTED = "task_moderation_rejected"  # task title/brief failed the content gate (code only)


# The ONLY fields an alert may carry — all structural, none content-bearing.
_ALLOWED_FIELDS = frozenset({
    "run_id", "tenant_id", "task_id", "proposal_id", "revision_id", "action_id",
    "approval_id", "artifact_id", "grant_jti", "epoch", "dimension", "reason",
    "count", "host_hash", "seq",
})
# Field names that would carry raw content — explicitly forbidden (defense in
# depth; any not-allowed name is dropped, but these raise to catch mistakes).
_FORBIDDEN_FIELDS = frozenset({
    "summary", "content", "payload", "text", "text_md", "args", "result",
    "thought", "reasoning", "message", "body", "prompt", "brief",
})


def emit_alert(alert: LabAlert, **fields) -> dict:
    """Record one content-free alert. Returns the structural record (also useful
    for assertions/tests). Raises ``TelemetryLeak`` if a forbidden
    content-bearing field is passed; unknown-but-harmless structural fields are
    dropped. Never raises for metric/log failures."""
    if not isinstance(alert, LabAlert):
        try:
            alert = LabAlert(alert)
        except ValueError:
            raise ValueError(f"unknown lab alert '{alert}'") from None

    leaked = _FORBIDDEN_FIELDS & set(fields)
    if leaked:
        raise TelemetryLeak(f"content-bearing field(s) not allowed in telemetry: {sorted(leaked)}")

    record = {"alert": alert.value}
    for k, v in fields.items():
        if k in _ALLOWED_FIELDS:
            record[k] = v
        # silently drop unknown structural keys (never ship an unvetted field)

    try:
        _counter().labels(alert=alert.value, reason=str(record.get("reason") or "")).inc()
    except Exception:  # pragma: no cover — metrics must not break callers
        logger.debug("alert counter failed", exc_info=True)
    try:
        logger.warning("lab_alert %s", record)
    except Exception:  # pragma: no cover
        pass
    return record


_COUNTER = None


def _counter():
    """Lazily create the Prometheus counter so importing this module never
    forces prometheus_client at import time (keeps it inert in minimal envs)."""
    global _COUNTER
    if _COUNTER is None:
        from prometheus_client import Counter
        _COUNTER = Counter(
            "sv_lab_alerts_total",
            "Content-free lab security/ops alerts, by alert type and reason code",
            ["alert", "reason"],
        )
    return _COUNTER
