"""P2-S7: 剧院 effect.data 的 stage 能力声明 —— 规范 dict 形态 + 三条守卫。

与邮局那条(P2-S1)同形状,但多一条**几何不冻结**的纪律:theater 的
bounds/center/entrance 是 design_P2.md §④ 认定的越界坐标(x2=178 > WALKABLE_X_RANGE
上限 173),归 P3-c 的迁移批次修,而 068_fix_theater_bounds 同批要改这里的字面量。
所以本文件只冻结**非几何**字段与结构不变量,绝不 pin 具体数值 —— pin 了,P3-c 落地
当天这条测试就是一条已知红。

第一条是 P2 → P1-S1 的依赖边守卫:stage 没登记 → normalize_capabilities 会把它
静默丢弃(只 logger.debug),全链零告警。所以这里用字符串字面量而不是 import 常量,
好让失败信息直接说清该改哪。
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

#: 剧院 effect.data 的完整键集(加上本 step 的 capabilities)。用「不多不少」而不是
#: 「至少有」——多一个键就是有人顺手往公投载荷里塞了别的东西。
THEATER_KEYS = {
    "slug", "name", "type", "role", "bounds", "center", "entrance",
    "description", "boosted_actions", "capabilities",
}


def _agenda_data(slug: str) -> dict:
    for item in CIVIC_AGENDA:
        for opt in item["options"]:
            data = ((opt.get("effect") or {}).get("data") or {})
            if data.get("slug") == slug:
                return data
    raise AssertionError(f"CIVIC_AGENDA 里没有 slug={slug} 的建楼选项")


def test_stage_is_registered_by_p1_s1():
    """P2 → P1-S1 的依赖边:stage 必须先在闭集注册表里登记且可被公投授予。"""
    assert "stage" in CAPABILITIES, (
        "app/agent/location_caps.py 的 CAPABILITIES 缺 'stage' —— P1-S1 必须先登记"
        "(civic_grantable=True, unlocks=(), category=None),见 P2 计划 notes 的"
        "「新增依赖边 A」")
    spec = CAPABILITIES["stage"]
    assert spec.civic_grantable is True
    assert spec.unlocks == (), "stage 不得解锁任何动作 —— P2 零新增 ActionType"
    assert spec.category is None, "stage 不得派生 category(会污染 EAT 通路)"
    assert "stage" in CIVIC_GRANTABLE_CAPABILITIES


def test_theater_declares_stage_in_the_canonical_dict_form():
    assert _agenda_data("theater")["capabilities"] == {"stage": {}}


def test_the_declaration_is_a_fixed_point_of_normalization():
    """规范形态 = 归一化的不动点。写成 [\"stage\"] 也能用,但落库的就不是规范形态。"""
    declared = _agenda_data("theater")["capabilities"]
    assert normalize_capabilities(declared) == declared
    assert normalize_capabilities(["stage"]) == declared  # 宽松入口仍等价


def test_theater_grants_nothing_outside_the_civic_whitelist():
    """CIVIC_AGENDA 是「公投能造出什么」的源头(routers/polls.py:94-96 允许 admin
    附带任意 effect dict,_add_dynamic_location 只校验 slug 非空 + bounds 在就整包
    落库)。research 恒不在白名单里,否则一张票就能绕过实验楼的地点门。"""
    declared = normalize_capabilities(_agenda_data("theater").get("capabilities"))
    assert set(declared) <= CIVIC_GRANTABLE_CAPABILITIES
    assert "research" not in declared


def test_only_the_data_changed_topics_stay_frozen():
    assert [item["topic"] for item in CIVIC_AGENDA] == FROZEN_TOPICS


def test_the_non_geometry_half_of_the_theater_payload_is_untouched():
    data = _agenda_data("theater")
    assert set(data) == THEATER_KEYS
    assert data["name"] == "剧院"
    assert data["type"] == "public" and data["role"] == "culture"
    assert data["description"] == "小镇剧院:说书、演展、故事会的舞台"
    assert data["boosted_actions"] == ["CHAT_RESIDENT", "OBSERVE"]


def test_the_geometry_is_structurally_valid_but_deliberately_not_frozen():
    """只判结构,不 pin 数值 —— 数值归 P3-c(068_fix_theater_bounds 同批改这里的
    字面量)。pin 了就是给那一批埋一条已知红。"""
    data = _agenda_data("theater")
    x1, y1, x2, y2 = data["bounds"]
    assert x1 < x2 and y1 < y2
    for key in ("center", "entrance"):
        px, py = data[key]
        assert x1 <= px <= x2 and y1 <= py <= y2, key


def test_boosted_actions_are_real_action_types():
    """prompts.py 的 boosted 提示句直接吃这些字符串,拼错就是一句永远命不中的提示。"""
    names = {a.name for a in ActionType}
    for item in CIVIC_AGENDA:
        for opt in item["options"]:
            data = ((opt.get("effect") or {}).get("data") or {})
            for act in data.get("boosted_actions") or []:
                assert act in names, (data.get("slug"), act)


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
