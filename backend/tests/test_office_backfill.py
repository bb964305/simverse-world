"""F3 Task 2/3/7 — 出缺后的补选钩子。

断链修复的核心：term_check() 到期只 vacate，没有任何路径触发补选，
current_mayor() 的两个回落也被同一次 term_check 清干净，世界于是进入
「无镇长且无人接任」的稳态。trigger_backfill 是补上的那一截，同时是 F2
撤销收口时的调用入口。

正确性硬约束：不得依赖 polis_office_enabled。gate 关时 offices 可能没有
mayor 行，也可能留着迁移 046 的陈旧 holder_slug——两种都要判对（Task 7 的
矩阵专门证明这一点）。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.office import Office
from app.models.resident import Resident
from app.models.season import Poll, Vote
from app.services.election_service import ELECTION_TAG
from app.services.office_service import (
    OfficeService,
    REASON_CIVIC_REVOCATION,
    REASON_MANUAL,
    REASON_TERM_EXPIRED,
    trigger_backfill,
)


def _res(slug, name, meta=None, rtype="npc"):
    return Resident(
        slug=slug, name=name, district="central_plaza", status="idle",
        resident_type=rtype, creator_id="sys", tile_x=70, tile_y=56,
        meta_json=meta,
    )


async def _seed_voters(db):
    db.add_all([_res("a", "甲"), _res("b", "乙"), _res("c", "丙")])
    await db.commit()


async def _open_election_polls(db) -> list[Poll]:
    return (await db.execute(
        select(Poll).where(
            Poll.status == "open", Poll.question.like(f"{ELECTION_TAG}%"),
        )
    )).scalars().all()


@pytest.mark.anyio
async def test_backfill_opens_election_for_vacant_elected_office(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    poll_id = await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED)
    assert poll_id
    poll = (await db_session.execute(
        select(Poll).where(Poll.id == poll_id)
    )).scalar_one()
    assert poll.status == "open"
    assert poll.question.startswith(ELECTION_TAG)
    assert len(poll.options_json) >= 2


@pytest.mark.anyio
async def test_backfill_noop_while_office_still_occupied(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_MANUAL) is None
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_ignores_labour_offices(db_session, monkeypatch):
    """town_clerk / postman / doctor 是劳动职务，不走选举补缺。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("postman", "a", fill_strategy="seed")
    await svc.vacate("postman")

    assert await trigger_backfill(db_session, "postman", reason=REASON_TERM_EXPIRED) is None
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_is_idempotent_against_open_election(db_session, monkeypatch):
    """已有一张 open 的选举 poll 时不得再开第二张。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    db_session.add(Poll(
        question=f"{ELECTION_TAG}:谁来当下一任镇长?",
        options_json=[], closes_at=datetime.now(UTC) + timedelta(days=3),
        status="open",
    ))
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED) is None
    assert len(await _open_election_polls(db_session)) == 1


@pytest.mark.anyio
async def test_backfill_stamps_election_cadence(db_session, monkeypatch):
    """开完补选要盖 election_last_opened，否则同一晚 maybe_open_seasonal_election
    会再开一张。"""
    from app.services.config_service import ConfigService

    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_CIVIC_REVOCATION)
    stamped = await ConfigService(db_session).get("election_last_opened")
    assert stamped == datetime.now(UTC).date().isoformat()


@pytest.mark.anyio
async def test_backfill_survives_open_election_failure(db_session, monkeypatch):
    """fail-open：选举服务炸了也不能把异常抛回 vacate 路径。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    async def _boom(db, **kwargs):
        raise RuntimeError("election service down")

    monkeypatch.setattr(
        "app.services.election_service.open_election", _boom)
    assert await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED) is None


@pytest.mark.anyio
async def test_backfill_failure_leaves_session_usable(db_session, monkeypatch):
    """fail-open 必须连 session 一起 fail-open。

    上一条的 _boom 是「进门就 raise」——一个 DB 语句都没跑过,session 是干净的,
    加不加 rollback 都会绿。真实故障形状是异常来自 **flush/commit**
    (open_election → civic_service.propose 里有 db.add + db.commit):那时
    session 停在 needs-rollback 状态,后续任何语句都抛 PendingRollbackError
    (本机实测:失败 flush 后不 rollback,下一条 SELECT 即 PendingRollbackError)。
    所以这里故意让写炸在 flush 里。
    """
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    # 一张已关闭的旧选举 poll:它的主键待会儿被拿去制造 flush 期冲突。
    # status="closed" 所以不会被 trigger_backfill 的「已有 open 选举」早退命中。
    old = Poll(question=f"{ELECTION_TAG}:上一届", options_json=[],
               closes_at=datetime.now(UTC) - timedelta(days=1), status="closed")
    db_session.add(old)
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    async def _boom_in_flush(db, **kwargs):
        db.add(Poll(id=old.id, question=f"{ELECTION_TAG}:x", options_json=[],
                    closes_at=datetime.now(UTC) + timedelta(days=1),
                    status="open"))
        await db.flush()          # IntegrityError:主键冲突,炸在 flush 里

    monkeypatch.setattr(
        "app.services.election_service.open_election", _boom_in_flush)
    assert await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED) is None
    # 关键断言:session 仍可用。没有 rollback 这里会抛 PendingRollbackError,
    # 「返回 None」只是把炸点推到了下一条语句。
    assert await OfficeService(db_session).get_holder("mayor") is None
    # 半途写入不得留在库里
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_declines_when_civic_gates_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    monkeypatch.setattr(settings, "election_enabled", False)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED) is None
    assert await _open_election_polls(db_session) == []


