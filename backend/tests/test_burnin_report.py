"""scripts/burnin_report.py 聚合冒烟测试：造几行 llm_usage 数据，断言聚合数字。"""
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

# scripts/ 不在安装包里（wheel 只含 app），从仓库 backend 根导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.llm_usage import LLMUsage
from scripts.burnin_report import aggregate, fetch_rows, render_report, summarize_day


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
