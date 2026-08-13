"""Duty (职务) system tests: perks, prompt hints, WORK outputs, and the
signature/coefficient hooks in gossip / digest / events / quests / encounters /
chat."""
from datetime import date

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.services import duty_service


def _resident(slug: str, name: str, duty: dict | None = None, **kw) -> Resident:
    meta = {"duty": duty} if duty else None
    defaults = dict(
        slug=slug, name=name, district="central_plaza", status="idle",
        resident_type="npc", tile_x=70, tile_y=56, meta_json=meta,
    )
    defaults.update(kw)
    return Resident(**defaults)


# ── accessors ──────────────────────────────────────────────────────────

def test_perk_and_hint_accessors():
    r = _resident("a", "甲", {
        "key": "tavern_hub", "title": "消息集散地",
        "prompt_hint": "你经营酒馆", "perks": {"gossip_multiplier": 2.0},
    })
    assert duty_service.duty_key(r) == "tavern_hub"
    assert duty_service.perk(r, "gossip_multiplier", 1.0) == 2.0
    assert duty_service.perk(r, "missing", 1.0) == 1.0
    hint = duty_service.prompt_hint(r)
    assert "消息集散地" in hint and "你经营酒馆" in hint

    plain = _resident("b", "乙")
    assert duty_service.duty_key(plain) is None
    assert duty_service.prompt_hint(plain) == ""
    assert duty_service.max_perk([plain, r], "gossip_multiplier", 1.0) == 2.0


# ── gossip multiplier gate ─────────────────────────────────────────────

class _SentinelDB:
    """DB stub that records whether maybe_gossip got past the probability gate."""
    def __init__(self):
        self.touched = False

    async def execute(self, *_a, **_kw):
        self.touched = True

        class _R:
            def scalars(self):
                return self

            def all(self):
                return []
        return _R()


@pytest.mark.anyio
async def test_gossip_multiplier_widens_gate(monkeypatch):
    from app.services import gossip_service

    monkeypatch.setattr(gossip_service.random, "random", lambda: 0.4)

    plain = _resident("p", "普通人")
    hub = _resident("h", "周大河", {
        "key": "tavern_hub", "perks": {"gossip_multiplier": 2.0},
    })
    listener = _resident("l", "听众")

    db = _SentinelDB()
    # 0.4 >= 0.3 → a plain speaker exits at the gate without touching the DB.
    assert await gossip_service.maybe_gossip(db, plain, listener) is None
    assert db.touched is False
    # 0.4 < 0.3×2.0 → the hub speaker passes the gate (no rumors → still None).
    assert await gossip_service.maybe_gossip(db, hub, listener) is None
    assert db.touched is True


# ── WORK outputs ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_on_work_workshop_fixer_creates_commission(db_session):
    from app.models.commission import Commission

    fixer = _resident("chen", "陈铁生", {
        "key": "workshop_fixer", "perks": {"commission_reward": 8},
    })
    db_session.add(fixer)
    await db_session.commit()

    line = await duty_service.on_work(db_session, fixer)
    assert line and "修理" in line or "修好" in line
    c = (await db_session.execute(select(Commission))).scalars().one()
    assert c.issuer_resident_id == fixer.id
    assert c.kind == "visit_location"
    assert (c.payload_json or {}).get("location_id") == "workshop"
    assert c.reward_sc == 8

    # Cooldown: a second WORK in the same window is a no-op.
    assert await duty_service.on_work(db_session, fixer) is None
    assert len((await db_session.execute(select(Commission))).scalars().all()) == 1


@pytest.mark.anyio
async def test_on_work_shop_keeper_restocks_and_posts(db_session):
    from app.models.bulletin_post import BulletinPost
    from app.models.shop import Item

    db_session.add(Item(code="tea", kind="consumable", name="花茶", price_sc=10))
    keeper = _resident("he", "何巧云", {
        "key": "shop_keeper", "perks": {"restock_jitter": 0.1},
    })
    db_session.add(keeper)
    await db_session.commit()

    line = await duty_service.on_work(db_session, keeper)
    assert line and "花茶" in line
    item = (await db_session.execute(select(Item))).scalars().one()
    assert 9 <= item.price_sc <= 11  # ±10% of 10, min 1
    post = (await db_session.execute(select(BulletinPost))).scalars().one()
    assert post.kind == "notice"
    assert post.author_resident_id == keeper.id
    assert "到货" in post.title


