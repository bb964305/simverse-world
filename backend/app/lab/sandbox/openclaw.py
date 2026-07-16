"""OpenClaw real sandbox adapter (spec §5.2, P2).

Import-safe: reads its base_url/key from settings (empty string = unconfigured,
mirroring the portrait/tts grouping convention). No hard dependency on any
external client beyond the shared httpx pool, so the app boots without OpenClaw.
"""
from app.config import settings
from app.lab.sandbox.base import HttpAgentAdapter


class OpenClawAdapter(HttpAgentAdapter):
    name = "openclaw"

    def __init__(self) -> None:
        super().__init__(
            base_url=getattr(settings, "lab_openclaw_base_url", "") or "",
            api_key=getattr(settings, "lab_openclaw_api_key", "") or "",
        )
