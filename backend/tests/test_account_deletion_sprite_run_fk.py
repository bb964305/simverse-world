"""07-27B B2 — `resident_sprite_runs` 指向 users 的三个 FK 必须带 `ondelete`。

**为什么是 P1 而不是洁癖。** 迁移 050 建表时按「谁审的 / 谁发布的 / 谁回滚的」
写了 provenance 外键，但三列都没有 `ondelete`，而 `settings_service.delete_account`
完全不碰这张表。在 PostgreSQL 上默认 `NO ACTION` 是硬约束：只要某个用户审过一
次 sprite run，删他的号就会 `IntegrityError`——**注销功能对他永久失效**。

同型错误 2026-07-23 已经犯过一次并留下迁移 045（`residents.creator_id` NOT NULL
导致删号 500），050 是新表却没继承那次教训。

三列都是 `nullable=True`，语义上「审核人账号注销了」本来就该退化成「审核人未知」
而不是拒绝删号，所以 `SET NULL` 是正解，不是 `CASCADE`——审计行本身要留下。

**时序**：本修复必须先于 T1 部署。生产此刻停在 049，050 尚未 apply，所以可以
就地改迁移文件、revision id 不变，`051_add_civic_standing_history` 的
`down_revision` 不受影响。一旦 050 上了生产就只能另开一个迁移。
"""
from __future__ import annotations

import ast
from pathlib import Path

import sqlalchemy as sa

from app.database import Base
from app.models.resident_sprite_run import ResidentSpriteRun  # noqa: F401

MIGRATION = (Path(__file__).resolve().parents[1]
             / "alembic" / "versions" / "050_add_resident_sprites.py")

#: 指向 `users.id` 的 provenance 列——审计信息，账号注销后应退化为「未知」。
USER_PROVENANCE_COLUMNS = ("reviewed_by", "published_by", "rolled_back_by")


def test_model_declares_set_null_on_every_user_provenance_fk():
    """ORM 侧：三列的 ForeignKey 都必须是 `ondelete="SET NULL"`。"""
    table = Base.metadata.tables["resident_sprite_runs"]
    bad = []
    for name in USER_PROVENANCE_COLUMNS:
        for fk in table.c[name].foreign_keys:
            if fk.column.table.name != "users":
                continue
            if fk.ondelete != "SET NULL":
                bad.append(f"{name} -> ondelete={fk.ondelete!r}")
    assert not bad, (
        "resident_sprite_runs 指向 users 的 FK 缺 ondelete=SET NULL，"
        f"部署后这些用户将无法注销: {bad}")


def test_migration_declares_set_null_on_every_user_provenance_fk():
    """迁移侧：静态解析 050，确认三处 `ForeignKeyConstraint` 带 SET NULL。

    模型和迁移是两份独立的真值，只查模型会漏掉「模型改了、迁移没改」——真库
    的约束由迁移决定，`create_all` 只在测试里生效。
    """
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    found: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "ForeignKeyConstraint"):
            continue
        if len(node.args) < 2:
            continue
        cols = [e.value for e in node.args[0].elts if isinstance(e, ast.Constant)]
        refs = [e.value for e in node.args[1].elts if isinstance(e, ast.Constant)]
        if not cols or not refs or not refs[0].startswith("users."):
            continue
        ondelete = next(
            (kw.value.value for kw in node.keywords
             if kw.arg == "ondelete" and isinstance(kw.value, ast.Constant)),
            None)
        found[cols[0]] = ondelete

    missing = sorted(set(USER_PROVENANCE_COLUMNS) - set(found))
    assert not missing, f"050 里找不到这些列的 users FK: {missing}"
    bad = sorted(f"{c}={found[c]!r}" for c in USER_PROVENANCE_COLUMNS
                 if found[c] != "SET NULL")
    assert not bad, f"050 的 users FK 缺 ondelete='SET NULL': {bad}"


def test_deleting_a_reviewer_nulls_the_column_instead_of_failing():
    """行为断言：真开 FK 强制时，删用户 → 列被置 NULL，审计行留下。

    用同步 sqlite + `PRAGMA foreign_keys=ON` 直接建表，因为项目的异步
    conftest 不开 FK 强制，不开就测不出 `NO ACTION` 与 `SET NULL` 的差别——
    这正是这个缺陷能在 sqlite 全绿的情况下溜到 PostgreSQL 上的原因。
    """
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)

    # 用 ORM 落种子而不是裸 SQL：多个 NOT NULL 列（如 users.soul_coin_balance）
    # 的默认值只在 Python 侧，裸 INSERT 会绕过它们，测试就会因为与本缺陷无关的
    # 约束而红。
    from sqlalchemy.orm import Session

    from app.models.resident import Resident
    from app.models.user import User

    with Session(engine) as s:
        s.add(User(id="u1", name="审核员", email="a@b.c"))
        s.add(Resident(id="r1", slug="s1", name="n1"))
        s.flush()
        s.add(ResidentSpriteRun(id="sr1", run_id="run1", resident_id="r1",
                                generation_request_json={}, reviewed_by="u1"))
        s.commit()

    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM users WHERE id='u1'"))

    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT reviewed_by FROM resident_sprite_runs WHERE id='sr1'")).one()
    assert row[0] is None, "审核人账号注销后 reviewed_by 应退化为 NULL"
    with engine.connect() as conn:
        kept = conn.execute(sa.text(
            "SELECT count(*) FROM resident_sprite_runs")).scalar()
    assert kept == 1, "审计行本身必须留下——是 SET NULL 不是 CASCADE"
