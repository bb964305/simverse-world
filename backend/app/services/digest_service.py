"""A5 village daily report: gather material (SQL, no LLM) → compose (1 LLM call).

Cold-start days with no material produce a canned fallback and skip the LLM.
Regeneration is idempotent via the (scope, date, user_id) uniqueness.
"""

import logging
from datetime import datetime, date as date_type, timedelta, UTC

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.client import chat as llm_chat
from app.llm.metering import Meter
from app.models.digest import Digest
from app.models.memory import Memory
from app.models.personality_history import PersonalityHistory
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.ws.manager import manager

logger = logging.getLogger(__name__)

DIGEST_SYSTEM = (
    "你是 Simverse World 小镇的日报编辑。根据下面的今日素材，写一篇温暖、有趣的村落日报（小报体）。"
    "要求：以「# 」开头写一个标题，然后 3-5 段，总字数不超过 600 字，中文，突出居民故事与小镇氛围。"
)

#: DIGEST_SYSTEM 要的是「3-5 段、不超过 600 字中文 + 标题」，800 从一开始就
#: 不够；叠上没关掉的 thinking 直接把输出吃光（生产 12 天里 7 天触顶 801，
#: 其中 4 天正文长度为 0）。
DIGEST_MAX_TOKENS = 2000
WEEKLY_MAX_TOKENS = 1500

#: 「有实质正文」的阈值：去掉首行「# 标题」之后剩余文本的字符数下限。
#: 特意压得很低（不是质量门槛）——这个守卫只负责拦住 CRIT-2 描述的那种
#: 退化输出：`"# 今日头条"` 这样只有标题、标题之后什么都没有的字符串（生产
#: 4 天实证正是 body 长度恰好为 0），不负责判断正文「写得够不够长/够不够
#: 好」。真实 DIGEST_SYSTEM 产出是 3-5 段、上百字起，与「0 字」之间没有
#: 现实中会出现的中间地带，所以阈值只要能把 0 和「>0」分开就够用；定得更高
#: （比如 20）会连累 test_digest.py / test_opinion_service.py 里用短占位字符
#: 串（"小镇很热闹" 5 字、"镇上为夜市吵起来了" 9 字）当"有效内容"的既有用
#: 例——那些测的是别的东西（compose 是否被调用/opinion 素材是否进 prompt），
#: 不该被这里的阈值连带打断。冷启动兜底文案（约 34 字正文）显然远超，不受
#: 影响（见 test_cold_start_fallback_is_still_allowed_to_persist）。
DIGEST_MIN_BODY_CHARS = 1


def _digest_body(content: str) -> str:
    """去掉首行「# 标题」（如果有）之后剩余的正文，已 strip。

    必须先 strip() 整个字符串，再判断/切分——不能对 lstrip() 过的字符串判断
    是否以 "#" 开头，却对未 strip 的原始字符串按物理行 splitlines()[1:] 切。
    旧写法正是这样：``"\\n# 今日头条"`` 的 lstrip() 版本以 "#" 开头判为真，
    但 splitlines()[1:] 砍掉的是原字符串的物理第一行——那是前导换行留下的
    空行，标题行原封不动地留在结果里，body 变成 "# 今日头条"（长度 6），
    骗过 has_real_digest_body 的守卫。先 strip() 让"判断用的字符串"和"用来
    切分的字符串"是同一个，物理第一行与逻辑第一行才会一致。
    """
    body = content.strip()
    if body.startswith("#"):
        body = "\n".join(body.splitlines()[1:])
    return body.strip()


def has_real_digest_body(content: str) -> bool:
    """判据用一个函数喂三处调用点，避免标准不一致。

    三处调用点：本模块 ``generate_village_digest`` 的存量早返回判据、
    compose 结果的落库前守卫，以及 ``scripts/refill_empty_digests.py`` 的
    ``find_targets``。三处标准若各写一套（尤其早返回若继续用松判据
    ``.strip()``），回填脚本挑出的「标题行」问题行会被早返回悄悄放过——
    脚本永远填不回去，这正是本轮要堵的洞，而不只是不让新的坏数据落库。
    """
    return len(_digest_body(content)) >= DIGEST_MIN_BODY_CHARS


