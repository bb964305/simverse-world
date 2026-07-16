"""Hermes real sandbox adapter (spec §5.2, P2). Import-safe; base_url empty =
unconfigured (portrait/tts convention)."""
from app.config import settings
from app.lab.sandbox.base import HttpAgentAdapter


class HermesAdapter(HttpAgentAdapter):
    name = "hermes"

    def __init__(self) -> None:
        super().__init__(
            base_url=getattr(settings, "lab_hermes_base_url", "") or "",
            api_key=getattr(settings, "lab_hermes_api_key", "") or "",
        )
