"""Safety and protocol guards for the write-enabled local market bridge."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from scripts.local_market_purchase_bridge import (
    LOCAL_VISIT_PREFIX,
    decode_local_frame,
    require_temp_sqlite,
    sqlite_async_url,
)


def test_temp_database_guard_accepts_only_existing_file_below_temp(tmp_path):
    database = tmp_path / "demo.db"
    sqlite3.connect(database).close()

    resolved = require_temp_sqlite(database)

    assert resolved == database.resolve()
    assert sqlite_async_url(resolved).startswith("sqlite+aiosqlite:////")


def test_temp_database_guard_rejects_repository_database():
    repository_db = Path(__file__).resolve().parents[1] / "skills_world_dev.db"
    if not repository_db.exists():
        pytest.skip("repository development database is absent")
    with pytest.raises(ValueError, match="OS temp"):
        require_temp_sqlite(repository_db)


def test_decode_local_frame_accepts_broadcast_data_without_mutating_it():
    frame = {
        "type": "caravan_state",
        "visit_id": LOCAL_VISIT_PREFIX + "abc-0001",
        "phase": "trading",
    }
    encoded = json.dumps({"op": "broadcast", "data": frame, "exclude": None})

    assert decode_local_frame(encoded) == frame
    assert decode_local_frame(encoded.encode()) == frame


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"op": "send", "data": {"type": "market_purchase"}}),
        json.dumps({"op": "broadcast", "data": "not-an-object"}),
        None,
    ],
)
def test_decode_local_frame_rejects_non_broadcast_shapes(payload):
    assert decode_local_frame(payload) is None
