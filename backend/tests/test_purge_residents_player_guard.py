"""线 A hotfix-3 — ``purge_residents`` refuses to touch player characters.

Direct cause: the 2026-07-25 16:53 roster migration bypassed
``find_targets()`` and called ``purge_residents(db, targets)`` with a
hand-built id list. ``find_targets()`` is safe — its very first condition is
``Resident.resident_type != "player"`` (``seed/reset_builtin_residents.py:57``)
— but ``purge_residents`` performed **no type check of its own** and cascaded
across a dozen dependent tables, taking 12 player characters with it.

The guard is a *refusal*, not a silent skip: a caller that asked to delete a
player and got a partial success back would believe the purge completed. It
raises, and nothing is deleted. ``allow_players=True`` is the explicit opt-in
for the rare case that really does mean to remove a player character.
"""
import inspect

import pytest
from sqlalchemy import func, select

from app.models.bulletin_post import BulletinPost
from app.models.commission import Commission
from app.models.conversation import Conversation, Message
from app.models.memory import Memory
from app.models.personality_history import PersonalityHistory
from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal
from app.models.resident_relation import ResidentRelation
from app.models.user import User
from seed.reset_builtin_residents import (
    PlayerPurgeRefused,
    find_targets,
    purge_residents,
)

#: Every table purge_residents cascades into that this test seeds a row for.
#: A refusal must leave every one of them untouched.
CASCADE_TABLES = (
    Message, Conversation, Memory, PersonalityHistory, ResidentGoal,
    ResidentRelation, BulletinPost, Commission, Resident,
)


def _res(slug, rtype, **kw):
    d = dict(slug=slug, name=slug, district="town_hall", status="idle",
             resident_type=rtype, creator_id="sys", tile_x=1, tile_y=1)
    d.update(kw)
    return Resident(**d)


async def _seed_with_dependents(db, resident):
    """Give ``resident`` one row in each cascaded table."""
    db.add(resident)
    await db.flush()
    user = User(name="u", email=f"{resident.slug}@t.com")
    db.add(user)
    await db.flush()
    conv = Conversation(user_id=user.id, resident_id=resident.id)
    db.add(conv)
    await db.flush()
    db.add_all([
        Message(conversation_id=conv.id, role="user", content="hi"),
        Memory(resident_id=resident.id, type="event", content="m", source="observation"),
        PersonalityHistory(resident_id=resident.id, trigger_type="chat",
                           changes_json={}, old_type="AAAA", new_type="BBBB"),
        ResidentGoal(resident_id=resident.id, title="g"),
        ResidentRelation(party_a=resident.id, party_b="other"),
        BulletinPost(author_resident_id=resident.id, kind="journal", title="t"),
        Commission(issuer_resident_id=resident.id, kind="chat_topic", title="c"),
    ])
    await db.commit()
    return resident


async def _table_counts(db):
    return {
        m.__name__: int((await db.execute(select(func.count()).select_from(m))).scalar() or 0)
        for m in CASCADE_TABLES
    }


# ── the refusal semantics ──────────────────────────────────────────────

