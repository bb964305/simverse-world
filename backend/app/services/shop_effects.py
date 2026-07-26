"""Shop effect registry + built-in effects (S3 registry, D2 effects).

Two registries dispatched by item ``kind``:
  - prechecks run BEFORE the charge and may raise ShopError (e.g. rename sensitive
    word) so a rejected purchase never debits the player.
  - effects run AFTER the charge/Purchase row and perform the mutation. Effect
    failures are isolated (logged, not raised) since the charge already happened.

D2 ships handlers for gift / consumable (rename_card, portrait_redraw) / decor.
"""

import asyncio
import logging
import re
from typing import Awaitable, Callable

from app.config import settings

logger = logging.getLogger(__name__)

EffectHandler = Callable[..., Awaitable[dict | None]]

_effects: dict[str, EffectHandler] = {}
_prechecks: dict[str, EffectHandler] = {}


def register(kind: str) -> Callable[[EffectHandler], EffectHandler]:
    def decorator(fn: EffectHandler) -> EffectHandler:
        _effects[kind] = fn
        return fn
    return decorator


def register_precheck(kind: str) -> Callable[[EffectHandler], EffectHandler]:
    def decorator(fn: EffectHandler) -> EffectHandler:
        _prechecks[kind] = fn
        return fn
    return decorator


async def _skim_town_tax(db, gross: int, rate: float, reason: str) -> int:
    """S1-5: take ``int(gross * rate)`` out of a payout and credit the town
    treasury. Returns the cut actually taken (0 when the gate is off, the rate
    truncates to nothing, or the treasury write fails).

    Pure rule — a multiplication and a truncation, zero LLM cost. Gated on
    ``town_treasury_enabled`` (off → byte-level status quo: the caller pays the
    full gross and no town row is ever created) and fail-open: a treasury error
    must never break a purchase, matching the discipline of every other economy
    hook (``_pay_wage`` / ``on_work`` / market discount).
    """
    if not settings.town_treasury_enabled:
        return 0
    try:
        # S2-5: once policy storage is enabled, the typed tax_rate becomes the
        # single town tax ratio. Gate off keeps the legacy per-channel setting.
        from app.services import fiscal_policy_service
        rate = await fiscal_policy_service.tax_rate(db, fallback=rate)
        cut = int(gross * rate)
        if cut <= 0:
            return 0
        cut = min(cut, gross)
        from app.services import treasury_service
        await treasury_service.tax(db, cut, reason=reason)
        return cut
    except Exception:
        logger.warning("town tax skim failed (%s)", reason, exc_info=True)
        return 0


async def precheck_effect(db, user_id: str, item, qty: int, context: dict | None) -> None:
    """Run the pre-charge validation for the item's kind (may raise ShopError)."""
    handler = _prechecks.get(item.kind)
    if handler is not None:
        await handler(db, user_id, item, qty, context or {})


async def apply_effect(db, user_id: str, item, qty: int, context: dict | None) -> dict | None:
    """Run the post-charge effect for the item's kind, if any (isolated)."""
    handler = _effects.get(item.kind)
    if handler is None:
        return None
    try:
        return await handler(db, user_id, item, qty, context or {})
    except Exception:
        logger.warning("shop effect for kind=%s (item=%s) failed", item.kind, item.code, exc_info=True)
        return None


def _reset_for_tests() -> None:  # pragma: no cover - test helper
    _effects.clear()
    _prechecks.clear()


# ── Built-in effects (D2) ────────────────────────────────────────────

SENSITIVE_WORDS = {"习近平", "法轮功", "共产党", "fuck", "shit", "傻逼", "操你", "nigger"}
MAX_NAME_LEN = 20


def _has_sensitive(text: str) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in SENSITIVE_WORDS)


@register_precheck("consumable")
async def _precheck_consumable(db, user_id, item, qty, context):
    """Validate rename before charging so a bad name never costs coins."""
    from sqlalchemy import select
    from app.models.resident import Resident
    from app.services.shop_service import ShopError

    if item.code == "rename_card":
        slug = context.get("resident_slug")
        new_name = (context.get("new_name") or "").strip()
        if not slug or not new_name:
            raise ShopError("resident_slug and new_name are required")
        if len(new_name) > MAX_NAME_LEN:
            raise ShopError(f"name too long (max {MAX_NAME_LEN})")
        if _has_sensitive(new_name):
            raise ShopError("name contains disallowed words")
        resident = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one_or_none()
        if resident is None:
            raise ShopError("resident not found")
        if resident.creator_id != user_id:
            raise ShopError("you can only rename your own resident")
    elif item.code == "portrait_redraw":
        slug = context.get("resident_slug")
        if not slug:
            raise ShopError("resident_slug is required")
        resident = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one_or_none()
        if resident is None:
            raise ShopError("resident not found")


