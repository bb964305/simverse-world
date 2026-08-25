"""配置读写路径不得把密钥明文送到浏览器。"""
import pytest

from app.models.user import User
from app.services.auth_service import create_token


async def _admin_headers(db):
    admin = User(name="adm", email="adm-secrets@test.com", is_admin=True, is_banned=False)
    db.add(admin)
    await db.commit()
    return {"Authorization": f"Bearer {create_token(admin.id)}"}


@pytest.mark.anyio
async def test_group_read_masks_api_key(client, db_session, monkeypatch):
    """llm 分组的默认值直接来自 settings.effective_api_key —— 不能原样下发。"""
    from app.config import settings
    from app.routers.admin.system_config import MASKED_VALUE

    monkeypatch.setattr(settings, "llm_api_key", "sk-test-secret")
    headers = await _admin_headers(db_session)
    resp = await client.get("/admin/system/groups/llm", headers=headers)
    assert resp.status_code == 200

    entries = resp.json()["entries"]
    assert entries["api_key"] == MASKED_VALUE
    assert settings.effective_api_key not in resp.text
    # 非密钥字段照常明文返回
    assert entries["model"] == settings.effective_model


@pytest.mark.anyio
async def test_entries_listing_masks_stored_secrets(client, db_session):
    """/entries 裸返 SystemConfig 行，是第二条泄漏出口。"""
    from app.routers.admin.system_config import MASKED_VALUE, _set_config

    headers = await _admin_headers(db_session)
    await _set_config(db_session, key="llm.api_key", value="sk-super-secret",
                      group="llm", admin_id="x")

    resp = await client.get("/admin/system/entries", headers=headers)
    assert resp.status_code == 200
    assert "sk-super-secret" not in resp.text
    entry = next(e for e in resp.json() if e["key"] == "llm.api_key")
    assert entry["value"] == MASKED_VALUE


@pytest.mark.anyio
async def test_writing_blank_or_mask_keeps_the_stored_secret(client, db_session):
    """面板每次保存都会把整组字段发回来；掩码/空串必须表示「不修改」，
    否则点一次保存就把真密钥覆盖成 '********'。"""
    from app.routers.admin.system_config import MASKED_VALUE, _set_config
    from app.services.config_service import ConfigService

    headers = await _admin_headers(db_session)
    await _set_config(db_session, key="llm.api_key", value="sk-original",
                      group="llm", admin_id="x")

    for ignored in (MASKED_VALUE, ""):
        resp = await client.put(
            "/admin/system/entry", headers=headers,
            json={"key": "api_key", "value": ignored, "group": "llm"},
        )
        assert resp.status_code == 200
        stored = await ConfigService(db_session).get("llm.api_key")
        assert stored == "sk-original", f"writing {ignored!r} clobbered the secret"


@pytest.mark.anyio
async def test_writing_a_real_value_still_updates(client, db_session):
    from app.services.config_service import ConfigService

    headers = await _admin_headers(db_session)
    resp = await client.put(
        "/admin/system/entry", headers=headers,
        json={"key": "api_key", "value": "sk-rotated", "group": "llm"},
    )
    assert resp.status_code == 200
    assert await ConfigService(db_session).get("llm.api_key") == "sk-rotated"


@pytest.mark.anyio
async def test_batch_update_skips_masked_secret_but_writes_other_fields(client, db_session):
    """/admin/system/batch 的过滤逻辑此前完全没有路由级覆盖：masked 密钥跳过，
    同批次的非密钥字段照常写入，skipped_secrets 计数要对。"""
    from app.routers.admin.system_config import MASKED_VALUE, _set_config
    from app.services.config_service import ConfigService

    headers = await _admin_headers(db_session)
    await _set_config(db_session, key="llm.api_key", value="sk-original",
                      group="llm", admin_id="x")

    resp = await client.put(
        "/admin/system/batch", headers=headers,
        json={"updates": [
            {"key": "api_key", "value": MASKED_VALUE, "group": "llm"},
            {"key": "model", "value": "new-model", "group": "llm"},
        ]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped_secrets"] == 1
    assert body["updated"] == 1

    svc = ConfigService(db_session)
    assert await svc.get("llm.api_key") == "sk-original"
    assert await svc.get("llm.model") == "new-model"


@pytest.mark.anyio
async def test_batch_update_skips_blank_secret(client, db_session):
    from app.routers.admin.system_config import _set_config
    from app.services.config_service import ConfigService

    headers = await _admin_headers(db_session)
    await _set_config(db_session, key="llm.api_key", value="sk-original",
                      group="llm", admin_id="x")

    resp = await client.put(
        "/admin/system/batch", headers=headers,
        json={"updates": [{"key": "api_key", "value": "", "group": "llm"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped_secrets"] == 1
    assert body["updated"] == 0
    assert await ConfigService(db_session).get("llm.api_key") == "sk-original"


@pytest.mark.anyio
async def test_batch_update_real_secret_value_still_writes(client, db_session):
    """批量接口里带一个真正的新密钥值，必须照常轮换（不是所有密钥字段都被拦下）。"""
    from app.services.config_service import ConfigService

    headers = await _admin_headers(db_session)
    resp = await client.put(
        "/admin/system/batch", headers=headers,
        json={"updates": [{"key": "api_key", "value": "sk-rotated", "group": "llm"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped_secrets"] == 0
    assert body["updated"] == 1
    assert await ConfigService(db_session).get("llm.api_key") == "sk-rotated"


def test_mask_leaves_non_string_config_values_untouched():
    """DEFAULT_CONFIGS 里混着 int/float/dict；_mask 只应该动字符串密钥字段。"""
    from app.routers.admin.system_config import _mask

    assert _mask("llm.max_retries", 3) == 3
    assert _mask("llm.temperature", 0.7) == 0.7
    assert _mask("heat.scoring_weights", {"avg_rating": 0.4}) == {"avg_rating": 0.4}
    # 非密钥字符串字段本来就不该被掩码
    assert _mask("llm.model", "claude-sonnet-4-20250514") == "claude-sonnet-4-20250514"
