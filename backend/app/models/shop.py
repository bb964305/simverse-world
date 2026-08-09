import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Text, Integer, Boolean, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Item(Base):
    """A purchasable catalog item (S3). Effect on purchase is dispatched by
    ``kind`` through the shop_effects registry (D2/B3/A3 register handlers)."""

    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(20))  # consumable|gift|decor|cosmetic
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(20), default="📦")
    price_sc: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # M-A 加固:库存从 payload_json 抬成真列,扣减才能走
    # ``UPDATE ... WHERE stock >= qty`` 的守卫 —— payload_json 是 JSON 列,判据
    # 写不进 WHERE,只能读-改-写,cron 与玩家撞上同一行就超卖(见
    # app/services/item_stock.py)。
    # nullable:绝大多数商品(consumable/gift/decor/tip)没有库存概念,
    # NULL = 不计库存;只有 resident_work / import_good 有值。
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    item_code: Mapped[str] = mapped_column(String(50))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    total_sc: Mapped[int] = mapped_column(Integer, default=0)
    context_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
