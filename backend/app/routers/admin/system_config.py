"""Admin System Config — read/write dynamic config for all groups.

Config groups:
- llm: system_model, system_temperature, system_timeout, system_max_retries,
       user_model, user_temperature_chat, user_temperature_forge, user_timeout,
       user_max_retries, user_concurrency,
       portrait_model, portrait_base_url, portrait_timeout,
       thinking_enabled, thinking_budget, fallback_model
- economy: signup_bonus, daily_reward, chat_cost_per_turn, creator_reward, rating_bonus
- heat: popular_threshold, sleeping_days, scoring_weights
- district: map_config
- oauth: linuxdo_enabled, github_enabled
- sprite: template_list
- user_llm: allow_custom_llm
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.system_config import SystemConfig
from app.routers.admin.middleware import require_admin
from app.services.config_service import ConfigService
from app.services.url_guard import ensure_url_is_public, UnsafeURLError


async def _validate_config_value(key: str, value) -> None:
    """Reject non-public base URLs in outbound-endpoint config keys (SSRF, P0-4d)."""
    if key.endswith(".base_url") or key.endswith("_base_url"):
        if isinstance(value, str) and value:
            try:
                await ensure_url_is_public(value)
            except UnsafeURLError as e:
                raise HTTPException(status_code=400, detail=f"Invalid base URL for {key}: {e}")
from app.schemas.admin import (
    ConfigGroupResponse,
    ConfigUpdateRequest,
    ConfigBatchUpdateRequest,
    ConfigEntry,
)

router = APIRouter(prefix="/system", tags=["admin-system"])

def get_default_configs() -> dict[str, dict[str, object]]:
    """Return default values for each config group evaluated against current settings."""
    return {
        "llm": {
            "llm.model": settings.effective_model,
            "llm.base_url": settings.llm_base_url,
            "llm.api_key": settings.effective_api_key,
            "llm.temperature": 0.7,
            "llm.timeout": 120,
            "llm.max_retries": 3,
            "llm.concurrency": 5,
        },
        "heat": {
            "heat.popular_threshold": 50,
            "heat.sleeping_days": 7,
            "heat.cron_interval": 3600,
        },
        "scoring": {
            "scoring.min_content_length": 50,
            "scoring.star3_min_conversations": 50,
            "scoring.star3_min_rating": 3.5,
        },
        "searxng": {
            "searxng.url": settings.searxng_url,
            "searxng.query_delay": 1.0,
            "searxng.top_n": 5,
        },
        "portrait": {
            "portrait.model": settings.portrait_llm_model,
            "portrait.base_url": settings.portrait_llm_base_url,
            "portrait.api_key": settings.portrait_llm_api_key,
            "portrait.timeout": 180,
        },
    }


# Backwards-compatible module-level reference
DEFAULT_CONFIGS: dict[str, dict[str, object]] = get_default_configs()

VALID_GROUPS = set(DEFAULT_CONFIGS.keys()) | {"economy", "district", "oauth", "sprite", "user_llm", "portrait"}

# 密钥类 key 的读侧掩码。这些值的默认来源是 settings.effective_api_key /
# portrait_llm_api_key，会随分组读取直接落到浏览器；写侧把掩码与空串都当成
# 「不修改」，否则面板一次整组保存就会把真密钥覆盖成掩码字面量。
_SECRET_KEY_SUFFIXES = ("api_key", "secret", "token", "password")
MASKED_VALUE = "********"


def _is_secret_key(key: str) -> bool:
    return key.rsplit(".", 1)[-1].endswith(_SECRET_KEY_SUFFIXES)


def _mask(key: str, value: object) -> object:
    if _is_secret_key(key) and isinstance(value, str) and value:
        return MASKED_VALUE
    return value


async def _get_config_group(db: AsyncSession, group: str) -> dict:
    """Get all config entries for a group, merged with defaults."""
    svc = ConfigService(db)
    db_values = await svc.get_group(group)
    # Merge: start with defaults (strip group prefix for key), then overlay DB values
    defaults = get_default_configs().get(group, {})
    merged: dict[str, object] = {}
    # Add defaults using short keys (strip "group." prefix if present)
    for full_key, default_val in defaults.items():
        short_key = full_key.removeprefix(f"{group}.")
        merged[short_key] = default_val
    # DB values override defaults — strip group prefix for consistent short keys
    for db_key, db_val in db_values.items():
        short_key = db_key.removeprefix(f"{group}.")
        merged[short_key] = db_val
    return {k: _mask(k, v) for k, v in merged.items()}


async def _set_config(
    db: AsyncSession,
    key: str,
    value: object,
    group: str,
    admin_id: str,
) -> None:
    """Set a single config entry."""
    svc = ConfigService(db)
    await svc.set(key, value, group=group, updated_by=admin_id)


async def _set_config_batch(
    db: AsyncSession,
    updates: list[dict],
    admin_id: str,
) -> None:
    """Set multiple config entries at once."""
    svc = ConfigService(db)
    for entry in updates:
        await svc.set(
            entry["key"], entry["value"],
            group=entry["group"], updated_by=admin_id,
        )


async def _get_all_groups(db: AsyncSession) -> list[str]:
    """Get all distinct config groups."""
    result = await db.execute(
        select(func.distinct(SystemConfig.group)).order_by(SystemConfig.group)
    )
    return [row[0] for row in result.all()]


# ── Routes ─────────────────────────────────────────────────

@router.get("/groups")
async def list_config_groups(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all distinct config groups (includes default groups even if DB is empty)."""
    db_groups = await _get_all_groups(db)
    all_groups = sorted(set(db_groups) | set(DEFAULT_CONFIGS.keys()))
    return {"groups": all_groups}


