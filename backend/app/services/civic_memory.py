"""世界公共记忆 S6 —— 镇务事件的记忆广播通道。

``town_facts_service`` 讲「小镇**现在是什么样**」,这一层讲「小镇**刚才发生了
什么**」。生产实测:何巧云当选那天,全镇只有她自己留下一条第一人称记忆,其余
13 人零条 —— 镇务事件从来没进过任何人的脑子,于是第二天问谁都答不上来。

写入侧只有这一个出口(S7 把它收敛到 ``_clerk_announce``),幂等键
``metadata_json["civic_event"] = "<kind>:<ref>"`` 保证夜间任务补跑
(``nightly: catching up`` 是真实触发过的机制)不会重复灌记忆。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident

logger = logging.getLogger(__name__)

#: 落库的 ``memories.source``。列宽是 ``String(20)``,别往长里改;运维按这个值
#: 一句 SQL 就能把全部镇务记忆捞出来核对(S12 的可检索性硬门就这么查)。
MEMORY_SOURCE = "civic"


async def broadcast_civic_memory(
    db: AsyncSession, content: str, *, kind: str, ref: str,
    importance: float | None = None,
    exclude_resident_id: str | None = None,
) -> int:
    """把一条镇务事实写成全体自治居民的一等事件记忆。返回写入条数。

    与 ``world_event_service.write_collective_memories`` 的分工:那条通道直写
    ``Memory(importance=0.5)`` 服务天气/节庆这类琐事,刻意不参与检索候选池的
    竞争;镇务事件必须走 ``add_memory`` 的分位归一,否则永远进不了 top-30
    (生产 1280 条集体记忆全卡在 0.5-0.6,写了等于没写)。

    收件人 = ``is_autonomous``(K13 的 SQL expression;K15:含 UGC ``resident``,
    只有玩家分身不收)。镇务公告是**公共信息**,一个没有投票权的居民同样会听说
    镇长换人 —— 拿 ``is_civic_voter`` 当收件人口径是把知情面和政治权利这两个
    正交概念焊死。

    幂等键 ``civic_event = f"{kind}:{ref}"``,``ref`` 必须是调用方给的**稳定
    值**(如 ``poll_result:{poll.id}``),不能用「刚建的那行的主键」—— 每次补跑
    都是新 uuid,幂等就完全落空了。查重按 JSON 路径逐键取值(K17:PG 的 ``->>``
    与 sqlite 的 ``JSON_EXTRACT`` 都编得出来),不按 ``source=='civic'`` 全表
    粗筛:``memories`` 没有 source 索引,而这张表只会越长越大。

    TOCTOU 容忍:nightly 与手动调用并发时最坏重复一轮广播,无资金/无状态破坏。
    不上 Redis 锁 —— 收益不抵复杂度,且该竞态 sqlite 复现不出来,单测会假绿。
    真需要时用 ``sv:civic:bcast:{kind}:{ref}`` 的 SET NX。

    整段 fail-open:广播是镇务流程的副作用,写不进去只记 warning 并返回**已写
    条数**,绝不能把结票或公告本身带崩(下一次补跑会把缺的人补齐 —— 幂等键是
    按人查的,半截轮次能续上)。
    """
    if not settings.civic_memory_broadcast_enabled:
        return 0
    content = (content or "").strip()
    if not content:
        return 0

    civic_event = f"{kind}:{ref}"
    raw_importance = (settings.civic_memory_importance if importance is None
                      else importance)
    written = 0
    try:
        already = set((await db.execute(
            select(Memory.resident_id).where(
                Memory.metadata_json["civic_event"].as_string() == civic_event)
        )).scalars().all())
        # 按 slug 定序:半截轮次续写时的推进顺序稳定,排查时好对账。
        recipients = (await db.execute(
            select(Resident.id).where(Resident.is_autonomous).order_by(Resident.slug)
        )).scalars().all()

        svc = MemoryService(db)
        for resident_id in recipients:
            if resident_id in already or resident_id == exclude_resident_id:
                continue
            # add_memory 自带 commit(K16),所以这里逐条落地 —— 本函数因此绝不
            # 能被塞进别人的复合事务中段(S9 的广播点就为这条挪到 commit 之后)。
            await svc.add_memory(
                resident_id, "event", content, raw_importance, MEMORY_SOURCE,
                metadata_json={"civic_event": civic_event, "civic_kind": kind},
            )
            written += 1
    except Exception:
        logger.warning("CIVIC_BROADCAST_FAILED civic_event=%s written=%d",
                       civic_event, written, exc_info=True)
    return written
