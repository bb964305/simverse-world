"""059: portable partial index for the FIFO embedding compensation queue."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

from app.models.memory import Memory


MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "059_add_embedding_queue_index.py"
)
INDEX_NAME = "ix_memories_embedding_backfill_queue"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_059", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_059_chains_after_058_on_the_single_head_branch():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    assert len(script.get_heads()) == 1
    revision = script.get_revision("059_add_embedding_queue_index")
    assert revision.down_revision == "058_add_town_ledger"
    assert "059_add_embedding_queue_index" in {
        rev.revision for rev in script.walk_revisions()
    }
    assert len(revision.revision) <= 32


def test_059_runs_in_transaction_without_concurrent_machinery():
    source = MIGRATION.read_text(encoding="utf-8").lower()
    assert "autocommit" not in source
    assert "concurrently" not in source


def test_model_declares_the_partial_fifo_index():
    index = next(index for index in Memory.__table__.indexes if index.name == INDEX_NAME)
    assert [column.name for column in index.columns] == ["created_at", "id"]
    for dialect in ("postgresql", "sqlite"):
        predicate = str(index.dialect_options[dialect]["where"])
        assert "type = 'event'" in predicate
        assert "embedding IS NULL" in predicate
        assert "archived_at IS NULL" in predicate


def test_migration_creates_and_drops_partial_index_on_sqlite():
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "memories",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("type", sa.String),
        sa.Column("embedding", sa.JSON),
        sa.Column("archived_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        indexes = {index["name"]: index for index in sa.inspect(connection).get_indexes("memories")}
        assert indexes[INDEX_NAME]["column_names"] == ["created_at", "id"]
        sql = connection.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = :name"
            ),
            {"name": INDEX_NAME},
        ).scalar_one()
        assert "WHERE type = 'event' AND embedding IS NULL" in sql

        module.downgrade()
        names = {index["name"] for index in sa.inspect(connection).get_indexes("memories")}
        assert INDEX_NAME not in names
