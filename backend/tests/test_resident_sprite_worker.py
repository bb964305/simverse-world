import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.resident import Resident
from app.models.resident_sprite_run import ResidentSpriteRun
from app.models.user import User
from app.schemas.resident_sprite import ResidentSpriteProgressRequest
from app.services.resident_sprite_generation import (
    CapabilityContract,
    CapabilityReceipt,
    QCFinding,
    ResidentSpriteRequest,
    ResidentSpriteRunResult,
)
from app.services.resident_sprite_publish_service import update_progress
from app.tasks import resident_sprite_worker as worker


@pytest.fixture(autouse=True)
def _enable_resident_sprite_feature(monkeypatch):
    monkeypatch.setattr(settings, "resident_sprite_enabled", True)


def _run_id(number: int) -> str:
    return uuid.UUID(f"{number:08x}-0000-4000-8000-000000000000").hex


def _request(model: str = "gpt-image-2") -> ResidentSpriteRequest:
    return ResidentSpriteRequest(
        asset_key="worker-resident", display_name="Worker Resident",
        appearance="Short dark hair and a green practical jacket", gender="neutral",
        age_group="adult", vibe="focused", tags=["engineering"], model=model,
    )


def test_cost_upper_bound_is_unknown_or_rounded_up():
    assert worker.estimate_cost_upper_bound(7, 0.0) is None
    assert worker.estimate_cost_upper_bound(1, 0.00000001) == 0.000001


def _run(resident_id: str, number: int, *, status: str = "requested", attempts: int = 0, **kwargs):
    request = _request()
    return ResidentSpriteRun(
        resident_id=resident_id, run_id=_run_id(number), status=status,
        direction_policy=request.direction_policy,
        generation_request_json=request.model_dump(mode="json"), attempts=attempts,
        **kwargs,
    )


async def _resident(db: AsyncSession) -> Resident:
    user = User(name="worker-owner", email=f"worker-{uuid.uuid4().hex}@test.com")
    db.add(user)
    await db.flush()
    resident = Resident(
        slug=f"worker-{uuid.uuid4().hex[:8]}", name="Worker Resident", creator_id=user.id,
        district="engineering", persona_md="Short dark hair and a green jacket",
        ability_md="Repairs mechanical devices", resident_type="npc",
    )
    db.add(resident)
    await db.commit()
    return resident


