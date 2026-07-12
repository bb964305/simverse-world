"""`.env.example` <-> `app.config.Settings` consistency gate (PLAN_P3 批次 3).

Two invariants:
1. Every key in .env.example maps to a real Settings field — a renamed or
   removed setting must not leave a stale example line behind (ops copy this
   file; a dead key silently does nothing).
2. Every Settings field is either documented in .env.example or explicitly
   listed in UNDOCUMENTED_OK — new config should ship with an example line,
   or a conscious decision not to.

Pure static check: no app boot, no DB.
"""
import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

# Settings fields intentionally NOT in .env.example (internal knobs with safe
# defaults / derived values). Add here only with a reason.
UNDOCUMENTED_OK: dict[str, str] = {}


def _example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            keys.add(m.group(1).lower())
    return keys


def test_every_example_key_is_a_settings_field():
    fields = set(Settings.model_fields)
    stale = sorted(_example_keys() - fields)
    assert not stale, f".env.example has keys with no Settings field: {stale}"


def test_every_settings_field_is_documented_or_allowlisted():
    documented = _example_keys() | set(UNDOCUMENTED_OK)
    missing = sorted(set(Settings.model_fields) - documented)
    assert not missing, (
        "Settings fields missing from .env.example (document them or add to "
        f"UNDOCUMENTED_OK with a reason): {missing}"
    )
