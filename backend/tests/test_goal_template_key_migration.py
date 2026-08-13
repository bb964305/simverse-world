"""Migration 057: stable, unique identities for preset goal templates."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal


pytestmark = pytest.mark.anyio
MIGRATION = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
             / "057_add_arc_template_key.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_057", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_migration_chains_directly_after_056_on_the_single_head_line():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    revision = script.get_revision("057_add_arc_template_key")
    assert revision.down_revision == "056_add_item_stock"
    ancestry = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert revision.revision in ancestry
    assert len(revision.revision) <= 32


def test_model_has_nullable_unique_template_key():
    column = ResidentGoal.__table__.c.template_key
    assert isinstance(column.type, sa.String)
    assert column.type.length == 128
    assert column.nullable is True
    index = next(i for i in ResidentGoal.__table__.indexes
                 if i.name == "uq_resident_goals_template_key")
    assert index.unique is True


async def test_database_enforces_unique_template_key_but_allows_custom_nulls(
    db_session,
):
    resident = Resident(
        id="template-unique-resident",
        slug="template-unique-resident",
        name="Template Unique",
        district="central_plaza",
        status="idle",
        resident_type="npc",
        creator_id="system",
    )
    db_session.add(resident)
    db_session.add_all([
        ResidentGoal(
            resident_id=resident.id,
            kind="arc",
            title="custom one",
            template_key=None,
        ),
        ResidentGoal(
            resident_id=resident.id,
            kind="arc",
            title="custom two",
            template_key=None,
        ),
    ])
    await db_session.commit()

    db_session.add_all([
        ResidentGoal(
            resident_id=resident.id,
            kind="arc",
            title="seed one",
            template_key="preset_arc:test:v1",
        ),
        ResidentGoal(
            resident_id=resident.id,
            kind="arc",
            title="seed replay",
            template_key="preset_arc:test:v1",
        ),
    ])
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_backfill_claims_only_earliest_life_and_arc_rows(db_engine, db_session):
    module = _load_migration()
    resident = Resident(
        id="zhou",
        slug="zhou-dahe",
        name="周大河",
        district="central_plaza",
        status="idle",
        resident_type="npc",
        creator_id="system",
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    life_first = ResidentGoal(
        id="life-first", resident_id=resident.id, kind="life",
        title="凑齐一百个小镇故事", created_at=start,
    )
    life_replay = ResidentGoal(
        id="life-replay", resident_id=resident.id, kind="life",
        title="凑齐一百个小镇故事", created_at=start + timedelta(days=1),
    )
    arc_first = ResidentGoal(
        id="arc-first", resident_id=resident.id, kind="arc",
        title="凑齐一百个小镇故事", created_at=start,
    )
    arc_replay = ResidentGoal(
        id="arc-replay", resident_id=resident.id, kind="arc",
        title="凑齐一百个小镇故事", created_at=start + timedelta(days=1),
    )
    db_session.add_all([resident, life_first, life_replay, arc_first, arc_replay])
    await db_session.commit()

    async with db_engine.begin() as conn:
        changed = await conn.run_sync(module._backfill_template_keys)
        changed_again = await conn.run_sync(module._backfill_template_keys)
    assert changed == 2
    assert changed_again == 0

    rows = dict((await db_session.execute(
        sa.select(ResidentGoal.id, ResidentGoal.template_key)
    )).all())
    assert rows == {
        "life-first": "preset_goal:zhou-dahe:v1",
        "life-replay": None,
        "arc-first": "preset_arc:zhou-dahe:v1",
        "arc-replay": None,
    }
