"""C3 NPC 接单：委托承接方从「只能是玩家」扩到「玩家或居民」的载体。

本文件先落 Step 6 的迁移/模型往返（暗上：只加列不改行为），Step 7 再在同一
文件里续接单/结算 pass 的行为测试。

列形态刻意与同表 ``issuer_resident_id`` 对齐：``sa.String`` 无 FK、带索引
（022_add_commissions.py:23-30）——commissions 表整表都不挂 residents 外键，
新列跟随既有风格，避免 purge_residents（手工逐表 delete）路径上多出一条约束。
``acceptor_user_id`` 与 ``acceptor_resident_id`` 是互斥的两列而非合并成一列：
前者对 users.id、后者对 residents.id，玩家已接的单必须能被 NPC pass 逐字节
识别为「别碰」。
"""
from pathlib import Path

import sqlalchemy as sa
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.commission import Commission
from app.models.resident import Resident


def test_migration_single_head_and_chains_onto_054():
    """`alembic heads` 单头，且新迁移挂在本 worktree 实测的链头 054 上。

    revision id 是文件名 stem（``054_freeze_lab_model_cost_rate``）而不是裸
    ``"054"``，写错会静默断链成双头。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    assert heads == ["055_add_commission_acceptor"]
    rev = script.get_revision("055_add_commission_acceptor")
    assert rev.down_revision == "054_freeze_lab_model_cost_rate"


def test_every_revision_id_fits_alembic_version_column():
    """alembic 自建的 ``alembic_version.version_num`` 是 ``varchar(32)``：超长的
    revision id 在 sqlite 上悄悄过、在真 PostgreSQL 上 upgrade 末尾才炸
    （StringDataRightTruncationError，本步实测复现过一次）。整链一起守。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    too_long = [r.revision for r in script.walk_revisions() if len(r.revision) > 32]
    assert too_long == [], f"revision id 超过 alembic_version 的 32 字符上限: {too_long}"


def test_migration_is_additive_only():
    """暗上边界的机器可查版本：只许加列/建索引，不许出现任何数据写语句。"""
    import ast

    path = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / "055_add_commission_acceptor.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    allowed_ops = {"add_column", "create_index", "drop_index", "drop_column",
                   "batch_alter_table"}
    forbidden_sql = ("insert", "update", "delete")
    called: list[str] = []
    literals: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in ("upgrade", "downgrade"):
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
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        if func.value.id in ("op", "batch"):
                            called.append(func.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literals.append(node.value)

    assert called, "没扫到任何 op.* 调用 —— 迁移文件形状变了，先看一眼再改守卫"
    assert set(called) <= allowed_ops, (
        f"暗上迁移只许 {sorted(allowed_ops)}，实际出现了 {sorted(set(called))}")
    offenders = [s for s in literals if any(kw in s.lower() for kw in forbidden_sql)]
    assert offenders == [], f"迁移正文里出现了数据写语句字符串 {offenders}"


def test_model_shape_matches_issuer_column():
    cols = Commission.__table__.columns
    col = cols["acceptor_resident_id"]
    assert isinstance(col.type, sa.String)
    assert col.nullable is True
    assert col.index is True
    # 与同表 issuer_resident_id 同形（String 无 FK），跟随 022 的既有风格
    assert col.foreign_keys == set()
    assert type(col.type) is type(cols["issuer_resident_id"].type)


@pytest.mark.anyio
async def test_acceptor_resident_id_defaults_none_and_roundtrips(db_engine):
    """新 session 重读断言：默认 None，可写可读。"""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        r = Resident(slug="tie-sheng", name="陈铁生", creator_id="system",
                     district="central_plaza", status="idle", tile_x=1, tile_y=1)
        db.add(r)
        await db.flush()
        c = Commission(issuer_resident_id=r.id, kind="chat_topic", title="带个话",
                       payload_json={"target_slug": "tie-sheng"}, reward_sc=8)
        db.add(c)
        await db.commit()
        cid, rid = c.id, r.id

    async with factory() as db:
        row = (await db.execute(select(Commission).where(Commission.id == cid))).scalar_one()
        assert row.acceptor_resident_id is None
        assert row.acceptor_user_id is None
        row.acceptor_resident_id = rid
        await db.commit()

    async with factory() as db:
        row = (await db.execute(select(Commission).where(Commission.id == cid))).scalar_one()
        assert row.acceptor_resident_id == rid
        assert row.acceptor_user_id is None
