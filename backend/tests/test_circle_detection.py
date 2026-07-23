"""P2 Task 4 — circle detection + three consumers.

Covers: connected-components partition on a known graph, meta_json.circle_id
stamping, the admin /admin/social-graph endpoint (auth + payload), the script
circle:<id> secret expansion, and the digest circle line.
"""
import pytest
from sqlalchemy import select

from app.config import settings
from app.services import relation_service as rel
from app.services import circle_service
from app.models.resident import Resident
from app.models.user import User
from app.models.memory import Memory


async def _residents(db, *ids):
    for i in ids:
        db.add(Resident(id=i, slug=i, name=i.upper(), creator_id="sys",
                        district="cafe", status="idle", tile_x=1, tile_y=1))
    await db.commit()


async def _known_graph(db):
    # Circle 1: A-B(0.5), B-C(0.4)  → {A,B,C}
    # Circle 2: D-E(0.5)            → {D,E}
    # F-G(0.2 < 0.3)                → not a circle
    await _residents(db, "A", "B", "C", "D", "E", "F", "G")
    await rel.bump(db, "A", "B", d_familiarity=0.5)
    await rel.bump(db, "B", "C", d_familiarity=0.4)
    await rel.bump(db, "D", "E", d_familiarity=0.5)
    await rel.bump(db, "F", "G", d_familiarity=0.2)


@pytest.mark.anyio
async def test_compute_circles_partitions_known_graph(db_session):
    await _known_graph(db_session)
    components, edges = await circle_service.compute_circles(db_session)
    comps = sorted([tuple(sorted(c)) for c in components])
    assert comps == [("A", "B", "C"), ("D", "E")]
    # weak edge F-G excluded
    assert all("F" not in c and "G" not in c for c in comps)


@pytest.mark.anyio
async def test_refresh_stamps_circle_id(db_session):
    await _known_graph(db_session)
    snap = await circle_service.refresh_circles(db_session)
    assert snap["count"] == 2
    a = await db_session.get(Resident, "A")
    b = await db_session.get(Resident, "B")
    c = await db_session.get(Resident, "C")
    d = await db_session.get(Resident, "D")
    f = await db_session.get(Resident, "F")
    assert a.meta_json["circle_id"] == "A" == b.meta_json["circle_id"] == c.meta_json["circle_id"]
    assert d.meta_json["circle_id"] == "D"
    assert (f.meta_json or {}).get("circle_id") is None   # isolated resident: no circle


@pytest.mark.anyio
async def test_expand_circle(db_session):
    await _known_graph(db_session)
    await circle_service.refresh_circles(db_session)
    members = set(await circle_service.expand_circle(db_session, "A"))
    assert members == {"A", "B", "C"}
    assert await circle_service.expand_circle(db_session, "nope") == []


@pytest.mark.anyio
async def test_social_graph_endpoint_requires_admin(client, db_session):
    # No token → rejected.
    resp = await client.get("/admin/social-graph")
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_social_graph_endpoint_returns_graph(client, db_session):
    from app.services.auth_service import create_token
    await _known_graph(db_session)
    await circle_service.refresh_circles(db_session)
    admin = User(name="adm", email="adm-sg@test.com", is_admin=True, is_banned=False)
    db_session.add(admin)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(admin.id)}"}

    resp = await client.get("/admin/social-graph", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["nodes"]) == 7
    circ = sorted([tuple(sorted(c["members"])) for c in body["circles"]])
    assert circ == [("A", "B", "C"), ("D", "E")]
    # every resident-resident edge with a real strength appears
    assert any(e["a"] in ("A",) or e["b"] in ("A",) for e in body["edges"])


@pytest.mark.anyio
async def test_script_circle_secret_expansion(db_session):
    from app.services.script_service import _resolve_secret_targets
    await _known_graph(db_session)
    await circle_service.refresh_circles(db_session)
    targets = await _resolve_secret_targets(db_session, "circle:A")
    assert {t.id for t in targets} == {"A", "B", "C"}
    # plain slug still resolves to one resident
    one = await _resolve_secret_targets(db_session, "D")
    assert [t.id for t in one] == ["D"]


@pytest.mark.anyio
async def test_digest_circle_line(db_session, monkeypatch):
    from app.services import digest_service
    from datetime import datetime, UTC
    await _known_graph(db_session)
    # Gate off → no circle line in the digest material.
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    mat = await digest_service.gather_material(db_session, datetime.now(UTC).date())
    assert mat["circle_line"] is None
    # Gate on → a circle line naming the most-active circle.
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    mat = await digest_service.gather_material(db_session, datetime.now(UTC).date())
    assert mat["circle_line"] is not None
    assert "圈子" in mat["circle_line"]
