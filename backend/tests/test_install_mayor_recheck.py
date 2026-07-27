"""F2 Task 7 —— install_mayor 的结票复核与事务化。

归属说明：election_service.py:135-193 落在 F1 独占区（:53-60 候选排序）之外，
F1/F3 都不覆盖它。本文件是 F2 对这段代码的收口测试。

两条被收口的语义，各自的判据写在对应用例的 docstring 里：

1. **结票复核**——winner 用 ``Resident.is_civic_voter`` 解析，不是
   ``is_autonomous``；候选名单是开票那一刻的快照，快照不构成信任。
2. **事务化**——旧镇长清理 / 新镇长安装 / ``current_mayor`` 记录必须在同一次
   commit 里；解析失败时零写入。
"""
import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.system_config import SystemConfig
from app.services import civic_membership as cm
from app.services import election_service


def _res(slug, rtype=cm.CIVIC_MEMBER_TYPE, *, meta=None):
    return Resident(slug=slug, name=slug, district="central_plaza",
                    status="idle", resident_type=rtype, creator_id="sys",
                    tile_x=70, tile_y=56, meta_json=meta)


async def _meta_by_slug(db) -> dict[str, dict]:
    """列级读取，绕开 identity map（``expire_on_commit=False`` + 回滚后的过期
    对象都会让实体查询取回陈旧/待刷新的实例）。"""
    return {slug: (meta or {}) for slug, meta in (await db.execute(
        select(Resident.slug, Resident.meta_json))).all()}


# ── ① 结票复核：候选资格开票时快照，结票时复核 ─────────────────────────

