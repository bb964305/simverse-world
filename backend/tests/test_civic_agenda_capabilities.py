"""P2-S1: 邮局 effect.data 的 postal 能力声明 —— 规范 dict 形态 + 两条守卫。

两条守卫各防一类事故:
  · 能力白名单:CIVIC_AGENDA 是「公投能造出什么」的源头(routers/polls.py:94-96 允许
    admin 附带任意 effect dict,_add_dynamic_location 只校验 slug 非空 + bounds 在就
    整包落库)。声明的能力必须全部落在 CIVIC_GRANTABLE_CAPABILITIES 内 ——
    research 恒不在其中,否则一张票就能绕过实验楼的地点门(actions.py:130)。
  · topic 冻结:seed_civic_agenda 的幂等键是 Poll.question 精确匹配
    (civic_service.py:208-210)。topic 改一个字符就重开一张票,而同 slug 再建走的是
    _add_dynamic_location 的整包覆盖分支(existing.data_json = payload),旧键全丢。

第一条是 P2 → P1-S1 的依赖边守卫:postal/stage 没登记 → normalize_capabilities 会把
它们静默丢弃(只 logger.debug),全链零告警。所以这里用字符串字面量而不是 import
常量,好让失败信息直接说清该改哪。
"""
import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import (
    CAPABILITIES,
    CIVIC_GRANTABLE_CAPABILITIES,
    normalize_capabilities,
)
from app.models.season import Poll
from app.services import civic_service
from app.services.civic_service import CIVIC_AGENDA

#: 生产两张建楼票的 topic 逐字快照。**任何 data 改动都不得让它变化。**
FROZEN_TOPICS = ["在南苑空地兴建一座邮局", "在东岸花园兴建一座剧院"]


def _agenda_data(slug: str) -> dict:
    for item in CIVIC_AGENDA:
        for opt in item["options"]:
            data = ((opt.get("effect") or {}).get("data") or {})
            if data.get("slug") == slug:
                return data
    raise AssertionError(f"CIVIC_AGENDA 里没有 slug={slug} 的建楼选项")


def test_postal_and_stage_are_registered_by_p1_s1():
    """P2 → P1-S1 的依赖边:两个能力必须先在闭集注册表里登记且可被公投授予。"""
    missing = [c for c in ("postal", "stage") if c not in CAPABILITIES]
    assert not missing, (
        f"app/agent/location_caps.py 的 CAPABILITIES 缺 {missing} —— "
        "P1-S1 必须先登记 postal/stage(civic_grantable=True,unlocks=(),category=None),"
        "见 P2 计划 notes 的「新增依赖边 A」")
    for cap in ("postal", "stage"):
        spec = CAPABILITIES[cap]
        assert spec.civic_grantable is True, cap
        assert spec.unlocks == (), f"{cap} 不得解锁任何动作 —— P2 零新增 ActionType"
        assert spec.category is None, f"{cap} 不得派生 category(会污染 EAT 通路)"
    assert {"postal", "stage"} <= CIVIC_GRANTABLE_CAPABILITIES


def test_post_office_declares_postal_in_the_canonical_dict_form():
    assert _agenda_data("post_office")["capabilities"] == {"postal": {}}


def test_the_declaration_is_a_fixed_point_of_normalization():
    """规范形态 = 归一化的不动点。写成 [\"postal\"] 也能用,但落库的就不是规范形态。"""
    declared = _agenda_data("post_office")["capabilities"]
    assert normalize_capabilities(declared) == declared
    assert normalize_capabilities(["postal"]) == declared  # 宽松入口仍等价


def test_every_capability_in_the_agenda_is_civic_grantable():
    for item in CIVIC_AGENDA:
        for opt in item["options"]:
            data = ((opt.get("effect") or {}).get("data") or {})
            declared = normalize_capabilities(data.get("capabilities"))
            assert set(declared) <= CIVIC_GRANTABLE_CAPABILITIES, data.get("slug")
            assert "research" not in declared, data.get("slug")


def test_only_the_data_changed_topics_stay_frozen():
    assert [item["topic"] for item in CIVIC_AGENDA] == FROZEN_TOPICS


def test_the_rest_of_the_post_office_payload_is_untouched():
    data = _agenda_data("post_office")
    assert data["bounds"] == [44, 100, 48, 106]
    assert data["center"] == [46, 103]
    assert data["entrance"] == [46, 100]
    assert data["type"] == "public" and data["role"] == "logistics"
    assert data["boosted_actions"] == ["WORK"]
    assert data["description"] == "小镇邮局:寄信、收件、时间胶囊的中转站"


@pytest.mark.anyio
async def test_seed_is_still_idempotent_on_the_frozen_topics(db_session):
    """topic 没动 → 已有票的世界不会因为 data 改动重开票(否则同 slug 整包覆盖)。"""
    for topic in FROZEN_TOPICS:
        db_session.add(Poll(question=topic, options_json=[], status="closed"))
    await db_session.commit()

    assert await civic_service.seed_civic_agenda(db_session) == 0
    rows = (await db_session.execute(select(Poll))).scalars().all()
    assert len(rows) == len(FROZEN_TOPICS)


def test_action_type_enum_is_untouched():
    """P2 全段零新增 ActionType(design_P2.md §「为什么不新增 ActionType」)。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
