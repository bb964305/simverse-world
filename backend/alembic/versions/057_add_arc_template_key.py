"""Add a stable identity for code-owned resident-goal templates.

Preset story arcs and life goals used to be deduplicated only while active.  An
achieved/abandoned goal was therefore inserted again by a later safe seed.  The
nullable key keeps custom goals unrestricted while making a preset template
one-time.

Existing production duplicates are deliberately preserved for audit.  Only the
earliest row for each known preset receives the canonical key; a separate,
reversible reconciliation may later mark replay rows as superseded.

Revision ID: 057_add_arc_template_key
Revises: 056_add_item_stock
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa


revision = "057_add_arc_template_key"
down_revision = "056_add_item_stock"
branch_labels = None
depends_on = None


_goals = sa.table(
    "resident_goals",
    sa.column("id", sa.String),
    sa.column("resident_id", sa.String),
    sa.column("kind", sa.String),
    sa.column("title", sa.String),
    sa.column("template_key", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_residents = sa.table(
    "residents",
    sa.column("id", sa.String),
    sa.column("slug", sa.String),
)

# Migrations are immutable deployment artifacts: do not import mutable seed
# modules here.  Title is part of the legacy-row fingerprint, not the new key.
_PRESET_KEYS = {
    ("lin-wanqiu", "life", "把咖啡馆变成小镇的客厅"): "preset_goal:lin-wanqiu:v1",
    ("zhou-dahe", "life", "凑齐一百个小镇故事"): "preset_goal:zhou-dahe:v1",
    ("chen-tiesheng", "life", "修好镇上每一件还能修的东西"): "preset_goal:chen-tiesheng:v1",
    ("shen-jingshu", "life", "写完那本没人知道的小说"): "preset_goal:shen-jingshu:v1",
    ("gu-mingyuan", "life", "编完《小镇镇志》"): "preset_goal:gu-mingyuan:v1",
    ("su-xiaoman", "life", "找到自己真正想学的东西"): "preset_goal:su-xiaoman:v1",
    ("he-qiaoyun", "life", "让杂货铺开成百年老店"): "preset_goal:he-qiaoyun:v1",
    ("zhao-qiwen", "life", "起草章程第五版"): "preset_goal:zhao-qiwen:v1",
    ("jiang-lin", "life", "完成旱季供水研究提案"): "preset_goal:jiang-lin:v1",
    ("a-lan", "life", "把画展办进父亲的工坊"): "preset_goal:a-lan:v1",
    ("luo-xiaozhou", "life", "让全镇没有一封信再迟到"): "preset_goal:luo-xiaozhou:v1",
    ("a-lan", "arc", "画展进工坊(与父亲和解)"): "preset_arc:a-lan:v1",
    ("shen-jingshu", "arc", "写完那本没人知道的小说"): "preset_arc:shen-jingshu:v1",
    ("zhao-qiwen", "arc", "起草并颁布章程第五版"): "preset_arc:zhao-qiwen:v1",
    ("jiang-lin", "arc", "完成旱季供水研究提案"): "preset_arc:jiang-lin:v1",
    ("zhou-dahe", "arc", "凑齐一百个小镇故事"): "preset_arc:zhou-dahe:v1",
}


def _backfill_template_keys(bind) -> int:
    """Claim only the first historical row for each preset template."""
    rows = bind.execute(
        sa.select(
            _goals.c.id,
            _residents.c.slug,
            _goals.c.kind,
            _goals.c.title,
            _goals.c.template_key,
            _goals.c.created_at,
        )
        .select_from(
            _goals.join(_residents, _residents.c.id == _goals.c.resident_id)
        )
        .where(_goals.c.kind.in_(("life", "arc")))
        .order_by(_goals.c.created_at.asc(), _goals.c.id.asc())
    ).fetchall()

    known_keys = set(_PRESET_KEYS.values())
    claimed: set[str] = {
        row.template_key for row in rows if row.template_key in known_keys
    }
    updated = 0
    for row in rows:
        template_key = _PRESET_KEYS.get((row.slug, row.kind, row.title))
        if template_key is None or template_key in claimed:
            continue
        bind.execute(
            _goals.update()
            .where(_goals.c.id == row.id)
            .values(template_key=template_key)
        )
        claimed.add(template_key)
        updated += 1
    return updated


def upgrade() -> None:
    op.add_column(
        "resident_goals",
        sa.Column("template_key", sa.String(length=128), nullable=True),
    )
    _backfill_template_keys(op.get_bind())
    op.create_index(
        "uq_resident_goals_template_key",
        "resident_goals",
        ["template_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_resident_goals_template_key", table_name="resident_goals")
    op.drop_column("resident_goals", "template_key")
