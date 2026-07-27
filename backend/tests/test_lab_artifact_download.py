"""Phase 5 (recovery plan) — authenticated, digest-checking artifact download
boundary. Content is served ONLY through this seam and ONLY for an artifact that
is ACL-owned, digest-intact, scan-clean+verified, and whose task is released.
"""
import pytest

from app.models.lab_artifact import LabArtifact
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.user import User
from app.services import lab_artifact_service as arts
from app.services import lab_task_service
from app.services.auth_service import create_token


@pytest.fixture
async def dl_env(db_session):
    db_session.add_all([
        User(id="owner", name="O", email="o@t.com"),
        User(id="other", name="X", email="x@t.com"),
    ])
    done = LabTask(id="tk-done", issuer_user_id="owner", title="t", status="completed")
    running = LabTask(id="tk-run", issuer_user_id="owner", title="t2", status="running")
    db_session.add_all([done, running])
    await db_session.flush()

    clean = LabArtifact(run_id="r", task_id="tk-done", kind="text", title="c", text_md="the report body")
    await arts.finalize_artifact(db_session, artifact=clean, tenant_id="owner", scanned_clean=True)
    quar = LabArtifact(run_id="r", task_id="tk-done", kind="text", title="q", text_md="secret")
    await arts.finalize_artifact(db_session, artifact=quar, tenant_id="owner", scanned_clean=False)
    locked = LabArtifact(run_id="r2", task_id="tk-run", kind="text", title="l", text_md="pending")
    await arts.finalize_artifact(db_session, artifact=locked, tenant_id="owner", scanned_clean=True)
    db_session.add_all([clean, quar, locked])
    await db_session.commit()
    return {"clean": clean.id, "quar": quar.id, "locked": locked.id, "sha": clean.sha256}


def _h(uid):
    return {"Authorization": f"Bearer {create_token(uid)}"}


@pytest.mark.anyio
async def test_download_serves_clean_verified_with_digest(client, dl_env):
    r = await client.get(f"/lab/artifacts/{dl_env['clean']}/download", headers=_h("owner"))
    assert r.status_code == 200
    assert r.text == "the report body"
    assert r.headers["x-content-sha256"] == dl_env["sha"]
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.anyio
async def test_download_blocks_quarantined_content(client, dl_env):
    r = await client.get(f"/lab/artifacts/{dl_env['quar']}/download", headers=_h("owner"))
    assert r.status_code == 409  # not scan-clean+verified — body never leaves


@pytest.mark.anyio
async def test_download_locked_until_task_released(client, dl_env):
    r = await client.get(f"/lab/artifacts/{dl_env['locked']}/download", headers=_h("owner"))
    assert r.status_code == 423  # verified but task not completed


@pytest.mark.anyio
async def test_download_cross_tenant_is_404(client, dl_env):
    r = await client.get(f"/lab/artifacts/{dl_env['clean']}/download", headers=_h("other"))
    assert r.status_code == 404  # anti-probing: not 403


@pytest.mark.anyio
async def test_accepting_v1_result_releases_its_quarantined_text_download(
    client, db_session, monkeypatch
):
    db_session.add(User(id="accept-owner", name="A", email="accept@t.com"))
    task = LabTask(
        id="accept-task",
        issuer_user_id="accept-owner",
        researcher_slug="sage",
        title="accepted report",
        status="review",
        accepted_run_id="accept-run",
    )
    run = LabRun(
        id="accept-run",
        task_id=task.id,
        researcher_slug="sage",
        adapter="codex",
        protocol_version=1,
        status="succeeded",
    )
    artifact = LabArtifact(
        run_id=run.id,
        task_id=task.id,
        kind="text",
        title="Codex report",
        text_md="accepted real report",
    )
    await arts.finalize_artifact(
        db_session,
        artifact=artifact,
        tenant_id="accept-owner",
        scanned_clean=False,
    )
    db_session.add_all([task, run, artifact])
    await db_session.commit()

    async def complete(_db, *, task, **_kwargs):
        task.status = "completed"
        await _db.commit()

    monkeypatch.setattr(
        lab_task_service.lab_terminalization_service,
        "submit_for_caller",
        complete,
    )

    accepted = await client.post(
        "/lab/tasks/accept-task/accept-result", headers=_h("accept-owner")
    )
    assert accepted.status_code == 200, accepted.text
    downloaded = await client.get(
        f"/lab/artifacts/{artifact.id}/download", headers=_h("accept-owner")
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.text == "accepted real report"
