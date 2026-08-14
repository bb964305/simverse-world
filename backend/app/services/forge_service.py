"""DEPRECATED re-export shim — the code moved out of this module (P1-6 file split).

- Resident placement utilities  -> app/services/resident_placement.py
- Legacy forge session store    -> app/forge/legacy_sessions.py
- Legacy forge LLM pipelines    -> app/forge/legacy_pipeline.py
- Legacy pure text helpers      -> app/forge/legacy_helpers.py
- Legacy prompt templates       -> app/forge/legacy_prompts.py

New code should import from those modules (or use app/forge/pipeline.py).
This shim only preserves old import paths; it contains no logic.
Note: patching names on this module no longer affects the real
implementations — patch the modules listed above instead.
"""

from app.forge.legacy_helpers import _compute_star_rating_fallback  # noqa: F401
from app.forge.legacy_pipeline import run_generation_pipeline, run_quick_pipeline  # noqa: F401
from app.forge.legacy_sessions import (  # noqa: F401
    get_status,
    start_forge,
    start_quick_forge,
    submit_answer,
)
from app.services.resident_placement import (  # noqa: F401
    ALLOCATABLE_LOCATION_IDS,
    DEFAULT_LOCATION_ID,
    DISTRICT_TILE_SLOTS,
    LEGACY_LOCATION_ALIASES,
    LOCATION_TILE_SLOTS,
    SPRITE_KEYS,
    VALID_LOCATION_IDS,
    _find_available_tile,
    _generate_slug,
    allocate_resident_location,
    infer_location_id_from_text,
    normalize_location_id,
)
