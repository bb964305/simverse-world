"""Add items.stock (M-A 加固:库存列化).

**暗上**:加一列 + 从 payload_json 回填初值,而**没有任何读者**——三处扣减
(shop_effects / npc_trade_service / caravan_service)在 ``ITEM_STOCK_GUARD_ENABLED``
翻开之前一行都不读这列,走的仍是旧 payload 读-改-写。开闸是 deploy/.env 的另
一次变更(红线:迁移与行为变更不同车,2026-07-25 事故复盘)。

回填不是"数据修复":它从同一行的 payload_json 推导新列的初值,不改变任何既有
语义,也**不删** payload 里的 stock —— 回滚回旧镜像后照样按 payload 跑。所以这
条迁移不像 055 那样是纯 additive(055 有一道 AST 闸禁掉任何数据写语句),它必须
写数据,理由就是上面这两句:写的是自己刚加的那一列。

nullable=True 是故意的:items 里绝大多数行(consumable/gift/decor/tip)根本没有
库存概念,NULL = 不计库存。

revision id 27→18 字符,仍在 ``alembic_version.version_num`` 的 varchar(32) 内
(055 那次实测过超长会以 StringDataRightTruncationError 炸在写版本号那一句)。

Revision ID: 056_add_item_stock
Revises: 055_add_commission_acceptor
Create Date: 2026-08-09
"""
import json

from alembic import op
import sqlalchemy as sa


revision = "056_add_item_stock"
down_revision = "055_add_commission_acceptor"
branch_labels = None
depends_on = None


# 显式带类型的 Core 表(不是裸 text()):psycopg 在 PG 上把 json 列还原成 dict、
# sqlite 上却给字符串,类型化的列让两边都走同一段 Python。
_items = sa.table(
    "items",
    sa.column("id", sa.String),
    sa.column("payload_json", sa.JSON),
    sa.column("stock", sa.Integer),
)


def _backfill_stock(bind) -> int:
    """``payload_json['stock']`` → ``items.stock``。返回回填的行数。

    单独拎成函数是为了能被测试直接调用(tests/test_item_stock_migration.py)——
    回填是这条迁移里唯一有逻辑的部分,不该只能靠一次真部署来验。
    items 是百行量级的商品目录,逐行 UPDATE 没有性能问题。
    """
    rows = bind.execute(sa.select(_items.c.id, _items.c.payload_json)).fetchall()
    filled = 0
    for row in rows:
        payload = row.payload_json
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        if not isinstance(payload, dict) or "stock" not in payload:
            continue
        try:
            stock = int(payload["stock"])
        except (TypeError, ValueError):
            continue                        # 脏值不猜,留 NULL 给自愈路径处理
        bind.execute(
            _items.update().where(_items.c.id == row.id).values(stock=stock))
        filled += 1
    return filled


def upgrade() -> None:
    op.add_column("items", sa.Column("stock", sa.Integer(), nullable=True))
    _backfill_stock(op.get_bind())


def downgrade() -> None:
    op.drop_column("items", "stock")
