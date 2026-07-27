"""F2 Task 1 — 档位变更历史表：可回滚硬门与公民时钟锚点的共同载体。

形状照抄仓内先例 app/models/personality_history.py（同为「一行一次变更 +
resident_id 索引 + created_at」的审计表）。reason 与 reason_code 刻意分列：
code 可以外发（WS payload / 探针），text 永不外发。
"""
import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident


def test_migration_single_head_and_chains_onto_050():
    """`alembic heads` 单头，且建表迁移挂在本 worktree 实测的链头 050 上。

    收口注记：主线并行的 lab 迁移也取 051 前缀（不同 revision id），两条线
    合并后会出现双头，按仓内先例（048_add_town_treasury / 049_add_policies
    的线性化）在收口时把后落地的一支 re-chain，本测试的断言随之更新。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("051_add_civic_standing_history")
    assert rev is not None
    assert rev.down_revision == "050_add_resident_sprites"


def test_migration_is_additive_only():
    """纯建表 additive：``upgrade()`` / ``downgrade()`` 里只许出现建表与建索引
    的 ``op.*`` 调用，且函数体内不得出现任何数据写语句的 SQL 字符串 ——
    「零数据迁移」边界的机器可查版本。

    刻意用 **AST 扫函数体**而不是裸 substring 扫全文：迁移的模块 docstring 里
    写「本文件不含任何数据写语句」这类解释性文字是正常的，裸扫会把守卫自己
    打红（第一版就踩过这个坑）。注释与 docstring 一律不计入。
    """
    path = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / "051_add_civic_standing_history.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    allowed_ops = {"create_table", "create_index", "drop_index", "drop_table"}
    forbidden_sql = ("insert", "update", "delete", "alter table")
    called: list[str] = []
    literals: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if fn.name not in ("upgrade", "downgrade"):
            continue
        body = fn.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                      # 跳过函数自己的 docstring
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "op"):
                        called.append(func.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literals.append(node.value)

    assert called, ("没扫到任何 op.* 调用 —— 迁移文件的形状变了，先看一眼再"
                    "决定要不要改守卫")
    assert set(called) <= allowed_ops, (
        f"建表迁移只许 {sorted(allowed_ops)}，实际出现了 {sorted(set(called))}"
        " —— 这一批只许建表（op.execute / batch_alter_table / add_column /"
        " drop_column 都在禁区里）")
    offenders = [s for s in literals
                 if any(kw in s.lower() for kw in forbidden_sql)]
    assert offenders == [], (
        f"迁移正文里出现了数据写语句字符串 {offenders} —— 这一批只许建表")


def test_model_shape():
    cols = CivicStandingHistory.__table__.columns
    assert CivicStandingHistory.__tablename__ == "civic_standing_history"
    assert cols["id"].primary_key is True
    assert cols["resident_id"].nullable is False
    assert cols["resident_id"].index is True
    assert cols["old_standing"].nullable is False
    assert cols["new_standing"].nullable is False
    # reason 是自由文本、可为空、永不外发；reason_code 是可外发的枚举码
    assert isinstance(cols["reason"].type, sa.Text)
    assert cols["reason"].nullable is True
    assert cols["reason_code"].nullable is False
    assert cols["actor"].nullable is False
    assert isinstance(cols["evidence_json"].type, sa.JSON)
    # 公民时钟锚点（世界时间）与审计时间（真实时间）是两列，不可合并
    assert cols["world_at"].nullable is False
    assert cols["created_at"].nullable is False
    names = {ix.name for ix in CivicStandingHistory.__table__.indexes}
    assert "ix_civic_standing_history_resident_created" in names


@pytest.mark.anyio
async def test_table_is_created_by_metadata(db_engine):
    """models/__init__.py 注册了模型，Base.metadata.create_all（main.py 的
    测试路径）才看得到这张表。"""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "civic_standing_history" in names


@pytest.mark.anyio
async def test_row_roundtrips_with_world_and_real_time(db_session):
    r = Resident(slug="ugc-1", name="ugc-1", district="town_hall", status="idle",
                 resident_type="resident", creator_id="u1", tile_x=1, tile_y=1)
    db_session.add(r)
    await db_session.flush()
    world_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    db_session.add(CivicStandingHistory(
        resident_id=r.id, old_standing="denizen", new_standing="citizen",
        reason="满足门槛：在镇 40 世界日 / 3 位锚定公民",
        reason_code="threshold_met", actor="civic_promotion",
        evidence_json={"world_days": 40.0, "peers": 3, "min_familiarity": 0.2},
        world_at=world_at,
    ))
    await db_session.commit()

    row = (await db_session.execute(select(CivicStandingHistory))).scalar_one()
    assert row.resident_id == r.id
    assert (row.old_standing, row.new_standing) == ("denizen", "citizen")
    assert row.evidence_json["peers"] == 3
    stored = row.world_at
    if stored.tzinfo is None:          # sqlite 丢时区 → 按 UTC 补回
        stored = stored.replace(tzinfo=UTC)
    assert stored == world_at