class DigestComposeEmpty(RuntimeError):
    """LLM 返回了空正文，或只有一行标题、没有实质正文。

    宁可让 nightly_cron 的 try/except 记一条 error，也不要把这种内容写进
    digests —— (scope, date, user_id) 唯一约束 + 幂等早返回会把这一天永久
    钉死，玩家连着几天打开日报都是空白面板。「只有标题行」比全空更隐蔽：
    `content.strip()` 非空能骗过松判据的早返回和旧的落库前守卫，且不像
    空字符串那样能从库里一眼查出来。
    """


async def gather_material(db: AsyncSession, day: date_type) -> dict:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)

    chats = (await db.execute(
        select(Memory.content).where(
            Memory.source == "chat_resident",
            Memory.created_at >= start, Memory.created_at < end,
        ).order_by(Memory.importance.desc()).limit(10)
    )).scalars().all()

    shifts = (await db.execute(
        select(PersonalityHistory.old_type, PersonalityHistory.new_type).where(
            PersonalityHistory.created_at >= start, PersonalityHistory.created_at < end,
        ).limit(10)
    )).all()

    events = (await db.execute(
        select(WorldEvent.title, WorldEvent.description).where(WorldEvent.is_active.is_(True))
    )).all()

    heat_top = (await db.execute(
        select(Resident.name, Resident.heat).order_by(Resident.heat.desc()).limit(3)
    )).all()

    # M2 F2.4: today's story-arc milestones — the ongoing serial. Read from the
    # feed events the arc engine emitted (kind goal_milestone / goal_achieved),
    # joined to the resident's name for readable lines.
    arc_lines: list[str] = []
    try:
        from app.models.feed import FeedEvent
        rows = (await db.execute(
            select(FeedEvent.resident_slug, FeedEvent.kind, FeedEvent.payload_json).where(
                FeedEvent.kind.in_(["goal_milestone", "goal_achieved"]),
                FeedEvent.created_at >= start, FeedEvent.created_at < end,
            ).order_by(FeedEvent.created_at.desc()).limit(8)
        )).all()
        name_by_slug = dict((await db.execute(select(Resident.slug, Resident.name))).all())
        for slug, kind, payload in rows:
            name = name_by_slug.get(slug, slug)
            payload = payload or {}
            if kind == "goal_achieved":
                arc_lines.append(f"{name} 达成了心愿:{payload.get('goal', '')}")
            else:
                arc_lines.append(f"{name} 有了进展:{payload.get('milestone', '')}")
    except Exception:
        logger.warning("digest arc lines failed", exc_info=True)

    stats = {
        "chat_count": len(chats),
        "shift_count": len(shifts),
        "event_count": len(events),
        "arc_count": len(arc_lines),
        "heat_top": [{"name": n, "heat": h} for n, h in heat_top],
    }
    # P2 §7.2: one line of circle dynamics (zero new LLM — augments the existing
    # digest prompt material). Only when the relations gate is on.
    circle_line = None
    if settings.realism_relations_enabled:
        try:
            from app.services import circle_service
            circle_line = await circle_service.digest_circle_line(db)
        except Exception:
            logger.warning("digest circle line failed", exc_info=True)

    # S1-3: one line of opinion dynamics (zero new LLM — rides the SAME
    # compose_digest call, circle_line precedent). Independent gate +
    # best-effort; gate off → no key at all, prompt byte-identical to status quo.
    opinion_line = None
    if settings.polis_opinion_enabled:
        try:
            from app.services.opinion_service import OpinionService
            opinion_line = await OpinionService(db).digest_opinion_line()
        except Exception:
            logger.warning("digest opinion line failed", exc_info=True)

    has_material = bool(chats or shifts or events or arc_lines)
    material = {
        "chats": list(chats),
        "shifts": [f"{o}→{n}" for o, n in shifts],
        "events": [f"{t}：{d}" for t, d in events],
        "arc_lines": arc_lines,
        "heat_top": [f"{n}(热度{h})" for n, h in heat_top],
        "circle_line": circle_line,
        "stats": stats,
        "has_material": has_material,
    }
    if opinion_line:
        material["opinion_line"] = opinion_line
    return material


