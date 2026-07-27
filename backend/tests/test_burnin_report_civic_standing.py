"""F2 Task 11 —— 晋升与撤销的可观测性（硬门 1）。

现有探针（burnin_report.py:1176-1282）只输出按 resident_type 分组的静态计数，
对 F2 的核心失败模式全盲：误升只会让 npc 计数合法增长，leaked_voter_types 判
的是常量集合被拓宽，F2 只改行值不改集合，永远不会触发。
"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import world_clock
from app.models.civic_standing_history import CivicStandingHistory
from app.models.office import Office
from app.models.resident import Resident
from app.models.season import Poll
from app.models.system_config import SystemConfig
from app.services import civic_membership as cm
from scripts.burnin_report import (
    civic_boundary_breakdown,
    fetch_civic_standing_snapshot,
    render_probes_civic_boundary,
    render_probes_civic_standing,
)


def _res(slug, rtype, *, creator_id="u1", meta=None, created_days_ago=200):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=datetime.now(UTC)
                    - timedelta(days=created_days_ago))


def _builtin(slug):
    return _res(slug, cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
                meta={"origin": "preset"})


def _ugc(slug, rtype=cm.UGC_RESIDENT_TYPE):
    return _res(slug, rtype, meta={"origin": "forge"})


async def _history(db, resident_id, old, new, *, world_days_ago=0.0,
                   actor="civic_promotion"):
    db.add(CivicStandingHistory(
        resident_id=resident_id, old_standing=old, new_standing=new,
        reason=None, reason_code="threshold_met", actor=actor,
        evidence_json={},
        world_at=(world_clock.now_world().astimezone(UTC)
                  - timedelta(days=world_days_ago))))
    await db.commit()


# ── ① 交叉表与泄漏判据 ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_cross_table_splits_builtin_promoted_and_unpromoted(db_session):
    promoted = _ugc("ugc-promoted", cm.CIVIC_MEMBER_TYPE)
    db_session.add_all([_builtin("b1"), _builtin("b2"), promoted,
                        _ugc("ugc-waiting")])
    await db_session.commit()
    await _history(db_session, promoted.id, cm.DENIZEN, cm.CITIZEN)

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["available"] is True
    assert snap["cross"]["builtin_citizen"] == 2
    assert snap["cross"]["ugc_citizen_promoted"] == 1
    assert snap["cross"]["ugc_citizen_unrecorded"] == 0
    assert snap["cross"]["ugc_denizen"] == 1
    assert snap["leaked"] == []


@pytest.mark.anyio
async def test_leak_is_a_ugc_voter_without_a_promotion_record(db_session):
    """判泄漏的条件改成「provenance=UGC 且 is_civic_voter 为真、但查不到晋升
    记录」——现有探针判的是常量集合被拓宽，F2 只改行值不改集合。"""
    db_session.add_all([_builtin("b1"), _ugc("sneaky", cm.CIVIC_MEMBER_TYPE)])
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["cross"]["ugc_citizen_unrecorded"] == 1
    assert snap["leaked"] == ["sneaky"]
    out = render_probes_civic_standing(snap)
    assert "🔴" in out and "sneaky" in out


# ── ② 晋升队列 ─────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_promotion_queue_counts_threshold_ready_denizens(
        db_session, monkeypatch):
    from app.models.resident_relation import ResidentRelation

    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.2")

    b1, b2, u = _builtin("b1"), _builtin("b2"), _ugc("u1")
    db_session.add_all([b1, b2, u, _ugc("u2")])
    await db_session.commit()
    for b in (b1, b2):
        a_id, b_id = sorted([b.id, u.id])
        db_session.add(ResidentRelation(party_a=a_id, party_b=b_id,
                                        familiarity=0.5))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["queue"]["size"] == 1
    assert snap["queue"]["slugs"] == ["u1"]


# ── ③ 翻转统计（告警条件，不是信息项）─────────────────────────────────

@pytest.mark.anyio
async def test_recent_flip_is_an_alert_not_an_info_line(db_session):
    """静态计数发现不了振荡——11 内置 + 3 归化的读数在 X 升 / Y 降的同一夜看
    起来完全正常。滞后设计生效后稳态下这个数应恒为 0。"""
    r = _ugc("flipper", cm.CIVIC_MEMBER_TYPE)
    db_session.add_all([_builtin("b1"), r])
    await db_session.commit()
    await _history(db_session, r.id, cm.DENIZEN, cm.CITIZEN, world_days_ago=6)
    await _history(db_session, r.id, cm.CITIZEN, cm.DENIZEN, world_days_ago=5)
    await _history(db_session, r.id, cm.DENIZEN, cm.CITIZEN, world_days_ago=1)

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["flips"]["recent_flip_residents"] == 1
    assert snap["flips"]["max_changes_per_resident"] == 3
    assert snap["flips"]["in_min_tenure"] >= 1
    out = render_probes_civic_standing(snap)
    assert "🔴" in out
    assert "翻转" in out


@pytest.mark.anyio
async def test_no_recent_flip_is_quiet(db_session):
    r = _ugc("settled", cm.CIVIC_MEMBER_TYPE)
    db_session.add_all([_builtin("b1"), r])
    await db_session.commit()
    await _history(db_session, r.id, cm.DENIZEN, cm.CITIZEN, world_days_ago=90)

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["flips"]["recent_flip_residents"] == 0
    assert snap["leaked"] == []


# ── ④ 交叉一致性 ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_elected_office_held_by_a_non_voter_is_red(db_session):
    u = _ugc("ugc-mayor")
    db_session.add_all([_builtin("b1"), u])
    db_session.add(Office(office_key="mayor", holder_slug="ugc-mayor",
                          institution="town_hall", perms_json={},
                          fill_strategy=cm.POLITICAL_FILL_STRATEGY))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session, gate_office_on=True)
    assert snap["crosscheck"]["election_office_non_voter"] == [
        ["mayor", "ugc-mayor"]]


@pytest.mark.anyio
async def test_labour_office_held_by_a_ugc_resident_is_not_flagged(db_session):
    """只对民选职位断言：town_clerk / postman / doctor 是劳动职务，UGC 居民
    担任它们是既定边界，不是红旗。"""
    db_session.add_all([_builtin("b1"), _ugc("ugc-postman")])
    db_session.add(Office(office_key="postman", holder_slug="ugc-postman",
                          institution="post_office", perms_json={},
                          fill_strategy="seed"))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session, gate_office_on=True)
    assert snap["crosscheck"]["election_office_non_voter"] == []


@pytest.mark.anyio
async def test_mayor_three_way_consistency_is_gated_on_the_office_flag(db_session):
    """gate 关时 offices 是迁移 046 的遗留值，不分档会在 T2 前直接报红并被当
    噪声关掉。"""
    db_session.add(_builtin("b1", ))
    db_session.add(Office(office_key="mayor", holder_slug="stale-046",
                          institution="town_hall", perms_json={},
                          fill_strategy=cm.POLITICAL_FILL_STRATEGY))
    db_session.add(SystemConfig(key="current_mayor", value=json.dumps(None),
                                group="civic", updated_by="ops"))
    await db_session.commit()

    gate_off = await fetch_civic_standing_snapshot(db_session,
                                                   gate_office_on=False)
    assert gate_off["crosscheck"]["mayor_reps"]["checked"] is False

    gate_on = await fetch_civic_standing_snapshot(db_session,
                                                  gate_office_on=True)
    assert gate_on["crosscheck"]["mayor_reps"]["checked"] is True
    assert gate_on["crosscheck"]["mayor_reps"]["consistent"] is False


@pytest.mark.anyio
async def test_ghost_votes_on_open_polls_are_counted(db_session):
    demoted = _ugc("demoted")
    db_session.add_all([_builtin("b1"), demoted])
    db_session.add(Poll(question="议题", status="open",
                        options_json=[{"label": "A", "npc_votes": 2,
                                       "_npc_voters": ["b1", "demoted"]},
                                      {"label": "B", "npc_votes": 0}]))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session)
    ghosts = snap["crosscheck"]["ghost_votes"]
    assert len(ghosts) == 1
    assert ghosts[0]["ghosts"] == 1
    assert "demoted" in ghosts[0]["slugs"]


@pytest.mark.anyio
async def test_dangling_office_holders_are_reported(db_session):
    """purge_residents 不清 offices 与 current_mayor：删掉在任镇长会留下悬空
    holder_slug，current_mayor() 照常返回它，townhall.py:61 会把 slug 当名字
    显示给玩家。"""
    db_session.add(_builtin("b1"))
    db_session.add(Office(office_key="mayor", holder_slug="deleted-guy",
                          institution="town_hall", perms_json={},
                          fill_strategy=cm.POLITICAL_FILL_STRATEGY))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session, gate_office_on=True)
    assert snap["crosscheck"]["dangling_holders"] == ["deleted-guy"]


@pytest.mark.anyio
async def test_probe_is_skipped_when_the_table_is_missing(db_session):
    """新表未建（迁移未跑）→ 探针跳过而不是炸掉整份报告。"""
    out = render_probes_civic_standing({"available": False})
    assert "探针跳过" in out


# ── ⑤ unknown_types 升为红旗 ───────────────────────────────────────────

def test_unknown_types_render_as_a_red_flag():
    """未来引入新 type 时唯一的自动发现口，也是写错一个字符的唯一兜底。"""
    snap = {"available": True, "by_type": {"npc": 10, "npc ": 1}}
    d = civic_boundary_breakdown(snap)
    assert d["unknown_types"] == {"npc ": 1}
    out = render_probes_civic_boundary(snap)
    assert "🔴" in out
    assert "⚠️ 两列之外的取值" not in out
