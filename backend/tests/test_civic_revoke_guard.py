"""F2 Task 4 —— 撤销的射程防呆。

对标 seed/reset_builtin_residents.py:84-114 的 _assert_no_players：
raise 而非静默跳过（静默跳过会让调用方以为动作完成了），查数据库而非信传入
对象（调用点自己建的目标列表里，target.resident_type 恰恰是不能信的字段）。
"""
import pytest
from sqlalchemy import func, select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.user import User
from app.services import civic_membership as cm


def _res(slug, rtype, *, creator_id="u1", meta=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id,
                    tile_x=1, tile_y=1, meta_json=meta)


async def _promote_record(db, resident_id):
    """给某人补一行晋升记录，让他进入撤销白名单。"""
    from datetime import UTC, datetime
    db.add(CivicStandingHistory(
        resident_id=resident_id, old_standing=cm.DENIZEN,
        new_standing=cm.CITIZEN, reason=None, reason_code="threshold_met",
        actor="civic_promotion", evidence_json={},
        world_at=datetime.now(UTC),
    ))
    await db.commit()


async def _fill_electorate(db, n, *, prefix="filler"):
    """把选民数增加 n 位内置公民。``prefix`` 让同一个测试里可以调用多次而不撞
    slug 的 UNIQUE 约束。"""
    db.add_all([_res(f"{prefix}-{i}", cm.CIVIC_MEMBER_TYPE,
                     creator_id=cm.SYSTEM_CREATOR_ID) for i in range(n)])
    await db.commit()


@pytest.mark.anyio
async def test_guard_accepts_a_naturalised_citizen(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("ugc-1", cm.CIVIC_MEMBER_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    await _promote_record(db_session, r.id)

    slug, rtype = await cm._assert_revocable(db_session, r.id)
    assert (slug, rtype) == ("ugc-1", cm.CIVIC_MEMBER_TYPE)


@pytest.mark.anyio
async def test_guard_refuses_a_player_avatar_by_type(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("avatar", cm.PLAYER_RESIDENT_TYPE)
    db_session.add(r)
    await db_session.commit()
    with pytest.raises(cm.CivicStandingRefused, match="player"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_a_player_avatar_by_fk_even_if_type_was_tampered(db_session):
    """admin 手滑把化身改成 npc 后 type 已不可信 —— 必须查
    users.player_resident_id 复核（app/models/user.py:30）。"""
    await _fill_electorate(db_session, 6)
    r = _res("avatar", cm.CIVIC_MEMBER_TYPE)     # 已被改成 npc
    db_session.add(r)
    await db_session.flush()
    db_session.add(User(name="玩家", email="p@t.com", player_resident_id=r.id))
    await db_session.commit()
    await _promote_record(db_session, r.id)      # 连晋升记录都伪造了

    with pytest.raises(cm.CivicStandingRefused, match="player"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_the_builtin_cast(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("builtin", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
             meta={"origin": "preset"})
    db_session.add(r)
    await db_session.commit()
    await _promote_record(db_session, r.id)

    with pytest.raises(cm.CivicStandingRefused, match="built-in"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_admin_presets(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("adminpreset", cm.ADMIN_PRESET_TYPE, creator_id="system",
             meta={"origin": "preset"})
    db_session.add(r)
    await db_session.commit()
    with pytest.raises(cm.CivicStandingRefused, match="preset"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_someone_without_a_promotion_record(db_session):
    """撤销是晋升的严格逆操作。没有晋升记录的 npc 不在射程内——admin 手工把
    某人改回 npc 会在探针上显示为「无晋升记录的 UGC-origin 公民」，这正好是
    一条有用的红旗，不是噪声。"""
    await _fill_electorate(db_session, 6)
    r = _res("no-record", cm.CIVIC_MEMBER_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    with pytest.raises(cm.CivicStandingRefused, match="no promotion record"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_someone_already_in_the_denizen_tier(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    await _promote_record(db_session, r.id)
    with pytest.raises(cm.CivicStandingRefused, match="not in the 'citizen' tier"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_an_unknown_id(db_session):
    with pytest.raises(cm.CivicStandingRefused, match="no resident"):
        await cm._assert_revocable(db_session, "nope")


@pytest.mark.anyio
async def test_guard_enforces_the_electorate_floor(db_session, monkeypatch):
    """数值闸门 3：撤销后选民数必须 ≥ max(min_peers + 1, CIVIC_MIN_ELECTORATE)。
    这条不变式在未来做逐出时同样成立——逐出内置成员必须撞同一道墙。"""
    monkeypatch.setenv("CIVIC_MIN_ELECTORATE", "3")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    # 选民总数 3：撤销后剩 2 < max(2+1, 3) = 3 → 拒绝
    await _fill_electorate(db_session, 2)
    r = _res("ugc-1", cm.CIVIC_MEMBER_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    await _promote_record(db_session, r.id)

    with pytest.raises(cm.CivicStandingRefused, match="electorate"):
        await cm._assert_revocable(db_session, r.id)

    # 再多两个内置公民 → 撤销后剩 4 ≥ 3 → 放行
    await _fill_electorate(db_session, 2, prefix="extra")
    assert (await cm._assert_revocable(db_session, r.id))[0] == "ugc-1"


@pytest.mark.anyio
async def test_guard_writes_nothing(db_session):
    """Guard first: no UPDATE has run yet —— 拒绝必须是真正的 no-op。"""
    await _fill_electorate(db_session, 6)
    r = _res("builtin", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID)
    db_session.add(r)
    await db_session.commit()
    before = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()

    with pytest.raises(cm.CivicStandingRefused):
        await cm._assert_revocable(db_session, r.id)

    after = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert before == after
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
