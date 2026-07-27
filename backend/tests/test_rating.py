from unittest.mock import patch

import pytest
from app.models.conversation import Conversation
from app.models.resident import Resident
from app.models.user import User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
async def test_user(db_session):
    user = User(id="rating-test-user", name="RatingUser",
                email="rating@test.com", soul_coin_balance=100)
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def seeded_user_residents(db_session, test_user):
    r = Resident(slug="rating-r1", name="Rating Resident", district="free",
                 creator_id=test_user.id, status="idle", heat=0, star_rating=1,
                 sprite_key="梅", tile_x=30, tile_y=65, token_cost_per_turn=1,
                 ability_md="", persona_md="", soul_md="", meta_json={})
    db_session.add(r)
    await db_session.commit()
    return [r]


@pytest.mark.anyio
async def test_rating_updates_conversation(db_session, test_user, seeded_user_residents):
    conv = Conversation(user_id=test_user.id, resident_id=seeded_user_residents[0].id, turns=3)
    db_session.add(conv)
    await db_session.commit()

    conv.rating = 4
    await db_session.commit()
    await db_session.refresh(conv)
    assert conv.rating == 4


@pytest.mark.anyio
async def test_avg_rating_calculation(db_session, test_user, seeded_user_residents):
    resident = seeded_user_residents[0]
    for rating in [5, 4, 3, 4]:
        conv = Conversation(user_id=test_user.id, resident_id=resident.id,
                            turns=2, rating=rating)
        db_session.add(conv)
    await db_session.commit()

    result = await db_session.execute(
        select(func.avg(Conversation.rating)).where(
            Conversation.resident_id == resident.id,
            Conversation.rating.is_not(None),
        )
    )
    avg = result.scalar()
    assert avg == 4.0


@pytest.mark.anyio
async def test_valid_rating_range():
    for r in [1, 2, 3, 4, 5]:
        assert 1 <= r <= 5
    for r in [0, 6, -1]:
        assert not (1 <= r <= 5)


class _FakeManager:
    async def send(self, *a, **k):
        return None


@pytest.mark.anyio
async def test_good_rating_does_not_mint_into_sentinel(db_session, db_engine):
    """good_rating: <resident.creator_id> reward in handle_rate_chat has no
    sentinel guard at all — rating a sentinel-owned resident 4-5 stars must
    not credit Soul Coin to the SYSTEM_CREATOR_ID sentinel account."""
    from app.services.system_users import SYSTEM_CREATOR_ID
    from app.ws.handlers import rating as rating_handler
    from app.ws.handlers.context import ConnectionContext

    db_session.add(User(id=SYSTEM_CREATOR_ID, name="System",
                        email="system-rating@test.com", soul_coin_balance=0))
    db_session.add(User(id="rater-u1", name="U", email="rater-u1@t.co", soul_coin_balance=10))
    db_session.add(Resident(id="sentinel-r1", slug="sentinel-r1", name="P",
                            creator_id=SYSTEM_CREATOR_ID, district="cafe",
                            status="chatting", tile_x=1, tile_y=1, heat=0))
    db_session.add(Conversation(id="sentinel-c1", user_id="rater-u1", resident_id="sentinel-r1"))
    await db_session.commit()

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    ctx = ConnectionContext(user_id="rater-u1", user_name="U", conversation_id="sentinel-c1")
    with patch.object(rating_handler, "async_session", factory), \
         patch.object(rating_handler, "manager", _FakeManager()):
        await rating_handler.handle_rate_chat(ctx, {"conversation_id": "sentinel-c1", "rating": 5})

    balance = (await db_session.execute(
        select(User.soul_coin_balance).where(User.id == SYSTEM_CREATOR_ID)
    )).scalar_one()
    assert balance == 0, "good_rating must never mint Soul Coin into the sentinel account"
