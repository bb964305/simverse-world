#!/usr/bin/env python3
"""把 scope="village" 的空/只有标题行的日报重新生成填回去（07-28 final review CRIT-2 配套）。

**为什么需要它。** ``generate_village_digest`` 里"回填已有空行走 UPDATE"那
段代码，在生产**不可达**：该函数全仓只有一个调用方 ``app/tasks/nightly_cron.py``
的夜间任务，且调用时 ``day=None`` 恒解析成"今天"——不会有人拿着一个历史
日期去调它。``app/routers/digest.py`` 只暴露 3 个只读 GET，没有重生成端点；
``backend/scripts/`` 下（在本脚本之前）也没有任何 digest 回填工具。

所以本分支 commit message 里"让生产那几天存量空行能被重新生成填回去"这句
话，在本脚本之前只是一句"有能力做到"而不是"已经在做"——2026-07-17/24/25/26
四天（以及任何只留了一行标题、被旧守卫放过的坏行）对玩家依然是空白/残缺
面板。本脚本就是那条唯一能真正触达 ``generate_village_digest`` 历史日期
分支的路径。

**目标判据与生产代码共用同一把尺子。** 目标集用的是
``app.services.digest_service.has_real_digest_body``——和
``generate_village_digest`` 自己的存量早返回判据、以及落库前守卫，三处都是
同一个函数。三处标准若各写一套，这里挑出来的行会在
``generate_village_digest`` 的早返回那一步被更松的判据悄悄放过，回填就是
个空转——这正是 CRIT-2 修复里一并堵上的洞（见 ``digest_service.py`` 里
``has_real_digest_body`` 的 docstring）。

**会真的调 LLM。** ``generate_village_digest`` 对每个目标日期都会走一次
``compose_digest``，即一次真实的 LLM 调用（除非那天恰好没有素材、落冷启动
兜底文案分支）。所以 ``--dry-run``（默认）只列目标、不碰 LLM 不碰库；只有
``--apply`` 才会真正调用 LLM 并写库。

**运行时机：会挪动公告板的置顶帖。** ``generate_village_digest`` 在 UPDATE
回填分支之后照样会调 ``_pin_digest_bulletin``——这不是 bug，是它必须保持
和夜间 cron 一致的行为（回填出来的日报也要能上公告板）。但副作用是：本脚本
按 ``date`` 升序逐条回填多天时，每一条都会 unpin 上一条、pin 自己，跑完之后
公告板置顶的是**目标集里日期最晚的那一天**，不是"今天"。如果回填目标里混
了历史日期（比如本脚本原本要处理的 2026-07-17/24/25/26），而当晚 cron 已经
在今天生成过正文，"今天"这条会被挤下来，且不会自愈——当晚 cron 在
``has_real_digest_body`` 早返回处直接 return（今天的行已有正文），不会重新
调 ``_pin_digest_bulletin``，要等**次日** 23:00 UTC 的 cron 才会用新一天的
日报重新置顶。

规避办法：要么在当晚夜间 cron（23:00 UTC）跑之前执行本脚本，让 cron 顺理
成章地把"今天"重新置顶；要么跑完本脚本后手工调用
``digest_service._pin_digest_bulletin`` 把当天日报重新置顶。这是可接受的
已知行为，不是需要修的 bug——运维跑之前心里有数即可。

**纪律**（对齐 ``postpone_open_polls.py`` 与 07-25 事故后定的规矩）：

1. 目标集由 :func:`find_targets` 自己查，**不接受调用方传日期/id 列表**——
   07-25 事故的根因正是"手工脚本自带 id 列表绕过 find_targets"；
2. ``--dry-run`` 是默认值，``--apply`` 才写库；
3. **不需要"不可重放"标记**（与 ``postpone_open_polls.py`` 的
   ``MARKER_KEY`` 不同，这里刻意不加等价物）：回填本身是幂等的——一行一旦
   被成功回填出实质正文，``find_targets`` 下次就不会再选中它（判据与
   ``generate_village_digest`` 的早返回一致），不存在"换了参数重跑会产生
   不同效果"的情况（回填不像 postpone 的 ``--until`` 那样带一个会变的目标
   值）。唯一会被重跑的是**失败**的目标（LLM 又吐出了退化输出，或调用出
   异常），而"失败的目标下次还能被选中重试"正是我们想要的行为，不是需要
   拦住的重放。

用法::

    # 差异报告（默认 dry-run，不调 LLM 不写库）
    python scripts/refill_empty_digests.py
    # 真跑（会调用真实 LLM）
    python scripts/refill_empty_digests.py --apply

生产上镜像里没有本文件（``Dockerfile`` 的 ``COPY . .`` 早于它，且 api 服务
没有源码 bind mount），用 heredoc 注入::

    ssh vm212 'cd /opt/skills-world/deploy && docker compose exec -T api \\
      python - --apply' \\
      < backend/scripts/refill_empty_digests.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# `python scripts/refill_empty_digests.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.models.digest import Digest  # noqa: E402
from app.services.digest_service import (  # noqa: E402
    generate_village_digest,
    has_real_digest_body,
)


async def find_targets(db) -> list[Digest]:
    """``scope="village"`` 且没有实质正文的行，按 ``date`` 升序。

    "没有实质正文"用 ``has_real_digest_body`` 判断——同时覆盖历史上真正的
    全空行（``content_md == ""``）和被旧守卫放过的"只有标题行"退化行
    （``content_md == "# 今日头条"``）。表规模是一天一行，全量扫描
    ``scope="village"`` 足够便宜，不值得为了下推到 SQL 而把"去掉标题行"的
    字符串逻辑复制成一段脆弱的 SQL LIKE。
    """
    rows = list((await db.execute(
        select(Digest).where(Digest.scope == "village").order_by(Digest.date)
    )).scalars().all())
    return [d for d in rows if not has_real_digest_body(d.content_md or "")]


async def refill(db, *, apply: bool = False) -> list[dict]:
    """对每个目标日期重新生成日报。返回逐条报告。

    报告 ``action`` 取值：

    ``would_refill``   dry-run 下会处理的
    ``refilled``       实际重新生成并写库了
    ``failed``         调用了但没成功（LLM 又给了退化输出，或抛了别的异常）
                        —— 这一行留在目标集里，下次重跑会再被选中，这是有意
                        的（见模块 docstring 关于"不需要不可重放标记"的说明）。

    每条目标独立处理：一条失败不影响其余目标继续跑。
    """
    report: list[dict] = []
    for row in await find_targets(db):
        entry = {
            "id": row.id,
            "date": str(row.date),
            "title_before": row.title,
            "body_len_before": len(row.content_md or ""),
        }
        if not apply:
            entry["action"] = "would_refill"
            report.append(entry)
            continue

        try:
            digest = await generate_village_digest(db, row.date)
            entry["action"] = "refilled"
            entry["title_after"] = digest.title
            entry["body_len_after"] = len(digest.content_md or "")
        except Exception as exc:  # noqa: BLE001 — 一条失败不许打断整批
            # 不 rollback：generate_village_digest 的守卫在任何 db.commit()
            # 之前抛出（见 digest_service.py），没有待提交的写入需要撤销；
            # 而 db.rollback() 会 expire 掉本次 find_targets 已加载的其它
            # ORM 对象（包括还没处理到的下一个目标行），在 async session 下
            # 对已过期属性的隐式重新加载不在 greenlet 上下文里，会直接抛
            # MissingGreenlet——rollback 在这里是有害的，不是防御性的。
            entry["action"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        report.append(entry)
    return report


def render(report: list[dict], *, apply: bool) -> str:
    """人读的报告。dry-run 与 apply 的措辞必须一眼可分。"""
    head = "APPLY(已写库)" if apply else "DRY-RUN(未写库)"
    lines = [f"refill_empty_digests — {head}"]
    counts: dict[str, int] = {}
    for e in report:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
        detail = f" error={e['error']}" if e.get("error") else ""
        lines.append(
            f"  [{e['action']:>12}] {e['date']}  {e['title_before'][:40]:<40}"
            f" body_before={e['body_len_before']}{detail}")
    if not report:
        lines.append("  (没有需要回填的空/标题行日报 —— 无事可做)")
    lines.append("汇总: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append(
        "提醒: --apply 会对每条目标真实调用一次 LLM 重新生成正文；"
        "dry-run 只读库、不调 LLM。")
    return "\n".join(lines)


async def _run(args) -> None:
    from app.database import async_session

    async with async_session() as db:
        report = await refill(db, apply=args.apply)
    print(render(report, apply=args.apply))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="把 scope=village 的空/标题行日报重新生成填回去。"
                    "默认 dry-run 只出差异报告，不调 LLM。")
    parser.add_argument("--apply", action="store_true",
                        help="真正写库并调用 LLM（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式 dry-run（默认行为，便于脚本自描述）")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 与 --dry-run 互斥")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
