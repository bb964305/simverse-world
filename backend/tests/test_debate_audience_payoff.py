"""P2-S16: 观众收益 —— 记忆/心情/社交需求/关系四层,零 SC 流动。

经济守恒是本组的硬门,两条独立证据:
  · 源码扫描:_audience_aftermath 的函数体不得出现任何货币符号;
  · 余额快照:走完整条 settle,users.soul_coin_balance 与
    resident_treasuries.balance_sc 的总变化恰等于 -burn(5% BURN_RATE),
    一枚都不多不少 —— 观众路径没有开新出口,也没有与 settle 分账双花。
"""
import inspect

import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.agent.needs import get_needs
from app.config import settings
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.user import User
from app.models.world_event import WorldEvent
from app.services import debate_service as ds
from app.services import relation_service

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}
INSIDE = (175, 45)
OUTSIDE = (75, 56)


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)


async def _resident(db, slug, tile=INSIDE):
    r = Resident(slug=slug, name=slug, creator_id="system", district="cafe",
                 status="idle", resident_type="npc",
                 tile_x=tile[0], tile_y=tile[1])
    db.add(r)
    await db.commit()
    return r


async def _staged_debate(db, *, with_event=True, watchers=2):
    await _resident(db, "ann", OUTSIDE)
    await _resident(db, "bo", OUTSIDE)
    seats = [await _resident(db, f"w{i}", INSIDE) for i in range(watchers)]
    d = await ds.create_debate(db, "猫和狗谁更好", "ann", "bo")
    if with_event:
        db.add(WorldEvent(type="script", title="辩论", description="",
                          payload_json={"location_id": "theater",
                                        "debate_id": d.id}))
        await db.commit()
    return d, seats


async def _audience_memories(db, resident_id):
    rows = (await db.execute(
        select(Memory).where(Memory.resident_id == resident_id,
                             Memory.source == "debate")
    )).scalars().all()
    return rows


# ── 依赖边守卫 ────────────────────────────────────────────────────────

def test_stage_event_flag_is_registered_by_the_previous_batch():
    from app.config import Settings
    field = Settings.model_fields.get("stage_event_enabled")
    assert field is not None, (
        "app/config.py 缺 stage_event_enabled —— design_P2.md 批次表 #7 必须先引入 "
        "STAGE_EVENT_ENABLED(默认 false)并同 commit 写进 backend/.env.example;"
        "#10/#11 沿用同一道闸,见本计划 notes 的「依赖边 C」")
    assert field.default is False, "新闸必须默认关"


# ── 闸关 = 今天 ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_changes_nothing(db_session, overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    d, seats = await _staged_debate(db_session)
    before = {r.id: dict(r.mood_json or {}) for r in seats}

    await ds._resident_aftermath(db_session, d, "a")

    for r in seats:
        await db_session.refresh(r)
        assert await _audience_memories(db_session, r.id) == []
        assert dict(r.mood_json or {}) == before[r.id]
        assert await relation_service.get_pair(
            db_session, seats[0].id, seats[1].id) is None


# ── 闸开 = 四层非货币收益 ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_audience_gets_memory_mood_social_and_relations(
        db_session, overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    monkeypatch.setattr(settings, "realism_enabled", True)
    d, seats = await _staged_debate(db_session)
    social_before = {r.id: get_needs(r)["social"] for r in seats}

    await ds._resident_aftermath(db_session, d, "a")

    for r in seats:
        await db_session.refresh(r)
        mems = await _audience_memories(db_session, r.id)
        assert len(mems) == 1 and "剧院" in mems[0].content
        assert float((r.mood_json or {}).get("valence", 0.0)) > 0.0
        assert get_needs(r)["social"] > social_before[r.id]
    pair = await relation_service.get_pair(db_session, seats[0].id, seats[1].id)
    assert pair is not None and pair.familiarity > 0.0


@pytest.mark.anyio
async def test_no_event_no_audience_path(db_session, overlay, monkeypatch):
    """#7 没建 script 事件(= 今天每一场辩论)→ 降级到今天的行为。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    d, seats = await _staged_debate(db_session, with_event=False)

    await ds._resident_aftermath(db_session, d, "a")

    for r in seats:
        assert await _audience_memories(db_session, r.id) == []


@pytest.mark.anyio
async def test_debaters_keep_exactly_their_own_aftermath(
        db_session, overlay, monkeypatch):
    """辩手不是自己的观众:赢家仍然只有一条记忆 + 原来的 +0.3/+0.1 心情。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    d, _ = await _staged_debate(db_session)
    ann = (await db_session.execute(
        select(Resident).where(Resident.slug == "ann"))).scalar_one()
    ann.tile_x, ann.tile_y = INSIDE       # 辩手就站在台上
    await db_session.commit()

    await ds._resident_aftermath(db_session, d, "a")

    mems = await _audience_memories(db_session, ann.id)
    assert len(mems) == 1 and "中赢了" in mems[0].content


# ── 零 SC:两条独立证据 ───────────────────────────────────────────────

def test_audience_path_never_mentions_money():
    src = inspect.getsource(ds._audience_aftermath)
    for token in ("coin_service", "reward", "treasury", "balance_sc",
                  "soul_coin", "charge("):
        assert token not in src, (
            f"_audience_aftermath 出现 {token!r} —— 观众收益必须留在"
            "「记忆/心情/需求/关系」四个非货币层(design_P2.md §②-c)")


@pytest.mark.anyio
async def test_settle_only_burns_and_audience_adds_zero(
        db_session, overlay, monkeypatch):
    """走完整条 settle:总币量变化恰为 -burn,观众一枚都没多拿。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    d, seats = await _staged_debate(db_session)
    db_session.add(ResidentTreasury(resident_slug="w0", balance_sc=42))
    await db_session.commit()

    u1 = User(name="u1", email="u1@d.com", soul_coin_balance=1000)
    u2 = User(name="u2", email="u2@d.com", soul_coin_balance=1000)
    db_session.add_all([u1, u2])
    await db_session.commit()
    await ds.stake(db_session, d.id, u1.id, "a", 100)
    await ds.stake(db_session, d.id, u2.id, "b", 100)
    d.status, d.votes_a, d.votes_b = "voting", 3, 1
    await db_session.commit()

    res = await ds.settle(db_session, d.id)

    assert res["winner"] == "a"
    assert res["loser_pool"] == 100 and res["distributable"] == 95
    assert res["burn"] == 5
    total = sum((await db_session.execute(
        select(User.soul_coin_balance))).scalars().all())
    assert total == 2000 - res["burn"], "settle 只销毁 5%,不铸币"
    treasury = (await db_session.execute(
        select(ResidentTreasury.balance_sc))).scalars().all()
    assert treasury == [42], "观众收益不得动任何居民金库"
    for r in seats:
        assert len(await _audience_memories(db_session, r.id)) == 1


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
