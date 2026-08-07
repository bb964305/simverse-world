"""seed/reset_builtin_residents.py 的依赖清理覆盖（046/047 + 胶囊 + 每日任务）.

purge_residents() 在 dde187c 重写后清理清单只覆盖 045 时代的依赖表：
offices.holder_slug(046)、issue_stances.resident_slug(047)、
time_capsules.carrier_resident_slug、daily_quests.quest_json.resident_slug
都按 slug 引用居民、且都没有 FK 约束——删掉居民只会静默留下孤儿行
（2026-07-25 vm212 生产库阵容迁移实测：删 klaus 留下 2 条 carrier_resident_slug
='klaus' 的 sealed 胶囊）。这些测试锁住"删干净 + 不误删"的边界。
"""

from datetime import date, timedelta

from sqlalchemy import select

from app.models.daily_quest import DailyQuest
from app.models.issue_stance import IssueStance
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.office import Office
from app.models.resident import Resident
from app.models.time_capsule import TimeCapsule
from app.models.world_change_proposal import WorldChangeProposal
from seed.reset_builtin_residents import purge_residents

import pytest

DOOMED = "klaus"
KEPT = "isabella-kept"


def _resident(slug: str) -> Resident:
    return Resident(slug=slug, name=slug, creator_id="sys", district="cafe",
                    resident_type="npc", status="idle", tile_x=1, tile_y=1)


def _office(office_key: str, holder: str | None) -> Office:
    return Office(office_key=office_key, holder_slug=holder, institution="town_hall",
                  fill_strategy="election", perms_json={})


def _capsule(slug: str) -> TimeCapsule:
    return TimeCapsule(user_id="u-1", carrier_resident_slug=slug,
                       deliver_on=date.today() + timedelta(days=30),
                       content="给未来的我", status="sealed")


def _quest(user: str, slug: str | None, status: str = "pending") -> DailyQuest:
    quest_json = {"resident_slug": slug, "resident_name": slug, "topic": "聊聊", "min_turns": 3}
    if slug is None:
        quest_json = {}  # 历史脏数据：没有 resident_slug 键
    return DailyQuest(user_id=user, date=date.today(), quest_json=quest_json, status=status)


@pytest.fixture
async def world(db_session):
    """一个待删居民 + 一个幸存居民，各自持有 office / stance / capsule。"""
    doomed = _resident(DOOMED)
    kept = _resident(KEPT)
    db_session.add_all([
        doomed, kept,
        _office("mayor", DOOMED),
        _office("doctor", KEPT),
        IssueStance(issue_key="修桥", resident_slug=DOOMED, stance=0.7),
        IssueStance(issue_key="修桥", resident_slug=KEPT, stance=-0.3),
        _capsule(DOOMED),
        _capsule(KEPT),
    ])
    await db_session.commit()
    return {"doomed": doomed, "kept": kept}


async def _slugs(db, column):
    return set((await db.execute(select(column))).scalars().all())


async def test_purge_vacates_office_but_keeps_the_row(db_session, world):
    """office 行永不删除（app/models/office.py:6）——只把 holder_slug 腾空。"""
    await purge_residents(db_session, [world["doomed"]])

    offices = {o.office_key: o.holder_slug for o in
               (await db_session.execute(select(Office))).scalars().all()}
    assert offices == {"mayor": None, "doctor": KEPT}


async def test_purge_deletes_issue_stances_of_removed_residents(db_session, world):
    """留着的话，同 slug 的新居民会继承已删 NPC 的立场。"""
    await purge_residents(db_session, [world["doomed"]])

    assert await _slugs(db_session, IssueStance.resident_slug) == {KEPT}


async def test_purge_deletes_time_capsules_carried_by_removed_residents(db_session, world):
    """携带者被删后胶囊无法正常投递（notify 里会显示不存在的居民名）。"""
    await purge_residents(db_session, [world["doomed"]])

    assert await _slugs(db_session, TimeCapsule.carrier_resident_slug) == {KEPT}


async def test_purge_deletes_pending_daily_quests_pointing_at_removed_residents(db_session, world):
    """待办任务指向已删居民就永远做不完，而生成器只在用户当天没有行时才补发
    （daily_quest_service.py:57-61）——删掉 pending 行当天就能拿到新任务。"""
    db_session.add_all([
        _quest("u-doomed", DOOMED),
        _quest("u-kept", KEPT),
        _quest("u-empty", None),
    ])
    await db_session.commit()

    await purge_residents(db_session, [world["doomed"]])

    rows = (await db_session.execute(select(DailyQuest))).scalars().all()
    assert sorted(q.user_id for q in rows) == ["u-empty", "u-kept"]


async def test_purge_keeps_done_daily_quests_for_removed_residents(db_session, world):
    """done 任务是已发币的历史（daily_quest_service.py:125）：删掉会让当天任务
    重新生成并二次发奖，必须保留。"""
    db_session.add(_quest("u-done", DOOMED, status="done"))
    await db_session.commit()

    await purge_residents(db_session, [world["doomed"]])

    rows = (await db_session.execute(select(DailyQuest))).scalars().all()
    assert [(q.user_id, q.status) for q in rows] == [("u-done", "done")]


async def test_purge_keeps_historical_records_referencing_the_slug(db_session, world):
    """lab_runs / lab_tasks / world_change_proposals 是历史记录，slug 只作展示，
    有意保留（见 purge_residents docstring）。"""
    db_session.add_all([
        LabRun(task_id="t-1", researcher_slug=DOOMED),
        LabTask(issuer_user_id="u-1", researcher_slug=DOOMED, title="旧课题"),
        WorldChangeProposal(kind="add_lore", title="旧提案", author_slug=DOOMED),
    ])
    await db_session.commit()

    await purge_residents(db_session, [world["doomed"]])

    assert await _slugs(db_session, LabRun.researcher_slug) == {DOOMED}
    assert await _slugs(db_session, LabTask.researcher_slug) == {DOOMED}
    assert await _slugs(db_session, WorldChangeProposal.author_slug) == {DOOMED}


async def test_purge_removes_the_resident_itself(db_session, world):
    await purge_residents(db_session, [world["doomed"]])

    assert await _slugs(db_session, Resident.slug) == {KEPT}
