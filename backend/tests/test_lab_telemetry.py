"""T8 — content-free lab telemetry / alerts (PRD §Observability; hard-#3: raw
thought/content never leaves the sandbox into logs/telemetry).

The alert emitter is content-free BY CONSTRUCTION: it accepts only an allowlist
of structural fields (ids, dimension names, reason codes, counts) and refuses
any free-text/content-bearing field. These tests pin the taxonomy (the 7 alert
conditions the deploy spec requires) and the redaction guard.
"""
import pytest

from app.lab import telemetry


def test_alert_taxonomy_complete():
    # every alert the deployment spec requires (art-spec/prd §Observability)
    required = {
        "orphan_heartbeat", "stale_epoch", "blocked_egress", "approval_timeout",
        "budget_exhausted", "world_apply_failed", "cleanup_quarantine",
    }
    assert required.issubset({a.value for a in telemetry.LabAlert})


def test_emit_alert_records_only_structural_fields():
    rec = telemetry.emit_alert(
        telemetry.LabAlert.BUDGET_EXHAUSTED,
        run_id="run-1", tenant_id="t-1", dimension="tool_calls", reason="limit", count=3,
    )
    assert rec == {
        "alert": "budget_exhausted", "run_id": "run-1", "tenant_id": "t-1",
        "dimension": "tool_calls", "reason": "limit", "count": 3,
    }


def test_emit_alert_rejects_content_bearing_fields():
    # anything that could carry raw thought / payload / summary is refused
    for bad in ("summary", "content", "payload", "text", "args", "thought", "message"):
        with pytest.raises(telemetry.TelemetryLeak):
            telemetry.emit_alert(telemetry.LabAlert.STALE_EPOCH, **{bad: "secret detail"})


def test_emit_alert_never_raises_on_unknown_but_structural_extra():
    # unknown structural kwargs are dropped, not leaked, and do not crash callers
    rec = telemetry.emit_alert(telemetry.LabAlert.BLOCKED_EGRESS, run_id="r", host_hash="deadbeef")
    assert rec["alert"] == "blocked_egress"
    assert "host_hash" in rec  # host_hash is on the structural allowlist
    assert rec["run_id"] == "r"


def test_unknown_alert_type_rejected():
    with pytest.raises(ValueError):
        telemetry.emit_alert("not_an_alert", run_id="r")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_broker_egress_denial_emits_blocked_egress(db_session, monkeypatch):
    """Wiring proof: an http.request to a host outside the grant's egress
    allowlist is denied by the Broker AND raises a content-free BLOCKED_EGRESS
    alert (only ids + reason code, never the URL)."""
    from app.config import settings
    from app.lab import broker, grants

    monkeypatch.setattr(settings, "lab_grant_secret", "test-secret", raising=False)
    seen = []
    monkeypatch.setattr("app.lab.telemetry.emit_alert",
                        lambda alert, **f: seen.append((alert, f)) or {})

    db = db_session
    token, claims = await grants.issue_run_grant(
        db, tenant_id="owner-1", task_id="task1", run_id="run1", agent_id="a",
        capabilities=["http"], egress=["*.allowed.org"],
    )
    with pytest.raises(broker.ActionDenied):
        await broker.request_action(
            db, claims=claims, token=token, tool_name="http.request",
            args={"url": "https://evil.example.com/x", "method": "POST"}, expected_epoch=0,
        )
    kinds = [a for (a, _f) in seen]
    assert telemetry.LabAlert.BLOCKED_EGRESS in kinds
    # the emitted record carries no URL / content — only structural fields
    payloads = [f for (a, f) in seen if a == telemetry.LabAlert.BLOCKED_EGRESS]
    blob = " ".join(str(v) for f in payloads for v in f.values())
    assert "evil.example.com" not in blob
