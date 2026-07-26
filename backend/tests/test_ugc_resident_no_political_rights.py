"""线 A hotfix-2 — a player-authored resident inhabits the town but does not
govern it.

The leak: five ``Resident(...)`` construction sites never set
``resident_type``, so they fell through to the model default ``"npc"``
(``app/models/resident.py:52``) and every UGC character silently acquired
active *and* passive suffrage. The 2026-07-25 audit found real evidence — the
14 civic voters included 「夜风侦探」×3 and 「部署回归图灵0724」.

The five paths (all write ``creator_id`` + ``meta_json['origin']`` and never
touch ``users.player_resident_id``, i.e. they are *creations*, not avatars):

  app/forge/pipeline.py:155         forge, current main path
  app/forge/legacy_pipeline.py:147  legacy forge full
  app/forge/legacy_pipeline.py:298  legacy forge quick
  app/routers/residents.py:179      POST /residents/import-card
  app/routers/residents.py:276      POST /residents/import

The fix types them ``"resident"`` — deliberately *not* ``"player"``, because
``"player"`` is the single-FK avatar type (``users.player_resident_id``,
``onboarding_service.py``) and is the sentinel of a third, untouched predicate
family (``!= "player"``). Typing UGC characters ``"player"`` would drop them
off the world map (``agent/map_data.py:475``), make them purge candidates
(``seed/reset_builtin_residents.py:57``) and hand their creator home-decor
rights (``routers/home_decor.py:56``). Any value that is neither ``"player"``
nor ``"npc"`` removes political rights without disturbing that family — that
is the load-bearing property of this fix.
"""
import ast
import io
import pathlib

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.models.user import User
from app.services.auth_service import create_token
from app.services.civic_membership import (
    CIVIC_VOTER_TYPES,
    SIM_RESIDENT_TYPES,
    UGC_RESIDENT_TYPE,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── the membership decision ────────────────────────────────────────────

def test_ugc_type_has_no_political_rights():
    assert UGC_RESIDENT_TYPE not in CIVIC_VOTER_TYPES


def test_ugc_type_is_part_of_the_world_population():
    """The other half of the fix: the type must stay inside the population set
    or duty lookups, the town-hall roster, the mayor sweeps and the burn-in
    probes all silently drop these residents."""
    assert UGC_RESIDENT_TYPE in SIM_RESIDENT_TYPES


def test_ugc_type_is_neither_player_nor_npc():
    """The load-bearing property: distinct from ``"npc"`` (so no ballot) and
    distinct from ``"player"`` (so the ``!= "player"`` family is untouched)."""
    assert UGC_RESIDENT_TYPE not in ("player", "npc")


# ── structural: every construction site is explicit ────────────────────

def test_every_resident_construction_sets_resident_type_explicitly():
    """The root cause was an *omission*, so guard the omission, not one site.

    Relying on the model default is what handed UGC characters the ballot in
    the first place; a new construction site that forgets the keyword must fail
    here rather than a year later in an election audit.
    """
    offenders = []
    for sub in ("app", "seed"):
        for path in (BACKEND_ROOT / sub).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Name) and fn.id == "Resident"):
                    continue
                if not any(kw.arg == "resident_type" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
    assert offenders == [], (
        "Resident(...) must set resident_type explicitly — falling through to "
        f"the model default 'npc' grants political rights: {offenders}"
    )


def test_legacy_forge_paths_type_their_residents_as_ugc():
    """legacy_pipeline's two sites run full LLM stages and are not reachable
    from a unit test, so pin them at the source level instead of pretending
    they are covered."""
    src = (BACKEND_ROOT / "app" / "forge" / "legacy_pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "Resident"
    ]
    assert len(sites) == 2, "legacy forge has a full path and a quick path"
    for node in sites:
        kw = next(k for k in node.keywords if k.arg == "resident_type")
        assert isinstance(kw.value, ast.Name) and kw.value.id == "UGC_RESIDENT_TYPE", (
            f"legacy_pipeline.py:{node.lineno} must use the shared constant"
        )


# ── functional: the three reachable creation paths ─────────────────────

async def _make_user(db, email):
    u = User(name="创作者", email=email, soul_coin_balance=100)
    db.add(u)
    await db.commit()
    return u


@pytest.fixture
def _offline_sbti():
    """compute_sbti's client raises → the service's own fail-open path. Keeps
    the creation paths fully offline without changing what they write."""
    from unittest.mock import AsyncMock, MagicMock, patch

    c = MagicMock()
    c.messages.create = AsyncMock(side_effect=RuntimeError("offline test"))
    with patch("app.services.sbti_service.get_client", return_value=c):
        yield


@pytest.mark.anyio
async def test_import_card_path_types_resident_as_ugc(client, db_session, _offline_sbti):
    """app/routers/residents.py:179 — POST /residents/import-card."""
    owner = await _make_user(db_session, "card-ugc@t.com")
    resp = await client.post(
        "/residents/import-card",
        json={"name": "夜风侦探", "ability_md": "# 能力", "persona_md": "# 人格",
              "soul_md": "# 灵魂"},
        headers={"Authorization": f"Bearer {create_token(owner.id)}"},
    )
    assert resp.status_code == 200, resp.text
    r = (await db_session.execute(
        select(Resident).where(Resident.slug == resp.json()["slug"])
    )).scalar_one()
    assert r.resident_type == UGC_RESIDENT_TYPE
    assert (r.meta_json or {}).get("origin") == "import"
    # not an avatar: the single-FK player pointer is untouched
    assert (await db_session.get(User, owner.id)).player_resident_id is None


@pytest.mark.anyio
async def test_import_file_path_types_resident_as_ugc(client, db_session, _offline_sbti):
    """app/routers/residents.py:276 — POST /residents/import."""
    owner = await _make_user(db_session, "file-ugc@t.com")
    skill_md = (
        "# Ability\n## Professional\n- Backend engineering expert, 10 years\n"
        "- Distributed systems and high availability\n\n"
        "# Persona\n## Layer 0: Core\n- Methodical, calm under pressure\n\n"
        "# Soul\n## Values\n- Reliability over speed always\n"
    )
    resp = await client.post(
        "/residents/import",
        headers={"Authorization": f"Bearer {create_token(owner.id)}"},
        files={"file": ("SKILL.md", io.BytesIO(skill_md.encode()), "text/markdown")},
        data={"name": "部署回归图灵0724", "slug": "deploy-turing-0724"},
    )
    assert resp.status_code == 200, resp.text
    r = (await db_session.execute(
        select(Resident).where(Resident.slug == "deploy-turing-0724")
    )).scalar_one()
    assert r.resident_type == UGC_RESIDENT_TYPE
    assert (await db_session.get(User, owner.id)).player_resident_id is None


@pytest.mark.anyio
async def test_forge_main_path_types_resident_as_ugc(db_session, _offline_sbti):
    """app/forge/pipeline.py:155 — the current forge main path, i.e. the
    flow that produced the leaked voters found in the 07-25 audit."""
    from app.forge.pipeline import ForgePipeline
    from app.models.forge_session import ForgeSession

    owner = await _make_user(db_session, "forge-ugc@t.com")
    fs = ForgeSession(
        user_id=owner.id, character_name="锻造居民", mode="quick", status="building",
        build_output={"ability_md": "# 能力", "persona_md": "# 人格", "soul_md": "# 灵魂"},
    )
    db_session.add(fs)
    await db_session.commit()

    pipeline = ForgePipeline(db_session, system_client=None, user_client=None)
    await pipeline._create_resident(fs)
    await db_session.commit()

    r = (await db_session.execute(
        select(Resident).where(Resident.creator_id == owner.id)
    )).scalar_one()
    assert r.resident_type == UGC_RESIDENT_TYPE
    assert (r.meta_json or {}).get("origin") == "forge"
    assert (await db_session.get(User, owner.id)).player_resident_id is None


# ── the two consequences, on a real UGC resident ───────────────────────

def _ugc(slug, **kw):
    d = dict(slug=slug, name=slug, district="town_hall", status="idle",
             resident_type=UGC_RESIDENT_TYPE, creator_id="u1",
             tile_x=119, tile_y=53, meta_json={"origin": "forge"})
    d.update(kw)
    return Resident(**d)


def _npc(slug, **kw):
    d = dict(slug=slug, name=slug, district="town_hall", status="idle",
             resident_type="npc", creator_id="sys", tile_x=119, tile_y=53,
             meta_json={"origin": "preset"})
    d.update(kw)
    return Resident(**d)


@pytest.mark.anyio
async def test_ugc_resident_cannot_vote_and_is_out_of_the_quorum(db_session):
    """A class: civic_service.py:153 ballot + :527 quorum denominator."""
    from app.services import civic_service

    db_session.add_all([_npc("builtin-1"), _ugc("ugc-1"), _ugc("ugc-2")])
    await db_session.commit()

    poll = await civic_service.propose(
        db_session, "广场要不要加长椅",
        [{"label": "支持", "effect": {"type": "narrative", "event": {"title": "x"}}},
         {"label": "维持现状", "effect": None}],
    )
    cast = await civic_service.run_npc_voting(db_session)
    await db_session.refresh(poll)

    assert cast == 1, "only the built-in NPC votes"
    assert set(poll.options_json[0].get("_npc_voters", [])) == {"builtin-1"}
    assert await civic_service._eligible_voter_count(db_session) == 1


@pytest.mark.anyio
async def test_ugc_resident_cannot_stand_for_mayor(db_session):
    """A class: election_service.py:40 candidate pool (passive suffrage)."""
    from app.services import election_service

    db_session.add_all([
        _npc("builtin-1", meta_json={"sbti": {"dimensions": {"Ac1": "H"}}}),
        _npc("builtin-2", meta_json={"sbti": {"dimensions": {"Ac1": "H"}}}),
        _ugc("ugc-1", meta_json={"sbti": {"dimensions": {"Ac1": "H"}}}),
        _ugc("ugc-2", meta_json={"sbti": {"dimensions": {"Ac1": "H"}}}),
    ])
    await db_session.commit()

    poll = await election_service.open_election(db_session)
    assert poll is not None
    assert {o["effect"]["slug"] for o in poll.options_json} == {"builtin-1", "builtin-2"}


@pytest.mark.anyio
async def test_ugc_resident_stays_on_the_townhall_roster(db_session):
    """B class: routers/townhall.py:51. Losing the ballot must not remove them
    from the world — that would be the regression a single shared constant
    would have caused."""
    from app.routers import townhall

    db_session.add_all([_npc("builtin-1"), _ugc("ugc-1")])
    await db_session.commit()
    rows = await townhall._npc_residents(db_session)
    assert {r.slug for r in rows} == {"builtin-1", "ugc-1"}


@pytest.mark.anyio
async def test_ugc_resident_can_still_hold_a_duty(db_session):
    """B class: duty_service.py:105. A UGC duty holder that fails to resolve
    silently stops working and stops being paid."""
    from app.services import duty_service

    db_session.add(_ugc("ugc-1", meta_json={"duty": {"key": "postman"}}))
    await db_session.commit()
    found = await duty_service.find_duty_resident(db_session, "postman")
    assert found is not None and found.slug == "ugc-1"


@pytest.mark.anyio
async def test_ugc_resident_stale_mayor_flag_is_still_swept(db_session):
    """B class: election_service.py:133. If the sweep skipped UGC residents a
    stale ``meta_json['mayor']`` would never clear and the town would keep two
    mayors — one of them collecting the wage bonus."""
    from app.services import election_service

    db_session.add_all([
        _npc("builtin-1"),
        _ugc("ugc-1", meta_json={"origin": "forge", "mayor": True}),
    ])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "builtin-1") is True
    rows = (await db_session.execute(select(Resident))).scalars().all()
    assert {r.slug for r in rows if (r.meta_json or {}).get("mayor")} == {"builtin-1"}


