"""backend/scripts/refill_empty_digests.py —— IMP-4 配套的一次性运维脚本。

覆盖：空行/标题行进目标集，有正文的行不进；dry-run 零写库且不调 LLM；
--apply 真的回填；回填后重跑收敛为空目标集；render 措辞可区分。
"""
from datetime import date, datetime, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, func

from app.models.digest import Digest
from app.models.memory import Memory
from scripts.refill_empty_digests import find_targets, refill, render


async def _material(db, day):
    """给 gather_material 一点素材，让当天走 compose_digest（真实 LLM 路径）
    而不是冷启动兜底分支——时间戳必须落在 day 当天，理由同
    test_digest_empty_guard.py 里的同名 helper。
    """
    db.add(Memory(resident_id="r1", type="event", content="今天大家聊得很开心",
                  importance=0.9, source="chat_resident",
                  created_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC)))
    await db.commit()


async def _row(db, day, content_md, title=None):
    d = Digest(scope="village", date=day, user_id="",
              title=title or f"{day} 村落日报", content_md=content_md, stats_json={})
    db.add(d)
    await db.commit()
    return d


# ── find_targets ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_empty_row_is_a_target(db_session):
    await _row(db_session, date(2026, 7, 17), "")
    targets = await find_targets(db_session)
    assert [d.date for d in targets] == [date(2026, 7, 17)]


@pytest.mark.anyio
async def test_title_only_row_is_a_target(db_session):
    """CRIT-2 的那种退化行——只有标题、没有正文——同样要被挑出来。"""
    await _row(db_session, date(2026, 7, 24), "# 今日头条")
    targets = await find_targets(db_session)
    assert [d.date for d in targets] == [date(2026, 7, 24)]


@pytest.mark.anyio
async def test_row_with_real_content_is_not_a_target(db_session):
    await _row(db_session, date(2026, 7, 20),
              "# 正常的一天\n小镇今天很热闹，居民们四处闲逛，聊了很多有趣的事情。")
    targets = await find_targets(db_session)
    assert targets == []


@pytest.mark.anyio
async def test_personal_scope_rows_are_never_targets(db_session):
    """脚本只处理 scope="village"——个人周报走另一套（generate_weekly_recap），
    本脚本不该碰它。"""
    d = Digest(scope="personal", date=date(2026, 7, 20), user_id="u1",
              title="本周回顾", content_md="", stats_json={})
    db_session.add(d)
    await db_session.commit()
    targets = await find_targets(db_session)
    assert targets == []


# ── refill: dry-run ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_dry_run_makes_zero_writes_and_never_calls_llm(db_session):
    await _row(db_session, date(2026, 7, 17), "")
    await _row(db_session, date(2026, 7, 24), "# 今日头条")

    from app.services import digest_service as ds
    calls = []

    async def _fake_chat(*a, **kw):
        calls.append(1)
        return "不该被调用"

    with patch.object(ds, "llm_chat", _fake_chat):
        report = await refill(db_session, apply=False)

    assert not calls, "dry-run 不该调 LLM"
    assert {e["action"] for e in report} == {"would_refill"}
    assert {e["date"] for e in report} == {"2026-07-17", "2026-07-24"}

    # 库里的内容原封不动——两行仍然是空/标题行
    rows = (await db_session.execute(select(Digest))).scalars().all()
    assert {(r.date, r.content_md) for r in rows} == {
        (date(2026, 7, 17), ""),
        (date(2026, 7, 24), "# 今日头条"),
    }


# ── refill: --apply ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_apply_actually_refills_the_row(db_session):
    day = date(2026, 7, 25)
    await _row(db_session, day, "")
    await _material(db_session, day)

    from app.services import digest_service as ds

    async def _fake_chat(system_prompt, messages, model=None, max_tokens=None, **kw):
        return "# 补写的头条\n这是真实回填出来的正文内容，长度足够。"

    with patch.object(ds, "llm_chat", _fake_chat):
        report = await refill(db_session, apply=True)

    assert len(report) == 1
    assert report[0]["action"] == "refilled"
    assert report[0]["title_after"] == "补写的头条"

    n = (await db_session.execute(select(func.count()).select_from(Digest))).scalar()
    assert n == 1, "UPDATE 而非 INSERT —— 唯一约束还在"
    row = (await db_session.execute(select(Digest))).scalar_one()
    assert row.title == "补写的头条" and "回填" in row.content_md


@pytest.mark.anyio
async def test_apply_a_failing_target_does_not_abort_the_rest(db_session):
    """一条目标回填失败（LLM 又吐了退化输出）不该拖累其它目标。"""
    day_bad = date(2026, 7, 17)
    day_good = date(2026, 7, 24)
    await _row(db_session, day_bad, "")
    await _material(db_session, day_bad)
    await _row(db_session, day_good, "")
    await _material(db_session, day_good)

    from app.services import digest_service as ds

    async def _fake_chat(system_prompt, messages, model=None, max_tokens=None, **kw):
        # 用 prompt 里带的日期区分两天，模拟其中一天又拿到了退化输出
        if str(day_bad) in messages[0]["content"]:
            return "# 今日头条"
        return "# 正常回填\n这次是正常长度的正文内容，可以顺利通过守卫。"

    with patch.object(ds, "llm_chat", _fake_chat):
        report = await refill(db_session, apply=True)

    by_date = {e["date"]: e for e in report}
    assert by_date[str(day_bad)]["action"] == "failed"
    assert "DigestComposeEmpty" in by_date[str(day_bad)]["error"]
    assert by_date[str(day_good)]["action"] == "refilled"

    # 失败的那天原样留在库里、留在下次的目标集里
    targets_after = await find_targets(db_session)
    assert [d.date for d in targets_after] == [day_bad]


@pytest.mark.anyio
async def test_rerunning_after_a_successful_refill_converges_to_no_targets(db_session):
    day = date(2026, 7, 26)
    await _row(db_session, day, "")
    await _material(db_session, day)

    from app.services import digest_service as ds

    async def _fake_chat(*a, **kw):
        return "# 回填成功\n这是一段足够长的真实正文，用来验证收敛行为。"

    with patch.object(ds, "llm_chat", _fake_chat):
        report1 = await refill(db_session, apply=True)
        report2 = await refill(db_session, apply=True)

    assert len(report1) == 1 and report1[0]["action"] == "refilled"
    assert report2 == [], "已经填好的行不该在下次运行里再次出现"


# ── render ───────────────────────────────────────────────────────────

def test_render_wording_distinguishes_dry_run_from_apply():
    report = [{"id": "x", "date": "2026-07-17", "title_before": "t", "body_len_before": 0,
              "action": "would_refill"}]
    dry = render(report, apply=False)
    assert "DRY-RUN" in dry and "APPLY" not in dry

    report2 = [{"id": "x", "date": "2026-07-17", "title_before": "t", "body_len_before": 0,
               "action": "refilled", "title_after": "t2", "body_len_after": 30}]
    applied = render(report2, apply=True)
    assert "APPLY" in applied and "DRY-RUN" not in applied


def test_render_reports_no_targets_case():
    out = render([], apply=False)
    assert "无事可做" in out
