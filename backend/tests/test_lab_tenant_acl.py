"""T5 — lab resource tenant ACL (PRD §Instruction and Memory Layers tenant
ACL section, §V03). Tenant mapping (v1): a lab record's tenant is its task's
``issuer_user_id``. A cross-tenant read must 404 — never 403 — so a probing
request can't distinguish "exists but isn't yours" from "doesn't exist".
Admin has its own router namespace and stays unaffected. ``approval_projection``
is the server-authoritative shape the client gates its approve/deny controls
on — never the client's own state.
"""
from datetime import datetime, timedelta, UTC

import pytest

from app.lab import acl
from app.models.lab_artifact import LabArtifact
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_task import LabTask
from app.models.user import User
from app.services.auth_service import create_token


@pytest.fixture
async def two_users_and_resource(db_session):
    owner = User(id="owner-a", name="Owner A", email="owner-a@t.com")
    intruder = User(id="intruder-b", name="Intruder B", email="intruder-b@t.com")
    admin = User(id="admin-c", name="Admin C", email="admin-c@t.com", is_admin=True)
    db_session.add_all([owner, intruder, admin])

    task = LabTask(issuer_user_id="owner-a", title="A's task", reward_sc=10, status="running")
    db_session.add(task)
    await db_session.flush()

    run = LabRun(task_id=task.id, researcher_slug="sage", status="needs_approval",
                 approvals_json=[{"id": "appr-1", "status": "pending"}])
    db_session.add(run)
    await db_session.flush()
    task.accepted_run_id = run.id

    step = LabRunStep(run_id=run.id, seq=1, phase="message", summary="hi")
    artifact = LabArtifact(run_id=run.id, task_id=task.id, kind="text", title="x", text_md="body")
    db_session.add_all([step, artifact])
    await db_session.commit()

    return {"task": task, "run": run, "artifact": artifact}


# ─── 1. cross-tenant task read → 404, not 403 ──────────────────────────


@pytest.mark.anyio
async def test_cross_tenant_task_read_is_404(client, two_users_and_resource):
    task = two_users_and_resource["task"]
    headers = {"Authorization": f"Bearer {create_token('intruder-b')}"}
    resp = await client.get(f"/lab/tasks/{task.id}", headers=headers)
    assert resp.status_code == 404


# ─── 2. cross-tenant run / steps / artifact read → 404 ─────────────────


@pytest.mark.anyio
async def test_cross_tenant_run_steps_artifact_read_is_404(client, two_users_and_resource):
    run = two_users_and_resource["run"]
    artifact = two_users_and_resource["artifact"]
    headers = {"Authorization": f"Bearer {create_token('intruder-b')}"}

    r1 = await client.get(f"/lab/runs/{run.id}", headers=headers)
    assert r1.status_code == 404

    r2 = await client.get(f"/lab/runs/{run.id}/steps", headers=headers)
    assert r2.status_code == 404

    r3 = await client.get(f"/lab/artifacts/{artifact.id}", headers=headers)
    assert r3.status_code == 404


# ─── 3. cross-tenant approval decision refused, no side effect ─────────


@pytest.mark.anyio
async def test_cross_tenant_approval_decision_refused_no_side_effect(client, db_session, two_users_and_resource):
    run = two_users_and_resource["run"]
    headers = {"Authorization": f"Bearer {create_token('intruder-b')}"}
    resp = await client.post(
        f"/lab/runs/{run.id}/approval",
        json={"approval_id": "appr-1", "decision": True},
        headers=headers,
    )
    assert not (200 <= resp.status_code < 300)

    fresh = await db_session.get(LabRun, run.id)
    assert fresh.approvals_json == [{"id": "appr-1", "status": "pending"}]


# ─── 4. admin channel stays intact (its own router, not ACL-gated) ─────


@pytest.mark.anyio
async def test_admin_channel_reads_via_admin_router_unaffected(client, two_users_and_resource):
    run = two_users_and_resource["run"]
    headers = {"Authorization": f"Bearer {create_token('admin-c')}"}
    resp = await client.get("/admin/lab/runs", headers=headers)
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["runs"]]
    assert run.id in ids


# ─── 5. approval_projection: server-authoritative shape ────────────────


class _FakeTask:
    def __init__(self, issuer_user_id):
        self.issuer_user_id = issuer_user_id


class _FakeApproval:
    def __init__(self, decision, expires_at, decision_scope="task_owner"):
        self.decision = decision
        self.expires_at = expires_at
        self.decision_scope = decision_scope


def test_approval_projection_owner_pending_can_decide():
    task = _FakeTask("owner-a")
    appr = _FakeApproval("pending", datetime.now(UTC) + timedelta(minutes=5))
    proj = acl.approval_projection(appr, task, user_id="owner-a", is_admin=False)
    assert proj == {
        "allowed_actions": ["approve", "deny"],
        "can_decide": True,
        "decision_scope": "task_owner",
        "status": "pending",
    }


def test_approval_projection_observer_cannot_decide():
    task = _FakeTask("owner-a")
    appr = _FakeApproval("pending", datetime.now(UTC) + timedelta(minutes=5))
    proj = acl.approval_projection(appr, task, user_id="intruder-b", is_admin=False)
    assert proj["can_decide"] is False
    assert proj["allowed_actions"] == []


def test_approval_projection_expired_has_no_allowed_actions():
    task = _FakeTask("owner-a")
    appr = _FakeApproval("pending", datetime.now(UTC) - timedelta(minutes=5))
    proj = acl.approval_projection(appr, task, user_id="owner-a", is_admin=False)
    assert proj["can_decide"] is True  # still the decider, just past the window
    assert proj["allowed_actions"] == []


def test_approval_projection_decided_has_no_allowed_actions():
    task = _FakeTask("owner-a")
    appr = _FakeApproval("approved", datetime.now(UTC) + timedelta(minutes=5))
    proj = acl.approval_projection(appr, task, user_id="owner-a", is_admin=False)
    assert proj["allowed_actions"] == []
    assert proj["status"] == "approved"
