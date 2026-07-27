"""admin 鉴权 sweep：每个 /admin 端点都必须拒绝 无 token / 普通用户 / 被封管理员。

发现方式刻意不用 app.openapi()——resident_sprites 的 10 个路由声明了
include_in_schema=settings.resident_sprite_enabled（默认 False），OpenAPI 里看不到，
只能数到 58/68；也不用 app.routes——FastAPI 0.139 的 include_router 留下的是
_IncludedRouter 懒包装而非扁平 APIRoute，遍历得到 0 个。直接走 admin 包的子模块
router 是唯一能拿全 68 个的方式，而且新增子模块会被自动纳入 sweep。

前提（已实测）：admin 的鉴权依赖在 request body 校验之前触发，所以这里可以不带
body 直接发写请求，仍然拿到 401/403 而不是 422。
"""
import importlib
import pkgutil
import re

import pytest
from fastapi.routing import APIRoute

from app.models.user import User
from app.services.auth_service import create_token

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _discover_admin_endpoints() -> list[tuple[str, str]]:
    """(METHOD, full path) for every route declared in app/routers/admin/*.py."""
    import app.routers.admin as pkg

    found: list[tuple[str, str]] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"app.routers.admin.{info.name}")
        router = getattr(module, "router", None)
        if router is None:  # middleware.py has no router
            continue
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods):
                if method in _HTTP_METHODS:
                    found.append((method, "/admin" + route.path))
    return sorted(set(found))


ADMIN_ENDPOINTS = _discover_admin_endpoints()


def _probe(path: str) -> str:
    """Fill path params with a value guaranteed not to match a real row."""
    return _PATH_PARAM.sub("sweep-probe", path)


@pytest.fixture(autouse=True)
def _enable_flagged_admin_routers(monkeypatch):
    """resident_sprites 的 router 挂了 router 级 Depends(require_resident_sprite_enabled)，
    它在每个 handler 的 Depends(require_admin) 之前跑，开关关着时直接抛 404 —— 那 10 个
    端点的鉴权就永远测不到，sweep 会以「404 != 401」的形式假失败，或者被放宽断言后假绿。

    该开关是请求期读 settings 的（include_in_schema 才是导入期），所以翻开它就能让全部
    68 个端点真正走到 require_admin。monkeypatch 负责还原，不污染其他测试。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "resident_sprite_enabled", True)


def test_discovery_is_not_vacuous():
    """A broken discovery helper must fail loudly, not make the sweep vacuously green."""
    assert len(ADMIN_ENDPOINTS) >= 60, (
        f"admin route discovery returned only {len(ADMIN_ENDPOINTS)} endpoints — "
        "the sweep below would pass without testing anything"
    )
    modules = {p.split("/")[2] for _, p in ADMIN_ENDPOINTS}
    assert len(modules) >= 12, f"only {len(modules)} admin modules discovered: {sorted(modules)}"


@pytest.mark.anyio
async def test_every_admin_endpoint_rejects_anonymous(client):
    failures = []
    for method, path in ADMIN_ENDPOINTS:
        resp = await client.request(method, _probe(path))
        if resp.status_code != 401:
            failures.append(f"{method} {path} -> {resp.status_code}")
    assert not failures, "reachable without a token:\n" + "\n".join(failures)


@pytest.mark.anyio
async def test_every_admin_endpoint_rejects_non_admin(client, db_session):
    user = User(name="pleb", email="pleb-sweep@test.com", is_admin=False, is_banned=False)
    db_session.add(user)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}

    failures = []
    for method, path in ADMIN_ENDPOINTS:
        resp = await client.request(method, _probe(path), headers=headers)
        if resp.status_code != 403:
            failures.append(f"{method} {path} -> {resp.status_code}")
    assert not failures, "reachable by a non-admin user:\n" + "\n".join(failures)


@pytest.mark.anyio
async def test_every_admin_endpoint_rejects_banned_admin(client, db_session):
    admin = User(name="banned", email="banned-sweep@test.com", is_admin=True, is_banned=True)
    db_session.add(admin)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(admin.id)}"}

    failures = []
    for method, path in ADMIN_ENDPOINTS:
        resp = await client.request(method, _probe(path), headers=headers)
        if resp.status_code != 403:
            failures.append(f"{method} {path} -> {resp.status_code}")
    assert not failures, "reachable by a banned admin:\n" + "\n".join(failures)


@pytest.mark.anyio
async def test_bulletin_admin_post_is_guarded(client, db_session):
    """POST /bulletin/posts 在 handler 内手工 await require_admin，既不在 /admin
    前缀下也不在 admin tag 里 —— 上面的 sweep 天然漏掉它，所以单独钉一条。
    注意它的 require_admin 在 body 解析之后才跑，所以必须带合法 body。"""
    user = User(name="pleb2", email="pleb2-sweep@test.com", is_admin=False, is_banned=False)
    db_session.add(user)
    await db_session.commit()
    body = {"title": "sweep", "content_md": "", "kind": "notice"}

    assert (await client.post("/bulletin/posts", json=body)).status_code == 401
    resp = await client.post(
        "/bulletin/posts", json=body,
        headers={"Authorization": f"Bearer {create_token(user.id)}"},
    )
    assert resp.status_code == 403
