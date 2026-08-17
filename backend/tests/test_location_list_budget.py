"""P3:计划 prompt 的地点清单也要有预算(公投可以无限建楼)。"""
import pytest

from app.agent import map_data
from app.agent.map_data import format_location_list_for_prompt as fmt


@pytest.fixture
def locations_snapshot():
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    yield map_data.LOCATIONS
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn


def test_todays_output_is_byte_identical():
    """实测今天 15 行 / 最长 description 36 字 —— 两个上限都取在当前值之上。"""
    text = fmt()
    assert len(text.splitlines()) == 15
    assert "学院" in text and "住宅A" not in text
    assert "…" not in text, "今天不该有任何一行被截断"


def test_static_places_survive_a_building_spree(locations_snapshot):
    for i in range(40):
        slug = f"zz{i:03d}"
        locations_snapshot[slug] = {"name": f"新楼{i:03d}", "type": "public",
                                    "description": "新建",
                                    "bounds": (2, 2, 3, 3), "entrance": (2, 2)}
        map_data._dynamic_slugs.add(slug)
    lines = fmt().splitlines()
    assert len(lines) == map_data.LOCATION_LIST_LIMIT
    assert any("实验楼" in ln for ln in lines), "静态设施不许被新楼整段顶掉"
    dyn = [ln for ln in lines if "新楼" in ln]
    # 口径抄 town_facts(_read_places:433 的 static+head+tail 也是填满 PLACES_LIMIT):
    # 预算填满,溢出的坑给非保留动态。15 静态 + 40 新楼 → 24 行里必有 24-15=9 行
    # 新楼,所以 RESERVE 保证的是「最新的那几栋一定在、且在末尾」,不是「动态只占 4 行」。
    reserve = map_data.LOCATION_LIST_DYNAMIC_RESERVE
    assert [ln.split("（")[0] for ln in lines[-reserve:]] == [
        f"- 新楼{i:03d}" for i in range(40 - reserve, 40)], "保留位恒给最新的 N 栋"
    assert "新楼039" in dyn[-1], "保留位给最新的楼(插入序末尾)"
    assert lines[0].startswith("- 学院"), "渲染顺序仍是静态在前(前缀缓存)"


def test_long_description_is_clipped(locations_snapshot):
    locations_snapshot["zz"] = {"name": "话痨楼", "type": "public",
                                "description": "啰" * 300,
                                "bounds": (2, 2, 3, 3), "entrance": (2, 2)}
    map_data._dynamic_slugs.add("zz")
    line = next(ln for ln in fmt().splitlines() if "话痨楼" in ln)
    assert "啰" * (map_data.LOCATION_LIST_DESC_CHARS + 1) not in line


def test_civic_build_cap_matches_the_prompt_clip():
    from app.services import civic_build
    assert (civic_build.MAX_DESCRIPTION_CHARS
            == map_data.LOCATION_LIST_DESC_CHARS == 40)
