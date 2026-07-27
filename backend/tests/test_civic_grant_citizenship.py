"""F2 Task 3 —— 晋升写入口。

批量写形态是 guarded UPDATE + rowcount 校验（正面样板：relation_service.py
:214-223、office_service.py:128-135；反面样板：admin/residents.py:103-127 的
读-改-写）。rowcount 与目标数不符 = 有人在窗口内改过 → 整批回滚并告警。
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, update

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.user import User
from app.services import civic_membership as cm


def _ugc(slug, **kw):
    d = dict(slug=slug, name=slug, district="town_hall", status="idle",
             resident_type=cm.UGC_RESIDENT_TYPE, creator_id="u1",
             tile_x=1, tile_y=1, meta_json={"origin": "forge"})
    d.update(kw)
    return Resident(**d)


def _npc(slug, **kw):
    d = dict(slug=slug, name=slug, district="town_hall", status="idle",
             resident_type=cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
             tile_x=1, tile_y=1, meta_json={"origin": "preset"})
    d.update(kw)
    return Resident(**d)


@pytest.fixture
def _no_ws():
    """WS 扇出在测试里没有 manager；显式打桩，免得每个断言都被 fail-open 的
    warning 噪声淹没。"""
    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as m:
        yield m


@pytest.mark.anyio
async def test_grant_flips_the_tier_and_writes_one_history_row(db_session, _no_ws):
    r = _ugc("ugc-1")
    db_session.add(r)
    await db_session.commit()

    ok = await cm.grant_citizenship(
        db_session, r, reason="满足门槛", actor="civic_promotion",
        evidence={"world_days": 40.0, "peers": 3, "min_familiarity": 0.2},
        reason_code="threshold_met",
    )
    assert ok is True

    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE

    row = (await db_session.execute(select(CivicStandingHistory))).scalar_one()
    assert (row.old_standing, row.new_standing) == (cm.DENIZEN, cm.CITIZEN)
    assert row.reason_code == "threshold_met"
    assert row.actor == "civic_promotion"
    assert row.evidence_json["peers"] == 3
    assert row.world_at is not None


@pytest.mark.anyio
async def test_grant_makes_the_resident_a_civic_voter(db_session, _no_ws):
    """晋升的全部意义：进政治层，同时不改变世界人口口径。"""
    r = _ugc("ugc-1")
    db_session.add(r)
    await db_session.commit()
    await cm.grant_citizenship(db_session, r, reason="x", actor="admin:1")

    voters = (await db_session.execute(
        select(Resident.slug).where(Resident.is_civic_voter))).scalars().all()
    population = (await db_session.execute(
        select(Resident.slug).where(Resident.is_autonomous))).scalars().all()
    assert set(voters) == {"ugc-1"}
    assert set(population) == {"ugc-1"}


@pytest.mark.anyio
async def test_grant_refuses_a_resident_that_is_not_in_the_denizen_tier(db_session):
    """撤销/晋升都是白名单：内置公民、玩家化身、admin preset 一律拒绝。"""
    builtin = _npc("b1")
    avatar = _ugc("a1", resident_type="player")
    preset = _ugc("p1", resident_type="preset", creator_id="system")
    db_session.add_all([builtin, avatar, preset])
    await db_session.commit()

    for target in (builtin, avatar, preset):
        with pytest.raises(cm.CivicStandingRefused):
            await cm.grant_citizenship(db_session, target, reason="x",
                                       actor="admin:1")
    # 拒绝是真正的 no-op：一行历史都没写
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_grant_reads_the_database_not_the_passed_object(db_session):
    """照抄 07-25 的设计选择：调用点自己建的目标列表里，target.resident_type
    恰恰是不能信的字段。

    这里用 SimpleNamespace 而不是把 ORM 对象改脏——改脏的 ORM 对象会在下一次
    查询的 autoflush 里真的落库，就测不出「信不信传入对象」了。SimpleNamespace
    正好模型化 07-25 那个「自带 id 列表」的调用点。
    """
    from types import SimpleNamespace

    r = _npc("b1")
    db_session.add(r)
    await db_session.commit()
    fake = SimpleNamespace(id=r.id, resident_type=cm.UGC_RESIDENT_TYPE)

    with pytest.raises(cm.CivicStandingRefused):
        await cm.grant_citizenship(db_session, fake, reason="x", actor="admin:1")


@pytest.mark.anyio
async def test_grant_refuses_a_tampered_player_avatar(db_session):
    """射程纪律必须与撤销侧对称：撤销查 users.player_resident_id 复核，晋升侧
    也要查（_assert_revocable 的第 ① 条同一段 SQL）。

    admin 手滑把化身的 resident_type 改成 'resident' 之后，档位检查会放行，而
    is_ugc_resident 的兜底分支会把它判成 UGC（化身的 creator_id 是真实 user
    id）——一旦升上去，_assert_revocable 的化身复核又会拒绝撤销，人永久卡在
    citizen 档。
    """
    avatar = _ugc("avatar", meta_json={"origin": "onboarding"})
    db_session.add(avatar)
    await db_session.flush()
    db_session.add(User(name="玩家", email="p@t.com",
                        player_resident_id=avatar.id))
    await db_session.commit()

    with pytest.raises(cm.CivicStandingRefused, match="player avatar"):
        await cm.grant_citizenship(db_session, avatar, reason="x",
                                   actor="civic_promotion")

    rtype = (await db_session.execute(
        select(Resident.resident_type)
        .where(Resident.id == avatar.id))).scalar_one()
    assert rtype == cm.UGC_RESIDENT_TYPE
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_grant_refuses_unknown_ids(db_session):
    with pytest.raises(cm.CivicStandingRefused):
        await cm.grant_citizenship_batch(
            db_session, ["no-such-id"], reason="x",
            reason_code="threshold_met", actor="civic_promotion")


@pytest.mark.anyio
async def test_batch_grant_is_all_or_nothing(db_session, _no_ws):
    """rowcount != len(ids) → 整批回滚 + 告警。用一次并发窗口内的改档位模拟。"""
    a, b = _ugc("ugc-a"), _ugc("ugc-b")
    db_session.add_all([a, b])
    await db_session.commit()

    real_execute = db_session.execute
    seen = {"n": 0}

    async def _sneaky(statement, *args, **kwargs):
        # 在 guard SELECT 之后、guarded UPDATE 之前，把 b 改掉
        result = await real_execute(statement, *args, **kwargs)
        if seen["n"] == 0 and getattr(statement, "is_select", False):
            seen["n"] = 1
            await real_execute(
                update(Resident).where(Resident.id == b.id)
                .values(resident_type=cm.CIVIC_MEMBER_TYPE)
                .execution_options(synchronize_session=False))
        return result

    with patch.object(db_session, "execute", new=_sneaky):
        with pytest.raises(cm.CivicStandingRefused):
            await cm.grant_citizenship_batch(
                db_session, [a.id, b.id], reason="x",
                reason_code="threshold_met", actor="civic_promotion")

    # a 没有被晋升，且一行历史都没留下
    rtype_a = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == a.id))).scalar_one()
    assert rtype_a == cm.UGC_RESIDENT_TYPE
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_batch_grant_writes_one_row_per_resident(db_session, _no_ws):
    residents = [_ugc(f"ugc-{i}") for i in range(3)]
    db_session.add_all(residents)
    await db_session.commit()
    ids = [r.id for r in residents]

    n = await cm.grant_citizenship_batch(
        db_session, ids, reason="满足门槛", reason_code="threshold_met",
        actor="civic_promotion",
        evidence_by_id={i: {"world_days": 40.0, "peers": 3} for i in ids},
    )
    assert n == 3
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 3
    assert (await db_session.execute(
        select(func.count()).select_from(Resident)
        .where(Resident.is_civic_voter))).scalar() == 3


@pytest.mark.anyio
async def test_batch_grant_of_nothing_is_a_noop(db_session):
    assert await cm.grant_citizenship_batch(
        db_session, [], reason="x", reason_code="y", actor="z") == 0


@pytest.mark.anyio
async def test_grant_broadcasts_the_standing_event_without_the_reason_text(db_session):
    """事件名不得叫 resident_type_changed（已被 SBTI 人格漂移占用，
    app/ws/handlers/chat.py:474-482）；payload 只带枚举码，不带原因文本。"""
    r = _ugc("ugc-1")
    db_session.add(r)
    await db_session.commit()

    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as bc:
        await cm.grant_citizenship(db_session, r, reason="秘密理由",
                                   actor="admin:1", reason_code="admin_grant")
    payload = bc.await_args.args[0]
    assert payload["type"] == "civic_standing_changed"
    assert payload["old_standing"] == cm.DENIZEN
    assert payload["new_standing"] == cm.CITIZEN
    assert payload["reason_code"] == "admin_grant"
    assert payload["resident_slug"] == "ugc-1"
    assert "秘密理由" not in str(payload)
    assert "reason" not in payload


@pytest.mark.anyio
async def test_broadcast_failure_never_breaks_the_write(db_session):
    """WS 扇出 fail-open：广播炸了，档位变更与历史行必须已经落地。"""
    r = _ugc("ugc-1")
    db_session.add(r)
    await db_session.commit()

    with patch("app.lab.apply.broadcast_world_changed",
               new=AsyncMock(side_effect=RuntimeError("ws down"))):
        assert await cm.grant_citizenship(db_session, r, reason="x",
                                          actor="admin:1") is True
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 1
