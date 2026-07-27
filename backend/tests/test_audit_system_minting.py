"""只读对账脚本的聚合逻辑。"""
from datetime import datetime, UTC

from scripts.audit_system_minting import MintingRow, aggregate


def _tx(user_id: str, amount: int, reason: str, day: str):
    return (user_id, amount, reason, datetime.fromisoformat(day).replace(tzinfo=UTC))


def test_aggregate_groups_by_account_day_and_reason():
    rows = aggregate([
        _tx("system", 1, "creator_passive:klaus", "2026-07-25T01:00:00"),
        _tx("system", 1, "creator_passive:maria", "2026-07-25T02:00:00"),
        _tx("system", 1, "creator_passive:klaus", "2026-07-26T01:00:00"),
    ])
    assert rows == [
        MintingRow(user_id="system", day="2026-07-25", reason="creator_passive", count=2, total=2),
        MintingRow(user_id="system", day="2026-07-26", reason="creator_passive", count=1, total=1),
    ]


def test_aggregate_strips_the_slug_suffix_from_reason():
    """reason 是 creator_passive:<slug>，按 slug 分组会炸成几百行噪音。"""
    rows = aggregate([
        _tx("system", 1, "creator_passive:a", "2026-07-25T01:00:00"),
        _tx("system", 1, "creator_passive:b", "2026-07-25T02:00:00"),
    ])
    assert len(rows) == 1
    assert rows[0].reason == "creator_passive"


def test_aggregate_keeps_negative_amounts_separate():
    rows = aggregate([
        _tx("system", 1, "creator_passive:a", "2026-07-25T01:00:00"),
        _tx("system", -5, "shop_purchase", "2026-07-25T03:00:00"),
    ])
    assert {r.reason: r.total for r in rows} == {"creator_passive": 1, "shop_purchase": -5}


def test_aggregate_handles_empty_input():
    assert aggregate([]) == []
