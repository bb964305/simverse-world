"""scripts/burnin_report.py 聚合冒烟测试：造几行 llm_usage 数据，断言聚合数字。"""
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

# scripts/ 不在安装包里（wheel 只含 app），从仓库 backend 根导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.llm_usage import LLMUsage
from app.models.memory import Memory
from scripts.burnin_report import (
    aggregate, fetch_rows, render_report, summarize_day,
    fetch_move_records, plan_arrival_rate, behavior_memory_consistency, render_probes,
    location_hourly_traffic, needs_health, render_probes_p1, fetch_resident_needs,
)


def _move_mem(rid, content, *, target, intent="VISIT_DISTRICT", moved, arrived, days_ago=0):
    m = Memory(resident_id=rid, type="event", content=content, importance=0.3,
               source="agent_action",
               metadata_json={"move": {"intent": intent, "target": target,
                                       "moved": moved, "arrived": arrived}})
    ts = datetime.now(UTC) - timedelta(days=days_ago)
    m.created_at = ts
    m.last_accessed_at = ts
    return m


def _usage(ts, scenario, *, in_tok, out_tok, cost, parse_ok=None, attempt_no=1,
           model="qwen3.7-plus"):
    return LLMUsage(
        ts=ts, scenario=scenario, model=model, owner="system",
        input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
        parse_ok=parse_ok, attempt_no=attempt_no,
    )


async def test_aggregate_groups_rates_and_window(db_session):
    now = datetime.now(UTC)
    db_session.add_all([
        _usage(now, "decide", in_tok=100, out_tok=50, cost=0.001,
               parse_ok=True, attempt_no=1),
        _usage(now, "decide", in_tok=200, out_tok=30, cost=0.002,
               parse_ok=False, attempt_no=2),
        # 对白输出无 JSON 语义：parse_ok=NULL，不进 parse 率分母
        _usage(now, "chat_turn", in_tok=300, out_tok=100, cost=0.003),
        # 窗口外（5 天前）：必须被 --days 2 过滤掉
        _usage(now - timedelta(days=5), "decide", in_tok=999, out_tok=999, cost=9.9,
               parse_ok=True),
    ])
    await db_session.commit()

    rows = await fetch_rows(db_session, since=now - timedelta(days=2))
    days = aggregate(rows)

    assert list(days) == [now.date().isoformat()]
    groups = days[now.date().isoformat()]

    decide = groups[("decide", "qwen3.7-plus")]
    assert decide.calls == 2
    assert decide.input_tokens == 300 and decide.output_tokens == 80
    assert decide.cost_usd == pytest.approx(0.003)
    assert decide.parse_ok_rate == pytest.approx(0.5)   # 1 ok / 2 评估
    assert decide.retry_share == pytest.approx(0.5)     # 1 行 attempt_no>1

    chat = groups[("chat_turn", "qwen3.7-plus")]
    assert chat.parse_ok_rate is None and chat.retry_share == 0.0

    total = summarize_day(groups)
    assert total.calls == 3
    assert total.cost_usd == pytest.approx(0.006)
    assert total.parse_ok_rate == pytest.approx(0.5)


async def test_render_report_per_resident_and_budget_math(db_session):
    now = datetime.now(UTC)
    db_session.add_all([
        _usage(now, "chat_wrapup", in_tok=1000, out_tok=400, cost=0.02, parse_ok=True),
        _usage(now, "decide", in_tok=500, out_tok=100, cost=0.01, parse_ok=True),
    ])
    await db_session.commit()

    rows = await fetch_rows(db_session, since=now - timedelta(days=2))
    report = render_report(
        aggregate(rows), residents=15, budget=1.5, window_days=2,
    )
    assert "chat_wrapup" in report and "decide" in report
    # $0.03 / 15 居民 = $0.0020/居民·天，低于 E-11 基线区间下沿 $0.0587
    assert "$/居民·天 = $0.0020" in report
    assert "低于区间" in report
    # $0.03 / $1.5 日预算 = 2.0%
    assert "预算占用 = 2.0%" in report
    # 预测区间出处标注在场
    assert "COST_RESEARCH_REPORT" in report


async def test_plan_arrival_rate_and_consistency(db_session):
    now = datetime.now(UTC)
    db_session.add_all([
        # trip A: enroute then arrived → counts as arrived
        _move_mem("r1", "正在前往学院", target="academy", moved=True, arrived=False),
        _move_mem("r1", "到达了学院", target="academy", moved=False, arrived=True),
        # trip B (different resident): only enroute → not arrived
        _move_mem("r2", "正在前往图书馆", target="library", moved=True, arrived=False),
        # a WANDER (not a planned trip) — excluded from arrival rate
        _move_mem("r3", "在户外停留", target=None, intent="WANDER", moved=False, arrived=False),
    ])
    await db_session.commit()

    records = await fetch_move_records(db_session, since=now - timedelta(days=1))
    assert len(records) == 4

    # 2 planned trips (academy, library); 1 arrived → 0.5
    assert plan_arrival_rate(records) == pytest.approx(0.5)
    # all 4 texts match their move state → 1.0
    assert behavior_memory_consistency(records) == pytest.approx(1.0)

    block = render_probes(records)
    assert "计划到达率" in block and "50.0%" in block


def test_probes_none_on_empty():
    assert plan_arrival_rate([]) is None
    assert behavior_memory_consistency([]) is None
    assert "-" in render_probes([])


def test_behavior_consistency_flags_phantom():
    # a phantom: text claims arrival but the breadcrumb says it never moved
    bad = [{"resident_id": "r", "content": "到达了学院", "day": "d",
            "target": "academy", "intent": "VISIT_DISTRICT", "moved": False, "arrived": False}]
    assert behavior_memory_consistency(bad) == pytest.approx(0.0)


def test_location_hourly_traffic():
    records = [
        {"arrived": True, "target": "cafe", "hour": 12},
        {"arrived": True, "target": "cafe", "hour": 12},
        {"arrived": True, "target": "tavern", "hour": 19},
        {"arrived": True, "target": "academy", "hour": 9},   # non-dining
        {"arrived": False, "target": "cafe", "hour": 8},     # not arrived → ignored
    ]
    traffic = location_hourly_traffic(records)
    assert traffic["dining"][12] == 2 and traffic["dining"][19] == 1
    assert 8 not in traffic.get("dining", {})   # en-route not counted


def test_needs_health():
    needs_list = [
        {"energy": 0.8, "satiety": 0.1, "social": 0.5},   # starving
        {"energy": 0.6, "satiety": 0.9, "social": 0.7},
    ]
    nh = needs_health(needs_list)
    assert nh["residents"] == 2
    assert nh["starving_count"] == 1
    assert nh["satiety"]["min"] == 0.1
    assert nh["energy"]["mean"] == pytest.approx(0.7)
    assert needs_health([]) is None


def test_render_probes_p1_smoke():
    records = [{"arrived": True, "target": "cafe", "hour": 12}]
    needs = [{"energy": 0.5, "satiety": 0.5, "social": 0.5}]
    block = render_probes_p1(records, needs)
    assert "地点小时人流" in block and "需求健康度" in block
