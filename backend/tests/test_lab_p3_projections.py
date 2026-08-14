"""T5 — P3 backend projections: world-snapshot revision anchor (V22) and the
artifact manifest fields (V-artifact / spec §5.3).

The approval projection (allowed_actions/can_decide/decision_scope/status),
cursor steps-after-seq, and artifact digest verification already landed in
P1/P2 (test_lab_approval_flow / test_lab_tenant_acl / test_lab_artifacts).
These tests cover the two remaining P3 gaps:

- ``GET /world/locations`` returns a ``world_revision_id`` + ``source_cursor``
  anchor so main map / minimap / Codex converge on ONE revision, and the cursor
  equals the durable ``world_changed`` seq the WS envelope carries;
- ``serialize_artifact`` exposes the manifest fields (producer, provenance,
  byte_size) alongside scan/verification/retention.
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.agent import location_lore, map_data
from app.models.lab_artifact import LabArtifact
from app.services import location_tracker
from app.services import lab_task_service as svc
from app.services import proposal_service as psvc
from app.services import world_revision_service as wrsvc


# ── artifact manifest fields (no DB needed) ────────────────────────────────

def test_artifact_manifest_fields_present():
    art = LabArtifact(
        id="a-1", run_id="r-1", task_id="t-1", kind="file", title="report.md",
        sha256="deadbeef", byte_size=1234, producer_action_id="act-9",
        provenance="verifier", scan_status="clean", verification_status="verified",
        retention_hold=True, storage_status="legacy",
    )
    locked = svc.serialize_artifact(art, unlocked=False)
    # manifest metadata is always present (even locked) — additive, read-only
    for field in ("kind", "producer_action_id", "provenance", "byte_size",
                  "scan_status", "verification_status", "retention_hold", "sha256"):
        assert field in locked, f"missing manifest field {field}"
    assert locked["producer_action_id"] == "act-9"
    assert locked["provenance"] == "verifier"
    assert locked["byte_size"] == 1234
    assert locked["verification_status"] == "verified"
    assert locked["retention_hold"] is True
    # Artifact projections are metadata-only in both states. Unlocking permits
    # the separate authenticated /download endpoint to release the body.
    assert "uri" not in locked and "text_md" not in locked
    unlocked = svc.serialize_artifact(art, unlocked=True)
    assert unlocked["unlocked"] is True
    assert "uri" not in unlocked and "text_md" not in unlocked


# ── world snapshot revision anchor ─────────────────────────────────────────

@pytest.fixture
def rev_env(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    import app.database as database
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr("app.services.proposal_service.emit", AsyncMock())
    broadcast_mock = AsyncMock()
    monkeypatch.setattr("app.ws.manager.manager.broadcast", broadcast_mock)
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    snap_lore = dict(location_lore._dynamic_lore)
    yield factory, broadcast_mock
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn
    location_lore._dynamic_lore = snap_lore
    location_tracker.rebuild_lookup()


@pytest.mark.anyio
async def test_source_cursor_starts_zero_and_tracks_world_changed_seq(rev_env):
    factory, broadcast_mock = rev_env

    async with factory() as db:
        assert await wrsvc.current_source_cursor(db) == 0
        assert await wrsvc.current_revision_id(db) is None

    async with factory() as db:
        p = await psvc.create_proposal(
            db, kind="add_lore", title="lore", rationale="...",
            patch={"location_id": "academy", "text": "秘密"},
            author_slug="sage", cost_sc=0,
        )
        pid = p.id
    async with factory() as db:
        await psvc.approve_proposal(db, pid, "admin1", "")

    # the applied envelope's seq is exactly the source_cursor now
    applied = None
    for call in broadcast_mock.call_args_list:
        data = call.args[0] if call.args else call.kwargs.get("data")
        if isinstance(data, dict) and data.get("action") == "applied":
            applied = data
    assert applied is not None
    async with factory() as db:
        cursor = await wrsvc.current_source_cursor(db)
        rev_id = await wrsvc.current_revision_id(db)
    assert cursor == applied["seq"]
    assert rev_id == applied["world_revision_id"]


@pytest.mark.anyio
async def test_world_locations_returns_revision_anchor(client):
    resp = await client.get("/world/locations")
    assert resp.status_code == 200
    body = resp.json()
    assert "locations" in body and isinstance(body["locations"], list)
    # anchor keys are always present; empty-history world → None + 0
    assert "world_revision_id" in body
    assert "source_cursor" in body
    assert body["source_cursor"] == 0
    assert body["world_revision_id"] is None
