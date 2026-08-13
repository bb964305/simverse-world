from app.agent.map_data import (
    LOCATIONS,
    assign_home,
    get_location_at,
    get_location_id_at,
    get_location_by_id,
    get_public_locations,
    get_housing_locations,
    find_nearest_location,
    format_location_list_for_prompt,
    get_valid_target_tile,
)


def test_locations_has_all_entries():
    assert len(LOCATIONS) == 34
    assert "academy" in LOCATIONS
    assert "tavern" in LOCATIONS
    assert "house_a" in LOCATIONS
    assert "apt_star" in LOCATIONS
    assert "central_plaza" in LOCATIONS
    assert "experiment_building" in LOCATIONS
    assert "market_hall" in LOCATIONS
    assert "east_gardens" in LOCATIONS
    assert "south_quarter" in LOCATIONS


def test_get_location_at_inside_library():
    loc = get_location_at(63, 48)
    assert loc is not None
    assert loc["name"] == "图书馆"


def test_get_location_at_outside():
    loc = get_location_at(0, 0)
    assert loc is None


def test_get_location_id_at():
    assert get_location_id_at(63, 48) == "library"
    assert get_location_id_at(0, 0) is None


def test_get_location_by_id():
    loc = get_location_by_id("tavern")
    assert loc is not None
    assert loc["name"] == "酒馆"
    assert get_location_by_id("nonexistent") is None


def test_get_public_locations():
    pubs = get_public_locations()
    assert len(pubs) == 9  # +experiment_building and standalone market_hall
    names = [p["name"] for p in pubs]
    assert "学院" in names
    assert "市政厅" in names


def test_get_housing_locations():
    houses = get_housing_locations()
    assert len(houses) == 19  # 9 private + 10 apartment
    total_cap = sum(h["capacity"] for h in houses)
    assert total_cap == 59


def test_find_nearest_location_public():
    loc_id, loc = find_nearest_location(60, 48)
    assert loc_id == "library"  # (63,48) center, dist=3


def test_find_nearest_location_filtered():
    loc_id, loc = find_nearest_location(60, 48, loc_type="public")
    assert loc_id == "library"
    loc_id2, _ = find_nearest_location(60, 48, loc_type="private")
    assert loc_id2 != "library"


def test_format_location_list_for_prompt():
    text = format_location_list_for_prompt()
    assert "学院" in text
    assert "酒馆" in text
    assert "图书馆" in text
    assert "住宅A" not in text  # private homes not listed


def test_get_valid_target_tile():
    tile = get_valid_target_tile("library")
    assert tile == (57, 43)  # entrance
    assert get_valid_target_tile("nonexistent") is None


def test_get_valid_target_tile_fallback_to_center():
    tile = get_valid_target_tile("central_plaza")
    assert tile == (75, 56)  # center, since no entrance


def test_market_day_location_metadata_tracks_the_authored_gate_and_parking():
    assert LOCATIONS["town_entrance"]["bounds"] == (100, 119, 104, 122)
    assert LOCATIONS["town_entrance"]["center"] == (102, 121)
    assert LOCATIONS["market_hall"]["bounds"] == (105, 89, 119, 99)
    assert LOCATIONS["market_hall"]["entrance"] == (105, 94)
    assert LOCATIONS["market_hall"]["caravan_parking"] == (109, 94)
    assert LOCATIONS["market_hall"]["allocatable"] is False
    assert "caravan_parking" not in LOCATIONS["central_plaza"]


def test_market_hall_is_visit_only_not_a_resident_allocation_district():
    from app.services.resident_placement import (
        ALLOCATABLE_LOCATION_IDS,
        LOCATION_TILE_SLOTS,
        normalize_location_id,
    )

    assert "market_hall" not in LOCATION_TILE_SLOTS
    assert "market_hall" not in ALLOCATABLE_LOCATION_IDS
    assert normalize_location_id("market_hall", allocatable_only=True) == "central_plaza"


# ── Housing Assignment Tests ─────────────────────────────────────────


def test_assign_home_first_resident_gets_private():
    """First 6 residents get private houses."""
    loc_id = assign_home(occupied={})
    assert loc_id == "house_a"


def test_assign_home_respects_capacity():
    """Once private houses are full, assigns apartments."""
    occupied = {"house_a": 1, "house_b": 1, "house_c": 1,
                "house_d": 1, "house_e": 1, "house_f": 1}
    loc_id = assign_home(occupied=occupied)
    assert loc_id == "apt_star"


def test_assign_home_apartment_fills():
    """Apartments fill room by room."""
    occupied = {"house_a": 1, "house_b": 1, "house_c": 1,
                "house_d": 1, "house_e": 1, "house_f": 1,
                "apt_star": 5, "apt_moon": 3}
    loc_id = assign_home(occupied=occupied)
    assert loc_id == "apt_moon"  # still has capacity


def test_assign_home_partial_fill_skips_correctly():
    """When house_a is full, next resident gets house_b."""
    occupied = {"house_a": 1}
    loc_id = assign_home(occupied=occupied)
    assert loc_id == "house_b"


def test_assign_home_all_full():
    """When everything is full, returns None."""
    occupied = {
        location_id: location["capacity"]
        for location_id, location in LOCATIONS.items()
        if location["type"] in ("private", "apartment")
    }
    loc_id = assign_home(occupied=occupied)
    assert loc_id is None