@router.get("/groups/{group}", response_model=ConfigGroupResponse)
async def get_config_group(
    group: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get all config entries for a specific group."""
    entries = await _get_config_group(db, group)
    return ConfigGroupResponse(group=group, entries=entries)


@router.get("/entries")
async def list_all_config_entries(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all config entries across all groups (secret values masked)."""
    result = await db.execute(
        select(SystemConfig).order_by(SystemConfig.group, SystemConfig.key)
    )
    entries = result.scalars().all()
    out = []
    for e in entries:
        item = ConfigEntry.model_validate(e, from_attributes=True)
        out.append(item.model_copy(update={"value": _mask(item.key, item.value)}))
    return out


@router.put("/entry")
async def update_config_entry(
    req: ConfigUpdateRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a single config entry."""
    # Always store with group prefix so runtime svc.get("llm.model") finds it
    full_key = req.key if "." in req.key else f"{req.group}.{req.key}"
    if _is_secret_key(full_key) and req.value in (MASKED_VALUE, ""):
        # 面板整组回传时，未改动的密钥字段带回来的是掩码；空串同理表示「不修改」。
        return {"key": full_key, "value": MASKED_VALUE, "group": req.group, "unchanged": True}
    await _validate_config_value(full_key, req.value)
    await _set_config(db, key=full_key, value=req.value, group=req.group, admin_id=admin.id)
    return {"key": full_key, "value": req.value, "group": req.group}


@router.put("/batch")
async def update_config_batch(
    req: ConfigBatchUpdateRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update multiple config entries at once."""
    updates = [
        {"key": u.key if "." in u.key else f"{u.group}.{u.key}", "value": u.value, "group": u.group}
        for u in req.updates
    ]
    skipped = [u for u in updates if _is_secret_key(u["key"]) and u["value"] in (MASKED_VALUE, "")]
    updates = [u for u in updates if u not in skipped]
    for u in updates:
        await _validate_config_value(u["key"], u["value"])
    await _set_config_batch(db, updates, admin_id=admin.id)
    return {"updated": len(updates), "skipped_secrets": len(skipped)}


# ── Convenience: LLM Config ───────────────────────────────

@router.get("/llm")
async def get_llm_config(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get all LLM-related config with defaults."""
    svc = ConfigService(db)
    return {
        "system_model": await svc.get("llm.system_model", default="claude-haiku-4-5-20251001"),
        "system_temperature": await svc.get("llm.system_temperature", default=0.3),
        "system_timeout": await svc.get("llm.system_timeout", default=30),
        "system_max_retries": await svc.get("llm.system_max_retries", default=2),
        "user_temperature_chat": await svc.get("llm.user_temperature_chat", default=0.7),
        "user_temperature_forge": await svc.get("llm.user_temperature_forge", default=0.5),
        "user_timeout": await svc.get("llm.user_timeout", default=120),
        "user_max_retries": await svc.get("llm.user_max_retries", default=3),
        "user_concurrency": await svc.get("llm.user_concurrency", default=5),
        "portrait_model": await svc.get("llm.portrait_model", default="gemini-3-pro-image-preview"),
        "portrait_timeout": await svc.get("llm.portrait_timeout", default=180),
        "thinking_enabled": await svc.get("llm.thinking_enabled", default=False),
        "thinking_budget": await svc.get("llm.thinking_budget", default=10000),
        "fallback_model": await svc.get("llm.fallback_model", default=""),
    }


@router.get("/heat")
async def get_heat_config(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get heat/scoring config with defaults."""
    svc = ConfigService(db)
    return {
        "popular_threshold": await svc.get("heat.popular_threshold", default=50),
        "sleeping_days": await svc.get("heat.sleeping_days", default=7),
        "scoring_weights": await svc.get("heat.scoring_weights", default={
            "persona_length": 0.3,
            "conversations": 0.3,
            "avg_rating": 0.4,
        }),
    }


@router.get("/user-llm-policy")
async def get_user_llm_policy(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get user custom LLM policy."""
    svc = ConfigService(db)
    return {
        "allow_custom_llm": await svc.get("user_llm.allow_custom_llm", default=False),
    }
