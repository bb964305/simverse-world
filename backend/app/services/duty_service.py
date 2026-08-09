"""Town duty (职务) system — per-resident special functions.

Generalizes the lab pattern (``meta_json["lab"]["access"]``) into a data-driven
namespace: a resident may carry ``meta_json["duty"]``::

    {
        "key": "tavern_hub",           # duty identifier (DUTY registry)
        "title": "消息集散地",          # display name
        "prompt_hint": "...",          # injected into the decision prompt
        "perks": {"gossip_multiplier": 2.0},   # tunable coefficients
    }

Integration points (all fail-open — a duty side effect must never break the
tick or the calling service):

- ``prompt_hint(resident)``      → agent/prompts.py decision prompt
- ``perk(resident, key)``        → gossip probability, chat relation/mood
                                   coefficients, encounter boost, quest magnet
- ``on_work(db, resident)``      → phases/execute WORK branch; produces the
                                   duty's real-world output (commission /
                                   bulletin / world event / sketch memory)
                                   at most once per resident per game day.
- ``find_duty_resident(db, key)``→ signature lookups (digest editor, town clerk)

No new ActionType is introduced and no schema changes are needed.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, UTC

from sqlalchemy import select

from app.models.resident import Resident
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

DUTY_WORK_COOLDOWN_HOURS = 20  # ≈ once per game day, tolerant of schedule drift


# ── meta_json accessors ────────────────────────────────────────────────

def get_duty(resident) -> dict:
    return ((getattr(resident, "meta_json", None) or {}).get("duty")) or {}


def duty_key(resident) -> str | None:
    return get_duty(resident).get("key")


def perk(resident, key: str, default: float = 0.0) -> float:
    try:
        return float((get_duty(resident).get("perks") or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def prompt_hint(resident) -> str:
    """One-line duty hint for the decision prompt ('' when no duty)."""
    duty = get_duty(resident)
    hint = duty.get("prompt_hint")
    if not hint:
        return ""
    title = duty.get("title", "")
    prefix = f"你的小镇职务：{title}。" if title else ""
    return f"\n{prefix}{hint}"


def max_perk(residents, key: str, default: float = 1.0) -> float:
    """Highest perk value among a group (used for presence-based boosts)."""
    values = [perk(r, key, default) for r in residents] or [default]
    return max([default, *values])


async def find_duty_resident(db, key: str) -> Resident | None:
    """First NPC holding the given duty key. Town-scale linear scan (the
    resident table is small and meta_json JSON operators are not portable
    between sqlite and Postgres).

    S2-1: with polis_office_enabled the offices table is consulted first —
    a single indexed lookup instead of the O(N) scan (net improvement, tick
    query budget +1 max). A missing/vacant offices row falls back to the
    legacy scan (fail-open: non-office duty keys and not-yet-backfilled
    worlds keep resolving). Gate off → byte-level legacy behavior."""
    from app.config import settings
    if settings.polis_office_enabled:
        try:
            from app.models.office import Office
            holder = (await db.execute(
                select(Office.holder_slug).where(Office.office_key == key)
            )).scalar_one_or_none()
            if holder:
                r = (await db.execute(
                    select(Resident).where(Resident.slug == holder)
                )).scalar_one_or_none()
                if r is not None:
                    return r
        except Exception:
            logger.warning("offices-backed duty lookup failed for %s", key,
                           exc_info=True)
    rows = (await db.execute(
        select(Resident).where(
            Resident.is_autonomous,
            Resident.meta_json.isnot(None),
        )
    )).scalars().all()
    for r in rows:
        if duty_key(r) == key:
            return r
    return None


# ── WORK dispatcher ────────────────────────────────────────────────────

async def on_work(db, resident, *, market_day: bool = False) -> str | None:
    """Run the resident's duty output for a WORK action, at most once per
    cooldown window. Returns a short narrative line (for logs/memory) or None.
    Fail-open by design.

    On success the resident earns their duty wage into the treasury (M1 F1.1).
    On a 集市日 (market_day) the cooldown is halved so摊贩 can produce twice.
    """
    key = duty_key(resident)
    handler = _WORK_HANDLERS.get(key or "")
    if handler is None:
        return None
    try:
        r = get_redis()
        cd_key = f"sv:duty_work:{resident.id}"
        if await r.exists(cd_key):
            return None
        result = await handler(db, resident)
        if result is not None:
            cooldown = DUTY_WORK_COOLDOWN_HOURS * 3600
            if market_day:
                cooldown //= 2
            await r.set(cd_key, "1", ex=cooldown)
            await _pay_wage(db, resident)
        return result
    except Exception:
        logger.warning("duty on_work failed for %s", resident.slug, exc_info=True)
        return None


async def _pay_wage(db, resident) -> None:
    """M1 F1.1: credit the resident's duty wage into their treasury and mirror
    the fresh balance into meta_json['wallet'] (write-through cache read by the
    decision prompt — no extra tick query). No-op when the economy gate is off.

    S1-5: with ``town_treasury_enabled`` the wage is no longer MINTED out of
    nothing — it is first debited from the town treasury, so tax → wage closes
    into a conserving loop. That is a BEHAVIOR change, hence its own gate; with
    the gate off this function is byte-level identical to the pre-S1-5 version.
    """
    from app.config import settings
    if not settings.npc_economy_enabled:
        return
    base_wage = settings.npc_default_wage_sc
    if settings.polis_policy_enabled:
        try:
            from app.services import fiscal_policy_service
            base_wage = await fiscal_policy_service.default_wage_sc(
                db, fallback=base_wage,
            )
        except Exception:
            logger.warning("policy-backed duty wage lookup failed", exc_info=True)
    wage = int(perk(resident, "wage_sc", base_wage))
    # M6: the sitting mayor earns a town-wide wage bonus (flag on meta_json —
    # zero extra query, it's already loaded).
    if settings.election_enabled and (getattr(resident, "meta_json", None) or {}).get("mayor"):
        wage = int(round(wage * settings.election_mayor_wage_bonus))
    if wage <= 0:
        return
    try:
        from app.services import coin_service
        # S1-5 funded wage: mirror coin_service.transfer's debit→credit ordering —
        # take the money out of the town account FIRST, and only credit the
        # resident once that guarded decrement actually won. The mayor bonus is
        # already folded into `wage` above (S2-1 semantics untouched), so the town
        # funds the bonus too. Zero extra per-resident SELECT: one more UPDATE on
        # the single town row inside the same transaction.
        if settings.town_treasury_enabled:
            from app.services import treasury_service
            funded = await treasury_service.disburse(
                db, wage, reason=f"wage:{resident.slug}")
            if not funded:
                if (settings.town_wage_unfunded_policy or "skip") != "mint":
                    # 'skip' = 欠薪: nothing credited, and crucially nothing minted.
                    # No rollback here — disburse's guard wrote nothing.
                    logger.info("town treasury short: wage skipped for %s", resident.slug)
                    return
                # 'mint' = explicit escape hatch back to the pre-S1-5 behavior.
                logger.info("town treasury short: minting wage for %s", resident.slug)
        await coin_service.treasury_credit(db, resident.slug, wage, reason="duty_wage")
        balance = await coin_service.treasury_balance(db, resident.slug)
        set_wallet_cache(db, resident, balance)
        await _feed(resident.slug, "wage", {"amount": wage, "duty": duty_key(resident)})
    except Exception:
        logger.warning("duty wage credit failed for %s", resident.slug, exc_info=True)


def set_wallet_cache(db, resident, balance: int) -> None:
    """Write-through the treasury balance into meta_json['wallet'] so the
    decision prompt can read wallet pressure without an extra tick query (M1
    F1.3). Mutates + flags the JSON column; the caller's commit persists it."""
    from sqlalchemy.orm.attributes import flag_modified
    meta = dict(resident.meta_json or {})
    meta["wallet"] = int(balance)
    resident.meta_json = meta
    try:
        flag_modified(resident, "meta_json")
    except Exception:
        pass


