"""Config-backed Lab visitor status and closed-beta publishing admission."""
from __future__ import annotations

from app.config import settings


def _adapter_endpoint() -> str:
    return {
        "codex": settings.lab_codex_base_url,
        "simverse_ref": settings.lab_simverse_ref_base_url,
        "openclaw": settings.lab_openclaw_base_url,
        "hermes": settings.lab_hermes_base_url,
        "computer_use": settings.lab_computer_use_base_url,
        "mock": "embedded",
    }.get(settings.lab_adapter, "")


async def snapshot(*, user_id: str | None, is_admin: bool = False) -> dict:
    from app.lab import is_lab_runtime_enabled

    runtime_enabled = await is_lab_runtime_enabled()
    allowlist = {value.strip() for value in settings.lab_beta_user_ids if value.strip()}
    beta_admitted = is_admin or not allowlist or (user_id in allowlist)
    endpoint = _adapter_endpoint()
    adapter_configured = bool(endpoint)
    protocol_compatible = not (
        settings.lab_adapter == "codex" and settings.lab_terminalizer_v2_enabled
    )
    publish_allowed = bool(
        settings.lab_enabled
        and runtime_enabled
        and beta_admitted
        and adapter_configured
        and protocol_compatible
    )
    blockers: list[str] = []
    if not settings.lab_enabled:
        blockers.append("deploy_disabled")
    if not runtime_enabled:
        blockers.append("runtime_paused")
    if not beta_admitted:
        blockers.append("beta_access_required")
    if not adapter_configured:
        blockers.append("adapter_unconfigured")
    if not protocol_compatible:
        blockers.append("codex_v2_cost_terminalization_incomplete")

    return {
        "visitor_open": True,
        "deploy_enabled": bool(settings.lab_enabled),
        "runtime_enabled": bool(runtime_enabled),
        "publish_allowed": publish_allowed,
        "beta_mode": bool(allowlist),
        "beta_admitted": beta_admitted,
        "adapter": settings.lab_adapter,
        "available_scopes": (
            ["code"] if settings.lab_adapter == "codex"
            else ["web_search", "browse", "code", "http"]
        ),
        "blockers": blockers,
        "checks": [
            {"key": "deploy", "label": "部署开关", "ok": bool(settings.lab_enabled)},
            {"key": "runtime", "label": "运行时开关", "ok": bool(runtime_enabled)},
            {"key": "adapter", "label": "执行适配器", "ok": adapter_configured},
            {"key": "protocol", "label": "成本结算协议", "ok": protocol_compatible},
            {"key": "beta", "label": "封闭测试准入", "ok": beta_admitted},
            {
                "key": "artifact_pipeline",
                "label": "产物扫描链",
                "ok": bool(settings.lab_artifact_pipeline_enabled),
                "optional": settings.lab_adapter == "mock",
            },
            {
                "key": "concurrency",
                "label": "单研究员并发限制",
                "ok": int(settings.lab_max_concurrent_per_researcher) == 1,
            },
        ],
    }
