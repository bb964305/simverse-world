import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.resident import Resident


async def _seed_system_user(db_session):
    db_session.add(User(
        id="00000000-0000-0000-0000-000000000001",
        name="System",
        email="system@skills.world",
        soul_coin_balance=0,
    ))
    await db_session.commit()


@pytest.mark.anyio
async def test_seed_presets_creates_residents(db_session):
    """Seeding presets should create the 10-person town cast."""
    from seed.preset_characters import seed_presets

    await _seed_system_user(db_session)
    count = await seed_presets(db_session)
    assert count == 11

    # Verify 林晚秋 (cafe owner, anchor character) exists with correct data
    result = await db_session.execute(
        select(Resident).where(Resident.slug == "lin-wanqiu")
    )
    wanqiu = result.scalar_one_or_none()
    assert wanqiu is not None
    assert wanqiu.name == "林晚秋"
    assert wanqiu.resident_type == "npc"
    assert "心智模型" in wanqiu.ability_md or "能力" in wanqiu.ability_md
    assert wanqiu.meta_json["origin"] == "preset"
    assert wanqiu.meta_json["is_preset"] is True
    assert wanqiu.star_rating == 3
    assert wanqiu.sprite_key == "伊莎贝拉"
    assert wanqiu.district == "cafe"

    # The lab researcher carries the admin whitelist flag for RESEARCH.
    result = await db_session.execute(
        select(Resident).where(Resident.slug == "jiang-lin")
    )
    jianglin = result.scalar_one_or_none()
    assert jianglin is not None
    assert jianglin.district == "experiment_building"
    assert jianglin.meta_json["lab"]["access"] is True


@pytest.mark.anyio
async def test_seed_presets_is_idempotent(db_session):
    """Running seed twice should not duplicate residents."""
    from seed.preset_characters import seed_presets

    await _seed_system_user(db_session)
    count1 = await seed_presets(db_session)
    count2 = await seed_presets(db_session)
    assert count1 == 11
    assert count2 == 0  # no new residents on second run


@pytest.mark.anyio
async def test_seed_presets_seeds_social_graph(db_session):
    """The cast ships with two-axis relations, mirrored relationship memories
    and life goals — and re-running the seed does not duplicate them."""
    from seed.preset_characters import (
        PRESET_RELATIONS, PRESET_GOALS, seed_presets,
    )
    from app.models.resident_relation import ResidentRelation
    from app.models.memory import Memory
    from app.models.resident_goal import ResidentGoal
    from app.services import relation_service

    await _seed_system_user(db_session)
    await seed_presets(db_session)

    relations = (await db_session.execute(select(ResidentRelation))).scalars().all()
    assert len(relations) == len(PRESET_RELATIONS)
    for rel in relations:
        assert 0.0 <= rel.familiarity <= 1.0
        assert -1.0 <= rel.affinity <= 1.0
        assert rel.interact_count > 0

    # 陈铁生 ↔ 阿岚: high familiarity, strained affinity (family tension)
    ids = {
        r.slug: r.id
        for r in (await db_session.execute(select(Resident))).scalars().all()
    }
    pair = await relation_service.get_pair(db_session, ids["chen-tiesheng"], ids["a-lan"])
    assert pair is not None
    assert pair.familiarity == pytest.approx(0.90)
    assert pair.affinity == pytest.approx(0.30)

    # 何巧云 ↔ 赵启文: the town's one negative-affinity feud
    feud = await relation_service.get_pair(db_session, ids["he-qiaoyun"], ids["zhao-qiwen"])
    assert feud is not None
    assert feud.affinity < 0

    # Mirrored relationship memories: one per direction per pair
    rel_memories = (await db_session.execute(
        select(Memory).where(Memory.type == "relationship")
    )).scalars().all()
    assert len(rel_memories) == 2 * len(PRESET_RELATIONS)

    # Every resident got a life goal
    life_goals = (await db_session.execute(
        select(ResidentGoal).where(ResidentGoal.status == "active", ResidentGoal.kind == "life")
    )).scalars().all()
    assert len(life_goals) == len(PRESET_GOALS) == 11

    # Idempotency across the whole social fabric
    await seed_presets(db_session)
    assert len((await db_session.execute(select(ResidentRelation))).scalars().all()) == len(PRESET_RELATIONS)
    assert len((await db_session.execute(
        select(Memory).where(Memory.type == "relationship")
    )).scalars().all()) == 2 * len(PRESET_RELATIONS)
    assert len((await db_session.execute(
        select(ResidentGoal).where(ResidentGoal.status == "active", ResidentGoal.kind == "life")
    )).scalars().all()) == 11
