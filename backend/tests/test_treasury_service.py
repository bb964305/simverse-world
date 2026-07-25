"""S1-5 镇财政闭环 — TreasuryService / 税 hook / funded wage / nightly / REST-WS.

Spec: archive/2026-07-25/docs/kickoffs/KICKOFF_S1-5_treasury.md §5 (test names are
taken verbatim from that section).

Every gated path carries a "gate off → byte-level status quo" assertion: the
module's single master switch is ``settings.town_treasury_enabled`` and it
defaults to False, so the whole suite must pin it explicitly.
"""
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models.town_treasury import TOWN_KEY, TownTreasury


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------- #
# Task 1 — town_treasuries table + model + migration                          #
# --------------------------------------------------------------------------- #

def test_town_treasury_migration_single_head():
    """`alembic heads` stays single-headed in this worktree and the S1-5
    migration chains onto the measured head (047_add_issue_stances).

    NOTE (收口): the migration file keeps the ``NNN`` placeholder number — the
    parallel S2-5 line also chains onto 047, so the main session linearizes the
    numbers at merge time and re-runs this single-head assertion.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("NNN_add_town_treasury")
    assert rev is not None
    assert rev.down_revision == "047_add_issue_stances"


def test_town_treasury_model_shape():
    """Mirrors resident_treasuries: slug-ish PK + balance_sc + updated_at."""
    cols = TownTreasury.__table__.columns
    assert TownTreasury.__tablename__ == "town_treasuries"
    assert cols["key"].primary_key is True
    assert isinstance(cols["key"].type, sa.String)
    assert cols["key"].type.length == 100
    assert isinstance(cols["balance_sc"].type, sa.Integer)
    assert isinstance(cols["updated_at"].type, sa.DateTime)
    assert cols["updated_at"].type.timezone is True
    assert TOWN_KEY == "town"


@pytest.mark.anyio
async def test_town_treasuries_table_created(db_engine):
    """models/__init__.py registers the model so Base.metadata.create_all
    (the main.py / conftest test path) sees the new table."""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "town_treasuries" in names


@pytest.mark.anyio
async def test_town_treasury_starts_empty(db_session):
    """The town account is created on demand (upsert), not seeded."""
    rows = (await db_session.execute(select(TownTreasury))).scalars().all()
    assert rows == []
