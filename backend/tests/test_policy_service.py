"""S2-5 policies + 四级分级审批 — unit tests (KICKOFF_S2-5_policies.md §5).

Every gated path carries a gate-off assertion: with ``POLIS_POLICY_*`` off the
behavior must be byte-level identical to the status quo (``ConfigService`` on
``system_config``, no ``policies`` row ever touched).
"""
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select

from app.config import settings
from app.models.policy import Policy


@pytest.fixture
def policy_gate(monkeypatch):
    """Turn the storage gate on for a test (default is False everywhere)."""
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    return settings


@pytest.fixture
def approval_gate(monkeypatch):
    """Turn the approval-routing gate on (independent of the storage gate)."""
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_approval_enabled", True)
    return settings


async def _count(db):
    return (await db.execute(select(func.count()).select_from(Policy))).scalar()


# --------------------------------------------------------------------------- #
# Task 1 — table + model + migration                                           #
# --------------------------------------------------------------------------- #

def test_integration_migration_single_head():
    """`alembic heads` stays single-headed and the S2-5 migration chains onto
    the verified head (047_add_issue_stances)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("048_add_policies")
    assert rev is not None
    assert rev.down_revision == "047_add_issue_stances"


def test_migration_creates_table_only_no_alter():
    """The migration must not ALTER any existing table — notably it must not
    add tier/procedure columns to world_change_proposals (KICKOFF §7)."""
    import ast

    path = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / "048_add_policies.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Compare code only — the module docstring legitimately names these APIs.
    body = [n for n in tree.body if not (isinstance(n, ast.Expr)
                                         and isinstance(n.value, ast.Constant))]
    code = "\n".join(ast.unparse(n) for n in body)
    assert "add_column" not in code
    assert "batch_alter_table" not in code
    assert "world_change_proposals" not in code


@pytest.mark.anyio
async def test_policies_table_created(db_engine):
    """models/__init__.py registers Policy so Base.metadata.create_all sees it."""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "policies" in names


@pytest.mark.anyio
async def test_policy_key_is_unique(db_session):
    """One row per key — the amend path relies on it for the conditional
    UPDATE to touch at most one row."""
    from sqlalchemy.exc import IntegrityError

    db_session.add(Policy(key="k", value="1", tier="administrative",
                          procedure="admin_direct", group="civic"))
    await db_session.commit()
    db_session.add(Policy(key="k", value="2", tier="administrative",
                          procedure="admin_direct", group="civic"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.anyio
async def test_policy_version_defaults_to_one(db_session):
    db_session.add(Policy(key="v", value="1", tier="administrative",
                          procedure="admin_direct", group="civic"))
    await db_session.commit()
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "v"))).scalar_one()
    assert row.version == 1
    assert row.created_at is not None and row.updated_at is not None


@pytest.mark.anyio
async def test_policy_value_holds_more_than_system_config_limit(db_session):
    """The 2000-char ceiling on system_config.value is why this table exists."""
    big = "x" * 5000
    db_session.add(Policy(key="big", value=f'"{big}"', tier="simple_majority",
                          procedure="civic_poll", group="civic"))
    await db_session.commit()
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "big"))).scalar_one()
    assert len(row.value) == 5002
