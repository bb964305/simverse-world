"""ORM metadata must be topologically sortable (no FK dependency cycles).

users.player_resident_id -> residents.id and residents.creator_id -> users.id
form a cycle; without use_alter SQLAlchemy cannot order INSERTs and
registration breaks on PostgreSQL with a transactions->users FK violation
(sqlite tests never catch it because FK enforcement is off by default).
"""
import warnings

import pytest
from sqlalchemy import exc as sa_exc


def test_metadata_sorts_without_cycle_warning():
    from app.database import Base
    import app.models.user  # noqa: F401
    import app.models.resident  # noqa: F401
    import app.models.conversation  # noqa: F401
    import app.models.transaction  # noqa: F401
    import app.models.system_config  # noqa: F401
    import app.models.forge_session  # noqa: F401
    import app.models.pending_message  # noqa: F401
    import app.models.memory  # noqa: F401
    import app.models.personality_history  # noqa: F401

    with warnings.catch_warnings():
        warnings.simplefilter("error", sa_exc.SAWarning)
        tables = [t.name for t in Base.metadata.sorted_tables]

    assert tables.index("users") < tables.index("transactions")