@pytest.mark.anyio
async def test_on_work_street_artist_sketches_nearby(db_session):
    from app.models.memory import Memory

    artist = _resident("alan", "阿岚", {
        "key": "street_artist", "perks": {"sketch_radius": 8},
    }, tile_x=70, tile_y=56)
    subject = _resident("subj", "路人", tile_x=72, tile_y=56)
    db_session.add_all([artist, subject])
    await db_session.commit()

    line = await duty_service.on_work(db_session, artist)
    assert line and "速写" in line
    memories = (await db_session.execute(select(Memory))).scalars().all()
    assert len(memories) == 2
    owners = {m.resident_id for m in memories}
    assert owners == {artist.id, subject.id}
    for m in memories:
        assert m.type == "event"
        assert m.related_resident_id in (artist.id, subject.id)


@pytest.mark.anyio
async def test_on_work_street_artist_no_subject_no_cooldown(db_session):
    from app.models.memory import Memory

    artist = _resident("alan2", "阿岚", {
        "key": "street_artist", "perks": {"sketch_radius": 3},
    }, tile_x=20, tile_y=20)
    db_session.add(artist)
    await db_session.commit()

    # Nobody nearby → no output and NO cooldown (retry next tick).
    assert await duty_service.on_work(db_session, artist) is None
    assert (await db_session.execute(select(Memory))).scalars().all() == []

    # Someone walks by → the sketch happens on the next WORK.
    db_session.add(_resident("subj2", "路人乙", tile_x=21, tile_y=20))
    await db_session.commit()
    assert await duty_service.on_work(db_session, artist) is not None


@pytest.mark.anyio
async def test_on_work_lecturer_schedules_event(db_session):
    from app.models.world_event import WorldEvent

    lecturer = _resident("gu", "顾明远", {
        "key": "lecturer", "perks": {"lecture_cooldown_days": 7},
    })
    db_session.add(lecturer)
    await db_session.commit()

    line = await duty_service.on_work(db_session, lecturer)
    assert line and "公开课" in line
    ev = (await db_session.execute(select(WorldEvent))).scalars().one()
    assert "顾明远的公开课" in ev.title
    assert (ev.payload_json or {}).get("location_id") == "academy"

    # Weekly cooldown (DB-backed, independent of redis) blocks a repeat.
    assert await duty_service.on_work(db_session, lecturer) is None
    assert len((await db_session.execute(select(WorldEvent))).scalars().all()) == 1


@pytest.mark.anyio
async def test_on_work_without_duty_is_noop(db_session):
    plain = _resident("plain", "无职务")
    db_session.add(plain)
    await db_session.commit()
    assert await duty_service.on_work(db_session, plain) is None


# ── signature hooks ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_digest_signed_by_chronicle_editor(db_session, monkeypatch):
    from app.models.bulletin_post import BulletinPost
    from app.services import digest_service

    editor = _resident("shen", "沈静书", {"key": "chronicle_editor", "perks": {}})
    db_session.add(editor)
    await db_session.commit()

    digest = await digest_service.generate_village_digest(db_session, date(2026, 7, 20))
    assert digest is not None
    post = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "digest")
    )).scalars().one()
    assert post.author_resident_id == editor.id


@pytest.mark.anyio
async def test_clerk_announces_festival(db_session, monkeypatch):
    from app.models.bulletin_post import BulletinPost
    from app.tasks import event_templates

    clerk = _resident("zhao", "赵启文", {"key": "town_clerk", "perks": {}})
    db_session.add(clerk)
    await db_session.commit()

    monkeypatch.setattr(event_templates.random, "random", lambda: 1.0)  # no news
    created = await event_templates.ensure_scheduled_events(db_session, date(2026, 6, 1))
    assert created == 1  # 儿童节

    post = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "notice")
    )).scalars().one()
    assert post.author_resident_id == clerk.id
    assert "市政厅公告" in post.title and "儿童节" in post.title


@pytest.mark.anyio
async def test_quest_magnet_biases_pick(db_session, monkeypatch):
    from app.services import daily_quest_service

    magnet = _resident("xiaoman", "苏小满", {
        "key": "explorer", "perks": {"quest_magnet": 0.5},
    }, heat=0)
    other = _resident("other", "其他人", heat=1)
    db_session.add_all([magnet, other])
    await db_session.commit()

    monkeypatch.setattr(daily_quest_service.random, "random", lambda: 0.4)
    picked = await daily_quest_service._pick_resident(db_session)
    assert picked.slug == "xiaoman"

    monkeypatch.setattr(daily_quest_service.random, "random", lambda: 0.9)
    picked = await daily_quest_service._pick_resident(db_session)
    assert picked is not None  # falls back to the normal pool


