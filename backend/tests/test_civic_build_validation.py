"""P3 ②:公投建楼的落库前几何校验(纯函数层)。

validate_add_location(apply.py:50-87) 今天把邮局/剧院判成 bounds overlap ——
命中的唯一对象都是 type="outdoor" 的大街区(south_quarter / east_gardens)。
本文件钉三件事:旧入口逐字节不变、outdoor 降级为 warning 且不 break、
upsert 允许同 slug。
"""
import pytest

from app.agent import map_data
from app.lab.apply import validate_add_location, validate_location_patch

POST_OFFICE = {"slug": "post_office", "data": {
    "name": "邮局", "type": "public", "role": "logistics",
    "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"]}}
THEATER = {"slug": "theater", "data": {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"]}}


@pytest.fixture
def locations_snapshot():
    """LOCATIONS 是可变全局;本文件会往尾部塞动态楼,必须快照+还原。"""
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    yield map_data.LOCATIONS
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn


def test_legacy_entry_is_byte_identical():
    """旧入口的返回值一个字都不许变(admin 预览 + lab apply 都在读它)。"""
    assert validate_add_location(POST_OFFICE) == [
        "bounds overlap existing location 'south_quarter'"]
    assert validate_add_location(THEATER) == [
        "bounds overlap existing location 'east_gardens'"]
    good = {"slug": "observatory", "data": {
        "name": "天文台", "bounds": [5, 88, 15, 96], "entrance": [10, 88]}}
    assert validate_add_location(good) == []


def test_outdoor_overlap_downgrades_to_warning():
    for patch, block in ((POST_OFFICE, "south_quarter"), (THEATER, "east_gardens")):
        errors, warnings = validate_location_patch(
            patch, outdoor_overlap_is_warning=True)
        assert errors == [], f"{patch['slug']} 是合法选址,不该被 outdoor 街区误杀"
        assert warnings == [f"bounds sit inside outdoor block '{block}'"]


def test_non_outdoor_overlap_stays_an_error():
    """楼压楼才是真冲突:academy(15,18,42,34) 是 public。"""
    patch = {"slug": "x", "data": {"name": "X", "bounds": [20, 20, 30, 30]}}
    errors, warnings = validate_location_patch(
        patch, outdoor_overlap_is_warning=True)
    assert errors == ["bounds overlap existing location 'academy'"]
    assert warnings == []


def test_scan_does_not_stop_at_the_first_outdoor_block(locations_snapshot):
    """east_gardens(索引 32) 排在动态楼(尾部)之前 —— 降级后若还 break,
    压在剧院身上的新楼就查不出来了。"""
    locations_snapshot["theater"] = {**THEATER["data"],
                                     "bounds": (172, 40, 178, 50)}
    patch = {"slug": "annex", "data": {
        "name": "侧厅", "bounds": [174, 44, 177, 48], "entrance": [175, 45]}}
    assert validate_add_location(patch) == [
        "bounds overlap existing location 'east_gardens'"], "legacy 仍是首命中即停"
    errors, warnings = validate_location_patch(
        patch, outdoor_overlap_is_warning=True)
    assert errors == ["bounds overlap existing location 'theater'"]
    assert warnings == ["bounds sit inside outdoor block 'east_gardens'"]


def test_existing_slug_is_an_upsert_when_allowed(locations_snapshot):
    """公投重复执行同一条 effect 是覆盖写,不是冲突;自己也不与自己重叠。"""
    locations_snapshot["theater"] = {**THEATER["data"],
                                     "bounds": (172, 40, 178, 50)}
    assert any("already exists" in e for e in validate_add_location(THEATER))
    errors, warnings = validate_location_patch(
        THEATER, allow_existing_slug=True, outdoor_overlap_is_warning=True)
    assert errors == []
    assert warnings == ["bounds sit inside outdoor block 'east_gardens'"]


# ── walkable 域越界(S2) ────────────────────────────────────────────────

def test_walkable_range_check_is_opt_in():
    """默认关 = 旧行为:天文台 bounds x1=5 在 walkable 域外,旧入口照样放行。"""
    good = {"slug": "observatory", "data": {
        "name": "天文台", "bounds": [5, 88, 15, 96], "entrance": [10, 88]}}
    assert validate_add_location(good) == []
    errors, _ = validate_location_patch(good, require_walkable_range=True)
    assert errors == [
        "bounds/entrance leave the walkable area [14,173]x[12,123]: (5,88)"]


def test_theater_is_rejected_by_walkable_range():
    """WALKABLE_X_RANGE 上限 173,而剧院 bounds x2=178 / center x=175。
    只比 MAP_WIDTH_TILES=180 的旧规则放行了它 —— 这条就是那道缺口。"""
    errors, _ = validate_location_patch(
        THEATER, allow_existing_slug=True, outdoor_overlap_is_warning=True,
        require_walkable_range=True)
    assert errors == [
        "bounds/entrance leave the walkable area [14,173]x[12,123]: (178,50)"]


def test_post_office_passes_walkable_range():
    errors, warnings = validate_location_patch(
        POST_OFFICE, outdoor_overlap_is_warning=True, require_walkable_range=True)
    assert errors == []
    assert warnings == ["bounds sit inside outdoor block 'south_quarter'"]