# ── admin UI label ─────────────────────────────────────────────────────

def test_admin_list_labels_ugc_resident_as_npc():
    """``routers/admin/residents.py:35`` hard-codes the NPC/Player split. A UGC
    resident is not the user's avatar, so mislabelling it "Player" in the admin
    panel is a real reporting bug shipped alongside the type change."""
    from app.routers.admin.residents import _resident_to_dict

    assert _resident_to_dict(_ugc("ugc-1"))["type"] == "NPC"
    assert _resident_to_dict(_npc("builtin-1"))["type"] == "NPC"
    avatar = _npc("avatar-1")
    avatar.resident_type = "player"
    assert _resident_to_dict(avatar)["type"] == "Player"


# ── master-only: the two predicates on the model ───────────────────────
#
# The branch this test came from predates PR #8, which introduced the
# ``Resident.is_autonomous`` hybrid and routed all ten reads through it —
# including five *new* ones in ``app/agent/loop.py`` that did not exist at
# 999e098. Collapsing both boundaries onto that single hybrid is exactly the
# regression ``civic_membership`` exists to prevent, so the split has to be
# expressed on the model, not only in the frozensets.

def test_is_autonomous_means_population_not_political_rights():
    """``is_autonomous`` drives the simulation (agent loop, roster, duties), so
    a UGC resident must satisfy it — otherwise every player-authored character
    freezes the moment it stops being typed ``"npc"``."""
    assert _ugc("ugc-1").is_autonomous is True
    assert _npc("builtin-1").is_autonomous is True


