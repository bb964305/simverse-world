"""P2-S8: STAGE_EVENT_ENABLED 闸 + create_debate 的 venue 参数与场地的 Redis 传递。

本 step 只把「场地」从 create_debate 送到 run_live 读得到的地方,不建任何
WorldEvent(那是 P2-S9)。debates 表不加列 —— 加列 = 迁移,触犯「迁移与开闸不同车」。
场地走 Redis,与同文件 _VOTING_SINCE_KEY 给 settle 传相位时刻是同一条思路。

fail-closed:读不到就没有场地,绝不臆造。announced→live 只隔 debate_stake_window_min
(默认 30 分钟),要在这个窗口里丢 Redis 才漏得掉一场的人流拉力。
"""
import inspect
from pathlib import Path

import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.config import Settings
from app.models.debate import Debate
from app.models.resident import Resident
from app.services import debate_service as ds

BACKEND = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND / ".env.example"
DEPLOY_ENV_EXAMPLE = BACKEND.parent / "deploy" / "backend" / ".env.example"

# 生产 dynamic_locations 里 theater 那行的 data_json(2026-08 公投建,active=t);
# capabilities 由调用方按场景决定加不加 —— 存量行今天**没有**这个键。
THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。"""
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


async def _residents(db):
    db.add_all([
        Resident(slug="ann", name="安", creator_id="system", district="cafe",
                 resident_type="npc", status="idle", tile_x=1, tile_y=1),
        Resident(slug="bo", name="波", creator_id="system", district="cafe",
                 resident_type="npc", status="idle", tile_x=2, tile_y=2),
    ])
    await db.commit()


async def _debate(db, **kw):
    await _residents(db)
    return await ds.create_debate(db, "猫和狗谁更好", "ann", "bo", **kw)


def _redis_down():
    raise RuntimeError("redis down")


# ── 闸 ────────────────────────────────────────────────────────────────

def test_flag_defaults_to_off():
    assert Settings.model_fields["stage_event_enabled"].default is False


def test_flag_is_documented_as_false_in_both_env_templates():
    for path in (ENV_EXAMPLE, DEPLOY_ENV_EXAMPLE):
        assert "STAGE_EVENT_ENABLED=false" in path.read_text(encoding="utf-8"), path


# ── 签名与 schema ─────────────────────────────────────────────────────

def test_venue_is_keyword_only_and_defaults_to_none():
    """默认 None = 今天所有调用方逐字节不变。"""
    sig = inspect.signature(ds.create_debate)
    assert list(sig.parameters) == ["db", "topic", "a_slug", "b_slug", "venue"]
    p = sig.parameters["venue"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is None


def test_debates_table_gained_no_location_column():
    """零迁移:场地不进 schema。"""
    assert set(Debate.__table__.columns.keys()) == {
        "id", "topic", "resident_a_slug", "resident_b_slug", "status",
        "transcript_json", "winner", "pool_a", "pool_b", "votes_a", "votes_b",
        "starts_at", "settled_at"}


# ── 传递 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_venue_means_nothing_is_remembered(db_session):
    d = await _debate(db_session)
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_a_stage_venue_round_trips(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _debate(db_session, venue="theater")
    assert await ds._debate_venue(d.id) == "theater"


@pytest.mark.anyio
async def test_legacy_row_without_the_declaration_is_not_a_venue(db_session, overlay):
    """存量 dynamic_locations 行没有 capabilities 键 —— 未回填时静默降级,不抛。"""
    overlay("theater", THEATER)
    d = await _debate(db_session, venue="theater")
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_an_unknown_slug_is_not_a_venue(db_session):
    d = await _debate(db_session, venue="nowhere")
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_the_venue_is_revalidated_on_read(db_session, overlay):
    """Redis 里的值可能是几天前写的,而能力声明是公投随时能改的数据。"""
    slug = overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _debate(db_session, venue=slug)
    LOCATIONS[slug].pop("capabilities")
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_redis_loss_degrades_to_no_venue(db_session, overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _debate(db_session, venue="theater")
    monkeypatch.setattr("app.redis_client.get_redis", _redis_down)
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_a_redis_write_failure_never_breaks_debate_creation(
        db_session, overlay, monkeypatch):
    """场地是叙事装饰;辩论本体(玩家能押注的那个对象)不能因它建不出来。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr("app.redis_client.get_redis", _redis_down)
    d = await _debate(db_session, venue="theater")
    assert d.id and d.status == "announced"


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
