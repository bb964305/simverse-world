"""computer-use real sandbox adapter (spec §5.2, P2). Import-safe; base_url
empty = unconfigured (portrait/tts convention)."""
from app.config import settings
from app.lab.sandbox.base import HttpAgentAdapter


class ComputerUseAdapter(HttpAgentAdapter):
    name = "computer_use"

    def __init__(self) -> None:
        super().__init__(
            base_url=getattr(settings, "lab_computer_use_base_url", "") or "",
            api_key=getattr(settings, "lab_computer_use_api_key", "") or "",
        )