def _build_prompt(day: date_type, material: dict) -> str:
    parts = [f"日期：{day}"]
    if material["events"]:
        parts.append("今日世界事件：\n" + "\n".join(f"- {e}" for e in material["events"]))
    if material["chats"]:
        parts.append("今日居民对话摘录：\n" + "\n".join(f"- {c}" for c in material["chats"]))
    if material["shifts"]:
        parts.append("今日人格变化：\n" + "\n".join(f"- {s}" for s in material["shifts"]))
    if material.get("arc_lines"):
        parts.append(
            "居民故事进展（这是连载剧情，请写出「上回说到」的连续感）：\n"
            + "\n".join(f"- {a}" for a in material["arc_lines"])
        )
    if material["heat_top"]:
        parts.append("人气居民：" + "、".join(material["heat_top"]))
    if material.get("circle_line"):
        parts.append("社交圈子：" + material["circle_line"])
    if material.get("opinion_line"):  # S1-3 — same single compose_digest call
        parts.append("小镇舆论：" + material["opinion_line"])
    return "\n\n".join(parts)


async def compose_digest(day: date_type, material: dict) -> tuple[str, str]:
    """一次 LLM 调用产出（标题, 正文）。

    必须走 ``app.llm.client.chat()``：它是全仓唯一会加
    ``thinking={"type": "disabled"}`` 的地方（client.py:148-149，条件是
    ``not settings.llm_thinking``，默认 False），也统一了 llm_usage 计量。
    直调 ``messages.create`` 时推理会把 max_tokens 吃光，响应里没有可用的
    text block。

    ``model`` 显式传 ``effective_model``：``chat()`` 对 ``owner="system"``
    默认解析的是 ``settings.background_model``（client.py:140），今天两者
    恰好相等（生产未设 BACKGROUND_LLM_MODEL），但日报是玩家可见内容，模型
    不该跟着后台路由走。
    """
    text = (await llm_chat(
        DIGEST_SYSTEM,
        [{"role": "user", "content": _build_prompt(day, material)}],
        model=settings.effective_model,
        max_tokens=DIGEST_MAX_TOKENS,
        owner="system",
        meter=Meter(scenario="digest"),
    )).strip()
    title = f"{day} 村落日报"
    if text.startswith("#"):
        first_line = text.splitlines()[0].lstrip("# ").strip()
        if first_line:
            title = first_line
    return title, text


async def _pin_digest_bulletin(db: AsyncSession, digest: Digest) -> None:
    """A5→A4: pin the village digest on the bulletin board.

    Unpins any previous digest pin first so only the latest stays pinned.
    Duty system: if the town has a chronicle editor (图书管理员), the digest is
    published under her name; otherwise author fields stay NULL (=「系统」).

    NOT "called only when a new row was inserted" — ``generate_village_digest``
    also calls this after an UPDATE-in-place refill of a previously empty/
    title-only row (that's the whole point of the refill script). The old
    docstring claimed that made it "idempotent per day for free"; it doesn't,
    because ``BulletinPost`` has no day column to key off of, so a second call
    for the same day would blindly unpin-then-insert again, leaving two
    ``kind="digest"`` posts around (one stale, one pinned). Instead this makes
    itself idempotent directly: if the currently-pinned digest post is already
    byte-identical (title + content_md) to the one we're about to publish, it
    is left alone rather than duplicated. A genuinely different digest (new
    day, or a refill that composed different text) still unpins the old post
    and pins a fresh one, same as before.

    Load-bearing: the idempotency check below reads ALL currently-pinned
    ``kind="digest"`` rows (``scalars().all()``), not a single row via
    ``scalar_one_or_none()``. ``script_service.settle_due_seasons`` inserts its
    own ``kind="digest", pinned=True`` finale-recap post on season close and
    never unpins the standing digest pin — season length is 28 days by
    default, so by day 28 the table already holds ≥2 pinned ``kind="digest"``
    rows. ``scalar_one_or_none()`` raises ``MultipleResultsFound`` the moment
    that happens; the caller's blanket ``except Exception`` swallows it, and
    because the raise happens BEFORE the unconditional unpin UPDATE below, the
    self-heal that UPDATE used to provide never runs either — the two pinned
    rows stay stuck forever and every digest after that silently stops
    reaching the bulletin board. Below, the idempotency short-circuit only
    fires when there is EXACTLY one pinned row and it matches byte-for-byte;
    any other shape (zero, one-but-different, or many) falls through to the
    same unconditional "unpin every pinned digest, then pin the new one" path
    that ran before this idempotency check was added, so a table full of N≥2
    stale pinned rows still converges to exactly 1 without raising.
    """
    from sqlalchemy import update
    from app.models.bulletin_post import BulletinPost
    from app.services.bulletin_service import create_post

    current_pins = (await db.execute(
        select(BulletinPost).where(BulletinPost.kind == "digest", BulletinPost.pinned.is_(True))
    )).scalars().all()
    if (len(current_pins) == 1
            and current_pins[0].title == digest.title
            and current_pins[0].content_md == digest.content_md):
        return  # already pinned, byte-identical — a repeat call, nothing to do

    await db.execute(
        update(BulletinPost)
        .where(BulletinPost.kind == "digest", BulletinPost.pinned.is_(True))
        .values(pinned=False)
    )
    author_id = None
    try:
        from app.services.duty_service import find_duty_resident
        editor = await find_duty_resident(db, "chronicle_editor")
        author_id = editor.id if editor else None
    except Exception:
        logger.warning("digest editor lookup failed", exc_info=True)
    await create_post(
        db, "digest", digest.title, digest.content_md,
        author_resident_id=author_id, pinned=True,
    )


