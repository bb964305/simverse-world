"""Server-side plan/decision target resolution (realism P0-1).

Model-reported coordinates are ignored by design — hallucinated tiles are half
the "plan doesn't move" bug (diagnosis §3.4). The target tile is always derived
from a known location: prefer an explicit location id (slug), else the display
name. Anything that is not a resolvable string slug/name yields ``None`` (the
caller then leaves the resident where it is rather than pathing to a phantom).
"""
from __future__ import annotations

from app.agent.map_data import (
    get_location_by_id,
    get_location_id_by_name,
    get_valid_target_tile,
)


def resolve_target_tile(
    target_slug: object | None,
    location_name: object | None,
) -> tuple[int, int] | None:
    """Resolve an entrance tile from a location slug and/or display name.

    ``target_slug`` is tried first as a known location id; if it is not a
    string or not a known slug it is ignored (this is how model-reported
    coordinates get discarded). ``location_name`` is then tried as a display
    name. Returns the entrance/center tile, or ``None`` if unresolvable.
    """
    loc_id = resolve_location_id(target_slug, location_name)
    if loc_id is not None:
        return get_valid_target_tile(loc_id)
    return None


def resolve_location_id(
    target_slug: object | None,
    location_name: object | None,
) -> str | None:
    """Return the canonical location id for old and new plan shapes.

    New plans carry a location id in ``target``.  Legacy production plans carry
    an entrance coordinate list there and retain only the display name in
    ``location``.  Coordinates are deliberately never trusted; the display-name
    fallback resolves them through the server-owned map instead.
    """
    if isinstance(target_slug, str):
        if get_location_by_id(target_slug) is not None:
            return target_slug
        by_target_name = get_location_id_by_name(target_slug)
        if by_target_name is not None:
            return by_target_name
    if isinstance(location_name, str):
        if get_location_by_id(location_name) is not None:
            return location_name
        return get_location_id_by_name(location_name)
    return None