def test_allow_players_defaults_to_refusing():
    """The safe value must be the *default*; an opt-out that has to be
    remembered is the same failure mode as no guard at all."""
    sig = inspect.signature(purge_residents)
    assert sig.parameters["allow_players"].default is False
    assert sig.parameters["allow_players"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.anyio
async def test_purge_refuses_when_a_player_is_in_the_target_list(db_session):
    npc = await _seed_with_dependents(db_session, _res("builtin-1", "npc"))
    player = await _seed_with_dependents(db_session, _res("avatar-1", "player"))

    with pytest.raises(PlayerPurgeRefused) as exc:
        await purge_residents(db_session, [npc, player])

    # The message must name the offenders — the 07-25 operator had no way to
    # tell which of the ids in their hand-built list were player characters.
    assert "avatar-1" in str(exc.value)
    assert "builtin-1" not in str(exc.value)


@pytest.mark.anyio
async def test_refusal_deletes_nothing_at_all(db_session):
    """Not "deletes fewer rows" — *nothing*. The guard runs before the first
    DELETE, so the co-listed NPC and all of its dependent rows survive too."""
    npc = await _seed_with_dependents(db_session, _res("builtin-1", "npc"))
    player = await _seed_with_dependents(db_session, _res("avatar-1", "player"))
    before = await _table_counts(db_session)

    with pytest.raises(PlayerPurgeRefused):
        await purge_residents(db_session, [npc, player])

    await db_session.rollback()
    assert await _table_counts(db_session) == before
    # both residents still resolvable by slug
    slugs = set((await db_session.execute(select(Resident.slug))).scalars().all())
    assert {"builtin-1", "avatar-1"} <= slugs


@pytest.mark.anyio
async def test_guard_reads_the_database_not_the_passed_objects(db_session):
    """Defence in depth: the 07-25 call site built its own target list, so a
    fabricated ``resident_type`` on the passed object must not be trusted."""
    player = await _seed_with_dependents(db_session, _res("avatar-1", "player"))
    db_session.expunge(player)

    class _FakeTarget:
        id = player.id
        slug = "avatar-1"
        resident_type = "npc"  # a lie

    with pytest.raises(PlayerPurgeRefused):
        await purge_residents(db_session, [_FakeTarget()])
    await db_session.rollback()
    assert (await db_session.execute(
        select(func.count()).select_from(Resident).where(Resident.slug == "avatar-1")
    )).scalar() == 1


# ── the opt-in and the unchanged automatic path ────────────────────────

@pytest.mark.anyio
async def test_allow_players_opt_in_still_works(db_session):
    """A caller that genuinely means to remove a player character can, but has
    to say so at the call site where it is reviewable."""
    player = await _seed_with_dependents(db_session, _res("avatar-1", "player"))

    await purge_residents(db_session, [player], allow_players=True)
    assert (await db_session.execute(
        select(func.count()).select_from(Resident).where(Resident.slug == "avatar-1")
    )).scalar() == 0


@pytest.mark.anyio
async def test_npc_only_purge_is_unaffected(db_session):
    """The guard must not change the normal path: an all-NPC target list still
    cascades exactly as before."""
    npc = await _seed_with_dependents(db_session, _res("builtin-1", "npc"))

    await purge_residents(db_session, [npc])
    counts = await _table_counts(db_session)
    assert counts["Resident"] == 0
    assert counts["Memory"] == 0 and counts["Message"] == 0
    assert counts["BulletinPost"] == 0 and counts["Commission"] == 0


@pytest.mark.anyio
async def test_ugc_residents_are_not_purge_candidates(db_session):
    """Cross-check with hotfix-2: the new ``"resident"`` type satisfies
    ``!= "player"``, so it does not change ``find_targets``' answer — a UGC
    resident is still excluded because its creator is a real user, not
    SYSTEM_USER_ID, and its slug is not a legacy built-in."""
    from app.services.civic_membership import UGC_RESIDENT_TYPE

    db_session.add_all([
        _res("ugc-1", UGC_RESIDENT_TYPE, creator_id="real-user-id"),
        _res("avatar-1", "player", creator_id="real-user-id"),
    ])
    await db_session.commit()
    assert await find_targets(db_session) == []


@pytest.mark.anyio
async def test_find_targets_never_returns_a_player(db_session):
    """seed/reset_builtin_residents.py:57 — the automatic path was already
    safe. Pin it so the guard below is defence in depth, not the only defence."""
    from seed.preset_characters import SYSTEM_USER_ID

    db_session.add_all([
        _res("isabella", "npc", creator_id=SYSTEM_USER_ID),   # legacy built-in
        _res("avatar-1", "player", creator_id=SYSTEM_USER_ID),
    ])
    await db_session.commit()
    targets = await find_targets(db_session)
    assert {t.slug for t in targets} == {"isabella"}
