"""T6 — World Governor v1: revisioned apply/revert + before-state capture +
Proposal Compiler (spec §Governance Plane, 美术规格 §World Changed v1).

Fixture mirrors test_world_governance.py::gov_env: same LOCATIONS/_dynamic_slugs
snapshot-restore, same async_session patch, same proposal_service.emit mock (so
``_on_proposal_applied``'s resident-memory side effect never fires and never
needs its own session patch).
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.agent import location_lore, map_data
from app.lab import compiler
from app.models.dynamic_location import DynamicLocation
from app.models.world_revision import WorldRevision
from app.services import location_tracker
from app.services import proposal_service as psvc
from app.services import world_revision_service as wrsvc

OBSERVATORY_DATA = {
    "name": "天文台", "type": "public", "role": "research",
    "bounds": [5, 88, 15, 96], "center": [10, 92], "entrance": [10, 88],
    "description": "旧描述",
}


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


async def _seed_observatory(factory, **overrides):
    data = dict(OBSERVATORY_DATA)
    data.update(overrides)
    async with factory() as db:
        db.add(DynamicLocation(slug="observatory", data_json=data, active=True, proposal_id=None))
        await db.commit()
    map_data.LOCATIONS["observatory"] = dict(data)
    map_data._dynamic_slugs.add("observatory")
    location_tracker.rebuild_lookup()


# ── scenario 3 (core regression) ───────────────────────────────────────
# edit_location mutates DynamicLocation.data_json in place with no pre-image;
# revert currently only flips active=False on rows matching proposal_id (and
# _apply_edit_location never re-attributes proposal_id, so even that no-op
# soft-delete doesn't fire) — the data_json edit is never undone. This test
# must be RED against current (pre-T6) code.

@pytest.mark.anyio
async def test_edit_location_revert_restores_before_state(rev_env):
    factory, _broadcast_mock = rev_env
    await _seed_observatory(factory)

    async with factory() as db:
        p = await psvc.create_proposal(
            db, kind="edit_location", title="改描述", rationale="...",
            patch={"slug": "observatory", "data": {"description": "新描述"}},
            author_slug="sage", cost_sc=0,
        )
        pid = p.id

    async with factory() as db:
        p = await psvc.approve_proposal(db, pid, "admin1", "ok")
        assert p.status == "applied"

    async with factory() as db:
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == "observatory")
        )).scalar_one()
        assert row.data_json["description"] == "新描述"

    async with factory() as db:
        p = await psvc.revert_proposal(db, pid, "admin1")
        assert p.status == "reverted"

    async with factory() as db:
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == "observatory")
        )).scalar_one()
        assert row.data_json["description"] == "旧描述"


# ── scenario 1: add_lore apply records a revision ──────────────────────

@pytest.mark.anyio
async def test_add_lore_apply_records_revision(rev_env):
    factory, _broadcast_mock = rev_env
    async with factory() as db:
        p = await psvc.create_proposal(
            db, kind="add_lore", title="加个传说", rationale="研究员的发现",
            patch={"location_id": "academy", "text": "学院深处藏着秘密"},
            author_slug="sage", cost_sc=0,
        )
        pid = p.id

    async with factory() as db:
        p = await psvc.approve_proposal(db, pid, "admin1", "ok")
        assert p.status == "applied"

    async with factory() as db:
        rev = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid)
        )).scalar_one()
        assert rev.status == "applied"
        assert rev.change_kind == "add_lore"
        assert rev.before_state_json is None  # first-time lore, no pre-image
        assert rev.after_state_json == {"location_id": "academy", "text": "学院深处藏着秘密"}

    assert location_lore.lore_for("academy") == "学院深处藏着秘密"


# ── scenario 2: add_lore revert falls back to the static blurb ─────────

@pytest.mark.anyio
async def test_add_lore_revert_restores_static_fallback(rev_env):
    factory, _broadcast_mock = rev_env
    async with factory() as db:
        p = await psvc.create_proposal(
            db, kind="add_lore", title="加个传说", rationale="...",
            patch={"location_id": "academy", "text": "新传说"},
            author_slug="sage", cost_sc=0,
        )
        pid = p.id
    async with factory() as db:
        await psvc.approve_proposal(db, pid, "admin1", "ok")
    assert location_lore.lore_for("academy") == "新传说"

    async with factory() as db:
        p = await psvc.revert_proposal(db, pid, "admin1")
        assert p.status == "reverted"

    async with factory() as db:
        rev = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid)
        )).scalar_one()
        assert rev.status == "reverted"
    assert location_lore.lore_for("academy") == location_lore.LORE["academy"]


# ── scenario 4: two stacked edits, revert the second → intermediate state ──

@pytest.mark.anyio
async def test_stacked_edit_location_revert_restores_intermediate_state(rev_env):
    factory, _broadcast_mock = rev_env
    await _seed_observatory(factory, description="A")

    async with factory() as db:
        p1 = await psvc.create_proposal(
            db, kind="edit_location", title="A->B", rationale="...",
            patch={"slug": "observatory", "data": {"description": "B"}},
            author_slug="sage", cost_sc=0,
        )
        pid1 = p1.id
    async with factory() as db:
        p1 = await psvc.approve_proposal(db, pid1, "admin1", "")
        assert p1.status == "applied"

    async with factory() as db:
        p2 = await psvc.create_proposal(
            db, kind="edit_location", title="B->C", rationale="...",
            patch={"slug": "observatory", "data": {"description": "C"}},
            author_slug="sage", cost_sc=0,
        )
        pid2 = p2.id
    async with factory() as db:
        p2 = await psvc.approve_proposal(db, pid2, "admin1", "")
        assert p2.status == "applied"

    async with factory() as db:
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == "observatory")
        )).scalar_one()
        assert row.data_json["description"] == "C"

    async with factory() as db:
        p = await psvc.revert_proposal(db, pid2, "admin1")
        assert p.status == "reverted"

    async with factory() as db:
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == "observatory")
        )).scalar_one()
        assert row.data_json["description"] == "B"

    async with factory() as db:
        rev1 = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid1)
        )).scalar_one()
        assert rev1.status == "applied"  # untouched by reverting the 2nd edit
        rev2 = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid2)
        )).scalar_one()
        assert rev2.status == "reverted"
        assert rev2.before_state_json["description"] == "B"


# ── scenario 5: stale base_world_revision rejected, no side effects ────
# p1 forks off B intending B->C, pinned to the revision current at fork time.
# p2 sneaks in first and applies B->D, moving "current" out from under p1.

@pytest.mark.anyio
async def test_stale_base_revision_rejected(rev_env):
    factory, _broadcast_mock = rev_env
    await _seed_observatory(factory, description="A")

    async with factory() as db:
        p0 = await psvc.create_proposal(
            db, kind="edit_location", title="A->B", rationale="...",
            patch={"slug": "observatory", "data": {"description": "B"}},
            author_slug="sage", cost_sc=0,
        )
        pid0 = p0.id
    async with factory() as db:
        await psvc.approve_proposal(db, pid0, "admin1", "")

    async with factory() as db:
        base_at_fork = await wrsvc.current_revision_id(db, "observatory")
    assert base_at_fork is not None

    async with factory() as db:
        p1 = await psvc.create_proposal(
            db, kind="edit_location", title="B->C", rationale="...",
            patch={
                "slug": "observatory", "data": {"description": "C"},
                "base_world_revision": base_at_fork,
            },
            author_slug="sage", cost_sc=0,
        )
        pid1 = p1.id

    async with factory() as db:
        p2 = await psvc.create_proposal(
            db, kind="edit_location", title="B->D", rationale="...",
            patch={"slug": "observatory", "data": {"description": "D"}},
            author_slug="sage", cost_sc=0,
        )
        pid2 = p2.id
    async with factory() as db:
        await psvc.approve_proposal(db, pid2, "admin1", "")

    async with factory() as db:
        with pytest.raises(psvc.ProposalError, match="stale"):
            await psvc.approve_proposal(db, pid1, "admin1", "")

    async with factory() as db:
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == "observatory")
        )).scalar_one()
        assert row.data_json["description"] == "D"  # untouched by the rejected p1

    async with factory() as db:
        revs = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid1)
        )).scalars().all()
        assert revs == []
        p1_row = await db.get(psvc.WorldChangeProposal, pid1)
        assert p1_row.status == "failed"


# ── scenario 6: Compiler validation ─────────────────────────────────────

@pytest.mark.anyio
async def test_compiler_rejects_out_of_scope_kind(rev_env):
    factory, _broadcast_mock = rev_env
    async with factory() as db:
        with pytest.raises(compiler.CompileError):
            await compiler.compile_draft(
                db, draft={"kind": "add_location", "patch": {}, "title": "x", "rationale": "y"},
                origin_ref="run1", author_slug="sage", tenant_id="sage",
            )


@pytest.mark.anyio
async def test_compiler_rejects_out_of_whitelist_field(rev_env):
    factory, _broadcast_mock = rev_env
    await _seed_observatory(factory)
    async with factory() as db:
        with pytest.raises(compiler.CompileError):
            await compiler.compile_draft(
                db, draft={
                    "kind": "edit_location",
                    "patch": {"location_id": "observatory", "fields": {"bounds": [0, 0, 1, 1]}},
                    "title": "x", "rationale": "y",
                },
                origin_ref="run1", author_slug="sage", tenant_id="sage",
            )


@pytest.mark.anyio
async def test_compiler_rejects_bad_text_length(rev_env):
    factory, _broadcast_mock = rev_env
    async with factory() as db:
        with pytest.raises(compiler.CompileError):
            await compiler.compile_draft(
                db, draft={"kind": "add_lore", "patch": {"location_id": "academy", "text": ""},
                          "title": "x", "rationale": "y"},
                origin_ref="run1", author_slug="sage", tenant_id="sage",
            )
        with pytest.raises(compiler.CompileError):
            await compiler.compile_draft(
                db, draft={"kind": "add_lore", "patch": {"location_id": "academy", "text": "x" * 2001},
                          "title": "x", "rationale": "y"},
                origin_ref="run1", author_slug="sage", tenant_id="sage",
            )


@pytest.mark.anyio
async def test_compiler_accepts_valid_draft(rev_env):
    factory, _broadcast_mock = rev_env
    async with factory() as db:
        p = await compiler.compile_draft(
            db, draft={"kind": "add_lore", "patch": {"location_id": "academy", "text": "新传说"},
                      "title": "探索发现", "rationale": "研究员的发现"},
            origin_ref="run1", author_slug="sage", tenant_id="sage",
        )
        assert p.status == "pending"
        assert p.risk_level == "low"
        assert p.kind == "add_lore"
        assert p.patch_json == {"location_id": "academy", "text": "新传说"}


# ── scenario 7: canonical world_changed envelope, applied then reverted ──

@pytest.mark.anyio
async def test_world_changed_envelope_applied_then_reverted(rev_env):
    factory, broadcast_mock = rev_env
    async with factory() as db:
        p = await psvc.create_proposal(
            db, kind="add_lore", title="加个传说", rationale="...",
            patch={"location_id": "academy", "text": "新传说"},
            author_slug="sage", cost_sc=0,
        )
        pid = p.id
    async with factory() as db:
        await psvc.approve_proposal(db, pid, "admin1", "")

    def _payloads(action):
        out = []
        for call in broadcast_mock.call_args_list:
            data = call.args[0] if call.args else call.kwargs.get("data")
            if isinstance(data, dict) and data.get("action") == action:
                out.append(data)
        return out

    applied = _payloads("applied")
    assert len(applied) == 1
    envelope = applied[0]
    frozen_fields = (
        "type", "schema_version", "event_id", "tenant_id", "seq",
        "world_revision_id", "proposal_id", "location_slug", "action",
        "change_kind", "bounds", "occurred_at",
    )
    for field in frozen_fields:
        assert field in envelope, f"missing frozen field: {field}"
    assert envelope["type"] == "world_changed"
    assert envelope["change_kind"] == "add_lore"
    assert envelope["proposal_id"] == pid
    assert isinstance(envelope["seq"], int)
    applied_seq, applied_event_id = envelope["seq"], envelope["event_id"]

    async with factory() as db:
        await psvc.revert_proposal(db, pid, "admin1")

    reverted = _payloads("reverted")
    assert len(reverted) == 1
    reverted_envelope = reverted[0]
    assert reverted_envelope["seq"] > applied_seq
    assert reverted_envelope["event_id"] != applied_event_id
    assert reverted_envelope["world_revision_id"] == envelope["world_revision_id"]


# ── scenario 8: existing test_world_governance.py regression is a separate
# file, run unmodified — see task-6-report.md verification section.
