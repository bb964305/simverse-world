"""F2 —— 公民权档位（civic standing）变更历史。

一行 = 一次档位变更。这张表承担两个互不重叠的职责：

1. **可回滚**（F2 硬门 2）。``old_standing`` 让恢复能回到「变更前那一档」，
   而不是一律回 citizen。T2 存量回填也必须写行（``actor="ops_backfill_t2"``），
   否则回填批次事后不可追溯。
2. **公民时钟锚点**（晋升门槛①的起算点）。锚 ``residents.created_at`` 会让
   T2 的降权对存量整批走过场——一个已在镇 200 世界日的 UGC 被降权后，开闸
   当晚条件①立刻重新满足。锚点取本表最近一行的 ``world_at``，无行才回落
   ``real_to_world(created_at)``。

形状照抄仓内先例 ``app/models/personality_history.py``。

两组时间列不可合并：``world_at`` 是**世界时间**（门槛判定用），``created_at``
是**真实时间**（审计/运维用）。世界时间以 UTC-aware 落库——``DateTime(timezone
=True)`` 在 SQLite 上会丢时区，统一转 UTC 存、读回补 UTC 才能无损往返（同
``app/services/office_service.py`` 的存储口径）。

``reason``（自由文本）与 ``reason_code``（枚举码）刻意分列：**code 可外发**
（WS payload、探针输出），**text 永不外发**。这是把撤销原因挡在无鉴权前台
接口之外的结构性保证——正是 ``meta_json`` 做不到的那一条。本表在 v1 **不加
任何读接口**（YAGNI + 隐私）。
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Text, DateTime, JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CivicStandingHistory(Base):
    __tablename__ = "civic_standing_history"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    resident_id: Mapped[str] = mapped_column(
        String, ForeignKey("residents.id"), index=True, nullable=False
    )
    # citizen / denizen / exiled —— app/services/civic_membership.CIVIC_STANDINGS
    old_standing: Mapped[str] = mapped_column(String(20), nullable=False)
    new_standing: Mapped[str] = mapped_column(String(20), nullable=False)
    # 自由文本，永不外发（无读接口、不进 WS payload、不进探针输出）
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可外发的枚举码：threshold_met / admin_grant / admin_revoke / ops_backfill / ...
    reason_code: Mapped[str] = mapped_column(String(50), nullable=False)
    # civic_promotion | civic_demotion | admin:<user_id> | ops_backfill_t2
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    # {"world_days": float, "peers": int, "min_familiarity": float, ...}
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 世界时间（公民时钟锚点）。存 UTC-aware，读回若 naive 按 UTC 补。
    world_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 真实时间（审计）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_civic_standing_history_resident_created",
              "resident_id", "created_at"),
    )
