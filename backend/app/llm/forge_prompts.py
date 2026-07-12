"""DEPRECATED re-export shim — legacy forge prompts moved to app/forge/legacy_prompts.py
(P1-6 file split; all forge prompts now live under app/forge/).

Prompts for the canonical stage pipeline are in app/forge/prompts.py.
"""

from app.forge.legacy_prompts import (  # noqa: F401
    ABILITY_SYSTEM_PROMPT, ABILITY_USER_TEMPLATE,
    DISTRICT_SYSTEM_PROMPT, DISTRICT_USER_TEMPLATE,
    FORGE_QUESTIONS,
    PERSONA_SYSTEM_PROMPT, PERSONA_USER_TEMPLATE,
    QUICK_EXTRACT_SYSTEM_PROMPT, QUICK_EXTRACT_USER_TEMPLATE,
    SCORING_SYSTEM_PROMPT, SCORING_USER_TEMPLATE,
    SOUL_SYSTEM_PROMPT, SOUL_USER_TEMPLATE,
)
