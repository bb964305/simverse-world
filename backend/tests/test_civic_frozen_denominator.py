"""F2 Task 8 —— 在途投票用「开票时冻结分母」解决，不实现撤票。

晋升与撤销都会在投票窗口内移动选民集合。若法定人数的分母读结票时的实时
``_eligible_voter_count()``，一张已经开出去的 poll 的判决门槛会在中途改变。
冻结分母（``propose()`` 把开票那一刻的合格选民数写进
``options_json[0][META_ELIGIBLE_AT_OPEN]``）对晋升与撤销**同时免疫**。

幽灵票保留并写成设计语义「投票时具备资格即计票」：``_npc_voters`` 是
``options_json[0]`` 上的扁平 slug 列表（``civic_service.py:165``/``:173``），
物理上没存票的归属，撤票要改结构且要兼容存量 poll。

适用面（否则这一整套代码根本不跑）：threshold / quorum 整段只在
``settings.polis_policy_approval_enabled``（默认 False，**vm212 为 true**）为真、
且 ``options_json[0]`` 带 ``META_THRESHOLD`` 时才计算（``civic_service.py:489``
的 gate）；quorum 还要额外带 ``META_QUORUM``。普通 civic poll 与镇长选举 poll
走纯 plurality，分母不参与判决——撤销对它们的影响是票差而非流会。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll
from app.services import civic_membership as cm
from app.services import civic_service
from app.services.policy_service import META_OUTCOME, META_QUORUM, META_THRESHOLD

#: 本模块的 logger 名字——断言 WARNING 时按名字过滤，免得被别处的告警蒙混过关。
CIVIC_LOGGER = "app.services.civic_service"


def _res(slug, rtype=cm.CIVIC_MEMBER_TYPE):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id="sys", tile_x=1, tile_y=1)


async def _stored_opts(db, poll_id) -> list[dict]:
    """列级 SELECT 读回 options_json——绕开 identity map，读的是**真落库**的值
    而不是会话里那个还没被 expire 的对象（conftest 是 expire_on_commit=False）。
    """
    return (await db.execute(
        select(Poll.options_json).where(Poll.id == poll_id))).scalar_one()


async def _demote(db, *slugs) -> None:
    """把已在籍公民降到 denizen 档。

    这里直接写 ``resident_type`` 而不走 ``civic_membership.revoke_citizenship``：
    本 Task 关心的是「实时分母变了之后判决会不会跟着漂」，撤销走哪个写入口是
    Task 2–5 的射程；真入口还要求晋升记录 + 选民下限，与本测试无关。
    """
    for slug in slugs:
        resident = (await db.execute(
            select(Resident).where(Resident.slug == slug))).scalar_one()
        resident.resident_type = cm.UGC_RESIDENT_TYPE
    await db.commit()


@pytest.fixture
def approval_gate(monkeypatch):
    monkeypatch.setattr(settings, "polis_policy_approval_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_quorum_fraction", 0.5)
    yield


@pytest.mark.anyio
async def test_propose_freezes_the_electorate_size(db_session):
    """分母口径必须是 ``is_civic_voter``（政治权利，仅 npc），不是
    ``is_autonomous``（人口，含 UGC）——所以 UGC 居民不进分母。"""
    db_session.add_all([_res(f"n{i}") for i in range(5)])
    db_session.add(_res("ugc", cm.UGC_RESIDENT_TYPE))
    await db_session.commit()

    poll = await civic_service.propose(
        db_session, "广场加长椅",
        [{"label": "支持", "effect": None}, {"label": "反对", "effect": None}])
    assert poll is not None
    stored = await _stored_opts(db_session, poll.id)
    assert stored[0][civic_service.META_ELIGIBLE_AT_OPEN] == 5


@pytest.mark.anyio
async def test_snapshot_survives_a_promotion_inside_the_voting_window(db_session):
    db_session.add_all([_res(f"n{i}") for i in range(5)])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    # 窗口内新增两位公民
    db_session.add_all([_res("late1"), _res("late2")])
    await db_session.commit()

    stored = await _stored_opts(db_session, poll.id)
    assert stored[0][civic_service.META_ELIGIBLE_AT_OPEN] == 5
    assert await civic_service._eligible_voter_count(db_session) == 7


@pytest.mark.anyio
async def test_closing_uses_the_frozen_denominator_after_a_demotion(
        db_session, approval_gate):
    """真正会翻转的算例，走完整的 propose → 结票路径。

    10 位选民 / 4 票 → ``4 < 10×0.5`` 判 ``quorum_not_met``；若分母读结票时的
    实时计数（窗口内降掉 4 位后 eligible=6）→ ``4 < 3`` 为假 → 法定人数过关 →
    再看门槛 ``4/4 = 1.0 ≥ 0.5`` → 本案**生效**。冻结分母让这张 poll 的判决不
    因窗口内的人事变动而改变。
    """
    db_session.add_all([_res(f"n{i}") for i in range(10)])
    await db_session.commit()

    poll = await civic_service.propose(
        db_session, "把某项政策调整为 Y",
        [{"label": "赞成", "effect": None}, {"label": "反对", "effect": None}])
    # policy_service._open_amend_poll:459-465 的原样姿势：propose 之后再把 tier
    # 元数据盖到 opts[0] 上。这里顺带填 4 张 NPC 票。
    opts = list(poll.options_json)
    opts[0][META_THRESHOLD] = 0.5
    opts[0][META_QUORUM] = True
    opts[0]["npc_votes"] = 4
    poll.options_json = opts
    poll.closes_at = datetime.now(UTC) - timedelta(days=1)
    flag_modified(poll, "options_json")
    await db_session.commit()

    await _demote(db_session, "n0", "n1", "n2", "n3")

    live = await civic_service._eligible_voter_count(db_session)
    assert live == 6
    # 反事实钉子：读实时分母的实现在这里会判「法定人数已达」，本案就生效了。
    assert 4 >= live * settings.polis_policy_quorum_fraction

    assert await civic_service.close_due_polls(db_session) == 1

    stored = await _stored_opts(db_session, poll.id)
    assert stored[0][META_OUTCOME] == "quorum_not_met"
    assert not any(o.get("won") for o in stored)


@pytest.mark.anyio
async def test_quorum_reads_the_snapshot_not_the_live_count(db_session,
                                                            approval_gate):
    """同一个算例的单元版：库里只有 6 位选民，快照写着 10 → 必须按 10 判。"""
    db_session.add_all([_res(f"n{i}") for i in range(6)])
    await db_session.commit()

    opts = [{"label": "赞成", "npc_votes": 4, META_THRESHOLD: 0.5,
             META_QUORUM: True, civic_service.META_ELIGIBLE_AT_OPEN: 10},
            {"label": "反对", "npc_votes": 0}]
    verdict = await civic_service._policy_threshold_verdict(
        db_session, opts, [4, 0], 0, poll_id="poll-under-test")
    assert verdict == "quorum_not_met"


@pytest.mark.anyio
async def test_legacy_poll_without_a_snapshot_falls_back_to_live_count(
        db_session, approval_gate):
    """存量 poll（本改动之前开的）没有快照 → 回落实时计数，行为与改动前一致。

    回落成 0（= 跳过法定人数判定）会让这条从 ``quorum_not_met`` 变成通过。
    """
    db_session.add_all([_res(f"n{i}") for i in range(10)])
    await db_session.commit()

    opts = [{"label": "赞成", "npc_votes": 4, META_THRESHOLD: 0.5,
             META_QUORUM: True},
            {"label": "反对", "npc_votes": 0}]
    assert await civic_service._policy_threshold_verdict(
        db_session, opts, [4, 0], 0, poll_id="poll-under-test") == "quorum_not_met"


@pytest.mark.anyio
async def test_plain_civic_polls_are_untouched_by_the_denominator(db_session,
                                                                  approval_gate):
    """普通 civic poll 不带 META_THRESHOLD → 纯 plurality，分母不参与判决。"""
    opts = [{"label": "A", "npc_votes": 1}, {"label": "B", "npc_votes": 0}]
    assert await civic_service._policy_threshold_verdict(
        db_session, opts, [1, 0], 0, poll_id="poll-under-test") is None


@pytest.mark.anyio
async def test_threshold_only_poll_never_touches_the_denominator(
        db_session, approval_gate, caplog):
    """带 META_THRESHOLD 但不带 META_QUORUM → 只判门槛，分母整段不进场（既不读
    快照也不读实时计数，更不该告警）。1/1 = 1.0 ≥ 0.9 → 通过。"""
    db_session.add_all([_res(f"n{i}") for i in range(4)])
    await db_session.commit()

    opts = [{"label": "赞成", "npc_votes": 1, META_THRESHOLD: 0.9,
             civic_service.META_ELIGIBLE_AT_OPEN: 0},
            {"label": "反对", "npc_votes": 0}]
    with caplog.at_level("WARNING", logger=CIVIC_LOGGER):
        assert await civic_service._policy_threshold_verdict(
            db_session, opts, [1, 0], 0, poll_id="poll-under-test") is None
    assert [r for r in caplog.records if r.name == CIVIC_LOGGER] == []


@pytest.mark.anyio
async def test_zero_electorate_warns_instead_of_silently_short_circuiting(
        db_session, approval_gate, caplog):
    """行为不变（分母为 0 时仍跳过法定人数判定），但必须留下 WARNING——安全阀
    在分母为 0 时自己关掉，语义上说不通。

    告警必须**指名是哪张 poll**：一条运维无法定位的告警等于没留痕，而这条会
    随部署即刻在 vm212 生效（``polis_policy_approval_enabled=true``）。
    """
    opts = [{"label": "赞成", "npc_votes": 2, META_THRESHOLD: 0.5,
             META_QUORUM: True, civic_service.META_ELIGIBLE_AT_OPEN: 0},
            {"label": "反对", "npc_votes": 0}]
    with caplog.at_level("WARNING", logger=CIVIC_LOGGER):
        verdict = await civic_service._policy_threshold_verdict(
            db_session, opts, [2, 0], 0, poll_id="poll-under-test")
    assert verdict is None
    warned = [r for r in caplog.records
              if r.name == CIVIC_LOGGER and r.levelname == "WARNING"]
    assert len(warned) == 1
    assert "quorum" in warned[0].message and "eligible" in warned[0].message
    assert "poll-under-test" in warned[0].message


@pytest.mark.anyio
async def test_a_usable_denominator_stays_quiet(db_session, approval_gate,
                                                caplog):
    """告警只属于 ``eligible <= 0``。分母正常时一句都不许说——否则「留痕」退化成
    每次结票都刷一条噪音，等于没留。"""
    opts = [{"label": "赞成", "npc_votes": 8, META_THRESHOLD: 0.5,
             META_QUORUM: True, civic_service.META_ELIGIBLE_AT_OPEN: 10},
            {"label": "反对", "npc_votes": 0}]
    with caplog.at_level("WARNING", logger=CIVIC_LOGGER):
        assert await civic_service._policy_threshold_verdict(
            db_session, opts, [8, 0], 0, poll_id="poll-under-test") is None
    assert [r for r in caplog.records if r.name == CIVIC_LOGGER] == []


@pytest.mark.anyio
async def test_the_zero_electorate_warning_names_the_real_poll(
        db_session, approval_gate, caplog):
    """结票路径必须把**真的 poll.id** 递下去，不是 ``None``、也不是别的常量。

    上一条用的是手搓的 poll_id，只能证明参数被格式化进去了；这条走完整
    ``propose → close_due_polls``，钉的是 ``_close_one`` 侧的接线。选民集为空
    （库里只有一位 UGC 居民）→ 快照 0 → 告警路径。
    """
    db_session.add(_res("ugc-only", cm.UGC_RESIDENT_TYPE))
    await db_session.commit()

    poll = await civic_service.propose(
        db_session, "选民集为空时开出的议案",
        [{"label": "赞成", "effect": None}, {"label": "反对", "effect": None}])
    opts = list(poll.options_json)
    assert opts[0][civic_service.META_ELIGIBLE_AT_OPEN] == 0
    opts[0][META_THRESHOLD] = 0.5
    opts[0][META_QUORUM] = True
    opts[0]["npc_votes"] = 2          # 幽灵票：投票时有资格，之后选民集空了
    poll.options_json = opts
    poll.closes_at = datetime.now(UTC) - timedelta(days=1)
    flag_modified(poll, "options_json")
    await db_session.commit()

    with caplog.at_level("WARNING", logger=CIVIC_LOGGER):
        assert await civic_service.close_due_polls(db_session) == 1
    warned = [r for r in caplog.records
              if r.name == CIVIC_LOGGER and r.levelname == "WARNING"]
    assert len(warned) == 1
    assert warned[0].message.startswith(f"poll {poll.id}:")


@pytest.mark.anyio
async def test_election_polls_carry_the_snapshot_too(db_session):
    """镇长选举 poll 走 ``civic_service.propose``（``election_service.py:69``），
    所以同样带快照。选举本身是纯 plurality、分母不参与判决——这条钉的是「快照
    写在 propose 里」这个位置，将来给选举加门槛时不必再补一次。"""
    db_session.add_all([_res(f"n{i}") for i in range(3)])
    await db_session.commit()

    from app.services import election_service
    poll = await election_service.open_election(
        db_session, candidate_slugs=["n0", "n1"])
    assert poll is not None
    stored = await _stored_opts(db_session, poll.id)
    assert stored[0][civic_service.META_ELIGIBLE_AT_OPEN] == 3


@pytest.mark.anyio
async def test_ghost_votes_are_kept_by_design(db_session):
    """投票时具备资格即计票。被降级者的票不撤——``_npc_voters`` 没存票的归属。"""
    db_session.add_all([_res("will-be-demoted"), _res("n1")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    cast = await civic_service.run_npc_voting(db_session)
    assert cast == 2

    await _demote(db_session, "will-be-demoted")

    stored = await _stored_opts(db_session, poll.id)
    assert "will-be-demoted" in stored[0]["_npc_voters"]
    assert sum(int(o.get("npc_votes", 0)) for o in stored) == 2
