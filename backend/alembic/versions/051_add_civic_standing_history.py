"""F2 —— civic_standing_history（纯建表 additive，零数据行为）。

「零迁移」边界在 F2 被显式改写为「零**数据**迁移」：允许这一次纯建表
migration，且它**不得与开闸同批**（上线四次独立变更的第 ①步，必须先于 T2
存量回填——T2 要写历史行作为公民时钟锚点）。

本文件只有 create_table + create_index，没有任何数据写语句，也不碰 residents
表；tests/test_civic_standing_history_model.py 用 AST 扫 upgrade/downgrade 的
函数体把这条约束钉住（扫函数体而不是扫全文，所以这段说明文字本身不算违规）。

Revision ID: 051_add_civic_standing_history
Revises: 050_add_resident_sprites
Create Date: 2026-07-27
"""
import sqlalchemy as sa
from alembic import op

revision = "051_add_civic_standing_history"
down_revision = "050_add_resident_sprites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "civic_standing_history",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("resident_id", sa.String(), nullable=False),
        sa.Column("old_standing", sa.String(length=20), nullable=False),
        sa.Column("new_standing", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.String(length=50), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("world_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["resident_id"], ["residents.id"],
                                 ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_civic_standing_history_resident_id",
                    "civic_standing_history", ["resident_id"])
    op.create_index("ix_civic_standing_history_resident_created",
                    "civic_standing_history", ["resident_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_civic_standing_history_resident_created",
                  table_name="civic_standing_history")
    op.drop_index("ix_civic_standing_history_resident_id",
                  table_name="civic_standing_history")
    op.drop_table("civic_standing_history")