async def generate_village_digest(db: AsyncSession, day: date_type | None = None) -> Digest:
    day = day or datetime.now(UTC).date()

    existing = (await db.execute(
        select(Digest).where(Digest.scope == "village", Digest.date == day, Digest.user_id == "")
    )).scalar_one_or_none()
    # 幂等的判据是「已经有实质正文」，不是「行存在」或「非空字符串」：存量空行
    # 与「只有标题行」的行（生产 07-17/24/25/26 四天 + 可能存在的标题行退化行）
    # 都必须能被重新生成填回去，否则那几天永远是空白/残缺面板。这里必须与下面
    # 落库前的守卫、以及 refill_empty_digests.py 的 find_targets 用同一个判据
    # （has_real_digest_body），否则回填脚本挑出来的行会在这里被松判据早早放行。
    if existing is not None and has_real_digest_body(existing.content_md or ""):
        return existing

    material = await gather_material(db, day)
    if not material["has_material"]:
        title = f"{day} 村落日报"
        content = f"# {title}\n\n今天的小镇静悄悄，居民们各自忙碌，没有特别的大事发生。明天再来看看吧。"
    else:
        title, content = await compose_digest(day, material)

    if not has_real_digest_body(content):
        logger.error("digest compose returned a title-only or empty body for %s (stats=%s): %r",
                     day, material["stats"], content[:80])
        raise DigestComposeEmpty(f"title-only or empty digest body for {day}")

    if existing is not None:
        # 回填已有的空行 —— UPDATE 而不是 INSERT，避开唯一约束。
        existing.title = title
        existing.content_md = content
        existing.stats_json = material["stats"]
        await db.commit()
        await db.refresh(existing)
        digest = existing
    else:
        digest = Digest(
            scope="village", date=day, user_id="", title=title,
            content_md=content, stats_json=material["stats"],
        )
        db.add(digest)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return (await db.execute(
                select(Digest).where(Digest.scope == "village", Digest.date == day, Digest.user_id == "")
            )).scalar_one()
        await db.refresh(digest)

    # A5→A4: pin the new digest on the bulletin board (best-effort; a bulletin
    # failure must never break digest generation).
    try:
        await _pin_digest_bulletin(db, digest)
    except Exception:
        logger.warning("digest bulletin pin failed", exc_info=True)

    try:
        await manager.broadcast({"type": "digest_ready", "date": str(day)})
    except Exception:
        logger.warning("digest_ready broadcast failed", exc_info=True)
    return digest


# ── E14 personal weekly recap ────────────────────────────────────────

WEEKLY_SYSTEM = (
    "你是私人回顾编辑。用第二人称给玩家写一段温暖的本周回顾，≤400 字，中文，"
    "结合下面的数据，突出与居民的连接。不要罗列数字，讲成故事。"
)

# 12 reproducible behavior tags (LLM only polishes copy, the tag itself is rule-based).
WEEKLY_TAGS = [
    "沉睡者", "城市漫游者", "社交名流", "健谈者", "深夜访客", "探索先锋",
    "长情之人", "新面孔收藏家", "安静的观察者", "热心肠", "梦想合伙人", "小镇常客",
]