async def _work_workshop_fixer(db, resident) -> str | None:
    """陈铁生:发布一条'到工坊取修好的物件'委托(visit_location,可被玩家
    接取并由 LocationTracker 自动完成)。"""
    from app.services.commission_service import create_commission

    reward = int(perk(resident, "commission_reward", 8))
    c = await create_commission(
        db, resident.id, "visit_location",
        f"{resident.name}的修理委托",
        {
            "location_id": "workshop",
            "note": "有件修好的物件等人来取。到工坊走一趟,他会把东西交给你。",
        },
        reward,
    )
    if c is None:
        return None  # global cap reached — retry next window
    await _maybe_list_resident_work(
        db, resident, "handcraft",
        f"{resident.name}的手工件", "🔧",
        f"{resident.name}在工坊打磨的小物件,榫卯严丝合缝,不用一根钉子。",
    )
    await _feed(resident.slug, "duty_output", {"duty": "workshop_fixer", "commission_id": c.id})
    return f"{resident.name}修好了一件物件,在委托栏贴出了取件通知"


async def _work_shop_keeper(db, resident) -> str | None:
    """何巧云:补货/调价一件商品,并以她的名义发'到货通知'公告。"""
    from app.models.shop import Item
    from app.services.bulletin_service import create_post

    # M-A C4:商队进口货不归杂货铺管——它对玩家目录不可见(shop_service.IMPORT_KIND),
    # 补货公告会把玩家引向一件买不到的商品,调价也会漂移商队定价。
    from app.services.shop_service import IMPORT_KIND
    items = (await db.execute(
        select(Item).where(Item.active.is_(True), Item.kind != IMPORT_KIND)
    )).scalars().all()
    if not items:
        return None
    item = random.choice(items)
    jitter = perk(resident, "restock_jitter", 0.1)
    factor = 1.0 + random.uniform(-jitter, jitter)
    item.price_sc = max(1, round(item.price_sc * factor))
    await db.commit()
    await create_post(
        db, "notice",
        f"杂货铺到货:{item.name}",
        f"{item.icon} 新一批「{item.name}」到货了,现价 {item.price_sc} SC。"
        f"要的赶紧来,过了这村没这店!——{resident.name}",
        author_resident_id=resident.id,
    )
    await _feed(resident.slug, "duty_output", {"duty": "shop_keeper", "item_code": item.code})
    return f"{resident.name}给杂货铺补了货:「{item.name}」现价 {item.price_sc} SC"