@register("consumable")
async def _consumable_effect(db, user_id, item, qty, context):
    if item.code == "rename_card":
        return await _rename_resident(db, user_id, context)
    if item.code == "portrait_redraw":
        return await _schedule_portrait_redraw(db, user_id, context)
    return None


async def _rename_resident(db, user_id, context):
    from sqlalchemy import select
    from app.models.resident import Resident

    slug = context["resident_slug"]
    new_name = context["new_name"].strip()
    resident = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one_or_none()
    if resident is None or resident.creator_id != user_id:
        return None
    resident.name = new_name
    await db.commit()
    try:
        from app.ws.manager import manager
        await manager.broadcast({"type": "resident_updated", "slug": slug, "name": new_name})
    except Exception:
        logger.warning("resident_updated broadcast failed", exc_info=True)
    return {"renamed": slug, "new_name": new_name}


async def _schedule_portrait_redraw(db, user_id, context):
    from sqlalchemy import select
    from app.models.resident import Resident

    slug = context["resident_slug"]
    resident = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one_or_none()
    if resident is None:
        return None
    # Off the request path: regenerate + notify when done.
    asyncio.create_task(redraw_and_notify(user_id, resident.id))
    return {"status": "redrawing", "resident_slug": slug}


async def redraw_and_notify(user_id: str, resident_id: str) -> None:
    """Regenerate a resident portrait then S4-notify. Best-effort, own session."""
    from sqlalchemy import select
    from app.database import async_session
    from app.models.resident import Resident
    from app.services.portrait_service import generate_portrait
    from app.services.notification_service import notify

    try:
        async with async_session() as db:
            resident = (await db.execute(select(Resident).where(Resident.id == resident_id))).scalar_one_or_none()
            if resident is None:
                return
            url = await generate_portrait(resident.id, resident.name, resident.persona_md or "")
            if url:
                resident.portrait_url = url
                await db.commit()
                await notify(db, user_id, "system", "肖像已重绘", f"{resident.name} 的新肖像已生成", {"resident_slug": resident.slug})
            else:
                await notify(db, user_id, "system", "肖像重绘未完成", "图像服务暂不可用，稍后再试", {"resident_slug": resident.slug})
    except Exception:
        logger.warning("portrait redraw failed for %s", resident_id, exc_info=True)


@register("gift")
async def _gift_effect(db, user_id, item, qty, context):
    """Write a gift memory to the target resident + pay the creator a 20% share."""
    from sqlalchemy import select
    from app.models.resident import Resident
    from app.models.user import User
    from app.memory.service import MemoryService
    from app.services.coin_service import reward

    slug = context.get("resident_slug")
    if not slug:
        return None
    resident = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one_or_none()
    if resident is None:
        return None

    buyer = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    buyer_name = buyer.name if buyer else "有人"
    boost = float((item.payload_json or {}).get("relationship_boost", 0.1))

    await MemoryService(db).add_memory(
        resident_id=resident.id,
        type="event",
        content=f"{buyer_name} 送了我{item.name}",
        importance=0.75,
        source="gift",
        related_user_id=user_id,
        metadata_json={"relationship_boost": boost, "gift": item.code},
    )

    share = 0
    gift_tax = 0
    if resident.creator_id and resident.creator_id not in ("system", user_id):
        share = int(item.price_sc * qty * 0.2)
        # S1-5 optional knob: town_tax_rate_gift defaults to 0.0, so the shipped
        # behavior is byte-identical to the pre-S1-5 payout even with the master
        # gate ON — the rate is the only thing that makes it bite.
        gift_tax = await _skim_town_tax(
            db, share, settings.town_tax_rate_gift, f"gift_tax:{item.code}")
        share -= gift_tax
        if share > 0:
            await reward(db, resident.creator_id, share, f"gift_share:{item.code}")

    # E1: receiving a gift lifts the resident's mood.
    try:
        from app.services.mood_service import apply_mood_event
        await apply_mood_event(db, resident, dv=0.25, da=0.1)
    except Exception:
        logger.warning("gift mood bump failed", exc_info=True)

    # Realism P2-2: a gift raises affinity (player→resident) by the item's
    # relationship_boost — this is the consumer the diagnosis report flagged as
    # missing ("未见消费端"). No-op when the relations gate is off.
    if settings.realism_relations_enabled:
        try:
            from app.services import relation_service
            await relation_service.bump(
                db, resident.id, user_id, d_affinity=boost,
                type1="resident", type2="player",
            )
        except Exception:
            logger.warning("gift relation bump failed", exc_info=True)

    return {"gift": item.code, "resident_slug": slug, "relationship_boost": boost,
            "creator_share": share, "gift_tax": gift_tax}


