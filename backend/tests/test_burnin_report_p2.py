"""P2 Task 8 — burn-in acceptance probes: degree-distribution skewness + info
diffusion half-life. Pure-function assertions + a seeded end-to-end fixture that
demonstrates the numbers (recorded in PROGRESS).
"""
from datetime import datetime, timedelta, UTC

import pytest

from app.config import settings
from app.services import relation_service as rel
from app.models.resident import Resident
from app.models.memory import Memory
from app.models.world_event import WorldEvent
from scripts.burnin_report import (
    degree_distribution_skewness, info_diffusion_half_life,
    diffusion_relation_correlation, render_probes_p2,
    fetch_relation_edges, fetch_event_diffusion,
)


# ------------------------- degree distribution skewness -------------------------

def test_skewness_star_is_right_skewed():
    # star: hub connected to 5 leaves → degrees [5,1,1,1,1,1] (one star, many fringe)
    edges = [("hub", f"l{i}", 0.5) for i in range(5)]
    s = degree_distribution_skewness(edges)
    assert s["n_nodes"] == 6
    assert s["max_degree"] == 5 and s["min_degree"] == 1
    assert s["skewness"] > 0        # right-skew = social stars exist


def test_skewness_regular_graph_is_symmetric():
    # a 4-cycle: every node degree 2 → zero variance → zero skew (uniform control)
    edges = [("a", "b", 0.5), ("b", "c", 0.5), ("c", "d", 0.5), ("d", "a", 0.5)]
    s = degree_distribution_skewness(edges)
    assert s["skewness"] == 0.0
    assert set(s["histogram"]) == {2}


def test_skewness_none_on_empty():
    assert degree_distribution_skewness([]) is None


def test_skewness_includes_autonomous_isolates():
    edges = [("a", "b", 0.5)]
    s = degree_distribution_skewness(edges, {"a", "b", "isolated"})
    assert s["n_nodes"] == 3
    assert s["min_degree"] == 0


# --------------------------- info diffusion half-life ---------------------------

def _diffusion(records, types, total):
    return {"records": records, "event_type": types, "total_residents": total}


def test_half_life_time_to_50pct(monkeypatch):
    monkeypatch.setattr(settings, "world_clock_k", 4)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    # 10 residents; 2 first-hand at t0, then one more each hour → 50% (5) at +3h
    recs = []
    recs += [{"event_id": "e", "resident_id": f"r{i}", "created_at": t0} for i in range(2)]
    for h in range(1, 6):
        recs.append({"event_id": "e", "resident_id": f"r{h+1}", "created_at": t0 + timedelta(hours=h)})
    out = info_diffusion_half_life(_diffusion(recs, {"e": "news"}, 10))
    ev = out["events"][0]
    assert ev["informed_count"] == 7
    assert ev["time_to_50pct_hours"] == pytest.approx(3.0)  # compatibility: real hours
    assert ev["time_to_50pct_world_hours"] == pytest.approx(12.0)


def test_half_life_weather_excluded():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    recs = [{"event_id": "w", "resident_id": f"r{i}", "created_at": t0} for i in range(10)]
    out = info_diffusion_half_life(_diffusion(recs, {"w": "weather"}, 10))
    assert out["events"] == []          # weather is not an information-gradient event


def test_half_life_control_instant():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    # all 10 informed at t0 (omniscient broadcast control) → t50 == 0
    recs = [{"event_id": "e", "resident_id": f"r{i}", "created_at": t0} for i in range(10)]
    out = info_diffusion_half_life(_diffusion(recs, {"e": "news"}, 10))
    assert out["events"][0]["time_to_50pct_hours"] == pytest.approx(0.0)
    assert out["events"][0]["time_to_50pct_world_hours"] == pytest.approx(0.0)
    assert out["events"][0]["informed_ratio"] == pytest.approx(1.0)


def test_diffusion_relation_correlation_negative_along_ties():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    # informed order r0,r1,r2,r3; strong ties chain earlier→earlier, weak later
    recs = [{"event_id": "e", "resident_id": f"r{i}", "created_at": t0 + timedelta(hours=i)}
            for i in range(4)]
    edges = [("r0", "r1", 0.9), ("r1", "r2", 0.6), ("r2", "r3", 0.2)]
    corr = diffusion_relation_correlation(_diffusion(recs, {"e": "news"}, 4), edges)
    assert corr is not None and corr < 0    # later informed = weaker tie to known set


