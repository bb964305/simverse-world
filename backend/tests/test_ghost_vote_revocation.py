"""E8 幽灵票：只撤「已从 residents 表消失」的票。

2026-07-25 的花名册重置删掉了 25 位居民，但票留在了 options_json 里
（npc_votes 是累加计数器、_npc_voters 是只增不减的集合）。生产三张 poll
各带 14 个查不到的 slug，25 张幽灵票让 13 人小镇里的 2 个真玩家永远投不赢
任何议案；镇长选举那张的 4 个候选人全部不存在。

**范围边界（拍板）**：F2 的「投票时具备资格即计票」保护的是**降级者**
（civic_service.py:31-42）——那种情况 Resident 行还在，票保留。这里只清
物理删除的事故残留。test_civic_frozen_denominator.test_ghost_votes_are_kept_by_design
必须保持绿，它就是这条边界的判据。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.resident import Resident
from app.models.season import Poll
from app.services import civic_membership as cm
from app.services import civic_service


def _res(slug, rtype=cm.CIVIC_MEMBER_TYPE):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id="sys", tile_x=1, tile_y=1)


async def _stored(db, poll_id) -> list[dict]:
    """列级读，绕开 identity map（conftest 是 expire_on_commit=False）。"""
    return (await db.execute(
        select(Poll.options_json).where(Poll.id == poll_id))).scalar_one()


@pytest.mark.anyio
async def test_voters_are_recorded_as_slug_to_option_index(db_session):
    """撤票要能定向回滚，就必须知道每个人投的是哪一项。"""
    db_session.add_all([_res("a"), _res("b")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    await civic_service.run_npc_voting(db_session)

    voters = (await _stored(db_session, poll.id))[0]["_npc_voters"]
    assert isinstance(voters, dict)
    assert set(voters) == {"a", "b"}
    assert all(isinstance(v, int) for v in voters.values())


@pytest.mark.anyio
async def test_a_deleted_resident_loses_its_vote(db_session):
    """居民行被删 → 撤票、npc_votes 减回去。"""
    db_session.add_all([_res("stays"), _res("vanishes")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    assert await civic_service.run_npc_voting(db_session) == 2
    before = sum(int(o.get("npc_votes", 0))
                 for o in await _stored(db_session, poll.id))
    assert before == 2

    gone = (await db_session.execute(
        select(Resident).where(Resident.slug == "vanishes"))).scalar_one()
    await db_session.delete(gone)
    await db_session.commit()

    await civic_service.run_npc_voting(db_session)
    opts = await _stored(db_session, poll.id)
    assert sum(int(o.get("npc_votes", 0)) for o in opts) == 1
    assert set(opts[0]["_npc_voters"]) == {"stays"}


@pytest.mark.anyio
async def test_a_demoted_resident_keeps_its_vote(db_session):
    """F2 语义边界：降级（行还在）不撤票 —— 投票时具备资格即计票。"""
    db_session.add_all([_res("keeps"), _res("demoted")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    assert await civic_service.run_npc_voting(db_session) == 2

    r = (await db_session.execute(
        select(Resident).where(Resident.slug == "demoted"))).scalar_one()
    r.resident_type = cm.UGC_RESIDENT_TYPE
    await db_session.commit()

    await civic_service.run_npc_voting(db_session)
    opts = await _stored(db_session, poll.id)
    assert sum(int(o.get("npc_votes", 0)) for o in opts) == 2
    assert "demoted" in opts[0]["_npc_voters"]


@pytest.mark.anyio
async def test_legacy_list_format_is_read_and_upgraded(db_session):
    """存量 poll 的 _npc_voters 是 list[str]，读侧必须兼容且不重复投票。"""
    db_session.add(_res("old-voter"))
    await db_session.commit()
    poll = Poll(question="存量议题", status="open",
                closes_at=datetime.now(UTC) + timedelta(days=3),
                options_json=[{"label": "A", "npc_votes": 1,
                               "_npc_voters": ["old-voter"]},
                              {"label": "B", "npc_votes": 0}])
    db_session.add(poll)
    await db_session.commit()

    assert await civic_service.run_npc_voting(db_session) == 0  # 已投过
    opts = await _stored(db_session, poll.id)
    assert sum(int(o.get("npc_votes", 0)) for o in opts) == 1


@pytest.mark.anyio
async def test_legacy_list_ghosts_are_dropped_without_touching_the_tally(db_session):
    """旧 list 格式不知道幽灵投的是哪一项——只能移出名册，不能瞎减票。

    减错票会凭空改变某个具体选项的得票，比留着一张来源不明的票更糟。存量
    tally 的订正由一次性脚本按备份数据做，不在这条自动路径里。
    """
    db_session.add(_res("alive"))
    await db_session.commit()
    poll = Poll(question="存量议题", status="open",
                closes_at=datetime.now(UTC) + timedelta(days=3),
                options_json=[{"label": "A", "npc_votes": 2,
                               "_npc_voters": ["alive", "deleted-long-ago"]},
                              {"label": "B", "npc_votes": 0}])
    db_session.add(poll)
    await db_session.commit()

    await civic_service.run_npc_voting(db_session)
    opts = await _stored(db_session, poll.id)
    assert "deleted-long-ago" not in opts[0]["_npc_voters"]
    assert opts[0]["npc_votes"] == 2  # 未知归属 → 不动 tally


@pytest.mark.anyio
async def test_closing_zeroes_options_whose_candidate_no_longer_exists(db_session):
    """结票兜底：候选人已不存在的选项归零，避免「有胜者但流会」的误导公告。

    生产那张镇长选举 4 个候选全废（klaus 17 / 夜风侦探 2 / isabella 5 /
    adam 1），不归零的话它会「以 17 票胜出」然后在 install_mayor 阶段流会，
    公告对玩家是误导。
    """
    db_session.add(_res("real-candidate"))
    await db_session.commit()
    poll = Poll(question="镇长选举:谁来当下一任镇长?", status="open",
                closes_at=datetime.now(UTC) - timedelta(days=1),
                options_json=[
                    {"label": "幽灵候选", "effect": {"type": "mayor",
                                                     "slug": "klaus"},
                     "npc_votes": 17},
                    {"label": "真候选", "effect": {"type": "mayor",
                                                   "slug": "real-candidate"},
                     "npc_votes": 3},
                ])
    db_session.add(poll)
    await db_session.commit()

    assert await civic_service.close_due_polls(db_session) == 1
    opts = await _stored(db_session, poll.id)
    assert opts[0]["npc_votes"] == 0        # 幽灵候选归零
    assert opts[1].get("won") is True       # 真候选胜出
    from app.services import election_service
    assert await election_service.current_mayor(db_session) == "real-candidate"


@pytest.mark.anyio
async def test_non_person_effects_are_never_zeroed(db_session):
    """只对 mayor/office/duty 这类「选项即人」的效果做存在性校验。"""
    poll = Poll(question="要不要建邮局", status="open",
                closes_at=datetime.now(UTC) - timedelta(days=1),
                options_json=[
                    {"label": "建", "effect": {"type": "system_config",
                                               "key": "x", "value": 1,
                                               "slug": "not-a-resident"},
                     "npc_votes": 5},
                    {"label": "不建", "effect": None, "npc_votes": 1},
                ])
    db_session.add(poll)
    await db_session.commit()

    await civic_service.close_due_polls(db_session)
    opts = await _stored(db_session, poll.id)
    assert opts[0].get("won") is True and opts[0]["final_votes"] == 5
