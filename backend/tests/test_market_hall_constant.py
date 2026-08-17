"""P1-S9: agent 侧 market_hall 字面量收敛到 event_location.MARKET_HALL_LOCATION_ID。

纯重构,同一字符串,零行为差,不挂闸。

明确不做能力派生:market_hall 不是「一个地点声明自己能做买卖」,而是「全镇有且只有
一个集市场地」。场地权威是 settings.market_day_venue + resolve_event_location_id,
路网几何(caravan_route._MARKET_AVENUE_X_BOUNDS / map_data 的 caravan_parking)按这一
栋楼的实际瓦片手调 —— 改成 capability_locations(market) 反查,一旦出现第二个
market-capable 地点,cohort 判据 / decide 目的地 / 商队停车锚点会指向不同的楼。
"""
import re
from pathlib import Path

from app.agent.map_data import LOCATIONS
from app.services.event_location import MARKET_HALL_LOCATION_ID

AGENT = Path(__file__).resolve().parents[1] / "app" / "agent"
SOURCES = [AGENT / "phases" / "decide" / "basic.py", AGENT / "tick.py"]


def test_the_constant_still_names_the_real_location():
    assert MARKET_HALL_LOCATION_ID == "market_hall"
    assert MARKET_HALL_LOCATION_ID in LOCATIONS
    assert LOCATIONS[MARKET_HALL_LOCATION_ID]["caravan_parking"] == (109, 94)


def test_no_bare_market_hall_literal_left_in_the_agent_hot_path():
    offenders = []
    for path in SOURCES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"[\"']market_hall[\"']", line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, offenders


def test_both_files_import_the_canonical_constant():
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        assert "MARKET_HALL_LOCATION_ID" in text, path.name
        assert "from app.services.event_location import" in text, path.name


def test_market_capability_is_not_used_for_venue_resolution():
    """收敛到常量,而不是收敛到能力反查 —— 判据只看**非注释代码**。

    逐行剥注释是硬要求:P1-S8 要在 decide/basic.py 插一段 P2 座位注释,其中逐字包含
    「map_data.capability_locations /」与「nearest_capability_location);分支本体在
    P2」。读全文断言会与 S8 互斥 —— 无论谁先落地都会把对方打红,两种顺序都违反
    「每 step 验证通过再进下一步」。过滤口径与本文件
    test_no_bare_market_hall_literal_left_in_the_agent_hot_path 完全一致。
    """
    offenders = []
    for path in SOURCES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if ("capability_locations" in line
                    or "nearest_capability_location" in line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, offenders