@register("decor")
async def _decor_effect(db, user_id, item, qty, context):
    """Decor purchase is inventory-only (B3 reads the purchases table); no-op."""
    return {"stored": item.code, "qty": qty}


@register("tip")
async def _tip_effect(db, user_id, item, qty, context):
    """A4: tip a bulletin post → tips_sc += amount, creator gets 80% share."""
    from sqlalchemy import select
    from app.models.bulletin_post import BulletinPost
    from app.models.resident import Resident
    from app.services.coin_service import reward
    from app.events.bus import emit

    post_id = (context or {}).get("post_id")
    if not post_id:
        return None
    post = await db.get(BulletinPost, post_id)
    if post is None:
        return None
    amount = item.price_sc * qty
    post.tips_sc += amount
    await db.commit()

    share = 0
    tip_tax = 0
    if post.author_resident_id:
        resident = (await db.execute(select(Resident).where(Resident.id == post.author_resident_id))).scalar_one_or_none()
        if resident and resident.creator_id and resident.creator_id not in ("system", user_id):
            share = int(amount * 0.8)
            # S1-5: shares the gift rate knob (default 0.0 → status quo).
            tip_tax = await _skim_town_tax(
                db, share, settings.town_tax_rate_gift, f"tip_tax:{post_id}")
            share -= tip_tax
            if share > 0:
                await reward(db, resident.creator_id, share, f"tip_share:{post_id}")

    await emit(db, "purchase_tip", user_id=user_id, post_id=post_id)  # D1 patron
    return {"tips_sc": post.tips_sc, "creator_share": share, "tip_tax": tip_tax}


@register("resident_work")
async def _resident_work_effect(db, user_id, item, qty, context):
    """M1 F1.4: a resident-made good. Buying it decrements the limited stock
    (deactivating the listing at 0), credits the maker's treasury, and leaves
    the maker a memory that their work sold — a purchase is a plot beat."""
    from sqlalchemy import select
    from app.models.resident import Resident
    from app.services import coin_service
    from app.services.duty_service import set_wallet_cache

    payload = dict(item.payload_json or {})
    creator_slug = payload.get("creator_slug")
    if not creator_slug:
        return None

    gross = item.price_sc * qty
    # S1-5: the town's primary tax intake — a sales-tax skim off resident-made
    # goods. Gate off → cut == 0 → ``earned`` is the untouched gross (status quo).
    cut = await _skim_town_tax(
        db, gross, settings.town_tax_rate_sales, f"sales_tax:{item.code}")
    earned = gross - cut
    if earned > 0:
        await coin_service.treasury_credit(db, creator_slug, earned, reason=f"work_sold:{item.code}")

    stock = int(payload.get("stock", 1)) - qty
    payload["stock"] = max(0, stock)
    item.payload_json = payload
    if stock <= 0:
        item.active = False
    await db.commit()

    creator = (await db.execute(
        select(Resident).where(Resident.slug == creator_slug)
    )).scalar_one_or_none()
    if creator is not None:
        try:
            balance = await coin_service.treasury_balance(db, creator_slug)
            set_wallet_cache(db, creator, balance)
            from app.memory.service import MemoryService
            await MemoryService(db).add_memory(
                creator.id, "event",
                f"我的「{item.name}」被人买走了,挣了 {earned} 枚硬币。有人喜欢我做的东西,真好。",
                0.6, "observation",
            )
            await db.commit()
        except Exception:
            logger.warning("resident_work maker memory failed for %s", creator_slug, exc_info=True)

    return {"resident_work": item.code, "creator_slug": creator_slug,
            "earned": earned, "sales_tax": cut, "stock": payload["stock"]}
