#!/usr/bin/env python3
"""把存量 list 格式的 open poll 的 NPC 票重置为零票,让下一轮从零重投(E8 收口)。

**为什么需要它。** 生产 3 张 open poll 的 ``_npc_voters`` 都是**存量
``list[str]`` 格式**,各带 14 个在 ``residents`` 表里查不到的 slug(2026-07-25
花名册重置的事故残留)。``civic_service.run_npc_voting`` 现在会把幽灵投票人
从名册里撤出去,但对 legacy 格式**只移出名册、不动 ``npc_votes`` 计数**——旧
格式物理上没存票的归属(``_npc_voters`` 是扁平 slug 列表,不是
``{slug: option_idx}``),减错票会凭空改变某个具体选项的得票,比留一张来源不明
的票更糟(见 ``civic_service.py`` 里 ``_voter_map``/``run_npc_voting`` 的注释)。

后果:两张建筑议案(``effect.type == "dynamic_location"``,不在 ``_PERSON_TYPES``
里,结票时不做候选人存在性校验/归零)在 2026-08-01 结票时**仍由 14 张幽灵票
决定**——13 人小镇里的 2 个真玩家依然投不赢。这是唯一在信息论上站得住的订正:
legacy 格式下**所有**投票人的归属都是未知的,不只是幽灵的,所以只能把那张 poll
的 NPC 票整体清零重投,不能选择性减票。

**不止两张建筑议案。** :func:`find_targets` 按格式查,不按 poll 类型查——生产
现存的 3 张 open poll(两张建筑议案 + 一张镇长选举)如果都是 legacy list 格式,
镇长选举那张也会被这次重置一并清零重投。这符合脚本"目标集自查、不接受调用
方传 id 列表"的纪律,且结果无害(``_PERSON_TYPES`` 校验下 4 个候选人当前都不
存在于 ``residents`` 表,选举结票本来就会走"无人当选"分支,清零重投不改变这
个结局),但运维在跑之前应当知道这一点,不要误以为脚本只碰两张建筑议案。

**关票时刻不是 ``closes_at``,且脚本必须在 07-31 23:00 UTC 之前落地。** 同
``postpone_open_polls.py`` 的提醒:``close_due_polls`` 的唯一调用方是夜间 cron
(每天 Beijing 07:00 = 23:00 UTC),判据是 ``if due > now: continue``。夜间 cron
里 ``close_due_polls`` 排在 ``run_npc_voting`` 之前(见
``app/tasks/nightly_cron.py``)——重置之后,当晚同一次夜间任务的
``run_npc_voting`` 就会用当前名册从零重投,不必等到 ``closes_at``。但这意味着
本脚本(``--apply``)**必须在 07-31 23:00 UTC 之前**执行完:如果晚于这个时刻,
当晚的 ``close_due_polls``/``run_npc_voting`` 已经用旧的幽灵票跑过一轮,要再
等到下一个夜间窗口才有下一次重投机会。

**纪律**(与 ``postpone_open_polls.py`` 同源,``docs/PARALLEL_WORKSTREAMS_2026-07-27.md``
的三条硬约束):

1. 目标集由 :func:`find_targets` 自己查,**不接受调用方传 id 列表**——2026-07-25
   事故的根因正是「手工脚本自带 id 列表绕过 find_targets」;
2. ``--dry-run`` 是默认值,``--apply`` 才写库;
3. 不可重放——完成标记落在 ``system_config``,目标集变化(比如又出现了新的
   legacy 格式 poll)而没带 ``--force-rerun`` 直接拒绝,且拒绝是真 no-op(在
   第一条 UPDATE 之前抛)。空目标集(已经收敛,没有 legacy poll 剩下)永远放行
   ——那是正常的幂等重跑,不是「换了目标」。

**只处理 legacy list 格式。** 已经是 ``dict`` 的(本分支上线后新写的,或已被
本脚本处理过的)说明投票归属已知,``civic_service.run_npc_voting`` 的自动路径
已经能正确地定向撤票,脚本不许碰。

用法::

    # 差异报告(默认 dry-run,不写库)
    python scripts/reset_legacy_poll_votes.py
    # 真跑
    python scripts/reset_legacy_poll_votes.py --apply

生产上镜像里没有本文件,用 heredoc 注入(同 ``postpone_open_polls.py``)::

    ssh vm212 'cd /opt/skills-world/deploy && docker compose exec -T api \\
      python - --apply' \\
      < backend/scripts/reset_legacy_poll_votes.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# `python scripts/reset_legacy_poll_votes.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.models.season import Poll  # noqa: E402
from app.services.config_service import ConfigService  # noqa: E402

#: 完成标记。存的是上次实际重置过的 poll id 排序列表(JSON),不是布尔——
#: 「同一批目标重跑」要收敛(包括收敛到空集),「换了目标」要拦,两者靠比对这个
#: 值区分。
MARKER_KEY = "civic_legacy_poll_votes_reset_ids"

#: 标记所属的 system_config 分组,与选举/投票的其它键一致。
MARKER_GROUP = "civic"


class ResetRefused(RuntimeError):
    """防呆拒绝。**必须在第一条 UPDATE 之前抛**,使拒绝是真正的 no-op。

    对标 ``postpone_open_polls.PostponeRefused``:raise 而非静默跳过——静默
    跳过会让运维以为脚本跑过了。
    """


def _is_legacy(poll: Poll) -> bool:
    """``_npc_voters`` 是扁平 ``list[str]``(存量格式)返回 True。

    ``dict``(新格式,归属已知)或缺失(还没跑过 NPC 投票)都不是本脚本的目标。
    """
    opts = poll.options_json or []
    if not opts:
        return False
    return isinstance((opts[0] or {}).get("_npc_voters"), list)


async def find_targets(db) -> list[Poll]:
    """待重置的 open poll:``_npc_voters`` 是存量 list[str] 格式的。

    **只查 status == "open"**。已 closed 的 poll 的票数已经写进历史结果
    (``won``/``final_votes``),重置只会制造一条自相矛盾的存量记录,且
    ``close_due_polls`` 不会重新读它。
    """
    polls = list((await db.execute(
        select(Poll).where(Poll.status == "open").order_by(Poll.id)
    )).scalars().all())
    return [p for p in polls if _is_legacy(p)]


async def reset_legacy_votes(
    db, *, apply: bool = False, force_rerun: bool = False,
) -> list[dict]:
    """把每张 legacy list 格式的 open poll 的 ``npc_votes`` 清零、
    ``_npc_voters`` 清空(升级成空 dict——下一轮 ``run_npc_voting`` 用当前名册
    从零重投)。返回逐条报告。

    报告 action 取值::

        would_reset   dry-run 下会改的
        reset         实际改了

    所有防呆检查跑在任何写入之前;任一不过直接抛 :class:`ResetRefused`。空
    目标集永远放行——那是脚本已经收敛(没有 legacy poll 剩下),不是「换了
    目标」。
    """
    targets = await find_targets(db)
    current_ids = sorted(p.id for p in targets)

    # ── 防呆(在任何 UPDATE 之前)────────────────────────────────────
    cs = ConfigService(db)
    marker = await cs.get(MARKER_KEY)
    if (marker is not None and current_ids
            and sorted(marker) != current_ids and not force_rerun):
        raise ResetRefused(
            f"本脚本已在目标集 {sorted(marker)} 上跑过一轮完整重置,而这次算出"
            f"的目标集是 {current_ids}——目标集变化(比如又出现了新的 legacy "
            "格式 poll)属于一次新的数据变更,须显式带 --force-rerun 才能处理")

    # ── 写入 ───────────────────────────────────────────────────────
    report: list[dict] = []
    for poll in targets:
        opts = list(poll.options_json or [])
        raw_voters = (opts[0] or {}).get("_npc_voters") if opts else None
        voter_count = len(raw_voters) if isinstance(raw_voters, list) else 0
        entry = {
            "id": poll.id,
            "question": poll.question,
            "before_npc_votes": sum(
                int((o or {}).get("npc_votes", 0)) for o in opts),
            "before_voter_count": voter_count,
        }
        if apply:
            for o in opts:
                o["npc_votes"] = 0
            opts[0]["_npc_voters"] = {}
            poll.options_json = opts
            flag_modified(poll, "options_json")
            entry["action"] = "reset"
        else:
            entry["action"] = "would_reset"
        report.append(entry)

    if apply:
        if current_ids:
            await cs.set(MARKER_KEY, current_ids, group=MARKER_GROUP,
                         updated_by="reset_legacy_poll_votes")
        # current_ids 为空(目标集已收敛)时不写标记——保留上一次实际重置过的
        # 那批 id。防呆能力不受影响:上面的拒绝检查靠 `current_ids and ...`
        # 短路,空目标集永远放行,与是否写标记无关;不写只是不让"这次收敛到
        # 零"把"上次到底重置了哪几张 poll"这条审计线索覆盖成 []。
        await db.commit()
    return report


def render(report: list[dict], *, apply: bool) -> str:
    """人读的报告。dry-run 与 apply 的措辞必须一眼可分。"""
    head = "APPLY(已写库)" if apply else "DRY-RUN(未写库)"
    lines = [f"reset_legacy_poll_votes — {head}"]
    counts: dict[str, int] = {}
    for e in report:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
        lines.append(
            f"  [{e['action']:>12}] {e['question'][:32]:<34}"
            f" npc_votes {e['before_npc_votes']} → 0,"
            f" 名册 {e['before_voter_count']} 人 → 0")
    if not report:
        lines.append("  (没有 legacy list 格式的 open poll——无事可做)")
    lines.append("汇总: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append(
        "提醒: 重置之后由下一次夜间 cron 的 run_npc_voting 用当前名册从零重投,"
        "不必等到 closes_at。")
    return "\n".join(lines)


async def _run(args) -> None:
    from app.database import async_session

    async with async_session() as db:
        report = await reset_legacy_votes(
            db, apply=args.apply, force_rerun=args.force_rerun)
    print(render(report, apply=args.apply))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="把 legacy list 格式 open poll 的 NPC 票清零重投(E8 收口)。"
                    "默认 dry-run 只出差异报告。")
    parser.add_argument("--apply", action="store_true",
                        help="真正写库(默认 dry-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式 dry-run(默认行为,便于脚本自描述)")
    parser.add_argument("--force-rerun", action="store_true",
                        help="标记已存在且目标集变了时,显式确认这是一次新的数据变更")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 与 --dry-run 互斥")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