async def _work_street_artist(db, resident) -> str | None:
    """阿岚:给附近一位居民画速写——双方各得一条事件记忆 + feed 事件。"""
    from app.memory.service import MemoryService

    radius = int(perk(resident, "sketch_radius", 8))
    nearby = (await db.execute(
        select(Resident).where(
            Resident.id != resident.id,
            Resident.status.notin_(["sleeping"]),
            Resident.tile_x.between(resident.tile_x - radius, resident.tile_x + radius),
            Resident.tile_y.between(resident.tile_y - radius, resident.tile_y + radius),
        )
    )).scalars().all()
    if not nearby:
        return None
    subject = random.choice(nearby)

    svc = MemoryService(db)
    await svc.add_memory(
        subject.id, "event",
        f"{resident.name}在旁边给我画了一张速写,几分钟就画完了,神态抓得真准。",
        0.55, "observation", related_resident_id=resident.id,
    )
    await svc.add_memory(
        resident.id, "event",
        f"给{subject.name}画了一张速写。今天的光线不错,这张画得很顺。",
        0.5, "observation", related_resident_id=subject.id,
    )
    await _maybe_list_resident_work(
        db, resident, "speedpaint",
        f"{resident.name}的速写", "🖼️",
        f"{resident.name}在广场为路人所作的速写,寥寥几笔,神态毕现。",
    )
    await _feed(resident.slug, "duty_output", {"duty": "street_artist", "subject_slug": subject.slug})
    return f"{resident.name}给{subject.name}画了一张速写"


async def _work_lecturer(db, resident) -> str | None:
    """顾明远:每周在学院开一场公开课(WorldEvent,event_cron 按窗口激活,
    活动期间注入所有居民的决策与对话 prompt,吸引大家去学院)。"""
    from app.models.world_event import WorldEvent

    cooldown_days = int(perk(resident, "lecture_cooldown_days", 7))
    since = datetime.now(UTC) - timedelta(days=cooldown_days)
    recent = (await db.execute(
        select(WorldEvent.id).where(
            WorldEvent.type == "news",
            WorldEvent.title.like(f"%{resident.name}的公开课%"),
            WorldEvent.created_at >= since,
        ).limit(1)
    )).scalar_one_or_none()
    if recent is not None:
        return None
    now = datetime.now(UTC)
    db.add(WorldEvent(
        type="news",
        title=f"{resident.name}的公开课",
        description=f"{resident.name}今天在学院开公开课,讲小镇的历史与来路。居民们可以去学院旁听。",
        payload_json={"location_id": "academy", "duty": "lecturer"},
        starts_at=now, ends_at=now + timedelta(hours=6), is_active=False,
    ))
    await db.commit()
    await _feed(resident.slug, "duty_output", {"duty": "lecturer"})
    return f"{resident.name}在学院挂出了公开课的讲题"