def test_render_probes_p2_empty():
    block = render_probes_p2([], _diffusion([], {}, 0))
    assert "-" in block and "P2" in block


# ------------------------- seeded end-to-end (出数) -----------------------------

@pytest.mark.anyio
async def test_seeded_probes_end_to_end(db_session, monkeypatch, capsys):
    """Seed a star-shaped relationship graph + a gradient-diffused event, then run
    the real fetch + probe path. Demonstrates the numbers for PROGRESS."""
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    # star graph: hub strongly tied to 6 spokes; two peripheral weak links
    for i in range(7):
        db_session.add(Resident(id=f"n{i}", slug=f"n{i}", name=f"N{i}", creator_id="sys",
                                district="cafe", status="idle", tile_x=1, tile_y=1))
    await db_session.commit()
    for i in range(1, 7):
        await rel.bump(db_session, "n0", f"n{i}", d_familiarity=0.5)
    await rel.bump(db_session, "n1", "n2", d_familiarity=0.3)   # a couple of cross links

    edges = await fetch_relation_edges(db_session)
    skew = degree_distribution_skewness(edges)
    assert skew["skewness"] > 0 and skew["max_degree"] >= 6      # hub is a social star

    # a non-weather event diffusing over ~4 hours (first-hand + gossip hops)
    ev = WorldEvent(id="ev1", type="festival", title="集市日", description="集市",
                    is_active=True)
    db_session.add(ev)
    t0 = datetime.now(UTC) - timedelta(hours=5)
    for i in range(2):   # first-hand at t0
        db_session.add(Memory(id=f"fh{i}", resident_id=f"n{i}", type="event", content="集市",
                              importance=0.6, source="world_event",
                              metadata_json={"event_id": "ev1", "first_hand": True},
                              created_at=t0))
    for h, rid in enumerate(["n2", "n3", "n4"], start=1):   # gossip spreads over hours
        db_session.add(Memory(id=f"g{h}", resident_id=rid, type="event", content="听说集市",
                              importance=0.4, source="gossip",
                              metadata_json={"event_id": "ev1", "hops": h},
                              created_at=t0 + timedelta(hours=h)))
    await db_session.commit()

    diffusion = await fetch_event_diffusion(db_session)
    hl = info_diffusion_half_life(diffusion)
    ev0 = hl["events"][0]
    assert ev0["informed_count"] == 5 and ev0["informed_ratio"] == pytest.approx(5 / 7, abs=0.01)
    assert ev0["time_to_50pct_hours"] is not None and ev0["time_to_50pct_hours"] > 0  # not instant

    block = render_probes_p2(edges, diffusion)
    print("\n" + block)     # -s to view; numbers recorded in PROGRESS
    assert "度分布偏度" in block and "信息扩散半衰期" in block


@pytest.mark.anyio
async def test_diffusion_population_excludes_player_avatar(db_session):
    db_session.add_all([
        Resident(id="npc", slug="npc", name="NPC", creator_id="sys",
                 resident_type="npc", district="cafe", status="idle", tile_x=1, tile_y=1),
        Resident(id="avatar", slug="avatar", name="Avatar", creator_id="player",
                 resident_type="player", district="cafe", status="idle", tile_x=1, tile_y=1),
        WorldEvent(id="scope-event", type="news", title="消息", description="消息",
                   is_active=True),
    ])
    db_session.add_all([
        Memory(id="npc-event", resident_id="npc", type="event", content="消息",
               importance=0.5, source="world_event", metadata_json={"event_id": "scope-event"}),
        Memory(id="avatar-event", resident_id="avatar", type="event", content="消息",
               importance=0.5, source="world_event", metadata_json={"event_id": "scope-event"}),
    ])
    await db_session.commit()

    diffusion = await fetch_event_diffusion(db_session)
    assert diffusion["total_residents"] == 1
    assert {row["resident_id"] for row in diffusion["records"]} == {"npc"}
