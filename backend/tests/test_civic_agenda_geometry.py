"""CIVIC_AGENDA 的建楼载荷必须自己过得了 P3 的几何校验。

seed_civic_agenda 的幂等键是 Poll.question 精确匹配(civic_service.py:208-210),
所以坐标可以改、topic 一个字都不能动 —— 改了就是重开一张票。
"""
import pytest

from app.agent import pathfinder
from app.lab.apply import validate_location_patch
from app.services.civic_service import CIVIC_AGENDA

TOPICS = ("在南苑空地兴建一座邮局", "在东岸花园兴建一座剧院")


@pytest.fixture(autouse=True)
def fresh_path_cache():
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


def test_topics_are_frozen():
    """幂等键 —— 动一个字就会给已建成的楼重开一张票。"""
    assert tuple(item["topic"] for item in CIVIC_AGENDA) == TOPICS


@pytest.mark.parametrize("idx", range(2))
def test_agenda_build_payload_passes_p3_validation(idx):
    data = CIVIC_AGENDA[idx]["options"][0]["effect"]["data"]
    errors, _ = validate_location_patch(
        {"slug": data["slug"],
         "data": {k: v for k, v in data.items() if k != "slug"}},
        allow_existing_slug=True,
        outdoor_overlap_is_warning=True,
        require_walkable_range=True,
        require_reachable_entrance=True,
    )
    assert errors == [], f"{data['slug']} 的 agenda 坐标过不了校验:{errors}"


def test_theater_literal_matches_the_068_migration():
    data = CIVIC_AGENDA[1]["options"][0]["effect"]["data"]
    assert data["slug"] == "theater"
    assert data["bounds"] == [168, 40, 173, 50]
    assert data["center"] == [170, 45]
    assert data["entrance"] == [172, 45]


def test_post_office_literal_is_untouched():
    data = CIVIC_AGENDA[0]["options"][0]["effect"]["data"]
    assert data["bounds"] == [44, 100, 48, 106]
    assert data["center"] == [46, 103]
    assert data["entrance"] == [46, 100]