async def _work_researcher(db, resident) -> str | None:
    """江临:整理'镇况日志'并发一条观测 feed(RESEARCH 大机制归 Lab,此处
    只是 WORK 时的轻量职业叙事)。"""
    await _feed(resident.slug, "duty_output", {"duty": "researcher", "note": "镇况日志更新"})
    return f"{resident.name}更新了镇况日志"


async def _work_chronicle_editor(db, resident) -> str | None:
    """沈静书:整理档案时偶尔誊出一本手抄本(可上架商店),并留一条创作记忆。"""
    from app.memory.service import MemoryService

    listed = await _maybe_list_resident_work(
        db, resident, "manuscript",
        f"{resident.name}的手抄本", "📖",
        f"{resident.name}亲手誊抄、装订的小册子,页边还留着铅笔的批注。",
    )
    await MemoryService(db).add_memory(
        resident.id, "event",
        "在图书馆誊抄整理了一册旧稿,顺手把想写的小说又推进了一段。",
        0.5, "reflection",
    )
    await _feed(resident.slug, "duty_output", {"duty": "chronicle_editor", "listed": bool(listed)})
    return f"{resident.name}誊抄整理了一册手稿"


async def _work_postman(db, resident) -> str | None:
    """骆小舟:跑一趟投递——把到期的时间胶囊送到,并留一条投递记忆。"""
    delivered = 0
    try:
        from app.services.capsule_service import deliver_due_capsules
        delivered = await deliver_due_capsules(db)
    except Exception:
        logger.warning("postman capsule delivery failed", exc_info=True)
    from app.memory.service import MemoryService
    note = (f"今天送到了 {delivered} 封到期的信,看着收信的人拆开,值了。"
            if delivered else "今天把该走的路线跑了一遍,没有迟到的信。")
    await MemoryService(db).add_memory(resident.id, "event", note, 0.5, "observation")
    await _feed(resident.slug, "duty_output", {"duty": "postman", "delivered": delivered})
    return f"{resident.name}跑完了今天的投递(送达 {delivered} 封)"


_WORK_HANDLERS = {
    "workshop_fixer": _work_workshop_fixer,
    "shop_keeper": _work_shop_keeper,
    "street_artist": _work_street_artist,
    "lecturer": _work_lecturer,
    "researcher": _work_researcher,
    "chronicle_editor": _work_chronicle_editor,
    "postman": _work_postman,
}


async def _maybe_list_resident_work(
    db, resident, code_stem: str, name: str, icon: str, description: str,
) -> bool:
    """M1 F1.4: with npc_work_item_prob, list a limited-stock 'resident_work'
    shop item crediting this resident on purchase. Returns True if listed.
    Idempotent-ish: one open (active) listing per creator+stem at a time."""
    from app.config import settings
    if not settings.npc_economy_enabled:
        return False
    if random.random() >= settings.npc_work_item_prob:
        return False
    from app.models.shop import Item
    code = f"work_{code_stem}_{resident.slug}"
    existing = (await db.execute(select(Item).where(Item.code == code))).scalar_one_or_none()
    if existing is not None and existing.active:
        return False  # a copy is still on the shelf
    payload = {
        "creator_slug": resident.slug,
        "stock": settings.npc_work_item_stock,
    }
    if existing is None:
        db.add(Item(
            code=code, kind="resident_work", name=name, description=description,
            icon=icon, price_sc=settings.npc_work_item_price_sc,
            payload_json=payload, stock=settings.npc_work_item_stock, active=True,
        ))
    else:
        existing.active = True
        existing.payload_json = payload
        # M-A 加固:列是真相(payload 里那份退化成镜像),上架/复活都要写。
        existing.stock = settings.npc_work_item_stock
        existing.price_sc = settings.npc_work_item_price_sc
    await db.commit()
    await _feed(resident.slug, "work_listed", {"item_code": code})
    return True


# ── shared helpers ─────────────────────────────────────────────────────

async def _feed(resident_slug: str, kind: str, payload: dict) -> None:
    try:
        from app.services.feed_service import push
        await push(resident_slug, kind, payload)
    except Exception:
        logger.debug("duty feed push failed for %s", resident_slug, exc_info=True)