def _factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.anyio
async def test_admin_create_persists_deterministic_request_snapshot(client, db_session):
    from app.services.auth_service import create_token

    admin = User(name="snapshot-admin", email="snapshot-admin@test.com", is_admin=True)
    db_session.add(admin)
    await db_session.flush()
    resident = Resident(
        slug="snapshot-resident", name="Snapshot Resident", creator_id=admin.id,
        district="cafe", persona_md="Silver hair, round glasses, navy apron",
        ability_md="Makes careful pour-over coffee", resident_type="npc",
    )
    db_session.add(resident)
    await db_session.commit()
    response = await client.post(
        "/admin/resident-sprites",
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
        json={
            "resident_id": resident.id, "appearance": "Copper hair and a blue work coat",
            "gender": "female", "age_group": "young", "vibe": "inventive",
            "tags": ["maker", "night-shift"],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "requested"
    assert body["attempts"] == 0 and body["lease_owner"] is None
    snapshot = body["generation_request_json"]
    assert snapshot["appearance"] == "Copper hair and a blue work coat"
    assert snapshot["gender"] == "female" and snapshot["age_group"] == "young"
    assert snapshot["tags"] == ["maker", "night-shift"]
    assert snapshot["model"] == settings.resident_sprite_provider_model
    assert len(body["run_id"]) == 32
    forbidden = await client.post(
        "/admin/resident-sprites",
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
        json={"resident_id": resident.id, "run_id": _run_id(99), "model": "other"},
    )
    assert forbidden.status_code == 422


@pytest.mark.anyio
async def test_two_workers_only_one_claims(db_engine, db_session):
    resident = await _resident(db_session)
    run = _run(resident.id, 1)
    db_session.add(run)
    await db_session.commit()
    factory = _factory(db_engine)
    now = datetime.now(UTC)

    async def claim(owner):
        async with factory() as db:
            return await worker.claim_next_run(
                db, owner=owner, now=now, lease_seconds=600
            )

    claims = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert sum(item is not None for item in claims) == 1
    db_session.expire_all()
    saved = await db_session.scalar(select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == _run_id(1)))
    assert saved.status == "generating" and saved.attempts == 1
    assert saved.lease_owner in {"worker-a", "worker-b"}


@pytest.mark.anyio
async def test_expired_lease_is_recovered(db_session):
    resident = await _resident(db_session)
    now = datetime.now(UTC)
    run = _run(
        resident.id, 2, status="generating", attempts=1,
        lease_owner="dead-worker", lease_expires_at=now - timedelta(seconds=1),
    )
    db_session.add(run)
    await db_session.commit()
    claim = await worker.claim_next_run(
        db_session, owner="recovery-worker", now=now, lease_seconds=600
    )
    assert claim is not None
    assert claim.previous_status == "generating" and claim.attempts == 2
    assert (await db_session.get(ResidentSpriteRun, run.id)).lease_owner == "recovery-worker"


@pytest.mark.anyio
async def test_missing_provider_configuration_fails_without_secret(db_engine, db_session, monkeypatch):
    resident = await _resident(db_session)
    run = _run(resident.id, 3)
    db_session.add(run)
    await db_session.commit()
    monkeypatch.setattr(settings, "resident_sprite_provider_base_url", "")
    monkeypatch.setattr(settings, "resident_sprite_provider_api_key", "")
    monkeypatch.setattr(settings, "resident_sprite_capability_receipt", "")
    assert await worker.process_one(
        session_factory=_factory(db_engine), owner="missing-config", lease_seconds=600
    )
    db_session.expire_all()
    saved = await db_session.scalar(select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == _run_id(3)))
    assert saved.status == "failed"
    assert saved.error_code == "SPRITE_PROVIDER_NOT_CONFIGURED"
    assert saved.lease_owner is None and saved.attempts == 1
    assert "api" not in (saved.error_message or "").lower()


@pytest.mark.anyio
async def test_worker_success_syncs_candidate_manifest_and_hashes(
    db_engine, db_session, tmp_path, monkeypatch
):
    resident = await _resident(db_session)
    run = _run(resident.id, 4)
    db_session.add(run)
    await db_session.commit()
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(tmp_path))
    monkeypatch.setattr(settings, "resident_sprite_request_cost_upper_bound_usd", 0.125)
    monkeypatch.setattr(
        worker, "load_run",
        lambda root, run_id: SimpleNamespace(
            request_budget=SimpleNamespace(submitted_image_request_count=7)
        ),
    )

    async def pipeline(request, **kwargs):
        assert request == _request()
        assert kwargs["retry_failed"] is False
        candidate = tmp_path / kwargs["run_id"] / "candidate"
        candidate.mkdir(parents=True)
        (candidate / "texture.png").write_bytes(b"texture-bytes")
        (candidate / "portrait.png").write_bytes(b"portrait-bytes")
        manifest = tmp_path / kwargs["run_id"] / "manifest.json"
        manifest.write_text("{}")
        return ResidentSpriteRunResult(
            run_id=kwargs["run_id"], state="auto_qc_passed", manifest_path=str(manifest)
        )

    runtime = lambda request: (SimpleNamespace(), SimpleNamespace(), "a" * 64)
    assert await worker.process_one(
        session_factory=_factory(db_engine), owner="success-worker", lease_seconds=600,
        runtime_factory=runtime, pipeline=pipeline,
    )
    db_session.expire_all()
    saved = await db_session.scalar(select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == _run_id(4)))
    assert saved.status == "candidate_ready" and saved.request_count == 7
    assert saved.estimated_cost_usd == pytest.approx(0.875)
    assert saved.capability_receipt_id == "a" * 64
    assert saved.candidate_texture_sha256 == hashlib.sha256(b"texture-bytes").hexdigest()
    assert saved.candidate_portrait_sha256 == hashlib.sha256(b"portrait-bytes").hexdigest()
    assert saved.lease_owner is None and saved.error_code is None


