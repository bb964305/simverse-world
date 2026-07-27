#!/usr/bin/env python3
"""清点两个非人类哨兵账号被误铸的 Soul Coin —— **纯只读**，不写任何一行。

背景：``reward_creator_passive`` 曾经只挡字面量 ``"system"``，而 seed 给内置 NPC
写的是 ``SYSTEM_CREATOR_ID``（UUID）。于是每一轮内置 NPC 对话都往 System 账号
铸 1 SC 并落一条 Transaction，这些交易被 ``/admin/economy/stats`` 的
``total_issued`` / ``net_circulation`` 统计进去。代码侧已止血（coin_service），
这个脚本只回答「历史上已经发生了多少」。

用法（vm212 api 容器内，DATABASE_URL 已由 deploy compose 注入）::

    docker compose exec api python scripts/audit_system_minting.py

本地 / 任意库::

    DATABASE_URL=sqlite+aiosqlite:////tmp/x.db python scripts/audit_system_minting.py

输出：按 账号 × UTC 日 × reason 前缀 聚合的笔数与净额，外加两个账号的当前余额。
本脚本**不会**修正任何数据；要不要冲正是单独的决定。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class MintingRow:
    user_id: str
    day: str
    reason: str
    count: int
    total: int


def _reason_prefix(reason: str) -> str:
    """``creator_passive:klaus`` → ``creator_passive``（按 slug 分组会炸成噪音）。"""
    return reason.split(":", 1)[0]


def aggregate(rows) -> list[MintingRow]:
    """[(user_id, amount, reason, created_at)] → 按 账号 × UTC 日 × reason 聚合。"""
    buckets: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for user_id, amount, reason, created_at in rows:
        day = created_at.strftime("%Y-%m-%d")
        buckets[(user_id, day, _reason_prefix(reason))].append(amount)
    return [
        MintingRow(user_id=uid, day=day, reason=reason,
                   count=len(amounts), total=sum(amounts))
        for (uid, day, reason), amounts in sorted(buckets.items())
    ]


async def _load():
    from sqlalchemy import select

    from app.database import async_session
    from app.models.transaction import Transaction
    from app.models.user import User
    from app.services.system_users import NON_USER_CREATOR_IDS

    async with async_session() as db:
        result = await db.execute(
            select(Transaction.user_id, Transaction.amount,
                   Transaction.reason, Transaction.created_at)
            .where(Transaction.user_id.in_(NON_USER_CREATOR_IDS))
            .order_by(Transaction.created_at)
        )
        rows = list(result.all())
        balances = (await db.execute(
            select(User.id, User.name, User.soul_coin_balance)
            .where(User.id.in_(NON_USER_CREATOR_IDS))
        )).all()
    return rows, balances


def render(rows: list[MintingRow], balances) -> str:
    out = ["账号哨兵当前余额", "-" * 64]
    if not balances:
        out.append("(两个哨兵账号在这个库里都不存在)")
    for uid, name, balance in balances:
        out.append(f"{uid:40} {name:16} {balance:>8}")

    out += ["", "误铸明细（账号 × UTC 日 × reason）", "-" * 64,
            f"{'账号':40} {'日期':12} {'reason':22} {'笔数':>6} {'净额':>8}"]
    if not rows:
        out.append("(无记录)")
        return "\n".join(out)

    for r in rows:
        out.append(f"{r.user_id:40} {r.day:12} {r.reason:22} {r.count:>6} {r.total:>8}")

    passive = [r for r in rows if r.reason == "creator_passive"]
    out += ["", "-" * 64,
            f"creator_passive 合计：{sum(r.count for r in passive)} 笔 / {sum(r.total for r in passive)} SC",
            "（这就是需要从 admin 经济面板 total_issued 里扣掉的量）"]
    return "\n".join(out)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    rows, balances = await _load()
    print(render(aggregate(rows), balances))


if __name__ == "__main__":
    asyncio.run(main())
