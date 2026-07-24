"""P2-B / V12 — artifact digest integrity + tenant ACL on retrieval (DB-slice,
no object store; scope ruling in .superpowers/sdd/task-9-brief.md). Tenant
mapping (v1): a lab record's tenant is its task's ``issuer_user_id``.

Scenario 4 (orchestrator e2e: happy path → artifact row carries tenant/sha256)
lives in ``tests/test_lab_e2e.py`` instead, appended as a new test function on
top of that file's own ``lab_env``/``_seed``/``_make_task`` fixtures — the
brief calls this out explicitly ("在既有 e2e fixture 上加一个断言测试,不改
已有测试函数").
"""
import hashlib
from datetime import UTC, datetime

import pytest

from app.lab import acl
from app.models.lab_artifact import LabArtifact
from app.models.lab_task import LabTask
from app.models.user import User
from app.services import lab_artifact_service
from app.services import lab_task_service as svc
from app.services.auth_service import create_token


# ── 1. finalize sets digest/size/expiry; tamper → DigestMismatch blocks retrieval ──

@pytest.mark.anyio
async def test_finalize_sets_fields_and_verify_detects_tamper(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_artifact_retention_days", 30, raising=False)

    task = LabTask(issuer_user_id="owner-a", title="t", reward_sc=10, status="completed")
    db_session.add(task)
    await db_session.flush()

    artifact = LabArtifact(run_id="run1", task_id=task.id, kind="text", title="x", text_md="hello world")
    await lab_artifact_service.finalize_artifact(db_session, artifact=artifact, tenant_id="owner-a")
    db_session.add(artifact)
    await db_session.commit()

    expected_digest = hashlib.sha256("hello world".encode("utf-8")).hexdigest()
    assert artifact.sha256 == expected_digest
    assert artifact.byte_size == len("hello world".encode("utf-8"))
    assert artifact.tenant_id == "owner-a"
    assert artifact.expires_at is not None
    expires = artifact.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    delta_days = (expires - datetime.now(UTC)).days
    assert 28 <= delta_days <= 30

    # Untampered → verify_and_get returns the row.
    fetched = await lab_artifact_service.verify_and_get(
        db_session, artifact_id=artifact.id, user_id="owner-a", is_admin=False,
    )
    assert fetched.id == artifact.id

    # Tampered content → DigestMismatch blocks retrieval.
    artifact.text_md = "tampered"
    await db_session.commit()
    with pytest.raises(lab_artifact_service.DigestMismatch):
        await lab_artifact_service.verify_and_get(
            db_session, artifact_id=artifact.id, user_id="owner-a", is_admin=False,
        )


# ── 1b. non-text kind digests uri, not an empty text_md (P2-B review) ──

@pytest.mark.anyio
async def test_finalize_and_verify_use_uri_digest_for_non_text_kind(db_session):
    """A link/file/image/dataset artifact commonly ships with text_md="" (the
    http adapter's collected artifacts do this) — the digest must reflect the
    uri, not silently hash the empty string and go stale the moment the uri
    changes."""
    task = LabTask(issuer_user_id="owner-a", title="t", reward_sc=10, status="completed")
    db_session.add(task)
    await db_session.flush()

    artifact = LabArtifact(
        run_id="run1", task_id=task.id, kind="link", title="x",
        text_md="", uri="https://example.org/report",
    )
    await lab_artifact_service.finalize_artifact(db_session, artifact=artifact, tenant_id="owner-a")
    db_session.add(artifact)
    await db_session.commit()

    expected_digest = hashlib.sha256("https://example.org/report".encode("utf-8")).hexdigest()
    assert artifact.sha256 == expected_digest  # not the empty-string digest of text_md=""

    fetched = await lab_artifact_service.verify_and_get(
        db_session, artifact_id=artifact.id, user_id="owner-a", is_admin=False,
    )
    assert fetched.id == artifact.id

    artifact.uri = "https://example.org/tampered"
    await db_session.commit()
    with pytest.raises(lab_artifact_service.DigestMismatch):
        await lab_artifact_service.verify_and_get(
            db_session, artifact_id=artifact.id, user_id="owner-a", is_admin=False,
        )


# ── 2. cross-tenant denied / admin allowed; REST 404 vs 409 vs flag-off 200 ──

@pytest.mark.anyio
async def test_verify_and_get_acl_cross_tenant_denied_admin_allowed(db_session):
    task = LabTask(issuer_user_id="owner-a", title="t", reward_sc=10, status="completed")
    db_session.add(task)
    await db_session.flush()
    artifact = LabArtifact(run_id="run1", task_id=task.id, kind="text", title="x", text_md="body")
    await lab_artifact_service.finalize_artifact(db_session, artifact=artifact, tenant_id="owner-a")
    db_session.add(artifact)
    await db_session.commit()

    with pytest.raises(acl.AclDenied):
        await lab_artifact_service.verify_and_get(
            db_session, artifact_id=artifact.id, user_id="intruder-b", is_admin=False,
        )

    got = await lab_artifact_service.verify_and_get(
        db_session, artifact_id=artifact.id, user_id="admin-c", is_admin=True,
    )
    assert got.id == artifact.id


@pytest.mark.anyio
async def test_rest_artifact_metadata_acl_and_download_digest_boundary(client, db_session, monkeypatch):
    """Current contract (ADR-lab-artifact-storage): GET /lab/artifacts/{id} is an
    ACL-only, metadata-ONLY projection (cross-tenant 404; owner 200 but no body/URI
    inline). Digest-tamper is enforced at the /download seam (409), never via the
    metadata endpoint. Body content leaves ONLY through /download."""
    from app.config import settings

    owner = User(id="owner-a", name="Owner A", email="owner-a@t.com")
    intruder = User(id="intruder-b", name="Intruder B", email="intruder-b@t.com")
    db_session.add_all([owner, intruder])
    task = LabTask(issuer_user_id="owner-a", title="t", reward_sc=10, status="completed")
    db_session.add(task)
    await db_session.flush()
    artifact = LabArtifact(run_id="run1", task_id=task.id, kind="text", title="x", text_md="body")
    await lab_artifact_service.finalize_artifact(db_session, artifact=artifact, tenant_id="owner-a")
    db_session.add(artifact)
    await db_session.commit()
    artifact_id = artifact.id

    monkeypatch.setattr(settings, "lab_agent_v1_enabled", True, raising=False)

    # Cross-tenant metadata read is refused (anti-probing 404, not 403).
    headers_intruder = {"Authorization": f"Bearer {create_token('intruder-b')}"}
    resp = await client.get(f"/lab/artifacts/{artifact_id}", headers=headers_intruder)
    assert resp.status_code == 404

    # Owner metadata read: 200, but body/URI are never inlined — metadata-only.
    headers_owner = {"Authorization": f"Bearer {create_token('owner-a')}"}
    meta = await client.get(f"/lab/artifacts/{artifact_id}", headers=headers_owner)
    assert meta.status_code == 200
    body = meta.json()
    assert "text_md" not in body and "uri" not in body
    assert body["sha256"] == artifact.sha256

    # Tamper the stored body: the digest boundary at /download rejects it (409),
    # so a tampered body can never leave the API.
    fresh = await db_session.get(LabArtifact, artifact_id)
    fresh.text_md = "tampered"
    await db_session.commit()

    dl = await client.get(f"/lab/artifacts/{artifact_id}/download", headers=headers_owner)
    assert dl.status_code == 409
    assert "digest" in dl.json()["detail"]


# ── 3. locked semantics regression: content hidden, new metadata fields present ──

def test_serialize_artifact_locked_hides_content_but_shows_integrity_metadata():
    a = LabArtifact(
        id="art1", run_id="run1", task_id="task1", kind="text", title="x",
        text_md="secret", uri="http://x", sha256="abc123", scan_status="clean",
        verification_status="verified", retention_hold=True, storage_status="legacy",
    )

    locked = svc.serialize_artifact(a, unlocked=False)
    assert "text_md" not in locked and "uri" not in locked and "meta" not in locked
    assert locked["sha256"] == "abc123"
    assert locked["scan_status"] == "clean"
    assert locked["verification_status"] == "verified"
    assert locked["retention_hold"] is True

    # Unlocking flips the ``unlocked`` flag, but body/URI still never inline into
    # metadata — content leaves ONLY through /download (ADR-lab-artifact-storage).
    unlocked = svc.serialize_artifact(a, unlocked=True)
    assert unlocked["unlocked"] is True
    assert "text_md" not in unlocked and "uri" not in unlocked
    assert unlocked["sha256"] == "abc123"
