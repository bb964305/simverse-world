from app.services.event_location import resolve_event_location_id


def test_new_and_legacy_market_days_resolve_to_the_market_hall():
    assert resolve_event_location_id({"market_day": True, "location_id": "market_hall"}) == "market_hall"
    assert resolve_event_location_id({"market_day": True, "location_id": "central_plaza"}) == "market_hall"
    assert resolve_event_location_id({"market_day": True}) == "market_hall"


def test_custom_explicit_market_location_is_preserved():
    assert resolve_event_location_id({"market_day": True, "location_id": "east_gardens"}) == "east_gardens"


def test_non_market_event_location_is_unchanged():
    assert resolve_event_location_id({"location_id": "central_plaza"}) == "central_plaza"
    assert resolve_event_location_id({}) is None
