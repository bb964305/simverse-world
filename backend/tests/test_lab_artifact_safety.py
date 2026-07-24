"""Phase 5 (recovery plan), gap #10 — no unverified artifact body/URI leaves the API.

Content (text_md / uri) is released ONLY when the task is released AND the
artifact is scan-clean AND verified. A skipped/unverified/flagged artifact keeps
its body and remote URL server-quarantined even after task completion, and the
authenticated read path refuses it. Mock/legacy artifacts are trusted synthetic
content, so they are marked clean+verified at finalize and release normally.
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.lab_artifact import LabArtifact
from app.models.lab_task import LabTask
from app.models.user import User
from app.services import lab_artifact_service as arts
from app.services import lab_task_service as svc


def _art(**over) -> LabArtifact:
    base = dict(kind="text", title="t", text_md="secret body", uri="https://evil.example/x",
                scan_status="skipped", verification_status="unverified")
    base.update(over)
    return LabArtifact(**base)


def test_serialize_withholds_unverified_content_even_when_released():
    a = _art()  # skipped / unverified
    view = svc.serialize_artifact(a, True)  # task released
    assert view["unlocked"] is False            # not releasable
    assert "text_md" not in view and "uri" not in view  # body + remote URL withheld


def test_serialize_releases_clean_verified_content():
    # A legacy (trusted-synthetic) artifact that is clean + verified is releasable.
    a = _art(storage_status="legacy", scan_status="clean", verification_status="verified")
    view = svc.serialize_artifact(a, True)
    assert view["unlocked"] is True
    # Metadata-only contract (ADR-lab-artifact-storage): a releasable artifact
    # flips ``unlocked`` True, but body/URI still leave ONLY through /download —
    # they are never inlined into the metadata projection.
    assert "text_md" not in view and "uri" not in view


def test_serialize_withholds_flagged_content():
    a = _art(scan_status="flagged", verification_status="verified")
    assert svc.serialize_artifact(a, True)["unlocked"] is False


@pytest.fixture
def factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.anyio
async def test_finalize_scanned_clean_sets_clean_verified(factory):
    async with factory() as db:
        a = _art()
        await arts.finalize_artifact(db, artifact=a, tenant_id="t1", scanned_clean=True)
        assert a.scan_status == "clean" and a.verification_status == "verified"
        b = _art()
        await arts.finalize_artifact(db, artifact=b, tenant_id="t1", scanned_clean=False)
        assert b.scan_status == "skipped" and b.verification_status == "unverified"  # stays quarantined


@pytest.mark.anyio
async def test_verify_and_get_blocks_quarantined_content(factory):
    async with factory() as db:
        db.add(User(id="owner", name="O", email="o@t.com", soul_coin_balance=0))
        task = LabTask(id="tk1", issuer_user_id="owner", title="x", status="completed")
        db.add(task)
        # Finalized real-adapter artifact left quarantined (scanned_clean=False)...
        quarantined = _art(id="a-q", task_id="tk1", run_id="r1")
        await arts.finalize_artifact(db, artifact=quarantined, tenant_id="owner", scanned_clean=False)
        # ...vs a scan-clean + verified one.
        clean = _art(id="a-c", task_id="tk1", run_id="r1")
        await arts.finalize_artifact(db, artifact=clean, tenant_id="owner", scanned_clean=True)
        db.add(quarantined)
        db.add(clean)
        await db.commit()

    async with factory() as db:
        with pytest.raises(arts.ArtifactQuarantined):
            await arts.verify_and_get(db, artifact_id="a-q", user_id="owner", is_admin=False)
        got = await arts.verify_and_get(db, artifact_id="a-c", user_id="owner", is_admin=False)
        assert got.id == "a-c"
