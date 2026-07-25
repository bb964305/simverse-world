"""S1-3 议题立场与舆论动力学 (KICKOFF_S1-3_opinion.md).

Bounded-confidence (Deffuant) stance dynamics over free-string issue keys:
atomic upsert (`_bump_stance`), chat-mood convergence, debate seeding /
settle reinforcement, nightly rule drift, digest opinion_line — all zero new
LLM calls, all behind the independent `polis_opinion_enabled` gate
(default False → byte-identical fallback to the status quo).
"""

import pytest
from pathlib import Path

import sqlalchemy as sa


# --------------------------------------------------------------------------- #
# Task 1 — table + migration                                                  #
# --------------------------------------------------------------------------- #

def test_integration_migration_single_head():
    """`alembic heads` stays single-headed and the S1-3 migration is on the
    chain (down_revision anchored on the real current head, 045)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("046_add_issue_stances")
    assert rev is not None
    assert rev.down_revision == "045_residents_creator_nullable"


@pytest.mark.anyio
async def test_issue_stances_table_created(db_engine):
    """models/__init__.py registers the model so Base.metadata.create_all
    (main.py test path) sees the new table."""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "issue_stances" in names
