"""write_collective_memories 收件人集合与 COLLECTIVE_MEMORY_SIM_ONLY 闸(F11)。

master 的收件人谓词只有 ``Resident.status != "sleeping"``——玩家化身
(``resident_type="player"``,不在 ``SIM_RESIDENT_TYPES``)也收 world_event 记忆。
caravan 分支曾无闸加上 ``is_autonomous`` 过滤,违反「新 flag 默认关时行为与
master 逐字节一致」红线。修法:过滤挂 ``collective_memory_sim_only`` 闸,
默认 False = master 谓词原样(含玩家化身);开闸才收紧到 sim 居民。

用天气事件测:``type == "weather"`` 是琐事档(TIER_TRIVIA),直写循环不算
embedding,测试不碰外部依赖。
"""
import pytest
from sqlalchemy import select

from app.config import Settings, settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import world_event_service as wes


async def _mixed_residents(db) -> dict[str, str]:
    """一个 npc、一个 UGC resident、一个玩家化身,全部非 sleeping。"""
    kinds = {"npc": "npc", "ugc": "resident", "player": "player"}
    for key, rtype in kinds.items():
        db.add(Resident(id=f"cmr-{key}", slug=f"cmr-{key}", name=f"CMR-{key}",
                        creator_id="sys", district="cafe", status="idle",
                        resident_type=rtype, tile_x=0, tile_y=0))
    await db.commit()
    return {key: f"cmr-{key}" for key in kinds}


async def _recipient_ids(db) -> set[str]:
    rows = (await db.execute(
        select(Memory.resident_id).where(Memory.source == "world_event")
    )).all()
    return {rid for (rid,) in rows}


_WEATHER_EVENT = {"id": "w-cmr", "type": "weather",
                  "description": "天空放晴,云散了。", "payload_json": {}}


def test_sim_only_knob_defaults_off():
    fields = Settings.model_fields
    assert "collective_memory_sim_only" in fields
    assert fields["collective_memory_sim_only"].default is False


@pytest.mark.anyio
async def test_collective_memory_includes_player_avatars_by_default(db_session):
    """闸关(默认):谓词与 master 逐字节一致——玩家化身照收。"""
    ids = await _mixed_residents(db_session)

    n = await wes.write_collective_memories(db_session, _WEATHER_EVENT)

    assert n == 3
    assert await _recipient_ids(db_session) == set(ids.values())


@pytest.mark.anyio
async def test_collective_memory_sim_only_excludes_players(db_session, monkeypatch):
    """闸开:收紧到 ``is_autonomous``(npc + UGC resident),玩家化身不收。"""
    monkeypatch.setattr(settings, "collective_memory_sim_only", True)
    ids = await _mixed_residents(db_session)

    n = await wes.write_collective_memories(db_session, _WEATHER_EVENT)

    assert n == 2
    assert await _recipient_ids(db_session) == {ids["npc"], ids["ugc"]}
