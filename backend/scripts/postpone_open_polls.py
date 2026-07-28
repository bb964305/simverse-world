#!/usr/bin/env python3
"""把在途 poll 的 ``closes_at`` 推后一次（07-27B 批次 A3）。

**为什么需要它。** 2026-07-25 的数据事故删掉了 26 位居民，三张 open poll 的
候选人 slug 却固化在 ``options_json`` 里没跟着走。生产跑的镜像早于两处修复：

- ``election_service.install_mayor`` 的结票复核 + 事务化（``e83ed51``/``93a2573``）
- ``civic_service._winner_lost_civic_rights`` 的流会公告分支（``d89f5fb``）

在**旧**镜像下结票会先清空全镇 ``meta_json['mayor']`` 再 ``return False``——
一次已提交的静默罢免——并公告一位库里不存在的当选人。推后 ``closes_at`` 不修
任何缺陷，只是买时间让上面两处修复先上生产。

**关票时刻不是 ``closes_at``。** ``close_due_polls`` 的唯一调用方是
``app/tasks/nightly_cron.py`` 的夜间任务（每天 Beijing 07:00 = 23:00 UTC），
判据是 ``if due > now: continue``。所以一张 ``closes_at=23:29`` 的 poll 会在
**次日** 23:00 那次 cron 才关。填 ``--until`` 时按这个口径算，别按 closes_at 直读。

**纪律**（``docs/PARALLEL_WORKSTREAMS_2026-07-27.md:151-157`` 的 T2 三条硬约束）：

1. 目标集由 :func:`find_targets` 自己查，**不接受调用方传 id 列表**——07-25
   事故的根因正是「手工脚本自带 id 列表绕过 find_targets」；
2. ``--dry-run`` 是默认值，``--apply`` 才写库；
3. 不可重放——完成标记落在 ``system_config``，换了 ``--until`` 而没带
   ``--force-rerun`` 直接拒绝，且拒绝是真 no-op（在第一条 UPDATE 之前抛）。

用法::

    # 差异报告（默认 dry-run，不写库）
    python scripts/postpone_open_polls.py --until 2026-07-31T23:29:43Z
    # 真跑
    python scripts/postpone_open_polls.py --until 2026-07-31T23:29:43Z --apply

生产上镜像里没有本文件（``Dockerfile`` 的 ``COPY . .`` 早于它，且 api 服务
没有源码 bind mount），用 heredoc 注入::

    ssh vm212 'cd /opt/skills-world/deploy && docker compose exec -T api \\
      python - --until 2026-07-31T23:29:43Z --apply' \\
      < backend/scripts/postpone_open_polls.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, date, datetime, timedelta

# `python scripts/postpone_open_polls.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.season import Poll  # noqa: E402
from app.services.config_service import ConfigService  # noqa: E402

#: 完成标记。存的是上次实际写入的 ``--until``（ISO 8601），不是布尔——
#: 「同一个 until 重跑」要收敛，「换了 until 重跑」要拦，两者靠比对这个值区分。
MARKER_KEY = "civic_poll_postpone_until"

#: 标记所属的 system_config 分组，与选举/投票的其它键一致。
MARKER_GROUP = "civic"


class PostponeRefused(RuntimeError):
    """防呆拒绝。**必须在第一条 UPDATE 之前抛**，使拒绝是真正的 no-op。

    对标 ``civic_membership.CivicStandingRefused`` 与 ``PlayerPurgeRefused``：
    raise 而非静默跳过——静默跳过会让运维以为脚本跑过了。
    """


async def find_targets(db) -> list[Poll]:
    """在途 poll，按 ``closes_at`` 排序。

    **只查 ``status == "open"``**。已 ``closed`` 的 poll 推后 ``closes_at`` 不会
    让它重开（``close_due_polls`` 只扫 open），但会污染历史记录，且下一个读这张
    表的人会以为它还在途。
    """
    return list((await db.execute(
        select(Poll).where(Poll.status == "open").order_by(Poll.closes_at)
    )).scalars().all())


async def _next_auto_election(db) -> date | None:
    """下一次自动选举的日期，取不到时返回 None（此时不设上界）。

    口径与 ``election_service.open_election`` 一致：``election_last_opened``
    + ``settings.election_interval_days``。推过这个边界会让新旧两张镇长 poll
    撞车——世界里同时有两场选举，谁先关票谁说了算。
    """
    last = await ConfigService(db).get("election_last_opened")
    if not last:
        return None
    try:
        return date.fromisoformat(str(last)) + timedelta(
            days=settings.election_interval_days)
    except ValueError:
        # 与 election_service.py:126 同样的容错：读不懂就不设上界，
        # 而不是让一个脏值把整个运维动作卡死。
        return None


async def postpone(
    db,
    *,
    until: datetime,
    apply: bool = False,
    force_rerun: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    """把每张在途 poll 的 ``closes_at`` 设为 ``until``。返回逐条报告。

    报告 action 取值：

    ``would_postpone``     dry-run 下会改的
    ``postponed``          实际改了
    ``already_at_target``  已经就是 ``until``（幂等重跑走这条）

    所有防呆检查跑在任何写入之前；任一不过直接抛 :class:`PostponeRefused`。
    """
    now = now or datetime.now(UTC)

    # ── 防呆（全部在第一条 UPDATE 之前）─────────────────────────────
    if until <= now:
        raise PostponeRefused(
            f"--until {until.isoformat()} 不晚于当前时间 {now.isoformat()}；"
            "推到过去等于让下一次夜间 cron 立刻结票")

    boundary = await _next_auto_election(db)
    if boundary is not None and until.date() > boundary:
        raise PostponeRefused(
            f"--until {until.date()} 晚于下次自动选举 {boundary}；"
            "两张镇长 poll 会撞车，请选更早的日期")

    cs = ConfigService(db)
    marker = await cs.get(MARKER_KEY)
    if marker and str(marker) != until.isoformat() and not force_rerun:
        raise PostponeRefused(
            f"本脚本已在 {marker} 上跑过，而这次的 --until 是 {until.isoformat()}。"
            "换目标日期属于一次新的数据变更，须显式带 --force-rerun")

    # ── 写入 ───────────────────────────────────────────────────────
    report: list[dict] = []
    for poll in await find_targets(db):
        current = poll.closes_at
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        entry = {
            "id": poll.id,
            "question": poll.question,
            "before": current.isoformat() if current else None,
            "after": until.isoformat(),
        }
        if current == until:
            entry["action"] = "already_at_target"
        elif apply:
            poll.closes_at = until
            entry["action"] = "postponed"
        else:
            entry["action"] = "would_postpone"
        report.append(entry)

    if apply:
        await cs.set(MARKER_KEY, until.isoformat(),
                     group=MARKER_GROUP, updated_by="postpone_open_polls")
        await db.commit()
    return report


def render(report: list[dict], *, apply: bool) -> str:
    """人读的报告。dry-run 与 apply 的措辞必须一眼可分。"""
    head = "APPLY(已写库)" if apply else "DRY-RUN(未写库)"
    lines = [f"postpone_open_polls — {head}"]
    counts: dict[str, int] = {}
    for e in report:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
        lines.append(
            f"  [{e['action']:>18}] {e['question'][:32]:<34}"
            f" {e['before']} → {e['after']}")
    if not report:
        lines.append("  (没有在途 poll——无事可做)")
    lines.append("汇总: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append(
        "提醒: 关票发生在 closes_at 之后的**第一次**夜间 cron（每天 23:00 UTC），"
        "不是 closes_at 那一刻。")
    return "\n".join(lines)


async def _run(args) -> None:
    from app.database import async_session

    until = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)

    async with async_session() as db:
        report = await postpone(db, until=until, apply=args.apply,
                                force_rerun=args.force_rerun)
    print(render(report, apply=args.apply))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="把在途 poll 的 closes_at 推后一次（07-27B A3）。"
                    "默认 dry-run 只出差异报告。")
    parser.add_argument("--until", required=True,
                        help="新的 closes_at，ISO 8601，例 2026-07-31T23:29:43Z")
    parser.add_argument("--apply", action="store_true",
                        help="真正写库（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式 dry-run（默认行为，便于脚本自描述）")
    parser.add_argument("--force-rerun", action="store_true",
                        help="标记已存在且换了 --until 时，显式确认这是一次新的数据变更")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 与 --dry-run 互斥")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