@pytest.mark.anyio
async def test_failed_run_retry_is_queued_and_pipeline_receives_retry_flag(
    db_engine, db_session, tmp_path, monkeypatch
):
    resident = await _resident(db_session)
    run = _run(resident.id, 5, status="failed", attempts=1, error_code="TEMP", error_message="temporary")
    db_session.add(run)
    await db_session.commit()
    retried = await update_progress(
        db_session, run,
        ResidentSpriteProgressRequest(action="retry", expected_version=1),
    )
    assert retried.status == "retrying"
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(tmp_path))
    monkeypatch.setattr(
        worker, "load_run",
        lambda root, run_id: SimpleNamespace(
            request_budget=SimpleNamespace(submitted_image_request_count=4)
        ),
    )

    async def pipeline(request, **kwargs):
        assert kwargs["retry_failed"] is True
        candidate = tmp_path / kwargs["run_id"] / "candidate"
        candidate.mkdir(parents=True)
        (candidate / "texture.png").write_bytes(b"texture")
        (candidate / "portrait.png").write_bytes(b"portrait")
        return ResidentSpriteRunResult(
            run_id=kwargs["run_id"], state="auto_qc_passed",
            manifest_path=str(tmp_path / kwargs["run_id"] / "manifest.json"),
        )

    assert await worker.process_one(
        session_factory=_factory(db_engine), owner="retry-worker", lease_seconds=600,
        runtime_factory=lambda request: (SimpleNamespace(), SimpleNamespace(), "b" * 64),
        pipeline=pipeline,
    )
    db_session.expire_all()
    saved = await db_session.scalar(select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == _run_id(5)))
    assert saved.status == "candidate_ready" and saved.attempts == 2


def test_runtime_rejects_provider_origin_not_in_receipt():
    contract = CapabilityContract(
        normalized_origin="https://qualified.example:443/v1",
        model_alias="gpt-image-2", multipart_field="image[]",
    )
    receipt = CapabilityReceipt.model_construct(
        **contract.model_dump(), receipt_id="a" * 64,
        qualified_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    with pytest.raises(worker.SpriteWorkerError) as exc:
        worker.validate_provider_binding(
            worker.ProviderConfig(
                base_url="https://other.example/v1", api_key="secret", model="gpt-image-2"
            ),
            receipt,
            _request(),
        )
    assert exc.value.code == "PROVIDER_ORIGIN_MISMATCH"


@pytest.mark.anyio
async def test_quarantined_candidate_is_not_downgraded_to_retryable_failure(
    db_engine, db_session, tmp_path, monkeypatch
):
    resident = await _resident(db_session)
    run = _run(resident.id, 6)
    db_session.add(run)
    await db_session.commit()
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(tmp_path))
    monkeypatch.setattr(settings, "resident_sprite_request_cost_upper_bound_usd", 0.0)
    monkeypatch.setattr(
        worker, "load_run",
        lambda root, run_id: SimpleNamespace(
            request_budget=SimpleNamespace(submitted_image_request_count=4)
        ),
    )

    async def pipeline(request, **kwargs):
        return ResidentSpriteRunResult(
            run_id=kwargs["run_id"], state="quarantined",
            manifest_path=str(tmp_path / kwargs["run_id"] / "manifest.json"),
            qc_findings=[QCFinding(code="FRAME_EMPTY", detail="one frame is empty")],
        )

    assert await worker.process_one(
        session_factory=_factory(db_engine), owner="quarantine-worker", lease_seconds=600,
        runtime_factory=lambda request: (SimpleNamespace(), SimpleNamespace(), "c" * 64),
        pipeline=pipeline,
    )
    db_session.expire_all()
    saved = await db_session.scalar(
        select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == _run_id(6))
    )
    assert saved.status == "quarantined" and saved.error_code == "FRAME_EMPTY"
    assert saved.request_count == 4 and saved.estimated_cost_usd is None
    replacement = await update_progress(
        db_session, saved,
        ResidentSpriteProgressRequest(action="retry", expected_version=saved.version),
    )
    assert replacement.status == "retrying"
    assert replacement.run_id != saved.run_id
    assert replacement.retry_of_run_id == saved.run_id
    assert replacement.generation_request_json == saved.generation_request_json
    original = await db_session.scalar(
        select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == _run_id(6))
    )
    assert original.status == "retry_spawned"
