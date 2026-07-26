import hashlib
import io
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.resident_sprite_run import ResidentSpriteRun
from app.models.user import User
from app.schemas.resident_sprite import CHECKLIST_KEYS
from app.services.auth_service import create_token
from app.services.resident_sprite_generation import ResidentSpriteRequest

PUBLISH_RUN = uuid.UUID("22222222-2222-4222-8222-222222222222").hex
FAILED_RUN = uuid.UUID("33333333-3333-4333-8333-333333333333").hex
NEW_RUN = uuid.UUID("44444444-4444-4444-8444-444444444444").hex
ESCAPE_RUN = uuid.UUID("55555555-5555-4555-8555-555555555555").hex
BAD_QC_RUN = uuid.UUID("66666666-6666-4666-8666-666666666666").hex


@pytest.fixture(autouse=True)
def _enable_resident_sprite_feature(monkeypatch):
    monkeypatch.setattr(settings, "resident_sprite_enabled", True)


def _png(size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    if size == (96, 128):
        draw = ImageDraw.Draw(image)
        for row in range(4):
            for column in range(3):
                x = column * 32
                y = row * 32
                draw.rectangle((x + 10, y + 5, x + 21, y + 28), fill=(20, 40, 60, 255))
    else:
        ImageDraw.Draw(image).rectangle((8, 8, size[0] - 9, size[1] - 9), fill=(20, 40, 60, 255))
    image.save(output, "PNG")
    return output.getvalue()


async def _actors(db):
    admin = User(name="sprite-admin", email="sprite-admin@test.com", is_admin=True, is_banned=False)
    user = User(name="sprite-user", email="sprite-user@test.com", is_admin=False, is_banned=False)
    db.add_all([admin, user])
    await db.flush()
    resident = Resident(slug="sprite-resident", name="Sprite Resident", creator_id=user.id)
    db.add(resident)
    await db.commit()
    return admin, user, resident


def _headers(user):
    return {"Authorization": f"Bearer {create_token(user.id)}"}


def _generation_request(resident: Resident) -> dict:
    return ResidentSpriteRequest(
        asset_key="sprite-resident", display_name=resident.name,
        appearance="A resident wearing a practical town outfit", gender="neutral",
        age_group="adult", vibe="friendly", tags=["town"], model="gpt-image-2",
    ).model_dump(mode="json")


def _candidate_files(tmp_path: Path, run_id: str = "run-1"):
    run_dir = tmp_path / run_id / "candidate"
    run_dir.mkdir(parents=True)
    texture = run_dir / "texture.png"
    portrait = run_dir / "portrait.png"
    texture.write_bytes(_png((96, 128)))
    portrait.write_bytes(_png((64, 64)))
    return texture, portrait


@pytest.mark.anyio
async def test_sprite_routes_require_admin(client, db_session):
    admin, user, resident = await _actors(db_session)
    payload = {"resident_id": resident.id}
    assert (await client.post("/admin/resident-sprites", json=payload)).status_code == 401
    assert (await client.post(
        "/admin/resident-sprites", json=payload, headers=_headers(user)
    )).status_code == 403
    assert (await client.post(
        "/admin/resident-sprites", json=payload, headers=_headers(admin)
    )).status_code == 201


@pytest.mark.anyio
async def test_sprite_routes_are_hidden_when_feature_is_disabled(
    client, db_session, monkeypatch
):
    admin, _, resident = await _actors(db_session)
    monkeypatch.setattr(settings, "resident_sprite_enabled", False)

    response = await client.post(
        "/admin/resident-sprites",
        json={"resident_id": resident.id},
        headers=_headers(admin),
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_review_approval_state_machine_and_list(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(tmp_path))
    admin, _, resident = await _actors(db_session)
    resident_id = resident.id
    headers = _headers(admin)
    created = await client.post("/admin/resident-sprites", headers=headers, json={
        "resident_id": resident.id,
    })
    assert created.status_code == 201
    assert created.json()["status"] == "requested"
    run_id = created.json()["run_id"]
    texture, portrait = _candidate_files(tmp_path, run_id)
    persisted = await db_session.scalar(
        select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == run_id)
    )
    persisted.status = "candidate_ready"
    persisted.candidate_texture_path = str(texture)
    persisted.candidate_portrait_path = str(portrait)
    await db_session.commit()
    detail = await client.get(f"/admin/resident-sprites/{run_id}", headers=headers)
    assert detail.json()["candidate_texture_url"].endswith(f"/{run_id}/candidate/texture")

    premature = await client.post(
        f"/admin/resident-sprites/{run_id}/publish", headers=headers, json={"expected_version": 1}
    )
    assert premature.status_code == 409
    assert premature.json()["detail"]["code"] == "NOT_APPROVED"

    checklist = {key: True for key in CHECKLIST_KEYS}
    reviewed = await client.put(f"/admin/resident-sprites/{run_id}/review", headers=headers, json={
        "expected_version": 1, "evidence": {"phaser": "passed"},
        "checklist": checklist, "notes": "reviewed",
    })
    assert reviewed.status_code == 200
    approved = await client.post(
        f"/admin/resident-sprites/{run_id}/approve", headers=headers, json={"expected_version": 2}
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    stale = await client.post(
        f"/admin/resident-sprites/{run_id}/reject", headers=headers,
        json={"expected_version": 2, "reason": "stale"},
    )
    assert stale.status_code == 409
    listing = await client.get("/admin/resident-sprites", headers=headers)
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["version"] == 3


@pytest.mark.anyio
async def test_publish_updates_public_resident_and_broadcasts(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "static_dir", str(tmp_path / "static"))
    admin, _, resident = await _actors(db_session)
    texture, portrait = _candidate_files(tmp_path / "work", PUBLISH_RUN)
    texture_sha = hashlib.sha256(texture.read_bytes()).hexdigest()
    headers = _headers(admin)
    db_session.add(ResidentSpriteRun(
        resident_id=resident.id, run_id=PUBLISH_RUN, status="approved",
        generation_request_json=_generation_request(resident),
        candidate_texture_path=str(texture), candidate_portrait_path=str(portrait),
        candidate_texture_sha256=texture_sha,
        review_checklist_json={key: True for key in CHECKLIST_KEYS},
    ))
    await db_session.commit()

    broadcast = AsyncMock()
    monkeypatch.setattr("app.routers.admin.resident_sprites.manager.broadcast", broadcast)
    response = await client.post(
        f"/admin/resident-sprites/{PUBLISH_RUN}/publish", headers=headers,
        json={"expected_version": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"
    public = (await client.get("/residents")).json()
    item = next(row for row in public if row["id"] == resident.id)
    assert item["sprite_url"].endswith("/texture.png")
    assert f"/{texture_sha}/" not in item["sprite_url"]  # directory also binds the portrait hash
    assert item["sprite_content_hash"] == texture_sha
    assert item["sprite_generation_run_id"] == PUBLISH_RUN
    assert (tmp_path / "static" / item["sprite_url"].removeprefix("/static/")).is_file()
    broadcast.assert_awaited_once()
    assert broadcast.await_args.args[0]["type"] == "sprite_updated"
    assert broadcast.await_args.args[0]["resident_id"] == resident.id


@pytest.mark.anyio
async def test_publish_file_failure_leaves_database_unchanged(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "static_dir", str(tmp_path / "static"))
    admin, _, resident = await _actors(db_session)
    resident_id = resident.id
    texture, portrait = _candidate_files(tmp_path / "work", FAILED_RUN)
    run = ResidentSpriteRun(
        resident_id=resident.id, run_id=FAILED_RUN, status="approved",
        generation_request_json=_generation_request(resident),
        candidate_texture_path=str(texture), candidate_portrait_path=str(portrait),
    )
    db_session.add(run)
    await db_session.commit()
    real_write = __import__("app.services.resident_sprite_publish_service", fromlist=["_atomic_write"])._atomic_write
    calls = 0

    def fail_second(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        real_write(path, data)

    monkeypatch.setattr("app.services.resident_sprite_publish_service._atomic_write", fail_second)
    response = await client.post(
        f"/admin/resident-sprites/{FAILED_RUN}/publish", headers=_headers(admin),
        json={"expected_version": 1},
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PUBLICATION_IO_FAILED"
    public_root = tmp_path / "static" / "resident-sprites" / resident_id
    assert not public_root.exists() or not [p for p in public_root.iterdir() if not p.name.startswith(".")]
    db_session.expire_all()
    persisted = await db_session.scalar(select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == FAILED_RUN))
    saved_resident = await db_session.get(Resident, resident_id)
    assert persisted.status == "approved" and persisted.version == 1
    assert saved_resident.sprite_url is None and saved_resident.sprite_generation_run_id is None


@pytest.mark.anyio
async def test_rollback_restores_previous_sprite(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "static_dir", str(tmp_path / "static"))
    admin, _, resident = await _actors(db_session)
    resident_id = resident.id
    resident.sprite_url = "/static/old/texture.png"
    resident.portrait_url = "/static/old/portrait.png"
    resident.sprite_content_hash = "a" * 64
    resident.sprite_generation_run_id = "old-run"
    texture, portrait = _candidate_files(tmp_path / "work", NEW_RUN)
    db_session.add(ResidentSpriteRun(
        resident_id=resident.id, run_id=NEW_RUN, status="approved",
        generation_request_json=_generation_request(resident),
        candidate_texture_path=str(texture), candidate_portrait_path=str(portrait),
    ))
    await db_session.commit()
    headers = _headers(admin)
    broadcast = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    monkeypatch.setattr("app.routers.admin.resident_sprites.manager.broadcast", broadcast)
    assert (await client.post(
        f"/admin/resident-sprites/{NEW_RUN}/publish", headers=headers, json={"expected_version": 1}
    )).status_code == 200
    rolled_back = await client.post(
        f"/admin/resident-sprites/{NEW_RUN}/rollback", headers=headers,
        json={"expected_version": 2, "reason": "visual regression"},
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["status"] == "rolled_back"
    db_session.expire_all()
    saved = await db_session.get(Resident, resident_id)
    assert saved.sprite_url == "/static/old/texture.png"
    assert saved.sprite_generation_run_id == "old-run"
    assert broadcast.await_count == 2
    assert broadcast.await_args_list[-1].args[0]["run_id"] == "old-run"


@pytest.mark.anyio
async def test_create_rejects_worker_owned_candidate_fields(client, db_session, tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png((96, 128)))
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(root))
    admin, _, resident = await _actors(db_session)
    response = await client.post("/admin/resident-sprites", headers=_headers(admin), json={
        "resident_id": resident.id,
        "candidate_texture_path": str(outside), "candidate_portrait_path": str(outside),
        "run_id": ESCAPE_RUN, "model": "attacker-model", "request_count": 99,
    })
    assert response.status_code == 422
    forbidden = {item["loc"][-1] for item in response.json()["detail"]}
    assert {"run_id", "model", "candidate_texture_path", "request_count"} <= forbidden


@pytest.mark.anyio
async def test_progress_rejects_worker_fields_and_resumes_expired_sqlite_lease(
    client, db_session
):
    admin, _, resident = await _actors(db_session)
    run = ResidentSpriteRun(
        resident_id=resident.id, run_id=ESCAPE_RUN, status="generating",
        generation_request_json=_generation_request(resident), lease_owner="dead-worker",
        # SQLite returns this as a naive datetime; the API must compare in SQL.
        lease_expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
    )
    db_session.add(run)
    await db_session.commit()
    headers = _headers(admin)
    forbidden = await client.post(
        f"/admin/resident-sprites/{ESCAPE_RUN}/progress", headers=headers,
        json={"action": "resume", "expected_version": 1, "candidate_texture_path": "/tmp/x"},
    )
    assert forbidden.status_code == 422
    resumed = await client.post(
        f"/admin/resident-sprites/{ESCAPE_RUN}/progress", headers=headers,
        json={"action": "resume", "expected_version": 1},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "requested"
    assert resumed.json()["lease_owner"] is None


@pytest.mark.anyio
async def test_publish_rejects_atlas_that_fails_automatic_qc(client, db_session, tmp_path, monkeypatch):
    root = tmp_path / "work"
    monkeypatch.setattr(settings, "resident_sprite_artifact_dir", str(root))
    monkeypatch.setattr(settings, "static_dir", str(tmp_path / "static"))
    admin, _, resident = await _actors(db_session)
    texture, portrait = _candidate_files(root, BAD_QC_RUN)
    Image.new("RGBA", (96, 128), (20, 40, 60, 255)).save(texture, "PNG")
    db_session.add(ResidentSpriteRun(
        resident_id=resident.id, run_id=BAD_QC_RUN, status="approved",
        generation_request_json=_generation_request(resident),
        candidate_texture_path=str(texture), candidate_portrait_path=str(portrait),
    ))
    await db_session.commit()
    response = await client.post(
        f"/admin/resident-sprites/{BAD_QC_RUN}/publish", headers=_headers(admin),
        json={"expected_version": 1},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "AUTOMATED_QC_FAILED"