def _personality_tag(chat_count: int, distinct: int, explore: int) -> str:
    if chat_count == 0 and explore == 0:
        return "沉睡者"
    if explore >= 5:
        return "城市漫游者"
    if distinct >= 5:
        return "社交名流"
    if chat_count >= 10:
        return "健谈者"
    if distinct >= 3:
        return "新面孔收藏家"
    if explore >= 2:
        return "探索先锋"
    if chat_count >= 3:
        return "小镇常客"
    return "安静的观察者"


def _week_sunday(today: date_type) -> date_type:
    return today - timedelta(days=(today.weekday() + 1) % 7)


async def generate_weekly_recap(db: AsyncSession, user_id: str) -> Digest:
    """Lazily generate this week's personal recap (idempotent per week).

    No ``has_real_digest_body``-style guard here (considered, rejected): unlike
    the village digest, ``content`` is never *just* the LLM's output — it's
    always wrapped in a fixed non-LLM template (`"# {title}\n\n{body}\n\n本周
    人格标签：**{tag}**"`), so even a degenerate/empty ``body`` from the LLM
    still leaves a real, non-title-only row (the tag line alone clears
    DIGEST_MIN_BODY_CHARS). The failure mode Fix 1 closes for the village
    digest — a title-only string that reads as "non-empty" and gets
    permanently idempotency-locked — doesn't arise here structurally, so the
    same guard would be dead code, not a fix.
    """
    from app.models.conversation import Conversation
    from app.models.user import User
    from app.models.achievement import UserAchievement
    from app.models.location_visit import LocationVisit

    today = datetime.now(UTC).date()
    week_key = _week_sunday(today)
    existing = (await db.execute(
        select(Digest).where(Digest.scope == "personal", Digest.user_id == user_id, Digest.date == week_key)
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    week_start = datetime(week_key.year, week_key.month, week_key.day, tzinfo=UTC)
    convs = (await db.execute(
        select(Conversation).where(Conversation.user_id == user_id, Conversation.started_at >= week_start)
    )).scalars().all()
    chat_count = len(convs)
    turns = sum((c.turns or 0) for c in convs)
    distinct = len({c.resident_id for c in convs})

    mem_rows = (await db.execute(
        select(Memory.content).where(Memory.related_user_id == user_id, Memory.created_at >= week_start)
        .order_by(Memory.importance.desc()).limit(3)
    )).scalars().all()
    ach_count = (await db.execute(
        select(func.count()).select_from(UserAchievement).where(
            UserAchievement.user_id == user_id, UserAchievement.unlocked_at >= week_start,
        )
    )).scalar() or 0
    explore = (await db.execute(
        select(func.count()).select_from(LocationVisit).where(LocationVisit.user_id == user_id)
    )).scalar() or 0

    tag = _personality_tag(chat_count, distinct, explore)
    stats = {"chats": chat_count, "turns": turns, "distinct_residents": distinct,
             "achievements": int(ach_count), "explored": int(explore), "tag": tag}

    if chat_count < 2:
        title = f"{week_key} 本周回顾"
        content = f"# {title}\n\n本周太安静了，几乎没有和居民互动。下周多出来走走，会有新的故事在等你。\n\n本周人格标签：**{tag}**"
    else:
        material = (f"对话 {chat_count} 次 / {turns} 轮，认识了 {distinct} 位居民；"
                    f"被写进 {len(mem_rows)} 条记忆；解锁成就 {ach_count} 个。\n"
                    + "记忆摘录：\n" + "\n".join(f"- {m}" for m in mem_rows))
        body = (await llm_chat(
            WEEKLY_SYSTEM,
            [{"role": "user", "content": f"本周人格标签：{tag}\n{material}"}],
            model=settings.effective_model,
            max_tokens=WEEKLY_MAX_TOKENS,
            owner="system",
            meter=Meter(scenario="weekly_recap"),
        )).strip()
        title = f"{week_key} 本周回顾"
        content = f"# {title}\n\n{body}\n\n本周人格标签：**{tag}**"

    digest = Digest(scope="personal", date=week_key, user_id=user_id, title=title,
                    content_md=content, stats_json=stats)
    db.add(digest)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return (await db.execute(
            select(Digest).where(Digest.scope == "personal", Digest.user_id == user_id, Digest.date == week_key)
        )).scalar_one()
    await db.refresh(digest)
    return digest


def serialize(d: Digest) -> dict:
    return {
        "id": d.id,
        "scope": d.scope,
        "date": str(d.date),
        "title": d.title,
        "content_md": d.content_md,
        "stats": d.stats_json or {},
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }
