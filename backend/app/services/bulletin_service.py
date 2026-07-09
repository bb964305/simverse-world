"""A4 bulletin posts: resident creations + ops notices + digest pins."""

import random
import logging
from datetime import datetime, UTC

from sqlalchemy import select, func

from app.models.bulletin_post import BulletinPost
from app.models.resident import Resident

logger = logging.getLogger(__name__)

CREATION_DAILY_CAP = 20
JOURNAL_PROBABILITY = 0.5
PAGE_SIZE = 20


async def create_post(db, kind, title, content_md, author_resident_id=None, author_user_id=None, pinned=False) -> BulletinPost:
    post = BulletinPost(
        kind=kind, title=title, content_md=content_md,
        author_resident_id=author_resident_id, author_user_id=author_user_id, pinned=pinned,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return post


def serialize(post: BulletinPost, author_name: str | None = None, author_portrait: str | None = None) -> dict:
    return {
        "id": post.id,
        "kind": post.kind,
        "title": post.title,
        "content_md": post.content_md,
        "likes": post.likes,
        "tips_sc": post.tips_sc,
        "pinned": post.pinned,
        "author_resident_id": post.author_resident_id,
        "author_name": author_name,
        "author_portrait": author_portrait,
        "created_at": post.created_at.isoformat() if post.created_at else None,
    }


async def list_posts(db, kind: str | None = None, cursor: str | None = None) -> dict:
    q = select(BulletinPost)
    if kind:
        q = q.where(BulletinPost.kind == kind)
    if cursor:
        try:
            q = q.where(BulletinPost.created_at < datetime.fromisoformat(cursor))
        except ValueError:
            pass
    q = q.order_by(BulletinPost.created_at.desc()).limit(PAGE_SIZE + 1)
    rows = list((await db.execute(q)).scalars().all())
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    author_ids = {p.author_resident_id for p in rows if p.author_resident_id}
    authors = {}
    if author_ids:
        for r in (await db.execute(select(Resident).where(Resident.id.in_(author_ids)))).scalars().all():
            authors[r.id] = (r.name, r.portrait_url)
    posts = [serialize(p, *(authors.get(p.author_resident_id, (None, None)))) for p in rows]
    next_cursor = rows[-1].created_at.isoformat() if (has_more and rows) else None
    return {"posts": posts, "next_cursor": next_cursor}


async def _count_today(db, resident_id: str | None = None) -> int:
    today = datetime.now(UTC).date()
    q = select(func.count()).select_from(BulletinPost).where(
        BulletinPost.kind == "journal", func.date(BulletinPost.created_at) == today,
    )
    if resident_id:
        q = q.where(BulletinPost.author_resident_id == resident_id)
    return int((await db.execute(q)).scalar() or 0)


async def maybe_create_journal_post(db, resident) -> BulletinPost | None:
    """A4: a resident may publish a short creation once per day (rule-gated).

    Uses the resident's latest memory as material with a template (zero LLM —
    LLM-styled generation by SBTI is a later enhancement, off on vm212).
    """
    if await _count_today(db, resident.id) > 0:
        return None
    if random.random() >= JOURNAL_PROBABILITY:
        return None
    if await _count_today(db) >= CREATION_DAILY_CAP:
        return None

    from app.models.memory import Memory
    recent = (await db.execute(
        select(Memory.content).where(Memory.resident_id == resident.id)
        .order_by(Memory.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    content = recent or "今天很平静，没什么特别的事，但平静也是一种幸福。"
    post = await create_post(db, "journal", "今日随笔", content, author_resident_id=resident.id)

    # E11: surface the creation to followers.
    try:
        from app.services.feed_service import push
        await push(resident.slug, "creation", {"post_id": post.id, "title": post.title})
    except Exception:
        logger.warning("feed push (creation) failed", exc_info=True)

    try:
        from app.memory.service import MemoryService
        await MemoryService(db).add_memory(
            resident.id, "reflection", "我写了一篇随笔发在公告板上。",
            importance=0.4, source="reflection",
        )
    except Exception:
        logger.warning("journal reflection memory failed", exc_info=True)
    return post
