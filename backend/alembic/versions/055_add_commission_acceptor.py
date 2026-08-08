"""Add commissions.acceptor_resident_id (C3 NPC 接单).

M-A 唯一的一条迁移，**暗上**：只加一列 + 索引，不改任何在产行为——NPC 接单
行为由 ``NPC_TRADE_ENABLED`` 单独控制，开闸是 vm212 `deploy/.env` 的另一次变更
（迁移与开闸不同车，红线见 2026-07-25 事故复盘）。

列形态跟随同表 ``issuer_resident_id``（022_add_commissions.py:23-30）：``String``
无 FK、带索引。commissions 整表都不挂 residents 外键，新列不破例——purge_residents
是手工逐表 delete 的路径，多一条 DB 约束就多一处炸点。

revision id 刻意短于列名：alembic 自建的 ``alembic_version.version_num`` 是
``varchar(32)``（仓内最长的既有 id ``053_add_lab_run_resource_profile`` 恰好 32），
按列名直译的 ``055_add_commission_acceptor_resident`` 有 36 字符，在真 PostgreSQL
上 upgrade 到最后一步会以 ``StringDataRightTruncationError`` 炸在写版本号那一句
（本步已实测复现），故取 27 字符的 ``055_add_commission_acceptor``。

Revision ID: 055_add_commission_acceptor
Revises: 054_freeze_lab_model_cost_rate
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa


revision = "055_add_commission_acceptor"
down_revision = "054_freeze_lab_model_cost_rate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "commissions",
        sa.Column("acceptor_resident_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_commissions_acceptor_resident_id", "commissions", ["acceptor_resident_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_commissions_acceptor_resident_id", table_name="commissions")
    op.drop_column("commissions", "acceptor_resident_id")
