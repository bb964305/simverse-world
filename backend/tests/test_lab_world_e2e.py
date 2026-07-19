"""T0.1 — World-segment end-to-end (test-spec V13/V15 world段).

Where ``test_world_revision.py`` exercises the revision *services* directly by
calling ``proposal_service.create_proposal`` with an already-shaped patch, this
file drives the whole governance pipeline through its real entry point — the
Proposal **Compiler** — so it covers the flow a lab run / resident actually
takes:

    draft → validate → Compiler → preflight(admin preview) → admin approve →
    one immutable revision (before-state captured) → revert (before-state
    restored) → a second apply pinned to a now-stale base is rejected.

Negative paths (V13 fixture): an *unverified* draft (out-of-scope kind,
out-of-whitelist field, bad text) never compiles — no proposal row is ever
authored — and a stale ``base_world_revision`` is rejected with no side
effects.

Fixture mirrors ``test_world_revision.py::rev_env`` /
``test_world_governance.py::gov_env``: same LOCATIONS/_dynamic_slugs/_dynamic
_lore snapshot-restore, the same ``app.database.async_session`` patch (so the
``reload_world`` triggered inside apply/revert reads the test engine), and the
same ``proposal_service.emit`` + ``manager.broadcast`` mocks.
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.agent import location_lore, map_data
from app.lab import compiler
from app.models.dynamic_location import DynamicLocation
from app.models.world_change_proposal import WorldChangeProposal
from app.models.world_revision import WorldRevision
from app.services import location_tracker
from app.services import proposal_service as psvc
from app.services import world_revision_service as wrsvc

OBSERVATORY_DATA = {
    "name": "天文台", "type": "public", "role": "research",
    "bounds": [5, 88, 15, 96], "center": [10, 92], "entrance": [10, 88],
    "description": "观测旧貌",
}


@pytest.fixture
def world_e2e_env(db_engine, monkeypatch):
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


def _envelopes(broadcast_mock, action):
    out = []
    for call in broadcast_mock.call_args_list:
        data = call.args[0] if call.args else call.kwargs.get("data")
        if isinstance(data, dict) and data.get("action") == action:
            out.append(data)
    return out


# ── headline E2E: edit_location draft all the way through revert ───────────

@pytest.mark.anyio
async def test_world_flow_draft_to_revert_restores_before_state(world_e2e_env):
    factory, broadcast_mock = world_e2e_env
    await _seed_observatory(factory)

    # 1+2. draft → validate → Compiler. The compiler is the only door a draft
    # may take into the world; a *dynamic-description* edit is in v1 scope.
    draft = {
        "kind": "edit_location",
        "patch": {"location_id": "observatory", "fields": {"description": "观测新貌"}},
        "title": "更新天文台描述",
        "rationale": "研究员观测后的新记录",
    }
    async with factory() as db:
        proposal = await compiler.compile_draft(
            db, draft=draft, origin_ref="lab-run-1", author_slug="sage",
        )
        pid = proposal.id

    # 3. preflight — the admin-facing preview. What the reviewer sees is exactly
    # what will be executed: pending, low risk, canonical slug+data patch.
    async with factory() as db:
        p = await db.get(WorldChangeProposal, pid)
        preview = psvc.serialize(p)
    assert preview["status"] == "pending"
    assert preview["risk_level"] == "low"
    assert preview["kind"] == "edit_location"
    assert preview["patch"] == {"slug": "observatory", "data": {"description": "观测新貌"}}
    executed_patch = preview["patch"]

    # 4. admin approve → applies and records exactly one immutable revision,
    # capturing the before/after image around the overlay write.
    async with factory() as db:
        p = await psvc.approve_proposal(db, pid, "admin1", "看起来不错")
        assert p.status == "applied"

    async with factory() as db:
        revs = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid)
        )).scalars().all()
        assert len(revs) == 1
        rev = revs[0]
        assert rev.status == "applied"
        assert rev.change_kind == "edit_location"
        assert rev.location_slug == "observatory"
        assert rev.before_state_json["description"] == "观测旧貌"
        assert rev.after_state_json["description"] == "观测新貌"
        # the executed change matches the preflight preview
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == "observatory")
        )).scalar_one()
        assert row.data_json["description"] == executed_patch["data"]["description"]

    # canonical world_changed 'applied' envelope broadcast exactly once
    applied = _envelopes(broadcast_mock, "applied")
    assert len(applied) == 1
    assert applied[0]["type"] == "world_changed"
    assert applied[0]["change_kind"] == "edit_location"
    assert applied[0]["world_revision_id"] == rev.id

    # 5. revert → before-state restored, revision flipped to reverted.
    async with factory() as db:
        p = await psvc.revert_proposal(db, pid, "admin1")
        assert p.status == "reverted"

    async with factory() as db:
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == "observatory")
        )).scalar_one()
        assert row.data_json["description"] == "观测旧貌"  # before-state restored
        rev = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid)
        )).scalar_one()
        assert rev.status == "reverted"

    reverted = _envelopes(broadcast_mock, "reverted")
    assert len(reverted) == 1
    assert reverted[0]["world_revision_id"] == rev.id
    assert reverted[0]["seq"] > applied[0]["seq"]
    assert reverted[0]["event_id"] != applied[0]["event_id"]


# ── add_lore variant through the Compiler, revert to static fallback ───────

@pytest.mark.anyio
async def test_add_lore_flow_via_compiler_revert_to_static(world_e2e_env):
    factory, _broadcast = world_e2e_env
    draft = {
        "kind": "add_lore",
        "patch": {"location_id": "academy", "text": "学院深处藏着一段被遗忘的传说"},
        "title": "学院传说",
        "rationale": "研究员的发现",
    }
    async with factory() as db:
        p = await compiler.compile_draft(db, draft=draft, origin_ref="lab-run-2", author_slug="sage")
        pid = p.id
        assert p.status == "pending"
        assert p.risk_level == "low"

    async with factory() as db:
        p = await psvc.approve_proposal(db, pid, "admin1", "ok")
        assert p.status == "applied"
    assert location_lore.lore_for("academy") == "学院深处藏着一段被遗忘的传说"

    async with factory() as db:
        rev = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid)
        )).scalar_one()
        assert rev.change_kind == "add_lore"
        assert rev.before_state_json is None  # first-time lore, no pre-image

    async with factory() as db:
        p = await psvc.revert_proposal(db, pid, "admin1")
        assert p.status == "reverted"
    # falls back to the code-owned static blurb, not the reverted overlay text
    assert location_lore.lore_for("academy") == location_lore.LORE["academy"]


# ── negative: an unverified draft never compiles (no proposal is authored) ──

@pytest.mark.anyio
async def test_unverified_draft_never_compiles(world_e2e_env):
    factory, _broadcast = world_e2e_env
    await _seed_observatory(factory)

    bad_drafts = [
        # out-of-scope kind (add_location is not a v1 governance kind)
        {"kind": "add_location", "patch": {"slug": "x", "data": {}}, "title": "x", "rationale": "y"},
        # out-of-whitelist field (bounds cannot be edited in v1)
        {"kind": "edit_location",
         "patch": {"location_id": "observatory", "fields": {"bounds": [0, 0, 1, 1]}},
         "title": "x", "rationale": "y"},
        # empty text
        {"kind": "add_lore", "patch": {"location_id": "academy", "text": ""},
         "title": "x", "rationale": "y"},
        # over-long text
        {"kind": "add_lore", "patch": {"location_id": "academy", "text": "x" * 2001},
         "title": "x", "rationale": "y"},
        # unknown location target
        {"kind": "add_lore", "patch": {"location_id": "nowhere", "text": "hi"},
         "title": "x", "rationale": "y"},
    ]
    for draft in bad_drafts:
        async with factory() as db:
            with pytest.raises(compiler.CompileError):
                await compiler.compile_draft(
                    db, draft=draft, origin_ref="lab-run-bad", author_slug="sage",
                )

    # no proposal row and no revision row was ever authored
    async with factory() as db:
        n_props = (await db.execute(select(func.count()).select_from(WorldChangeProposal))).scalar_one()
        n_revs = (await db.execute(select(func.count()).select_from(WorldRevision))).scalar_one()
    assert n_props == 0
    assert n_revs == 0


# ── negative: second apply against a now-stale base is rejected, no effect ──

@pytest.mark.anyio
async def test_second_apply_stale_base_conflict_rejected(world_e2e_env):
    factory, _broadcast = world_e2e_env
    await _seed_observatory(factory, description="A")

    # first change A→B lands and becomes "current"
    async with factory() as db:
        p0 = await compiler.compile_draft(
            db, draft={"kind": "edit_location",
                       "patch": {"location_id": "observatory", "fields": {"description": "B"}},
                       "title": "A->B", "rationale": "..."},
            origin_ref="lab-run-0", author_slug="sage",
        )
        pid0 = p0.id
    async with factory() as db:
        await psvc.approve_proposal(db, pid0, "admin1", "")

    async with factory() as db:
        base_at_fork = await wrsvc.current_revision_id(db, "observatory")
    assert base_at_fork is not None

    # p1 forks off B intending B→C, pinned to the revision current at fork time
    async with factory() as db:
        p1 = await compiler.compile_draft(
            db, draft={"kind": "edit_location",
                       "patch": {"location_id": "observatory", "fields": {"description": "C"}},
                       "base_world_revision": base_at_fork,
                       "title": "B->C", "rationale": "..."},
            origin_ref="lab-run-1", author_slug="sage",
        )
        pid1 = p1.id
        # compiler forwards the pinned base (draft top-level) into the proposal patch
        assert p1.patch_json.get("base_world_revision") == base_at_fork

    # p2 sneaks in first B→D and moves "current" out from under p1
    async with factory() as db:
        p2 = await compiler.compile_draft(
            db, draft={"kind": "edit_location",
                       "patch": {"location_id": "observatory", "fields": {"description": "D"}},
                       "title": "B->D", "rationale": "..."},
            origin_ref="lab-run-2", author_slug="sage",
        )
        pid2 = p2.id
    async with factory() as db:
        await psvc.approve_proposal(db, pid2, "admin1", "")

    # approving p1 now must be rejected as stale — with no side effects
    async with factory() as db:
        with pytest.raises(psvc.ProposalError, match="stale"):
            await psvc.approve_proposal(db, pid1, "admin1", "")

    async with factory() as db:
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == "observatory")
        )).scalar_one()
        assert row.data_json["description"] == "D"  # untouched by the rejected p1
        revs = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid1)
        )).scalars().all()
        assert revs == []  # no revision authored for the rejected apply
        p1_row = await db.get(WorldChangeProposal, pid1)
        assert p1_row.status == "failed"
