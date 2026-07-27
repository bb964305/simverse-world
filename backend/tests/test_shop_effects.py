"""D2 shop effects: gift, rename (+ sensitive/ownership prechecks), decor, portrait."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory
from app.models.shop import Purchase


async def _seed(db):
    from app.services.shop_service import seed_items
    await seed_items(db)


async def _user(db, email, bal=200):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _resident(db, slug, creator_id):
    r = Resident(slug=slug, name="小明", creator_id=creator_id,
                 district="central_plaza", status="idle", tile_x=1, tile_y=1, persona_md="p")
    db.add(r)
    await db.commit()
    return r


@pytest.mark.anyio
async def test_gift_writes_memory_and_pays_creator(db_session):
    from app.services.shop_service import purchase
    await _seed(db_session)
    creator = await _user(db_session, "creator@d2.com", 0)
    buyer = await _user(db_session, "buyer@d2.com", 200)
    await _resident(db_session, "klaus", creator.id)

    result = await purchase(db_session, buyer.id, "gift_flower", 1, {"resident_slug": "klaus"})

    mems = (await db_session.execute(select(Memory).where(Memory.source == "gift"))).scalars().all()
    assert len(mems) == 1
    assert mems[0].related_user_id == buyer.id and "送了我" in mems[0].content
    assert mems[0].metadata_json["relationship_boost"] == 0.1

    await db_session.refresh(creator)
    assert creator.soul_coin_balance == 3  # 20% of 15
    assert result["effect"]["creator_share"] == 3


@pytest.mark.anyio
async def test_rename_card_renames_owned_resident(db_session):
    from app.services.shop_service import purchase
    await _seed(db_session)
    owner = await _user(db_session, "owner@d2.com", 200)
    res = await _resident(db_session, "myres", owner.id)

    await purchase(db_session, owner.id, "rename_card", 1, {"resident_slug": "myres", "new_name": "新名字"})
    await db_session.refresh(res)
    assert res.name == "新名字"


@pytest.mark.anyio
async def test_rename_sensitive_word_rejected_no_charge(db_session):
    from app.services.shop_service import purchase, ShopError
    await _seed(db_session)
    owner = await _user(db_session, "o2@d2.com", 200)
    res = await _resident(db_session, "r2", owner.id)

    with pytest.raises(ShopError):
        await purchase(db_session, owner.id, "rename_card", 1, {"resident_slug": "r2", "new_name": "fuck you"})

    await db_session.refresh(owner)
    await db_session.refresh(res)
    assert owner.soul_coin_balance == 200  # never charged
    assert res.name == "小明"  # unchanged


@pytest.mark.anyio
async def test_rename_not_owned_rejected(db_session):
    from app.services.shop_service import purchase, ShopError
    await _seed(db_session)
    owner = await _user(db_session, "o3@d2.com", 200)
    other = await _user(db_session, "o4@d2.com", 200)
    await _resident(db_session, "r3", owner.id)

    with pytest.raises(ShopError):
        await purchase(db_session, other.id, "rename_card", 1, {"resident_slug": "r3", "new_name": "改个名"})


@pytest.mark.anyio
async def test_decor_is_noop_but_recorded(db_session):
    from app.services.shop_service import purchase
    await _seed(db_session)
    u = await _user(db_session, "decor@d2.com", 200)

    result = await purchase(db_session, u.id, "decor_lamp", 1, {})
    assert result["effect"]["stored"] == "decor_lamp"
    purchases = (await db_session.execute(select(Purchase))).scalars().all()
    assert len(purchases) == 1


@pytest.mark.anyio
async def test_portrait_redraw_schedules(db_session):
    from app.services import shop_effects
    from app.services.shop_service import purchase
    await _seed(db_session)
    owner = await _user(db_session, "pr@d2.com", 200)
    await _resident(db_session, "pres", owner.id)

    with patch.object(shop_effects, "redraw_and_notify", new_callable=AsyncMock):
        result = await purchase(db_session, owner.id, "portrait_redraw", 1, {"resident_slug": "pres"})
    assert result["effect"]["status"] == "redrawing"


@pytest.mark.anyio
async def test_gift_share_does_not_mint_into_sentinel(db_session):
    """gift_share: guarded ``creator_id not in ("system", user_id)`` — the
    literal only catches ADMIN_CREATOR_ID. Seed NPCs carry
    creator_id=SYSTEM_CREATOR_ID (a UUID), which slips through and mints the
    20% creator share into the sentinel account."""
    from app.services.shop_service import purchase
    from app.services.system_users import SYSTEM_CREATOR_ID
    await _seed(db_session)
    db_session.add(User(id=SYSTEM_CREATOR_ID, name="System",
                        email="system-gift@d2.com", soul_coin_balance=0))
    await db_session.commit()
    buyer = await _user(db_session, "gift-sentinel-buyer@d2.com", 200)
    await _resident(db_session, "sentinel-gift", SYSTEM_CREATOR_ID)

    await purchase(db_session, buyer.id, "gift_flower", 1, {"resident_slug": "sentinel-gift"})

    sentinel = await db_session.get(User, SYSTEM_CREATOR_ID)
    assert sentinel.soul_coin_balance == 0, \
        "gift_share must never mint Soul Coin into the sentinel account"


@pytest.mark.anyio
async def test_tip_share_does_not_mint_into_sentinel(db_session):
    """tip_share: same defect as gift_share — the bare literal ``"system"``
    lets SYSTEM_CREATOR_ID (a UUID) through and mints the 80% creator share
    into the sentinel account."""
    from app.models.bulletin_post import BulletinPost
    from app.services.shop_service import purchase
    from app.services.system_users import SYSTEM_CREATOR_ID
    await _seed(db_session)
    db_session.add(User(id=SYSTEM_CREATOR_ID, name="System",
                        email="system-tip@d2.com", soul_coin_balance=0))
    await db_session.commit()
    buyer = await _user(db_session, "tip-sentinel-buyer@d2.com", 200)
    resident = await _resident(db_session, "sentinel-tip", SYSTEM_CREATOR_ID)
    post = BulletinPost(kind="notice", title="t", content_md="c", author_resident_id=resident.id)
    db_session.add(post)
    await db_session.commit()

    await purchase(db_session, buyer.id, "tip_5sc", 1, {"post_id": post.id})

    sentinel = await db_session.get(User, SYSTEM_CREATOR_ID)
    assert sentinel.soul_coin_balance == 0, \
        "tip_share must never mint Soul Coin into the sentinel account"


@pytest.mark.anyio
async def test_gift_share_skips_sentinel_creator_but_still_levies_town_tax(db_session, monkeypatch):
    """The sentinel guard and ``_skim_town_tax`` share the same ``if`` block in
    ``_gift_effect``. Narrowing the guard must NOT skip the town tax too — a
    naive "skip the whole branch for a sentinel-owned resident" fix would zero
    out the town treasury's cut along with the (correctly) skipped payout.
    This asserts both halves independently: creator share stays unpaid, town
    tax is still skimmed."""
    from app.config import settings
    from app.models.shop import Item
    from app.services import shop_effects, treasury_service
    from app.services.system_users import SYSTEM_CREATOR_ID

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_gift", 0.2)

    db_session.add(User(id=SYSTEM_CREATOR_ID, name="System",
                        email="system-gift-tax@d2.com", soul_coin_balance=0))
    await db_session.commit()
    buyer = await _user(db_session, "gift-tax-buyer@d2.com", 200)
    await _resident(db_session, "sentinel-gift-tax", SYSTEM_CREATOR_ID)
    item = Item(code="flower-tax", kind="gift", name="花", price_sc=50, active=True,
                payload_json={"relationship_boost": 0.1})
    db_session.add(item)
    await db_session.commit()

    out = await shop_effects._gift_effect(
        db_session, buyer.id, item, 1, {"resident_slug": "sentinel-gift-tax"})

    sentinel = await db_session.get(User, SYSTEM_CREATOR_ID)
    assert sentinel.soul_coin_balance == 0, "sentinel must not be paid the creator share"
    assert out["gift_tax"] == 2, "town tax (20% of the 10 SC share) must still be skimmed"
    assert await treasury_service.balance(db_session) == 2, \
        "narrowing the sentinel guard must not skip the town-tax skim"


@pytest.mark.anyio
async def test_redraw_and_notify_updates_and_notifies(db_session):
    from app.services import shop_effects
    owner = await _user(db_session, "rn@d2.com", 0)
    res = await _resident(db_session, "rn1", owner.id)
    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    with patch("app.database.async_session", factory), \
         patch("app.services.portrait_service.generate_portrait", AsyncMock(return_value="/portraits/x.png")), \
         patch("app.services.notification_service.notify", new_callable=AsyncMock) as nmock:
        await shop_effects.redraw_and_notify(owner.id, res.id)

    nmock.assert_awaited()