def test_is_civic_voter_is_the_narrow_political_predicate():
    assert _ugc("ugc-1").is_civic_voter is False
    assert _npc("builtin-1").is_civic_voter is True


@pytest.mark.anyio
async def test_both_predicates_work_as_sql_expressions(db_session):
    """Every call site queries in SQL, so the hybrids' expression halves — not
    just the Python halves — decide who votes and who exists."""
    db_session.add_all([_npc("builtin-1"), _ugc("ugc-1")])
    await db_session.commit()

    population = (await db_session.execute(
        select(Resident).where(Resident.is_autonomous)
    )).scalars().all()
    voters = (await db_session.execute(
        select(Resident).where(Resident.is_civic_voter)
    )).scalars().all()

    assert {r.slug for r in population} == {"builtin-1", "ugc-1"}
    assert {r.slug for r in voters} == {"builtin-1"}


def test_agent_loop_selects_on_the_population_predicate():
    """``app/agent/loop.py`` must never narrow to the civic predicate: that
    would stop ticking, metabolizing and waking UGC residents."""
    src = (BACKEND_ROOT / "app" / "agent" / "loop.py").read_text(encoding="utf-8")
    assert "is_civic_voter" not in src, (
        "the agent loop drives the world population, not the electorate"
    )
    assert "is_autonomous" in src