# ── Task 3: term_check 断链修复（硬门）──────────────────────────────

@pytest.mark.anyio
async def test_term_check_triggers_backfill_frozen_clock(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1

    assert await svc.get_holder("mayor") is None
    polls = await _open_election_polls(db_session)
    assert len(polls) == 1  # 世界不得停在「无限期无镇长」


@pytest.mark.anyio
async def test_world_clock_advance_past_term_end_opens_backfill(db_session, monkeypatch):
    """硬门：推进世界时钟越过 term_ends_at，断言补选已开。

    term_days 是世界日；k=4 时 8 世界日 ≈ 2 真实日,所以把真实钟推 8/k+1 日
    一定越过 term_ends_at。用 world_clock 换算而不是裸 utcnow 比较。
    """
    from app import world_clock

    monkeypatch.setattr(settings, "polis_office_enabled", True)
    monkeypatch.setattr(settings, "polis_office_mayor_term_days", 8)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election",
                      term_days=settings.polis_office_mayor_term_days)

    base = world_clock.now_real()
    jump = timedelta(days=8 / settings.world_clock_k + 1)
    monkeypatch.setattr(world_clock, "now_real", lambda: base + jump)

    assert await svc.term_check() == 1              # 默认 now 走世界时钟
    assert await svc.get_holder("mayor") is None
    polls = await _open_election_polls(db_session)
    assert len(polls) == 1
    assert polls[0].question.startswith(ELECTION_TAG)


@pytest.mark.anyio
async def test_term_check_backfill_failure_does_not_break_vacate(db_session, monkeypatch):
    """补选炸了,出缺本身仍然成立(fail-open)。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    async def _boom(db, **kwargs):
        raise RuntimeError("election service down")

    monkeypatch.setattr("app.services.election_service.open_election", _boom)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1
    assert await svc.get_holder("mayor") is None


@pytest.mark.anyio
async def test_term_check_does_not_backfill_labour_office(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("postman", "a", fill_strategy="seed", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_poll_closing_actually_seats_a_successor(db_session, monkeypatch):
    """硬门本体:补选开出 → 关票 → 新镇长就位。

    spec §5 的硬门目标是「任期到期后世界不得出现『无限期无镇长』状态」,
    「开出一张 poll」只是半截:中间还隔着
    close_due_polls → _close_one → _execute_outcome(type="mayor")
    → install_mayor → OfficeService.appoint 这条链。这条链现在是通的,但只断言
    poll 数的话,它哪天断了 F3 的测试仍然全绿而世界照样停在无镇长。
    不违反独占文件约束:civic_service / election_service 只 import 调用。
    """
    from app.services import civic_service

    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1
    assert await svc.get_holder("mayor") is None

    polls = await _open_election_polls(db_session)
    assert len(polls) == 1
    poll = polls[0]
    winner_slug = poll.options_json[0]["effect"]["slug"]
    # 投一票给 0 号候选,并把截止时间挪到过去(与 test_m6_election 同姿势:
    # 走 votes 表而不是手改 options_json,免得跟 flag_modified 较劲)
    db_session.add(Vote(poll_id=poll.id, user_id="u1", option_idx=0))
    poll.closes_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    assert await civic_service.close_due_polls(db_session) == 1
    # 不再是「无限期无镇长」:继任者真的坐上了位置
    assert await svc.get_holder("mayor") == winner_slug


# ── Fix round 1/5: 多 office 循环的 rollback 牵连 ──────────────────────

@pytest.mark.anyio
async def test_term_check_survives_rollback_from_prior_office_backfill_failure(
    db_session, monkeypatch,
):
    """同一批 ``due`` 里有 ≥2 个 office 时,第一个的 ``trigger_backfill``
    内部失败会经 ``_rollback_quietly`` 调用 ``db.rollback()``——不同于
    ``commit()``(``expire_on_commit=False`` 时不 expire),``rollback()``
    无条件 expire 整个 identity map,包括 ``due`` 列表里还没处理的下一个
    ``Office`` ORM 对象。下一轮循环开头 ``office.office_key`` 这行同步属性
    读在 AsyncSession 上触发隐式惰性刷新,而这行代码不在任何
    ``greenlet_spawn`` 上下文里,于是抛 ``MissingGreenlet`` 而不是
    ``AttributeError``——term_check 直接炸,夜间 cron 的 office 段整段死掉。

    两个 office 都用 fill_strategy="election" 且都 due,保证不管 SELECT
    返回顺序如何,第一个处理的那个也会撞上 boom → rollback,第二个必然
    在展示这条崩溃路径。
    """
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election", term_days=7)
    await svc.appoint("town_clerk", "a", fill_strategy="election", term_days=7)

    async def _boom(db, **kwargs):
        raise RuntimeError("election service down")

    monkeypatch.setattr("app.services.election_service.open_election", _boom)

    n = await svc.term_check(now=datetime.now(UTC) + timedelta(days=365))
    assert n == 2
    assert await svc.get_holder("mayor") is None
    assert await svc.get_holder("town_clerk") is None
