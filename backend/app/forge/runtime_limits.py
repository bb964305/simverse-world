"""Shared terminality limits for canonical and legacy Forge runners."""

from datetime import timedelta


FORGE_PIPELINE_TIMEOUT_S = 15 * 60
FORGE_GENERATION_STALE_AFTER = timedelta(minutes=20)