@pytest.mark.anyio
async def test_encounter_multiplier_widens_gate(db_session):
    from app.services import encounter_service

    class _Rng:
        def __init__(self, v):
            self.v = v

        def random(self):
            return self.v

        def choice(self, seq):
            return seq[0]

    explorer = _resident("xm", "苏小满", {
        "key": "explorer", "perks": {"encounter_multiplier": 1.5},
    }, tile_x=57, tile_y=20)  # inside cafe bounds
    db_session.add(explorer)
    await db_session.commit()

    # 0.4 >= 0.3 would normally suppress the encounter; ×1.5 → 0.45 lets it through.
    payload = await encounter_service.maybe_encounter(
        db_session, "user-1", "cafe", rng=_Rng(0.4)
    )
    assert payload is not None
    assert payload["resident_slug"] == "xm"


@pytest.mark.anyio
async def test_cafe_host_chat_effects(db_session):
    from app.agent.chat import _apply_duty_chat_effects
    from app.services import relation_service

    host = _resident("wanqiu", "林晚秋", {
        "key": "cafe_host",
        "perks": {"chat_mood_uplift": 0.08, "chat_affinity_bonus": 0.02},
    })
    guest = _resident("guest", "客人")
    db_session.add_all([host, guest])
    await db_session.commit()

    await _apply_duty_chat_effects(db_session, host, guest)

    await db_session.refresh(guest)
    assert (guest.mood_json or {}).get("valence", 0.0) > 0.0
    pair = await relation_service.get_pair(db_session, host.id, guest.id)
    assert pair is not None
    assert pair.affinity == pytest.approx(0.02)


# ── seed integration ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_all_presets_carry_a_duty(db_session):
    from seed.preset_characters import PRESET_CHARACTERS

    keys = set()
    for char in PRESET_CHARACTERS:
        duty = (char.get("meta_json") or {}).get("duty")
        assert duty, f"{char['slug']} lacks a duty"
        assert duty.get("key") and duty.get("prompt_hint")
        keys.add(duty["key"])
    assert len(keys) == 11  # every resident has a distinct duty


@pytest.mark.anyio
async def test_sync_duty_meta_backfills_existing(db_session):
    from seed.preset_characters import sync_duty_meta

    # A pre-duty-era resident: seeded earlier without the duty block.
    legacy = _resident("lin-wanqiu", "林晚秋")
    legacy.meta_json = {"origin": "preset", "is_preset": True}
    db_session.add(legacy)
    await db_session.commit()

    updated = await sync_duty_meta(db_session)
    assert updated == 1
    await db_session.refresh(legacy)
    assert legacy.meta_json["duty"]["key"] == "cafe_host"
    assert legacy.meta_json["origin"] == "preset"  # merged, not replaced

    assert await sync_duty_meta(db_session) == 0  # idempotent


# ── F8 异常路径不触过期 ORM ─────────────────────────────────────────────

class _ExpiringORM:
    """handler/资金腿炸掉后再摸任何 ORM 属性都抛——模拟 rollback 之后的
    MissingGreenlet(rollback 会 expire 调用方 session 里的所有 ORM 对象,
    asyncio 下一次惰性取属性就炸,见 treasury_service 模块头军规 2)。"""

    def __init__(self, inner):
        self.expired = False
        self._inner = inner

    def __getattribute__(self, name):
        if name.startswith("_") or name == "expired":
            return object.__getattribute__(self, name)
        if object.__getattribute__(self, "expired"):
            raise AssertionError(f"expired ORM attribute touched: {name}")
        return getattr(object.__getattribute__(self, "_inner"), name)


@pytest.mark.anyio
async def test_on_work_except_log_does_not_touch_expired_orm(db_session, monkeypatch):
    """except 的日志行只许用进 try 前取好的局部变量, 不许再摸 resident 属性。"""
    proxy = _ExpiringORM(_resident("boomer", "炸弹人", {"key": "lecturer"}))

    async def _boom(db, r):
        proxy.expired = True
        raise RuntimeError("handler exploded after a rollback")

    monkeypatch.setitem(duty_service._WORK_HANDLERS, "lecturer", _boom)

    assert await duty_service.on_work(db_session, proxy) is None  # fail-open


@pytest.mark.anyio
async def test_pay_wage_except_log_does_not_touch_expired_orm(db_session, monkeypatch):
    """town_to_resident 做死锁牺牲者被 abort 时会 rollback 后 raise——
    _pay_wage 的 except 不许再摸 resident.slug。"""
    from app.config import settings
    from app.services import treasury_service

    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_duty_funding_enabled", False)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)

    proxy = _ExpiringORM(_resident("wager", "领薪人", {"key": "tavern_hub"}))

    async def _deadlock_victim(*a, **kw):
        proxy.expired = True
        raise RuntimeError("DeadlockDetected: transaction aborted")

    monkeypatch.setattr(treasury_service, "town_to_resident", _deadlock_victim)

    await duty_service._pay_wage(db_session, proxy)  # fail-open, 不许抛
