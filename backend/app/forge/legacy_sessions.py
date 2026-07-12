"""Legacy forge in-memory session store + 5-step Q&A state machine.

Serves the legacy /forge/start, /forge/answer, /forge/quick and /forge/status
endpoints. Superseded by app/forge/pipeline.py for new code. Moved verbatim
from app/services/forge_service.py (P1-6 file split).
"""

import uuid
from typing import Any

from app.forge.legacy_prompts import FORGE_QUESTIONS

# In-memory sessions (MVP — replace with Redis for production)
_sessions: dict[str, dict[str, Any]] = {}


def start_forge(user_id: str, name: str) -> dict[str, Any]:
    forge_id = str(uuid.uuid4())
    _sessions[forge_id] = {
        "forge_id": forge_id,
        "user_id": user_id,
        "status": "collecting",
        "step": 1,
        "name": name,
        "answers": {"1": name},
        "ability_md": "",
        "persona_md": "",
        "soul_md": "",
        "star_rating": 0,
        "district": "",
        "resident_id": None,
        "error": None,
    }
    return {
        "forge_id": forge_id,
        "step": 1,
        "question": FORGE_QUESTIONS[2],
    }


def submit_answer(forge_id: str, answer: str) -> dict[str, Any]:
    session = _sessions.get(forge_id)
    if not session:
        raise ValueError("Forge session not found")
    if session["status"] != "collecting":
        raise ValueError(f"Session is in '{session['status']}' state")

    current_step = session["step"] + 1
    session["answers"][str(current_step)] = answer
    session["step"] = current_step

    if current_step >= 5:
        session["status"] = "generating"
        return {
            "forge_id": forge_id,
            "step": current_step,
            "next_step": None,
            "question": None,
            "ability_md": None,
            "persona_md": None,
            "soul_md": None,
        }

    next_q = current_step + 1
    return {
        "forge_id": forge_id,
        "step": current_step,
        "next_step": next_q,
        "question": FORGE_QUESTIONS[next_q],
        "ability_md": None,
        "persona_md": None,
        "soul_md": None,
    }


def get_status(forge_id: str) -> dict[str, Any]:
    session = _sessions.get(forge_id)
    if not session:
        raise ValueError("Forge session not found")
    return {
        "forge_id": session["forge_id"],
        "status": session["status"],
        "step": session["step"],
        "name": session["name"],
        "answers": session["answers"],
        "ability_md": session["ability_md"],
        "persona_md": session["persona_md"],
        "soul_md": session["soul_md"],
        "star_rating": session["star_rating"],
        "district": session["district"],
        "resident_id": session["resident_id"],
        "error": session["error"],
    }
