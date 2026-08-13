"""Read-time location compatibility for world events.

Market days are authored at ``settings.market_day_venue``.  While the venue is
the default ``central_plaza`` this module is a pass-through: readers see the
stored payload value exactly as master did.  Once the venue is switched to
``market_hall``, existing active and upcoming rows keep their legacy plaza
payload in the database; readers project only those legacy market-day rows onto
the purpose-built market hall.  Explicit custom locations remain authoritative.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import settings

MARKET_HALL_LOCATION_ID = "market_hall"
LEGACY_MARKET_DAY_LOCATION_ID = "central_plaza"


def resolve_event_location_id(payload_json: Mapping[str, Any] | None) -> str | None:
    """Return the canonical event location without mutating its stored payload.

    With ``market_day_venue`` at its default ``central_plaza`` the stored
    ``location_id`` is returned untouched (master behavior).  When the venue is
    ``market_hall``, a market-day payload with no location, or the retired
    ``central_plaza`` location, resolves to ``market_hall``.  Any other explicit
    location is preserved so custom/scripted market days can intentionally run
    elsewhere.
    """
    payload = payload_json or {}
    location_id = payload.get("location_id")
    if settings.market_day_venue != MARKET_HALL_LOCATION_ID:
        return location_id
    if bool(payload.get("market_day")) and location_id in (
        None,
        "",
        LEGACY_MARKET_DAY_LOCATION_ID,
    ):
        return MARKET_HALL_LOCATION_ID
    return location_id if isinstance(location_id, str) and location_id else None
