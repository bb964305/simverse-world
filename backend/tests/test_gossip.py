"""E3 gossip: verbatim vs distorted, hop termination, importance cap, chain trace."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.models.memory import Memory


def _mock_client(text):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _resident(db, slug):
    r = Resident(slug=slug, name=slug, creator_id="system", district="cafe", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


async def _origin(db, speaker_id, third_id, imp=0.8, hops=0, content="C 昨天摔了一跤"):
    m = Memory(resident_id=speaker_id, type="event", content=content, importance=imp,
               source="event", related_resident_id=third_id, metadata_json={"hops": hops} if hops else None)
    db.add(m)
    await db.commit()
    return m


@pytest.mark.anyio
async def test_gossip_verbatim(db_session):
    from app.services import gossip_service as gs
    a = await _resident(db_session, "a")
    b = await _resident(db_session, "b")
    c = await _resident(db_session, "c")
    await _origin(db_session, a.id, c.id, imp=0.8, hops=0)

    with patch("app.services.gossip_service.random.random", side_effect=[0.1, 0.9]):  # gate pass, distort miss
        g = await gs.maybe_gossip(db_session, a, b)

    assert g is not None and g.source == "gossip" and g.related_resident_id == c.id
    assert g.metadata_json["hops"] == 1 and g.metadata_json["distorted"] is False
    assert g.content == "C 昨天摔了一跤"  # verbatim


@pytest.mark.anyio
async def test_gossip_distorted_at_higher_hops(db_session):
    from app.services import gossip_service as gs
    a = await _resident(db_session, "a")
    b = await _resident(db_session, "b")
    c = await _resident(db_session, "c")
    await _origin(db_session, a.id, c.id, hops=2)  # new hops 3, distort prob 0.6

    with patch("app.services.gossip_service.random.random", side_effect=[0.1, 0.1]), \
         patch.object(gs, "get_client", return_value=_mock_client("C 昨天从楼上摔了下来！")), \
         patch.object(gs, "record_usage", new_callable=AsyncMock):
        g = await gs.maybe_gossip(db_session, a, b)

    assert g.metadata_json["hops"] == 3 and g.metadata_json["distorted"] is True
    assert "楼上" in g.content


@pytest.mark.anyio
async def test_gossip_terminates_at_max_hops(db_session):
    from app.services import gossip_service as gs
    a = await _resident(db_session, "a")
    b = await _resident(db_session, "b")
    c = await _resident(db_session, "c")
    await _origin(db_session, a.id, c.id, hops=4)  # terminal

    with patch("app.services.gossip_service.random.random", side_effect=[0.1, 0.1]):
        assert await gs.maybe_gossip(db_session, a, b) is None


@pytest.mark.anyio
async def test_gossip_importance_capped(db_session):
    from app.services import gossip_service as gs
    a = await _resident(db_session, "a")
    b = await _resident(db_session, "b")
    c = await _resident(db_session, "c")
    await _origin(db_session, a.id, c.id, imp=1.0, hops=0)

    with patch("app.services.gossip_service.random.random", side_effect=[0.1, 0.9]):
        g = await gs.maybe_gossip(db_session, a, b)
    assert g.importance == 0.7  # min(1.0*0.8, 0.7)


@pytest.mark.anyio
async def test_rumor_chain(db_session):
    from app.services import gossip_service as gs
    a = await _resident(db_session, "a")
    b = await _resident(db_session, "b")
    c = await _resident(db_session, "c")
    origin = await _origin(db_session, a.id, c.id, hops=0)

    # A→B verbatim, then B→? — simulate two hops sharing the origin id.
    with patch("app.services.gossip_service.random.random", side_effect=[0.1, 0.9]):
        g1 = await gs.maybe_gossip(db_session, a, b)
    db_session.add(Memory(resident_id="x", type="event", content="传到更远", importance=0.5,
                          source="gossip", related_resident_id=c.id,
                          metadata_json={"origin_memory_id": origin.id, "hops": 2, "distorted": True}))
    await db_session.commit()

    chain = await gs.get_rumor_chain(db_session, origin.id)
    assert len(chain) == 3  # origin + g1 + the far one
    assert chain[0]["id"] == origin.id
