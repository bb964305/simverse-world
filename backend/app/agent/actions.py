"""Action type definitions and context-sensitive filtering for the agent loop."""
from dataclasses import dataclass
from enum import Enum


class ActionType(str, Enum):
    # Social
    CHAT_RESIDENT  = "CHAT_RESIDENT"
    CHAT_FOLLOW_UP = "CHAT_FOLLOW_UP"
    GOSSIP         = "GOSSIP"
    # Movement
    WANDER         = "WANDER"
    VISIT_DISTRICT = "VISIT_DISTRICT"
    GO_HOME        = "GO_HOME"
    # Observe
    OBSERVE        = "OBSERVE"
    EAVESDROP      = "EAVESDROP"
    # Self
    REFLECT        = "REFLECT"
    JOURNAL        = "JOURNAL"
    # Work
    WORK           = "WORK"
    STUDY          = "STUDY"
    # Rest
    IDLE           = "IDLE"
    NAP            = "NAP"
    # Lab (元游戏入口): narrative-only tick action, gated to researchers inside
    # the experiment building. It never runs the real sandbox (that is the Lab
    # Runner's job); it only flips status→researching + writes a memory.
    RESEARCH       = "RESEARCH"
    # Needs (realism P1-10): eat at a dining-category location (pure state change).
    # Append-only 16th action — RESEARCH stays the 15th.
    EAT            = "EAT"


@dataclass
class ActionResult:
    """Parsed output from the LLM decision step."""
    action: ActionType
    target_slug: str | None        # Resident slug if social action
    target_tile: tuple[int, int] | None  # (x, y) destination if movement
    reason: str                    # LLM's one-sentence rationale


# Actions that require a nearby idle/walking resident as target
_SOCIAL_NEEDS_IDLE_TARGET = {ActionType.CHAT_RESIDENT, ActionType.GOSSIP, ActionType.CHAT_FOLLOW_UP}

# Actions that can target chatting residents (observer role)
_SOCIAL_OBSERVER = {ActionType.EAVESDROP}

# Actions always available
_ALWAYS_AVAILABLE = {ActionType.WANDER, ActionType.VISIT_DISTRICT, ActionType.OBSERVE,
                     ActionType.REFLECT, ActionType.JOURNAL, ActionType.WORK,
                     ActionType.STUDY, ActionType.IDLE, ActionType.NAP}


def get_available_actions(resident, nearby_residents: list) -> list[ActionType]:
    """Return the list of valid actions given current world context.

    Args:
        resident: Resident ORM object (current actor)
        nearby_residents: Resident ORM objects within interaction range

    Returns:
        Ordered list of ActionType values the LLM may choose from.
    """
    available: list[ActionType] = list(_ALWAYS_AVAILABLE)

    idle_nearby = [r for r in nearby_residents if r.status in ("idle", "walking") and r.id != resident.id]
    chatting_nearby = [r for r in nearby_residents if r.status in ("chatting", "socializing") and r.id != resident.id]

    if idle_nearby:
        available.extend(_SOCIAL_NEEDS_IDLE_TARGET)

    if chatting_nearby or idle_nearby:
        available.extend(_SOCIAL_OBSERVER)

    # GO_HOME: available when not already at home — except when realism says the
    # resident is running on empty. A resident that walked home *before* energy
    # went critical parks on the entrance tile as `idle`; hiding GO_HOME there
    # strands it (0809 production deadlock: 7/11 residents sat at their own door
    # with energy=satiety=0). "Arriving home exhausted → sleep" lives in execute's
    # already-at-destination branch, so offering GO_HOME is what unlocks sleep.
    from app.config import settings as _needs_settings
    _exhausted_at_home = False
    if _needs_settings.realism_enabled:
        from app.agent.needs import get_needs
        _exhausted_at_home = (get_needs(resident).get("energy", 1.0)
                              < _needs_settings.realism_needs_critical)

    home_loc_id = getattr(resident, 'home_location_id', None)
    if home_loc_id:
        from app.agent.map_data import get_location_by_id
        home_loc = get_location_by_id(home_loc_id)
        if home_loc:
            entrance = home_loc.get("entrance") or home_loc.get("center")
            if entrance and (_exhausted_at_home
                             or not (resident.tile_x == entrance[0]
                                     and resident.tile_y == entrance[1])):
                available.append(ActionType.GO_HOME)
    else:
        # Fallback to old home_tile_x/y
        home_x = resident.home_tile_x
        home_y = resident.home_tile_y
        if home_x is not None and home_y is not None:
            if _exhausted_at_home or not (resident.tile_x == home_x
                                          and resident.tile_y == home_y):
                available.append(ActionType.GO_HOME)

    # RESEARCH (Lab): gated to authorized researchers standing inside the
    # experiment building. meta_json["lab"]["access"] is the admin-granted
    # whitelist flag (spec §14 "研究员资格：先手动授权"). This keeps the real
    # sandbox entirely off the tick — RESEARCH is narrative-only.
    lab_meta = (getattr(resident, "meta_json", None) or {}).get("lab") or {}
    if lab_meta.get("access"):
        from app.agent.map_data import get_location_id_at
        if get_location_id_at(resident.tile_x, resident.tile_y) == "experiment_building":
            available.append(ActionType.RESEARCH)

    # EAT (realism P1-10): only inside a dining-category location.
    from app.config import settings as _settings
    if _settings.realism_enabled:
        from app.agent.map_data import location_category, get_location_id_at
        if location_category(get_location_id_at(resident.tile_x, resident.tile_y)) == "dining":
            available.append(ActionType.EAT)

    # Deduplicate while preserving order
    seen: set[ActionType] = set()
    result: list[ActionType] = []
    for a in available:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return result
