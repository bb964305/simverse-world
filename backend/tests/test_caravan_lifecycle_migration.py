"""The caravan schema stays on the repository's single linear Alembic chain."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_caravan_migration_follows_embedding_queue_on_single_linear_chain():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("060_add_caravan_lifecycle")
    assert revision.down_revision == "059_add_embedding_queue_index"
    assert len(script.get_heads()) == 1
    assert "060_add_caravan_lifecycle" in {
        rev.revision for rev in script.walk_revisions()
    }


def test_market_visitor_migration_extends_caravan_lifecycle_on_linear_chain():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("061_add_caravan_market_visitors")

    assert revision.down_revision == "060_add_caravan_lifecycle"
    assert len(script.get_heads()) == 1
    assert "061_add_caravan_market_visitors" in {
        rev.revision for rev in script.walk_revisions()
    }


def test_agent_player_migrations_extend_market_visitors_and_are_head():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)

    agent_players = script.get_revision("062_add_agent_players")
    npc_turns = script.get_revision("063_agent_npc_chat_turns")

    assert agent_players.down_revision == "061_add_caravan_market_visitors"
    assert npc_turns.down_revision == "062_add_agent_players"
    assert script.get_heads() == ["063_agent_npc_chat_turns"]
