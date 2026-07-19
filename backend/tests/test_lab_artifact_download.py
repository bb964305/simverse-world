"""Phase 5 (recovery plan) — authenticated, digest-checking artifact download
boundary. Content is served ONLY through this seam and ONLY for an artifact that
is ACL-owned, digest-intact, scan-clean+verified, and whose task is released.
"""
import pytest

from app.models.lab_artifact import LabArtifact
from app.models.lab_task import LabTask
from app.models.user import User
from app.services import lab_artifact_service as arts
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