@pytest.mark.anyio
async def test_install_mayor_refuses_a_winner_who_lost_the_ballot(db_session):
    """候选资格开票时快照，结票时复核——快照不构成信任。被降级者不得就任。"""
    old = _res("old", meta={"mayor": True})
    demoted = _res("demoted", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add_all([old, demoted])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "demoted") is False

    # 零写入：旧镇长的标志必须原封不动（现状 bug 是先 commit 再判 winner）
    metas = await _meta_by_slug(db_session)
    assert metas["old"].get("mayor") is True
    assert metas["demoted"].get("mayor") in (None, False)
    assert (await db_session.execute(
        select(SystemConfig).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none() is None


@pytest.mark.anyio
async def test_install_mayor_refuses_an_unknown_slug_without_writing(db_session):
    """今天就可达的触发条件：目标 slug 查不到。"""
    old = _res("old", meta={"mayor": True})
    db_session.add(old)
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "ghost") is False
    metas = await _meta_by_slug(db_session)
    assert metas["old"].get("mayor") is True


@pytest.mark.anyio
async def test_install_mayor_refuses_an_empty_slug(db_session):
    assert await election_service.install_mayor(db_session, None) is False
    assert await election_service.install_mayor(db_session, "") is False


@pytest.mark.anyio
async def test_a_refused_install_leaves_the_offices_row_untouched(
        db_session, monkeypatch):
    """三向分歧的第三向：offices。拒绝就任时在任者的 offices 行不得被顶掉。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    from app.services.office_service import OfficeService

    db_session.add_all([
        _res("incumbent", meta={"mayor": True}),
        _res("demoted", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"}),
    ])
    await db_session.commit()
    await OfficeService(db_session).appoint(
        "mayor", "incumbent", fill_strategy="election")

    assert await election_service.install_mayor(db_session, "demoted") is False
    assert await OfficeService(db_session).get_holder("mayor") == "incumbent"


# ── ② 事务化 ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_install_mayor_lands_all_three_representations(db_session):
    """三个表示（旧镇长 meta / 新镇长 meta / system_config）都要落地。

    注意：这条只验**终态**，不验原子性——两次 commit 的实现同样会通过。原子性
    的判据在 ``test_a_failed_config_write_leaves_the_old_mayor_untouched``。
    """
    old = _res("old", meta={"mayor": True})
    new = _res("new")
    db_session.add_all([old, new])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "new") is True

    metas = await _meta_by_slug(db_session)
    assert metas["old"].get("mayor") in (None, False)
    assert metas["new"].get("mayor") is True
    cfg = (await db_session.execute(
        select(SystemConfig.value)
        .where(SystemConfig.key == "current_mayor"))).scalar_one()
    assert json.loads(cfg) == "new"


@pytest.mark.anyio
async def test_a_failed_config_write_leaves_the_old_mayor_untouched(
        db_session, monkeypatch):
    """事务化的真判据：``current_mayor`` 写失败时 meta_json 也不得落地。

    先 commit meta、再单独写 config 的实现（今天就是这样，只是顺序反着）在这里
    会留下两向分歧：旧镇长的标志已经没了，system_config 还指着他。
    """
    old = _res("old", meta={"mayor": True})
    new = _res("new")
    db_session.add_all([old, new])
    await db_session.commit()

    async def _boom(db, slug):
        raise RuntimeError("current_mayor write blew up")

    # raising=False：旧实现里没有这个符号，patch 变成 no-op，install_mayor 会
    # 一路成功返回 True —— pytest.raises 因此照样红，且红在正确的地方。
    monkeypatch.setattr(election_service, "_record_current_mayor", _boom,
                        raising=False)

    with pytest.raises(RuntimeError):
        await election_service.install_mayor(db_session, "new")
    # 这里刻意**不**替 install_mayor 回滚：回滚是它错误路径自己的责任（判据见
    # ``test_a_failed_install_does_not_leak_into_the_announcement_commit``）。
    # 它若不收，下面这条 SELECT 的 autoflush 会把脏对象刷进当前事务并被读到。
    metas = await _meta_by_slug(db_session)
    assert metas["old"].get("mayor") is True
    assert metas["new"].get("mayor") in (None, False)
    assert (await db_session.execute(
        select(SystemConfig).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none() is None


# ── ③ 清扫面不得是集合谓词 ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_stale_mayor_flag_on_a_demoted_resident_is_still_swept(db_session):
    """降级档（``resident``）今天还在 ``SIM_RESIDENT_TYPES`` 里，属侥幸命中。"""
    ex = _res("ex-mayor", cm.UGC_RESIDENT_TYPE,
              meta={"origin": "forge", "mayor": True})
    winner = _res("winner")
    db_session.add_all([ex, winner])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "winner") is True
    metas = await _meta_by_slug(db_session)
    assert {s for s, m in metas.items() if m.get("mayor")} == {"winner"}


@pytest.mark.anyio
async def test_stale_mayor_flag_outside_the_population_set_is_still_swept(
        db_session):
    """通用约束：清理「已离开集合 S 的居民」不得用 S 本身做 WHERE。

    上一条用的 ``resident`` 恰好还在 ``SIM_RESIDENT_TYPES`` 里，所以
    ``is_autonomous`` 的清扫面**也**能通过——它区分不出这次收口。这条用一个落在
    两个集合之外的活取值（``preset``，``schemas/admin.py`` 的默认值；逐出档落地
    后同理）来真正卡住：清扫面必须是全表 ``meta_json IS NOT NULL``。
    """
    outsider = _res("outsider", cm.ADMIN_PRESET_TYPE, meta={"mayor": True})
    winner = _res("winner")
    db_session.add_all([outsider, winner])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "winner") is True
    metas = await _meta_by_slug(db_session)
    assert {s for s, m in metas.items() if m.get("mayor")} == {"winner"}


# ── ④ 既有语义不得丢 ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_reinstalling_the_same_mayor_is_idempotent(db_session):
    sitting = _res("sitting", meta={"mayor": True})
    db_session.add(sitting)
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "sitting") is True
    metas = await _meta_by_slug(db_session)
    assert metas["sitting"].get("mayor") is True
    cfg = (await db_session.execute(
        select(SystemConfig.value)
        .where(SystemConfig.key == "current_mayor"))).scalar_one()
    assert json.loads(cfg) == "sitting"


@pytest.mark.anyio
async def test_office_dual_write_still_happens_when_gate_on(db_session, monkeypatch):
    """S2-1 的 offices 双写是 gate 开时的附加项，收口不得把它弄丢。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    from app.services.office_service import OfficeService

    db_session.add(_res("cand"))
    await db_session.commit()
    assert await election_service.install_mayor(db_session, "cand") is True
    assert await OfficeService(db_session).get_holder("mayor") == "cand"


# ── ⑤ 调用方：_close_one 的流会分支 ────────────────────────────────────

@pytest.mark.anyio
async def test_close_one_announces_a_failed_vote_when_the_winner_lost_rights(
        db_session):
    """当选人已失去资格 → _close_one 走流会公告分支，不安装镇长。"""
    from app.models.season import Poll
    from app.services import civic_service

    db_session.add(_res("demoted", cm.UGC_RESIDENT_TYPE,
                        meta={"origin": "forge"}))
    poll = Poll(question=f"{election_service.ELECTION_TAG}:谁来当下一任镇长?",
                options_json=[
                    {"label": "落选者", "effect": {"type": "mayor",
                                                   "slug": "demoted"},
                     "npc_votes": 3},
                    {"label": "弃权", "effect": None, "npc_votes": 1},
                ], status="open")
    db_session.add(poll)
    await db_session.commit()

    await civic_service._close_one(db_session, poll)
    await db_session.refresh(poll)

    assert poll.status == "closed"
    assert (await db_session.execute(
        select(SystemConfig).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none() is None
    from app.models.bulletin_post import BulletinPost
    posts = (await db_session.execute(select(BulletinPost))).scalars().all()
    assert posts, "公告本身必须发出来（否则下面的断言是空集恒真）"
    bodies = [p.content_md or "" for p in posts]
    assert any("失去" in b for b in bodies), \
        "公告必须说明本案流会的原因是当选人已失去资格"
    assert not any("议案已生效" in b for b in bodies), \
        "零写入的 return False 不得被公告成「已生效」"


@pytest.mark.anyio
async def test_close_one_still_reports_a_plain_failure_for_non_mayor_effects(
        db_session):
    """流会措辞只对 mayor 效果生效——别的效果类型仍是「生效时遇到问题」。"""
    from app.models.season import Poll
    from app.services import civic_service

    poll = Poll(question="要不要建个不存在的东西?",
                options_json=[
                    {"label": "要", "effect": {"type": "nonexistent_effect"},
                     "npc_votes": 3},
                    {"label": "不要", "effect": None, "npc_votes": 1},
                ], status="open")
    db_session.add(poll)
    await db_session.commit()

    await civic_service._close_one(db_session, poll)

    from app.models.bulletin_post import BulletinPost
    bodies = [p.content_md or "" for p in (await db_session.execute(
        select(BulletinPost))).scalars().all()]
    assert any("议案生效时遇到问题" in b for b in bodies)
    assert not any("失去" in b for b in bodies)


@pytest.mark.anyio
async def test_a_failed_install_does_not_leak_into_the_announcement_commit(
        db_session, monkeypatch):
    """真实调用链上的原子性：``_execute_outcome`` 把异常吞掉（``civic_service``
    :621），紧接着 ``_clerk_announce`` → ``create_post`` 自己 ``commit()``
    （``bulletin_service.py:25``）。install_mayor 若把脏对象留在 session 里，
    那次公告 commit 会替它落盘——「同一次 commit」的保证必须由 install_mayor
    自己的错误路径回滚兜住，光靠调用方自觉是兜不住的。
    """
    from app.models.season import Poll
    from app.services import civic_service

    db_session.add_all([_res("old", meta={"mayor": True}), _res("new")])
    poll = Poll(question=f"{election_service.ELECTION_TAG}:谁来当下一任镇长?",
                options_json=[
                    {"label": "新人", "effect": {"type": "mayor",
                                                 "slug": "new"},
                     "npc_votes": 3},
                    {"label": "弃权", "effect": None, "npc_votes": 1},
                ], status="open")
    db_session.add(poll)
    await db_session.commit()

    async def _boom(db, slug):
        raise RuntimeError("current_mayor write blew up")

    monkeypatch.setattr(election_service, "_record_current_mayor", _boom)

    await civic_service._close_one(db_session, poll)

    metas = await _meta_by_slug(db_session)
    assert metas["old"].get("mayor") is True, \
        "旧镇长的标志被后续的公告 commit 顺手落盘了 —— 事务化没兜住"
    assert metas["new"].get("mayor") in (None, False)
    assert (await db_session.execute(
        select(SystemConfig).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none() is None


@pytest.mark.anyio
async def test_close_one_does_not_defame_an_eligible_winner_on_an_infra_failure(
        db_session, monkeypatch):
    """错误归因会在世界内诽谤一位在籍公民。

    ``install_mayor`` 返回 ``False`` 的原因不止「结票复核不合格」一种——写入
    故障也被 ``_execute_outcome`` 的 ``except Exception`` 吞成 ``False``。按
    effect 类型无条件归因，就会把一次基础设施故障翻译成对一位具名角色的名誉
    裁决，而 ``BulletinPost`` 会经 ``app/routers/bulletin.py`` 永久呈现在玩家
    UI 上。「失去公民资格」只有在复核**确认**不合格时才说得出口。
    """
    from app.models.season import Poll
    from app.services import civic_service

    # winner 是一位完全合格的 npc 公民；失败的是写入，不是资格。
    db_session.add_all([_res("old", meta={"mayor": True}), _res("new")])
    poll = Poll(question=f"{election_service.ELECTION_TAG}:谁来当下一任镇长?",
                options_json=[
                    {"label": "新人", "effect": {"type": "mayor",
                                                 "slug": "new"},
                     "npc_votes": 3},
                    {"label": "弃权", "effect": None, "npc_votes": 1},
                ], status="open")
    db_session.add(poll)
    await db_session.commit()

    async def _boom(db, slug):
        raise RuntimeError("current_mayor write blew up")

    monkeypatch.setattr(election_service, "_record_current_mayor", _boom)

    await civic_service._close_one(db_session, poll)

    from app.models.bulletin_post import BulletinPost
    bodies = [p.content_md or "" for p in (await db_session.execute(
        select(BulletinPost))).scalars().all()]
    assert bodies, "公告本身必须发出来（否则下面的断言是空集恒真）"
    assert not any("失去公民资格" in b for b in bodies), \
        f"基础设施故障被公告成了对一位在籍公民的名誉裁决：{bodies}"
    assert any("议案生效时遇到问题" in b for b in bodies), \
        f"非资格原因的失败应回落到通用措辞：{bodies}"
    # 该走的复核仍然要走：new 确实还是 civic voter
    assert (await db_session.execute(
        select(Resident.resident_type).where(Resident.slug == "new")
    )).scalar_one() == cm.CIVIC_MEMBER_TYPE
