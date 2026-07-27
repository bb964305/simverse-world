# 管理系统「立刻做」批次 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉 2026-07-27 管理系统分析报告里「立刻做」一档的 8 项——生产暴露面收口、两条必然失败/持续污染数据的写路径、一个会崩渲染的前后端契约漂移、一个永久失真的监控口径、密钥明文下发，并补上把 admin 鉴权覆盖从 8/68 提到 68/68 的 sweep 测试与首个管理员的提权脚本。

**Architecture:** 八个彼此独立的小修复，**不含任何 alembic 迁移**，不含任何行为开闸。共享的新增物只有一个：`backend/app/services/system_users.py`，把两个非人类 `creator_id` 哨兵（seed 系统账号 / admin 控制台账号）收敛成单一真源，Task 5 建立、Task 6 复用。Task 1 的 sweep 测试先落地，为后面所有改动提供鉴权回归护栏。前端只动 forge 监控一条线（类型 + 组件 + 新测试）与系统配置面板的密钥输入框。

**Tech Stack:** Python 3.12 / FastAPI 0.139 / SQLAlchemy 2 async / pytest + anyio / React 19 + TypeScript / Vitest 4 + @testing-library/react / Docker Compose。

## Global Constraints

- 仓库是 **PUBLIC**。进仓库的文档、脚本、commit message **禁止出现生产裸 IP / 域名 / 凭据**，一律用 `vm212` 别名。
- 代码改动**只在本 worktree 内做**：`/Volumes/data/dev/simverse-world/.claude/worktrees/optimistic-chebyshev-eb79f3`。不碰主工作区。
- **后端解释器固定用主仓 venv**（本 worktree 内没有 `.venv`；已实测该 venv 的 `app` 包解析到本 worktree，不会串到主仓）：
  ```
  /Volumes/data/dev/simverse-world/backend/.venv/bin/python
  ```
  下文统一记作 `$PY`。所有 pytest 命令都在 `backend/` 目录下执行。
- 前端命令在 `frontend/` 目录下执行：`npm run test`（vitest run）、`npm run lint`、`npx tsc --noEmit`。
- **硬门 = 相对基线零新增失败**，不是 literal `0 failed`。基线见下方「执行前基线」。判定用失败集**双向差集**，不是数量比较。
- 严格 TDD：红 → 绿 → 提交。**一 step 一 commit**，commit message 末尾带**真实** `Verified-by:` 输出（粘贴真实命令输出的最后一行，不要编造）。
- 禁 `--no-verify` / `git commit --amend` / `squash` / 编造测试数据。卡住就 commit WIP 停下报告，不跨 task 抢做、不扩 scope。
- **本批次不包含任何 alembic 迁移，也不包含任何开关翻转**（「迁移/清库 与 开闸/行为变更 不得同一次变更」——07-25 事故窗口）。任何一步如果发现非加迁移不可，**停下报告**，不要即兴加。
- **不部署、不 push、不合并 master、不碰 vm212 的数据**。Task 6 的对账脚本是**纯只读**的，且本批次不在生产上运行它——跑不跑、什么时候跑，由 Jimmy 另行决定。
- 报告里列为「这周做 / 有空做」的条目**一律不做**，即使代码就在眼前。已知诱惑：`ForgeMonitorPanel.tsx` 的 `color: 'white'` 白字白底（P1，属「这周做」）就在 Task 4 要改的文件里——**不要顺手改**，那是独立一档的工作。

### 执行前基线（写 plan 时已实测，2026-07-27）

```
51 failed, 2207 passed, 25 skipped, 11 deselected, 17 errors in 479.04s
```

失败节点共 **68** 个，已存盘到 `/tmp/baseline_failed.txt`（来自 `.pytest_cache/v/cache/lastfailed`）。
**全部 68 个都是 lab 线的**（`test_lab_*` 与 `tests/integration/test_lab_*_postgres.py`，需要 redis / testcontainers），**没有一个落在 admin / economy / forge / config 区域**——也就是说本批次要碰的所有代码路径，基线都是干净的绿。

判定方式（每个 task 结束、以及收尾时都做）：

```bash
cd backend && $PY -m pytest tests/ -q 2>&1 | tail -5
$PY -c "import json,pathlib; print('\n'.join(sorted(json.loads(pathlib.Path('.pytest_cache/v/cache/lastfailed').read_text()))))" > /tmp/after_failed.txt
comm -13 /tmp/baseline_failed.txt /tmp/after_failed.txt   # 新增失败 —— 必须为空
comm -23 /tmp/baseline_failed.txt /tmp/after_failed.txt   # 被修好的（信息用）
```

硬门：**第一条 comm 必须无输出**。数量比较不算数，只认双向差集。

单个 task 期间不必跑全量（8 分钟）；跑该 task 涉及的测试文件即可，全量留到收尾。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/tests/test_admin_authz_sweep.py` | Create | Task 1。遍历 admin 包全部子路由，断言 68 个端点对 无 token / 普通用户 / 被封管理员 分别返回 401/403/403；外加 `POST /bulletin/posts` 这个包外 admin 写操作的单独钉子 |
| `deploy/backend/docker-compose.yml` | Modify（`:60` 一行） | Task 2。api 端口映射从 `0.0.0.0:8100` 改绑 `127.0.0.1:8100` |
| `deploy/backend/.env.example` | Modify（追加一段） | Task 2。补 `METRICS_ENABLED` / `METRICS_TOKEN` 说明 |
| `backend/tests/test_deploy_exposure.py` | Create | Task 2 的回归锁：解析 compose YAML 断言无对外端口绑定；断言 `.env.example` 覆盖 metrics 两项 |
| `backend/app/forge/pipeline.py` | Modify（顶部加常量） | Task 3。`TERMINAL_STATUSES` 单一真源，就放在真正写 status 的地方 |
| `backend/app/routers/forge.py` | Modify（`:165`） | Task 3。`_TERMINAL_STATUSES` 改为从 pipeline 导入的别名，消除第二份定义 |
| `backend/app/routers/admin/forge_monitor.py` | Modify（`:92-96`） | Task 3。`/active` 改用 `TERMINAL_STATUSES` 并加 `.limit(200)` |
| `backend/tests/test_admin_forge_monitor.py` | Modify（追加 2 个测试） | Task 3 的红相与回归 |
| `frontend/src/services/api/adminWorld.ts` | Modify（forge 三个 interface + 两个函数） | Task 4。前端类型改吃后端真实字段；history 分页参数 `page/per_page` → `offset/limit` |
| `frontend/src/components/admin/ForgeMonitorPanel.tsx` | Modify | Task 4。活跃卡片与历史表改用真实字段；耗时前端算；删掉后端不存在的「居民 ID」列 |
| `frontend/src/components/admin/ForgeMonitorPanel.test.tsx` | Create | Task 4。用后端真实响应形状渲染，锁住契约 |
| `backend/app/services/system_users.py` | Create | Task 5。`SYSTEM_CREATOR_ID` / `ADMIN_CREATOR_ID` / `NON_USER_CREATOR_IDS` / `ensure_admin_creator_user()` 单一真源 |
| `backend/tests/test_system_users.py` | Create | Task 5。常量漂移守卫（与 `seed.preset_characters.SYSTEM_USER_ID` 对齐）+ `ensure_admin_creator_user` 幂等性 |
| `backend/app/routers/admin/residents.py` | Modify（`:275-283`） | Task 5。`create_preset` 先 ensure 哨兵用户行再插入，`creator_id` 改用 `ADMIN_CREATOR_ID` 常量 |
| `backend/seed/reset_builtin_residents.py` | Modify（`:179` 附近） | Task 5。seed 路径同样 ensure 哨兵用户 |
| `backend/tests/test_admin_residents.py` | Modify（`:81-85`） | Task 5。修正把 bug 焊死的断言 |
| `backend/app/services/coin_service.py` | Modify（`:519`） | Task 6。被动铸币哨兵改用 `NON_USER_CREATOR_IDS`，并显式处理 `creator_id is None` |
| `backend/tests/test_realism_coin_atomic.py` | Modify（追加断言） | Task 6 的红相与回归 |
| `backend/scripts/audit_system_minting.py` | Create | Task 6。**纯只读**对账脚本：清点两个哨兵账号被误铸的 Soul Coin |
| `backend/tests/test_audit_system_minting.py` | Create | Task 6。对账聚合逻辑的单测 |
| `backend/app/routers/admin/system_config.py` | Modify | Task 7。读侧掩码 secret 类 key（`/groups/{group}` 与 `/entries` 两条出口都要）；写侧留空 = 不修改 |
| `backend/tests/test_admin_config_secrets.py` | Create | Task 7。掩码与「留空不覆盖」的回归 |
| `frontend/src/components/admin/SystemConfigPanel.tsx` | Modify | Task 7。删掉「显示」按钮，提示文案改成与实际行为一致 |
| `backend/scripts/grant_admin.py` | Create | Task 8。按 email 幂等提权/降权的 CLI，`--dry-run` 默认 |
| `backend/tests/test_grant_admin.py` | Create | Task 8。提权/降权/幂等/「不得清零管理员」的单测 |
| `docs/ADMIN_BOOTSTRAP.md` | Create | Task 8。新环境首个管理员怎么来 + 哨兵账号说明 |
| `README.md` | Modify（一行指针） | Task 8。指向上面这份文档 |

---

## Task 1: admin 鉴权 sweep 测试

先立护栏再改代码：后面 7 个 task 都会碰 admin 路由，这条测试保证任何一次改动都不会悄悄拿掉某个端点的 `require_admin`。

**Files:**
- Create: `backend/tests/test_admin_authz_sweep.py`

**Interfaces:**
- Consumes: `app.routers.admin`（包）、`app.models.user.User`、`app.services.auth_service.create_token`、conftest 的 `client` / `db_session` fixture
- Produces: 模块级常量 `ADMIN_ENDPOINTS: list[tuple[str, str]]`（本 task 之外无人依赖）

**为什么不用 `app.openapi()` 也不用 `app.routes`（这是本 task 唯一的技术难点，已实测）：**
- `app.openapi()` 只给 **58** 个：`resident_sprites` 的 10 个路由声明了 `include_in_schema=settings.resident_sprite_enabled`，默认 `False` 时不进 schema。
- `app.routes` 给 **0** 个：FastAPI 0.139 的 `include_router` 留下的是 `_IncludedRouter` 懒包装（`Counter({'_IncludedRouter': 36, ...})`），不是扁平的 `APIRoute`，且它是私有内部结构。
- 遍历 admin 包的**子模块 `router`** 给 **68** 个（含 10 个 sprite），且新增子模块自动纳入。这是本 task 采用的方式。

**已实测的前提 ①：** admin 端点的鉴权依赖在 body 校验**之前**触发，所以 sweep 可以不带 body 直接发请求。实测 `POST /admin/items`、`PUT /admin/system/entry`、`PATCH /admin/users/x`、`POST /admin/events` 在无 header 时全部返回 `401` 而非 `422`。

**已实测的前提 ②（不做这一步 sweep 会假绿）：** `resident_sprites` 的 router 挂了 **router 级** `dependencies=[Depends(require_resident_sprite_enabled)]`（`backend/app/routers/admin/resident_sprites.py:33-38`），它在每个 handler 自己的 `Depends(require_admin)` **之前**跑，开关关着时直接抛 404。实测默认配置下这 10 个端点匿名请求返回的是 **404 而不是 401**，鉴权根本没被触达。

好在这个开关是**请求期**读 `settings` 的（不像 `include_in_schema` 是导入期求值），所以测试里 `monkeypatch.setattr(settings, "resident_sprite_enabled", True)` 能让它们真正走到 `require_admin`。**写 plan 时已经把三档全跑过一遍验证**：

```
flag ON  | discovered 68 | NOT 401: 0        # 匿名
non-admin      | NOT 403: 0
banned-admin   | NOT 403: 0
```

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_admin_authz_sweep.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认它真的在测东西**

```bash
cd backend && $PY -m pytest tests/test_admin_authz_sweep.py -v
```

期望：**5 个测试全部 PASS**（当前 68/68 确实都挂了 `require_admin`，所以这条护栏一上来就是绿的）。

这是**明知故犯的「先实现后补测试」**——被测行为已经正确，这个 task 的交付是**回归锁**而不是修 bug。所以必须**验证锁真的会响**，否则等于没写。做法：临时把 `backend/app/routers/admin/offices.py` 里 `admin: User = Depends(require_admin),` 这一行注释掉，重跑：

```bash
cd backend && $PY -m pytest tests/test_admin_authz_sweep.py -v 2>&1 | tail -20
```

期望：三条 sweep 测试 FAIL，失败信息里能看到 `GET /admin/offices -> 200`。

**然后把注释改回去**（`git diff` 确认 `offices.py` 干净），再重跑一次确认回到全绿。**不要把临时改动提交进去。**

- [ ] **Step 3: 提交**

```bash
cd backend && git add tests/test_admin_authz_sweep.py
git status --short   # 确认只有这一个新文件，offices.py 不在列表里
git commit -m "test(admin): sweep every admin endpoint for 401/403 authz

Covers all 68 endpoints (not the 58 that app.openapi() exposes — the 10
resident_sprites routes are include_in_schema=False by default) plus the
out-of-package POST /bulletin/posts. Raises authz assertion coverage from
8/68 to 68/68.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

---

## Task 2: 生产暴露面收口

**Files:**
- Modify: `deploy/backend/docker-compose.yml:60`
- Modify: `deploy/backend/.env.example`（追加）
- Create: `backend/tests/test_deploy_exposure.py`

**Interfaces:**
- Consumes: `yaml`（已在 venv 中，6.0.3）
- Produces: 无（纯回归锁 + 配置）

**背景：** `db` 与 `redis` 都刻意绑 `127.0.0.1`，只有 `api` 绑 `0.0.0.0:8100`，deploy 下没有任何 nginx/caddy/traefik。任何人直连 `:8100` 即可到达全部 68 个 admin 端点与 `/metrics`，完全绕过 Cloudflare。连锁反应：`backend/app/rate_limit.py` 的注释写明「唯一入口是 CF tunnel 所以信任 `CF-Connecting-IP`」，从 8100 直连时该头攻击者完全可控，限流键可逐请求伪造。

**注意：** 这一步改的是**部署配置文件**，不触发任何部署。改完后 vm212 上的运行实例不受影响，直到 Jimmy 另行决定重新部署。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_deploy_exposure.py`：

```python
"""生产 compose 的暴露面回归锁。

db / redis 一直刻意绑 127.0.0.1，api 却绑过 0.0.0.0:8100 —— 那等于把 68 个
admin 端点和 /metrics 直接挂到公网，同时让 rate_limit.py 信任的
CF-Connecting-IP 变成攻击者可控的头。这条测试把「不得有对外端口绑定」钉死。
"""
from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "backend" / "docker-compose.yml"
_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / "backend" / ".env.example"


def _port_bindings() -> list[tuple[str, str]]:
    """[(service, published-port-spec)] for every published port in the prod compose."""
    spec = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    out = []
    for name, svc in (spec.get("services") or {}).items():
        for entry in (svc or {}).get("ports") or []:
            out.append((name, str(entry)))
    return out


def test_no_service_publishes_to_all_interfaces():
    """Every published port must be bound to loopback; cloudflared reaches it there."""
    offenders = [
        f"{svc}: {binding}"
        for svc, binding in _port_bindings()
        if not str(binding).startswith("127.0.0.1:")
    ]
    assert not offenders, (
        "prod compose publishes ports outside loopback:\n" + "\n".join(offenders)
    )


def test_env_example_documents_metrics_guard():
    """/metrics is open when METRICS_TOKEN is empty — the deploy template must say so."""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "METRICS_TOKEN" in text, ".env.example must document METRICS_TOKEN"
    assert "METRICS_ENABLED" in text, ".env.example must document METRICS_ENABLED"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && $PY -m pytest tests/test_deploy_exposure.py -v
```

期望：**两条都 FAIL**。第一条报 `api: 0.0.0.0:8100:8000`，第二条报 `METRICS_TOKEN` 缺失。

- [ ] **Step 3: 改 compose 端口绑定**

`deploy/backend/docker-compose.yml` 第 60 行：

```yaml
    ports:
      - "0.0.0.0:8100:8000"
```

改成：

```yaml
    ports:
      # loopback only — cloudflared runs on the host and reaches the API here.
      # Publishing on 0.0.0.0 exposed all 68 /admin endpoints and /metrics
      # directly, and made rate_limit.py's trusted CF-Connecting-IP header
      # attacker-controlled for anyone hitting the port directly.
      - "127.0.0.1:8100:8000"
```

- [ ] **Step 4: 补 .env.example 的 metrics 两项**

在 `deploy/backend/.env.example` 末尾追加：

```bash
# Observability. /metrics is served by the API process itself; with an empty
# METRICS_TOKEN it is unauthenticated and leaks every /admin/* route name and
# its call volume. The API now binds loopback only, so /metrics is not directly
# reachable from outside — set a token anyway before exposing it through the
# tunnel for a scraper.
METRICS_ENABLED=true
METRICS_TOKEN=
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend && $PY -m pytest tests/test_deploy_exposure.py -v
```

期望：**两条都 PASS**。

- [ ] **Step 6: 提交**

```bash
git add deploy/backend/docker-compose.yml deploy/backend/.env.example backend/tests/test_deploy_exposure.py
git commit -m "fix(deploy): bind prod API to loopback instead of 0.0.0.0

The API published 0.0.0.0:8100 while db/redis were deliberately on
127.0.0.1, so all 68 admin endpoints and /metrics were reachable without
going through the tunnel — which also made rate_limit.py's trusted
CF-Connecting-IP header attacker-controlled. Adds a regression lock over
the compose file and documents METRICS_TOKEN in the deploy template.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

---

## Task 3: forge/active 终态口径 + 结果上限

**Files:**
- Modify: `backend/app/forge/pipeline.py`（顶部加常量）
- Modify: `backend/app/routers/forge.py:165`
- Modify: `backend/app/routers/admin/forge_monitor.py:91-98`
- Modify: `backend/tests/test_admin_forge_monitor.py`（追加 2 个测试）

**Interfaces:**
- Produces: `app.forge.pipeline.TERMINAL_STATUSES: frozenset[str]` —— Task 3 之后 `app.routers.forge._TERMINAL_STATUSES` 是它的别名

**背景（已实测）：** `/admin/forge/active` 用 `status.notin_(["completed", "error"])` 过滤，但流水线真实终态是 `"done"`（`backend/app/forge/pipeline.py:92` 写 `session.status = "done"`），全仓没有任何地方写 `"completed"`。于是**每一个成功完成的会话都永久留在「活跃会话」列表里**，且该端点无 limit。`backend/app/routers/forge.py:165` 已经有一份正确的 `_TERMINAL_STATUSES = {"done", "error"}`，本 task 把它提升为单一真源。

已验证 `app.forge.pipeline` → `app.routers.forge` → `app.routers.admin.forge_monitor` 三者依次导入无循环。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_admin_forge_monitor.py` 末尾追加：

```python
@pytest.mark.anyio
async def test_active_excludes_done_sessions(client, db_session):
    """流水线的成功终态是 "done"（pipeline.py 写的），不是 "completed"。
    过滤词写错会让每一个成功会话永久留在「活跃会话」里。"""
    from app.models.forge_session import ForgeSession
    from app.models.user import User
    from app.services.auth_service import create_token

    owner = User(name="owner", email="owner-forge-active@test.com")
    admin = User(name="adm", email="adm-forge-active@test.com", is_admin=True, is_banned=False)
    db_session.add_all([owner, admin])
    await db_session.commit()

    db_session.add_all([
        ForgeSession(user_id=owner.id, character_name="已完成", mode="quick",
                     status="done", current_stage="done"),
        ForgeSession(user_id=owner.id, character_name="出错了", mode="quick",
                     status="error", current_stage="error"),
        ForgeSession(user_id=owner.id, character_name="进行中", mode="deep",
                     status="building", current_stage="building"),
    ])
    await db_session.commit()

    resp = await client.get(
        "/admin/forge/active",
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200
    names = [s["character_name"] for s in resp.json()]
    assert names == ["进行中"], f"expected only the in-flight session, got {names}"


def test_terminal_statuses_has_a_single_source():
    """admin 监控与 forge 路由必须共用同一份终态集合，否则会再次漂移。"""
    from app.forge.pipeline import TERMINAL_STATUSES
    from app.routers.forge import _TERMINAL_STATUSES

    assert TERMINAL_STATUSES == frozenset({"done", "error"})
    assert _TERMINAL_STATUSES is TERMINAL_STATUSES
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && $PY -m pytest tests/test_admin_forge_monitor.py -v -k "active_excludes or single_source"
```

期望：
- `test_active_excludes_done_sessions` FAIL —— 断言错误显示 `['已完成', '进行中']`（"done" 没被过滤掉）
- `test_terminal_statuses_has_a_single_source` FAIL —— `ImportError: cannot import name 'TERMINAL_STATUSES' from 'app.forge.pipeline'`

- [ ] **Step 3: 在 pipeline 里建立单一真源**

`backend/app/forge/pipeline.py`，在 import 段之后、第一个函数之前加：

```python
# 流水线的两个终态。写在这里是因为本模块就是唯一给 session.status 赋终态的地方
# （"done" / "error"）；admin 监控曾经自己抄了一份写成 "completed"，全仓没有任何
# 地方产生该值，导致每个成功会话永久停留在「活跃会话」列表里。
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "error"})
```

- [ ] **Step 4: 让 routers/forge.py 复用它**

`backend/app/routers/forge.py` 第 165 行：

```python
_TERMINAL_STATUSES = {"done", "error"}
```

改成：

```python
from app.forge.pipeline import TERMINAL_STATUSES as _TERMINAL_STATUSES
```

把这行 import 挪到该文件的 import 段（与其他 `from app.forge...` 导入放一起），并删掉原来第 165 行的字面量定义。保留原处的注释上下文（`# deep-status lazy sweep: ...`）不动。

- [ ] **Step 5: 修 admin 监控端点**

`backend/app/routers/admin/forge_monitor.py`，import 段加：

```python
from app.forge.pipeline import TERMINAL_STATUSES
```

把 `list_active_forge_sessions`（第 86-98 行）改成：

```python
@router.get("/active")
async def list_active_forge_sessions(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List currently in-flight forge sessions (not yet done/error)."""
    result = await db.execute(
        select(ForgeSession)
        .where(ForgeSession.status.notin_(TERMINAL_STATUSES))
        .order_by(ForgeSession.updated_at.desc())
        .limit(200)
    )
    sessions = result.scalars().all()
    return [ForgeSessionListItem.model_validate(s, from_attributes=True) for s in sessions]
```

（同时把 docstring 里的 "non-completed" 措辞改掉——它正是那个错字的来源。）

- [ ] **Step 6: 跑测试确认通过**

```bash
cd backend && $PY -m pytest tests/test_admin_forge_monitor.py -v
cd backend && $PY -m pytest tests/ -q -k "forge" 2>&1 | tail -5
```

期望：新增两条 PASS，且 forge 相关测试无新增失败。

- [ ] **Step 7: 提交**

```bash
git add backend/app/forge/pipeline.py backend/app/routers/forge.py \
        backend/app/routers/admin/forge_monitor.py backend/tests/test_admin_forge_monitor.py
git commit -m "fix(admin): filter forge/active by the real terminal statuses

The endpoint filtered on \"completed\", a status no code path ever writes —
the pipeline's success terminal is \"done\" — so every finished session
stayed in the admin \"active sessions\" list forever. Hoists
TERMINAL_STATUSES into pipeline.py as the single source both routers now
import, and caps the unbounded result set at 200.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

---

## Task 4: ForgeMonitorPanel 前后端契约对齐

**Files:**
- Modify: `frontend/src/services/api/adminWorld.ts`（三个 interface + 两个函数）
- Modify: `frontend/src/components/admin/ForgeMonitorPanel.tsx`
- Create: `frontend/src/components/admin/ForgeMonitorPanel.test.tsx`

**Interfaces:**
- Consumes: 后端 `ForgeSessionListItem`（`backend/app/schemas/admin.py:145`）与 `GET /admin/forge` 的响应包装
- Produces: `AdminForgeSession` / `AdminForgeHistoryResponse`（前端类型，仅本面板消费）

**后端真实契约（照抄自 `backend/app/schemas/admin.py:145-155` 与 `admin/forge_monitor.py:62-83`）：**

```
GET /admin/forge/active  →  ForgeSessionListItem[]
GET /admin/forge         →  { items: ForgeSessionListItem[], total, offset, limit }
                            query: offset, limit, status, mode, sort_by, sort_order

ForgeSessionListItem = {
  id, user_id, character_name, mode, status, current_stage, created_at, updated_at
}
```

**前端当前声明的（全错）：** `forge_id`（后端是 `id`）、`started_at`（后端是 `created_at`）、`elapsed_seconds`（后端**根本没有**）、history 的 `stage` / `finished_at` / `resident_id`（后端分别是 `current_stage` / `updated_at` / **没有**），响应包装 `page` / `per_page`（后端是 `offset` / `limit`），请求参数也发的 `page` / `per_page`（后端只认 `offset` / `limit`，所以翻页按钮点了没反应）。

后果：有活跃会话时 `s.forge_id.slice(0, 8)` 对 `undefined` 调 `.slice` 直接抛 `TypeError`，炸掉整个「活跃会话」区块。

**明确不在本 task 范围内：** 该文件里 `selectStyle` 与翻页按钮的 `color: 'white'`（浅色主题下白字白底，P1「这周做」档）。**不要顺手改。**

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/components/admin/ForgeMonitorPanel.test.tsx`：

```tsx
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../services/api', () => ({
  getAdminForgeActive: vi.fn(),
  getAdminForgeHistory: vi.fn(),
}))

import { getAdminForgeActive, getAdminForgeHistory } from '../../services/api'
import { ForgeMonitorPanel } from './ForgeMonitorPanel'

afterEach(cleanup)

// 后端 ForgeSessionListItem 的真实形状（backend/app/schemas/admin.py:145）。
// 面板曾经声明 forge_id / started_at / elapsed_seconds，后端一个都不返回。
const SESSION = {
  id: 'f1e2d3c4-aaaa-bbbb-cccc-000000000001',
  user_id: 'u-1',
  character_name: '测试角色',
  mode: 'deep',
  status: 'building',
  current_stage: 'building',
  created_at: '2026-07-27T03:00:00',
  updated_at: '2026-07-27T03:04:00',
}

describe('ForgeMonitorPanel', () => {
  it('renders an active session from the real backend payload', async () => {
    vi.mocked(getAdminForgeActive).mockResolvedValue([SESSION])
    vi.mocked(getAdminForgeHistory).mockResolvedValue({
      items: [], total: 0, offset: 0, limit: 20,
    })

    render(<ForgeMonitorPanel token="t" />)

    await waitFor(() => expect(screen.getByText('测试角色')).toBeTruthy())
    // id 前 8 位，而不是对 undefined 调 .slice 抛 TypeError
    expect(screen.getByText('f1e2d3c4')).toBeTruthy()
    expect(screen.getByText('构建中')).toBeTruthy()
  })

  it('asks the history endpoint for offset/limit, not page/per_page', async () => {
    vi.mocked(getAdminForgeActive).mockResolvedValue([])
    vi.mocked(getAdminForgeHistory).mockResolvedValue({
      items: [SESSION], total: 1, offset: 0, limit: 20,
    })

    render(<ForgeMonitorPanel token="t" />)

    await waitFor(() => expect(getAdminForgeHistory).toHaveBeenCalled())
    const params = vi.mocked(getAdminForgeHistory).mock.calls[0][1]
    expect(params).toMatchObject({ offset: 0, limit: 20 })
    expect(params).not.toHaveProperty('page')
    expect(params).not.toHaveProperty('per_page')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run src/components/admin/ForgeMonitorPanel.test.tsx
```

期望：两条都 FAIL。第一条大概率直接抛 `TypeError: Cannot read properties of undefined (reading 'slice')`；第二条报 `params` 是 `{page: 1, per_page: 20}`。

- [ ] **Step 3: 改前端类型**

`frontend/src/services/api/adminWorld.ts`，把 forge 三个 interface 与 `getAdminForgeHistory` 替换为：

```ts
// 与后端 ForgeSessionListItem 逐字对应（backend/app/schemas/admin.py:145）。
// 这里曾经自造 forge_id / started_at / elapsed_seconds 三个后端不存在的字段，
// 而 apiFetch 只做裸 resp.json() 不做映射，于是活跃会话一出现就崩渲染。
export interface AdminForgeSession {
  id: string
  user_id: string
  character_name: string
  mode: string
  status: string
  current_stage: string
  created_at: string
  updated_at: string
}

export type AdminForgeHistoryItem = AdminForgeSession

export interface AdminForgeHistoryResponse {
  items: AdminForgeHistoryItem[]
  total: number
  offset: number
  limit: number
}

export function getAdminForgeActive(token: string): Promise<AdminForgeSession[]> {
  return apiFetch('/admin/forge/active', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminForgeHistory(
  token: string,
  params: { offset?: number; limit?: number; status?: string },
): Promise<AdminForgeHistoryResponse> {
  const qs = new URLSearchParams()
  if (params.offset != null) qs.set('offset', String(params.offset))
  if (params.limit != null) qs.set('limit', String(params.limit))
  if (params.status) qs.set('status', params.status)
  const query = qs.toString() ? `?${qs.toString()}` : ''
  return apiFetch(`/admin/forge${query}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}
```

- [ ] **Step 4: 改 ModeBadge 接受任意 mode**

后端 `mode` 是裸 `str`，前端原来声明成 `'quick' | 'deep'` 联合类型。`ForgeMonitorPanel.tsx` 第 51 行：

```tsx
function ModeBadge({ mode }: { mode: 'quick' | 'deep' }) {
```

改成：

```tsx
function ModeBadge({ mode }: { mode: string }) {
```

函数体内的 `mode === 'deep'` 三处判断保持不变（非 deep 一律按快速渲染）。

- [ ] **Step 5: 改活跃会话卡片**

`ForgeMonitorPanel.tsx` 的 `ActiveSessions`：把 `formatElapsed` 的调用改为前端自算耗时。在 `formatElapsed` 下面加一个 helper：

```tsx
function elapsedSince(iso: string): string {
  // 后端不返回 elapsed_seconds，用 created_at 现算。naive-UTC 要走 parseUTC。
  const started = parseUTC(iso).getTime()
  if (Number.isNaN(started)) return '—'
  return formatElapsed(Math.max(0, Math.floor((Date.now() - started) / 1000)))
}
```

然后把渲染块（原第 151-176 行）里的三处字段替换：

```tsx
        <div
          key={s.id}
```

```tsx
          <div style={{ display: 'flex', gap: 12, fontSize: 12, color: 'var(--text-muted)' }}>
            <span>耗时 {elapsedSince(s.created_at)}</span>
            <span style={{ marginLeft: 'auto', fontSize: 10, opacity: 0.7 }}>{s.id.slice(0, 8)}</span>
          </div>
```

- [ ] **Step 6: 改历史表**

`HistoryTable` 里：

1. 表头（原第 275 行）删掉后端不存在的「居民 ID」，「结束时间」改名为「更新时间」：

```tsx
              {['角色名', '模式', '状态', '最终阶段', '开始时间', '更新时间'].map((h) => (
```

2. 两处 `colSpan={7}` 改成 `colSpan={6}`。

3. 表体（原第 305-324 行）改成：

```tsx
            ) : items.map((item) => (
              <tr
                key={item.id}
                style={{ borderBottom: '1px solid var(--border)' }}
              >
                <td style={{ padding: '10px 12px', fontWeight: 600 }}>{item.character_name}</td>
                <td style={{ padding: '10px 12px' }}><ModeBadge mode={item.mode} /></td>
                <td style={{ padding: '10px 12px' }}><StageBadge stage={item.status} /></td>
                <td style={{ padding: '10px 12px' }}><StageBadge stage={item.current_stage} /></td>
                <td style={{ padding: '10px 12px', color: 'var(--text-muted)', fontSize: 12, whiteSpace: 'nowrap' }}>
                  {formatDateTime(item.created_at)}
                </td>
                <td style={{ padding: '10px 12px', color: 'var(--text-muted)', fontSize: 12, whiteSpace: 'nowrap' }}>
                  {formatDateTime(item.updated_at)}
                </td>
              </tr>
            ))}
```

4. 请求改成 offset/limit（原第 204-220 行的 `fetchHistory`）：

```tsx
  const fetchHistory = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await getAdminForgeHistory(token, {
        offset: (page - 1) * perPage,
        limit: perPage,
        status: statusFilter || undefined,
      })
      setItems(resp.items)
      setTotal(resp.total)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [token, page, perPage, statusFilter])
```

- [ ] **Step 7: 跑测试确认通过**

```bash
cd frontend && npx vitest run src/components/admin/ForgeMonitorPanel.test.tsx
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
```

期望：两条测试 PASS；`tsc` 与 `lint` 无新增错误。

- [ ] **Step 8: 全量前端测试确认无回归**

```bash
cd frontend && npm run test 2>&1 | tail -10
```

期望：相对基线零新增失败。

- [ ] **Step 9: 提交**

```bash
git add frontend/src/services/api/adminWorld.ts \
        frontend/src/components/admin/ForgeMonitorPanel.tsx \
        frontend/src/components/admin/ForgeMonitorPanel.test.tsx
git commit -m "fix(admin): align forge monitor types with the real API contract

The panel declared forge_id / started_at / elapsed_seconds / stage /
finished_at / resident_id — the backend returns none of them — so the first
active session crashed the block with a TypeError on undefined.slice().
History paging also sent page/per_page to an endpoint that only reads
offset/limit, so the pager did nothing. Adds the panel's first render test.

Verified-by: <粘贴 vitest 最后一行真实输出>"
```

---

## Task 5: admin 预设居民的 creator_id 外键

**Files:**
- Create: `backend/app/services/system_users.py`
- Create: `backend/tests/test_system_users.py`
- Modify: `backend/app/routers/admin/residents.py:275-283`
- Modify: `backend/seed/reset_builtin_residents.py`（`:179` 附近）
- Modify: `backend/tests/test_admin_residents.py:81-85`

**Interfaces:**
- Produces:
  - `app.services.system_users.SYSTEM_CREATOR_ID: str`（`"00000000-0000-0000-0000-000000000001"`）
  - `app.services.system_users.ADMIN_CREATOR_ID: str`（`"system"`）
  - `app.services.system_users.NON_USER_CREATOR_IDS: frozenset[str]`
  - `app.services.system_users.ensure_admin_creator_user(db: AsyncSession) -> None`
  - Task 6 消费 `NON_USER_CREATOR_IDS`

**背景与已定决策：** `residents.creator_id` 是 `ForeignKey("users.id")`（`backend/app/models/resident.py:27`，nullable）。`POST /admin/residents/presets` 写死字面量 `"system"`，而全仓**从未创建过 `id="system"` 的 users 行**（只有 seed 的 `SYSTEM_USER_ID` UUID），所以生产 PG 上必然 `ForeignKeyViolation`，被外层裸 `except Exception` 吞成 400。测试跑 sqlite 且不开 FK 强制，`backend/tests/test_admin_residents.py:85` 还把 `creator_id == "system"` 断言成期望值，等于把 bug 焊死。

**Jimmy 已拍板：补一行 `id="system"` 的哨兵用户，保留「admin 建的 vs seed 建的」区分。** 这与 `docs/plans/2026-07-27-T-ops.md:628-630` 已定义的 `SYSTEM_CREATOR_ID` / `ADMIN_CREATOR_ID` / `NON_USER_CREATOR_IDS` 三个常量**同名同值**，T-ops 线执行时直接从本模块 import 即可，不要再声明第二份。

**为什么不加迁移：** 全局约束禁止本批次带迁移。`ensure_admin_creator_user()` 走两条路径覆盖所有情况：① seed/bootstrap 路径（`reset_builtin_residents.py`，deploy compose 的 bootstrap 服务会跑）；② `create_preset` 内部在插入前自愈调用一次（幂等，一次 SELECT）。这样无论部署是否重跑过 seed，该端点都不会再违约。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_system_users.py`：

```python
"""非人类 creator_id 哨兵的真源与幂等性。"""
import pytest

from sqlalchemy import select, func

from app.models.user import User
from app.services.system_users import (
    ADMIN_CREATOR_ID,
    NON_USER_CREATOR_IDS,
    SYSTEM_CREATOR_ID,
    ensure_admin_creator_user,
)


def test_constants_do_not_drift_from_seed():
    """seed 那份 SYSTEM_USER_ID 是同一个值的第二个声明点；漂移了必须炸。"""
    from seed.preset_characters import SYSTEM_USER_ID

    assert SYSTEM_CREATOR_ID == SYSTEM_USER_ID
    assert ADMIN_CREATOR_ID == "system"
    assert NON_USER_CREATOR_IDS == frozenset({SYSTEM_CREATOR_ID, ADMIN_CREATOR_ID})


@pytest.mark.anyio
async def test_ensure_admin_creator_user_is_idempotent(db_session):
    """residents.creator_id 是 users.id 的 FK —— 这一行必须真实存在，且只存在一行。"""
    await ensure_admin_creator_user(db_session)
    await ensure_admin_creator_user(db_session)

    count = (await db_session.execute(
        select(func.count(User.id)).where(User.id == ADMIN_CREATOR_ID)
    )).scalar()
    assert count == 1

    row = (await db_session.execute(
        select(User).where(User.id == ADMIN_CREATOR_ID)
    )).scalar_one()
    assert row.is_admin is False, "sentinel row must not be a usable admin account"
    assert row.soul_coin_balance == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && $PY -m pytest tests/test_system_users.py -v
```

期望：两条都 FAIL，报 `ModuleNotFoundError: No module named 'app.services.system_users'`。

- [ ] **Step 3: 建立单一真源模块**

创建 `backend/app/services/system_users.py`：

```python
"""非人类 ``creator_id`` 哨兵账号。

两个刻意分开的哨兵：

- ``SYSTEM_CREATOR_ID`` —— seed 内置角色班底的所有者（``seed/preset_characters.py``
  的 ``SYSTEM_USER_ID``，由 ``seed_residents.ensure_system_user()`` 建行）。
- ``ADMIN_CREATOR_ID``  —— 通过 admin 控制台创建的居民的所有者。

两者都**必须**在 ``users`` 表里有真实行：``residents.creator_id`` 是
``ForeignKey("users.id")``（``app/models/resident.py``）。admin 建预设居民一直写字面量
``"system"`` 而从没有人建过这一行，所以生产 PostgreSQL 会直接拒绝插入（sqlite
测试不强制外键，所以一直没被发现）。

``docs/plans/2026-07-27-T-ops.md`` 的 F2/T 线用同名同值的三个常量；那条线执行时
从本模块 import，不要再声明第二份。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

SYSTEM_CREATOR_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_CREATOR_ID = "system"
NON_USER_CREATOR_IDS = frozenset({SYSTEM_CREATOR_ID, ADMIN_CREATOR_ID})


async def ensure_admin_creator_user(db: AsyncSession) -> None:
    """Idempotently create the ``users`` row admin-created residents point at.

    Deliberately NOT an admin account and never credited: it exists only to
    satisfy the FK. ``reward_creator_passive`` skips every id in
    ``NON_USER_CREATOR_IDS`` so this row's balance stays 0.
    """
    existing = await db.execute(select(User).where(User.id == ADMIN_CREATOR_ID))
    if existing.scalar_one_or_none():
        return
    db.add(User(
        id=ADMIN_CREATOR_ID,
        name="Admin Console",
        email="admin-console@skills.world",
        soul_coin_balance=0,
        is_admin=False,
    ))
    await db.commit()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && $PY -m pytest tests/test_system_users.py -v
```

期望：两条都 PASS。

- [ ] **Step 5: 提交真源模块**

```bash
git add backend/app/services/system_users.py backend/tests/test_system_users.py
git commit -m "feat(admin): add system_users as the single source for creator sentinels

residents.creator_id is a FK to users.id, but the two non-human owner ids
were spelled inline in three places and one of them (\"system\") had no
matching row at all. Same names/values as the F2/T lines' planned constants
so those import from here instead of redeclaring.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

- [ ] **Step 6: 写 create_preset 的失败测试**

> **不要把测试里的三段 markdown 填成"真实"的长文本。** `_create_preset` 会调
> `compute_sbti()`，而它只在 `ability_md + persona_md + soul_md` 合计 **≥ 50 字符**时才真的发
> LLM 请求（`backend/app/services/sbti_service.py:195-198` 短路返回 `None`）。下面的用例
> 三段都传空串（合计 0），所以走短路、零网络调用；现有的
> `test_admin_residents.py:68-85` 也是同样的理由才跑得快。填长了会在测试里打真实
> LLM 端点。

在 `backend/tests/test_admin_residents.py` 末尾追加：

```python
@pytest.mark.anyio
async def test_create_preset_owner_row_exists(client, db_session):
    """admin 建的预设居民，其 creator_id 必须指向一行真实存在的 users
    —— 否则生产 PG 直接外键违约（sqlite 不强制 FK，所以只能显式断言那一行在）。"""
    from sqlalchemy import select

    from app.models.resident import Resident
    from app.models.user import User
    from app.services.auth_service import create_token
    from app.services.system_users import ADMIN_CREATOR_ID

    admin = User(name="adm", email="adm-preset-fk@test.com", is_admin=True, is_banned=False)
    db_session.add(admin)
    await db_session.commit()

    resp = await client.post(
        "/admin/residents/presets",
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
        json={
            "slug": "preset-fk", "name": "外键测试", "district": "academy",
            "ability_md": "", "persona_md": "", "soul_md": "",
            "sprite_key": "伊莎贝拉", "tile_x": 1, "tile_y": 1,
            "resident_type": "preset", "reply_mode": "auto", "meta_json": None,
        },
    )
    assert resp.status_code == 200, resp.text

    created = (await db_session.execute(
        select(Resident).where(Resident.slug == "preset-fk")
    )).scalar_one()
    assert created.creator_id == ADMIN_CREATOR_ID

    owner = (await db_session.execute(
        select(User).where(User.id == created.creator_id)
    )).scalar_one_or_none()
    assert owner is not None, "creator_id points at a users row that does not exist"
```

同时修掉 `backend/tests/test_admin_residents.py:81-85` 那两处把 bug 焊死的地方——把第 81 行的 `creator_id="system",` 改成：

```python
        creator_id=ADMIN_CREATOR_ID,
```

第 85 行的断言改成：

```python
    assert preset.creator_id == ADMIN_CREATOR_ID
```

并在该文件的 import 段加：

```python
from app.services.system_users import ADMIN_CREATOR_ID
```

- [ ] **Step 7: 跑测试确认失败**

```bash
cd backend && $PY -m pytest tests/test_admin_residents.py -v -k "owner_row_exists"
```

期望：FAIL，报 `creator_id points at a users row that does not exist`（sqlite 让插入成功了，但那一行 users 确实不存在）。

- [ ] **Step 8: 修 create_preset**

`backend/app/routers/admin/residents.py` 的 import 段加：

```python
from app.services.system_users import ADMIN_CREATOR_ID, ensure_admin_creator_user
```

把 `create_preset`（第 267-285 行）的 try 块改成：

```python
    try:
        # residents.creator_id 是 users.id 的 FK；哨兵行可能还没被 seed 建出来
        # （lifespan 的 seeding 只在 auto_create_tables 下跑，生产够不着），
        # 所以在插入前自愈一次。幂等，命中时只多一次 SELECT。
        await ensure_admin_creator_user(db)
        resident = await _create_preset(
            db,
            slug=req.slug, name=req.name, district=req.district,
            ability_md=req.ability_md, persona_md=req.persona_md, soul_md=req.soul_md,
            sprite_key=req.sprite_key, tile_x=req.tile_x, tile_y=req.tile_y,
            resident_type=req.resident_type, reply_mode=req.reply_mode,
            meta_json=req.meta_json, creator_id=ADMIN_CREATOR_ID,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
```

- [ ] **Step 9: 把 seed 路径也补上**

`backend/seed/reset_builtin_residents.py` 第 45 行的 import 改成：

```python
from seed.seed_residents import ensure_system_user
from app.services.system_users import ensure_admin_creator_user
```

第 179 行 `await ensure_system_user(db)` 之后追加一行：

```python
        await ensure_admin_creator_user(db)
```

- [ ] **Step 10: 跑测试确认通过**

```bash
cd backend && $PY -m pytest tests/test_admin_residents.py -v
cd backend && $PY -m pytest tests/test_admin_authz_sweep.py -q
```

期望：`test_admin_residents.py` 全绿（含新增那条），Task 1 的 sweep 仍然全绿。

- [ ] **Step 11: 提交**

```bash
git add backend/app/routers/admin/residents.py backend/seed/reset_builtin_residents.py \
        backend/tests/test_admin_residents.py
git commit -m "fix(admin): give admin-created presets a creator row that exists

POST /admin/residents/presets wrote the literal \"system\" into a FK column
with no matching users row, so it failed outright on PostgreSQL — masked
because tests run sqlite without FK enforcement, and the test asserted the
broken value as expected. Ensures the sentinel row at both the seed path
and the endpoint itself; no migration needed.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

---

## Task 6: System 账号被动铸币止血 + 只读对账

**Files:**
- Modify: `backend/app/services/coin_service.py:514-524`
- Modify: `backend/tests/test_realism_coin_atomic.py`（追加断言）
- Create: `backend/scripts/audit_system_minting.py`
- Create: `backend/tests/test_audit_system_minting.py`

**Interfaces:**
- Consumes: `app.services.system_users.NON_USER_CREATOR_IDS`（Task 5 建立）
- Produces: `scripts.audit_system_minting.aggregate(rows) -> list[MintingRow]`（供测试消费）

**背景：** `reward_creator_passive` 只挡字面量 `"system"`，但 seed 给所有内置 NPC 写的是 `SYSTEM_USER_ID` UUID。于是任何玩家和任何内置 NPC 说一句话，System 用户就 +1 SC 并写一条 Transaction。这些交易被 `/admin/economy/stats` 的 `total_issued` / `net_circulation` 和首页 `soul_coin_net_flow` 统计进去，admin 看到的通胀曲线里混着本不该存在的铸币量。

**Jimmy 已拍板：只写只读对账脚本，先看数。** 本 task **不做任何数据修正**，脚本里**不得出现** UPDATE / DELETE / INSERT。

`creator_id` 是 nullable 的（账号注销会把它置 NULL），顺带显式处理。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_realism_coin_atomic.py` 的 `test_reward_creator_passive_system_and_missing` 里追加断言：

```python
@pytest.mark.anyio
async def test_reward_creator_passive_system_and_missing(db_session):
    assert await coin_service.reward_creator_passive(db_session, "system", "r") is None
    assert await coin_service.reward_creator_passive(db_session, "nobody", "r") is None


@pytest.mark.anyio
async def test_reward_creator_passive_skips_every_sentinel(db_session):
    """seed 把内置 NPC 的 creator_id 写成 SYSTEM_CREATOR_ID（UUID），而哨兵只挡
    字面量 "system" —— 于是每一轮内置 NPC 对话都在给 System 账号铸币，
    并污染 admin 经济面板的 total_issued / net_circulation。"""
    from sqlalchemy import select

    from app.models.user import User
    from app.services.system_users import NON_USER_CREATOR_IDS, SYSTEM_CREATOR_ID

    db_session.add(User(id=SYSTEM_CREATOR_ID, name="System",
                        email="system-mint@test.com", soul_coin_balance=0))
    await db_session.commit()

    for sentinel in NON_USER_CREATOR_IDS:
        assert await coin_service.reward_creator_passive(db_session, sentinel, "r") is None

    balance = (await db_session.execute(
        select(User.soul_coin_balance).where(User.id == SYSTEM_CREATOR_ID)
    )).scalar_one()
    assert balance == 0, "the seed system account must never be credited"

    # creator_id 是 nullable（注销会置 NULL），不能炸也不能铸币
    assert await coin_service.reward_creator_passive(db_session, None, "r") is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && $PY -m pytest tests/test_realism_coin_atomic.py -v -k "skips_every_sentinel"
```

期望：FAIL —— `SYSTEM_CREATOR_ID` 没被挡住，balance 变成 1。

- [ ] **Step 3: 修哨兵**

`backend/app/services/coin_service.py` 的 import 段加：

```python
from app.services.system_users import NON_USER_CREATOR_IDS
```

把第 514-521 行改成：

```python
async def reward_creator_passive(db: AsyncSession, creator_id: str | None, resident_slug: str) -> dict | None:
    """
    Award 1 SC to creator when their resident gets a conversation.
    Returns notification payload if reward given, None if the creator is one of
    the non-human sentinels (or missing).

    NON_USER_CREATOR_IDS covers both spellings: the admin console's "system"
    and the seed cast's SYSTEM_CREATOR_ID UUID. Only the former used to be
    checked, so every built-in NPC conversation minted 1 SC into the seed
    System account and inflated the admin economy panel's total_issued.
    creator_id is nullable (account deletion orphans residents).
    """
    if creator_id is None or creator_id in NON_USER_CREATOR_IDS:
        return None
```

（其余函数体不动。）

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && $PY -m pytest tests/test_realism_coin_atomic.py -v
cd backend && $PY -m pytest tests/ -q -k "coin or economy" 2>&1 | tail -5
```

期望：全绿，且 coin/economy 相关无新增失败。

- [ ] **Step 5: 提交止血**

```bash
git add backend/app/services/coin_service.py backend/tests/test_realism_coin_atomic.py
git commit -m "fix(economy): stop minting Soul Coin into the seed System account

reward_creator_passive only skipped the literal \"system\", but seed writes
SYSTEM_CREATOR_ID (a UUID) onto every built-in NPC — so each of their
conversations credited 1 SC to the System user and inflated total_issued /
net_circulation on the admin economy panel. Also handles the nullable
creator_id explicitly. Historical rows are untouched; see
scripts/audit_system_minting.py.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

- [ ] **Step 6: 写对账脚本的失败测试**

创建 `backend/tests/test_audit_system_minting.py`：

```python
"""只读对账脚本的聚合逻辑。"""
from datetime import datetime, UTC

from scripts.audit_system_minting import MintingRow, aggregate


def _tx(user_id: str, amount: int, reason: str, day: str):
    return (user_id, amount, reason, datetime.fromisoformat(day).replace(tzinfo=UTC))


def test_aggregate_groups_by_account_day_and_reason():
    rows = aggregate([
        _tx("system", 1, "creator_passive:klaus", "2026-07-25T01:00:00"),
        _tx("system", 1, "creator_passive:maria", "2026-07-25T02:00:00"),
        _tx("system", 1, "creator_passive:klaus", "2026-07-26T01:00:00"),
    ])
    assert rows == [
        MintingRow(user_id="system", day="2026-07-25", reason="creator_passive", count=2, total=2),
        MintingRow(user_id="system", day="2026-07-26", reason="creator_passive", count=1, total=1),
    ]


def test_aggregate_strips_the_slug_suffix_from_reason():
    """reason 是 creator_passive:<slug>，按 slug 分组会炸成几百行噪音。"""
    rows = aggregate([
        _tx("system", 1, "creator_passive:a", "2026-07-25T01:00:00"),
        _tx("system", 1, "creator_passive:b", "2026-07-25T02:00:00"),
    ])
    assert len(rows) == 1
    assert rows[0].reason == "creator_passive"


def test_aggregate_keeps_negative_amounts_separate():
    rows = aggregate([
        _tx("system", 1, "creator_passive:a", "2026-07-25T01:00:00"),
        _tx("system", -5, "shop_purchase", "2026-07-25T03:00:00"),
    ])
    assert {r.reason: r.total for r in rows} == {"creator_passive": 1, "shop_purchase": -5}


def test_aggregate_handles_empty_input():
    assert aggregate([]) == []
```

- [ ] **Step 7: 跑测试确认失败**

```bash
cd backend && $PY -m pytest tests/test_audit_system_minting.py -v
```

期望：全部 FAIL，报 `ModuleNotFoundError: No module named 'scripts.audit_system_minting'`。

- [ ] **Step 8: 写脚本**

创建 `backend/scripts/audit_system_minting.py`：

```python
#!/usr/bin/env python3
"""清点两个非人类哨兵账号被误铸的 Soul Coin —— **纯只读**，不写任何一行。

背景：``reward_creator_passive`` 曾经只挡字面量 ``"system"``，而 seed 给内置 NPC
写的是 ``SYSTEM_CREATOR_ID``（UUID）。于是每一轮内置 NPC 对话都往 System 账号
铸 1 SC 并落一条 Transaction，这些交易被 ``/admin/economy/stats`` 的
``total_issued`` / ``net_circulation`` 统计进去。代码侧已止血（coin_service），
这个脚本只回答「历史上已经发生了多少」。

用法（vm212 api 容器内，DATABASE_URL 已由 deploy compose 注入）::

    docker compose exec api python scripts/audit_system_minting.py

本地 / 任意库::

    DATABASE_URL=sqlite+aiosqlite:////tmp/x.db python scripts/audit_system_minting.py

输出：按 账号 × UTC 日 × reason 前缀 聚合的笔数与净额，外加两个账号的当前余额。
本脚本**不会**修正任何数据；要不要冲正是单独的决定。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class MintingRow:
    user_id: str
    day: str
    reason: str
    count: int
    total: int


def _reason_prefix(reason: str) -> str:
    """``creator_passive:klaus`` → ``creator_passive``（按 slug 分组会炸成噪音）。"""
    return reason.split(":", 1)[0]


def aggregate(rows) -> list[MintingRow]:
    """[(user_id, amount, reason, created_at)] → 按 账号 × UTC 日 × reason 聚合。"""
    buckets: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for user_id, amount, reason, created_at in rows:
        day = created_at.strftime("%Y-%m-%d")
        buckets[(user_id, day, _reason_prefix(reason))].append(amount)
    return [
        MintingRow(user_id=uid, day=day, reason=reason,
                   count=len(amounts), total=sum(amounts))
        for (uid, day, reason), amounts in sorted(buckets.items())
    ]


async def _load():
    from sqlalchemy import select

    from app.database import async_session
    from app.models.transaction import Transaction
    from app.models.user import User
    from app.services.system_users import NON_USER_CREATOR_IDS

    async with async_session() as db:
        result = await db.execute(
            select(Transaction.user_id, Transaction.amount,
                   Transaction.reason, Transaction.created_at)
            .where(Transaction.user_id.in_(NON_USER_CREATOR_IDS))
            .order_by(Transaction.created_at)
        )
        rows = list(result.all())
        balances = (await db.execute(
            select(User.id, User.name, User.soul_coin_balance)
            .where(User.id.in_(NON_USER_CREATOR_IDS))
        )).all()
    return rows, balances


def render(rows: list[MintingRow], balances) -> str:
    out = ["账号哨兵当前余额", "-" * 64]
    if not balances:
        out.append("(两个哨兵账号在这个库里都不存在)")
    for uid, name, balance in balances:
        out.append(f"{uid:40} {name:16} {balance:>8}")

    out += ["", "误铸明细（账号 × UTC 日 × reason）", "-" * 64,
            f"{'账号':40} {'日期':12} {'reason':22} {'笔数':>6} {'净额':>8}"]
    if not rows:
        out.append("(无记录)")
        return "\n".join(out)

    for r in rows:
        out.append(f"{r.user_id:40} {r.day:12} {r.reason:22} {r.count:>6} {r.total:>8}")

    passive = [r for r in rows if r.reason == "creator_passive"]
    out += ["", "-" * 64,
            f"creator_passive 合计：{sum(r.count for r in passive)} 笔 / {sum(r.total for r in passive)} SC",
            "（这就是需要从 admin 经济面板 total_issued 里扣掉的量）"]
    return "\n".join(out)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    rows, balances = await _load()
    print(render(aggregate(rows), balances))


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 9: 跑测试确认通过**

```bash
cd backend && $PY -m pytest tests/test_audit_system_minting.py -v
```

期望：4 条全 PASS。

- [ ] **Step 10: 确认脚本是纯只读的**

```bash
cd backend && grep -niE "update|delete|insert|\.add\(|commit\(" scripts/audit_system_minting.py
```

期望：**无输出**。有任何一行命中就是写路径混进来了，停下报告。

再对空库跑一次确认不炸：

```bash
cd backend && DEBUG=true LLM_API_KEY=x DATABASE_URL="sqlite+aiosqlite:////tmp/audit_probe.db" \
  $PY scripts/audit_system_minting.py
```

期望：正常打印表头 + `(两个哨兵账号在这个库里都不存在)` + `(无记录)`，不抛异常。

- [ ] **Step 11: 提交**

```bash
git add backend/scripts/audit_system_minting.py backend/tests/test_audit_system_minting.py
git commit -m "feat(scripts): add read-only audit for sentinel-account minting

Counts how much Soul Coin the two non-human sentinel accounts were credited
before the coin_service fix, grouped by account x UTC day x reason prefix,
so the inflated total_issued on the admin economy panel can be quantified.
Strictly read-only: no UPDATE/INSERT/DELETE, no commit.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

---

## Task 7: 配置密钥掩码

**Files:**
- Modify: `backend/app/routers/admin/system_config.py`
- Create: `backend/tests/test_admin_config_secrets.py`
- Modify: `frontend/src/components/admin/SystemConfigPanel.tsx`

**Interfaces:**
- Produces: `app.routers.admin.system_config.MASKED_VALUE: str`、`_is_secret_key(key: str) -> bool`

**背景：** `DEFAULT_CONFIGS` 把 `settings.effective_api_key`（`system_config.py:51`）与 `settings.portrait_llm_api_key`（`:75`）直接作为默认值参与合并返回。LLM 分组在面板上是 `defaultOpen`，进 tab 就自动加载，密钥落在浏览器内存与 DevTools 响应体里；旁边的「显示」按钮一点即可肉眼复制，而字段提示写的是「保存后不明文显示」，与实际行为相反。

**两条出口都要堵：** `/admin/system/groups/{group}`（合并默认值）与 `/admin/system/entries`（裸返 SystemConfig 全表行）。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_admin_config_secrets.py`：

```python
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
async def test_group_read_masks_api_key(client, db_session):
    """llm 分组的默认值直接来自 settings.effective_api_key —— 不能原样下发。"""
    from app.config import settings
    from app.routers.admin.system_config import MASKED_VALUE

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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && $PY -m pytest tests/test_admin_config_secrets.py -v
```

期望：4 条全 FAIL，前两条报 `ImportError: cannot import name 'MASKED_VALUE'`。

- [ ] **Step 3: 后端加掩码**

`backend/app/routers/admin/system_config.py`，在 `VALID_GROUPS` 定义之后加：

```python
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
```

在 `_get_config_group` 的 `return merged` 之前，把返回值改成掩码后的副本：

```python
    return {k: _mask(k, v) for k, v in merged.items()}
```

`list_all_config_entries`（第 159-169 行）的返回改成：

```python
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
```

写侧：`update_config_entry`（第 172-183 行）在 `_validate_config_value` 之前插入跳过逻辑：

```python
    full_key = req.key if "." in req.key else f"{req.group}.{req.key}"
    if _is_secret_key(full_key) and req.value in (MASKED_VALUE, ""):
        # 面板整组回传时，未改动的密钥字段带回来的是掩码；空串同理表示「不修改」。
        return {"key": full_key, "value": MASKED_VALUE, "group": req.group, "unchanged": True}
    await _validate_config_value(full_key, req.value)
```

`update_config_batch`（第 186-200 行）的 updates 构造后加过滤：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && $PY -m pytest tests/test_admin_config_secrets.py -v
cd backend && $PY -m pytest tests/test_admin_config.py -v
```

期望：新增 4 条全 PASS，原有 `test_admin_config.py` 无新增失败。

- [ ] **Step 5: 前端删掉「显示」按钮、改文案**

`frontend/src/components/admin/SystemConfigPanel.tsx`：

1. 把 password 分支（第 169-195 行的 `<div style={{ display: 'flex', gap: 6, ...}}>` 整块）替换成不带「显示」按钮的单个输入框：

```tsx
                    {field.type === 'password' ? (
                      <input
                        type="password"
                        value={values[field.key] ?? ''}
                        onChange={(e) => setField(field.key, e.target.value)}
                        placeholder="已设置 · 留空则不修改"
                        style={{
                          width: '100%', padding: '8px 12px',
                          background: 'var(--bg-input)', border: '1px solid var(--border)',
                          borderRadius: 6, color: 'var(--text-primary)', fontSize: 13,
                          outline: 'none', boxSizing: 'border-box',
                        }}
                      />
                    ) : (
```

2. 删掉现在没有引用的 `showPassword` state 与 `toggleShowPassword` 函数（`npx tsc --noEmit` 会指出确切位置）。

3. 两处 hint 改成与实际行为一致（第 234 行与第 244 行）：

```tsx
  { key: 'api_key', label: 'API 密钥 (api_key)', type: 'password', hint: '服务端只回传掩码；留空保存不会覆盖已有值' },
```

```tsx
  { key: 'api_key', label: 'API 密钥 (api_key)', type: 'password', hint: '服务端只回传掩码；留空保存不会覆盖已有值' },
```

- [ ] **Step 6: 前端校验**

```bash
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
cd frontend && npm run test 2>&1 | tail -10
```

期望：无新增错误/失败。

- [ ] **Step 7: 提交**

```bash
git add backend/app/routers/admin/system_config.py backend/tests/test_admin_config_secrets.py \
        frontend/src/components/admin/SystemConfigPanel.tsx
git commit -m "fix(admin): mask config secrets instead of shipping them to the browser

llm.api_key and portrait.api_key defaulted straight from settings, so opening
the (default-open) LLM group put the upstream provider key in the response
body, and a \"show\" button revealed it in plaintext — while the field hint
claimed the opposite. Masks on both read paths (/groups/{group} and
/entries); blank or masked writes now mean \"leave unchanged\" so a full-group
save can't clobber the real key.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

---

## Task 8: 首个管理员的提权脚本与文档

**Files:**
- Create: `backend/scripts/grant_admin.py`
- Create: `backend/tests/test_grant_admin.py`
- Create: `docs/ADMIN_BOOTSTRAP.md`
- Modify: `README.md`（一行指针）

**Interfaces:**
- Produces: `scripts.grant_admin.set_admin(db, email: str, *, grant: bool, dry_run: bool) -> str`（返回给人看的一行结果，供 CLI 与测试共用）

**背景：** `is_admin` 只有模型 `default=False` 和迁移 `server_default='false'`；`backend/scripts/`、`backend/seed/`、alembic 全链、deploy 脚本、两份 `.env.example`、docs 里都没有任何提权入口，而唯一的写入口 `PATCH /admin/users/{id}` 自身就要求 admin 身份。新环境部署完必须上生产手工跑 `UPDATE users SET is_admin=true`——与 07-25 事故完全同一类未受控的手工 SQL 操作面。

**范围边界：** 报告里「给 PATCH 加『至少保留一名管理员』约束」属于「这周做」档，**本 task 不做**。但脚本自身的降权路径必须有这个守卫（否则脚本会造出它要解决的那个死锁）。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_grant_admin.py`：

```python
"""首个管理员提权脚本。"""
import pytest
from sqlalchemy import select

from app.models.user import User
from scripts.grant_admin import set_admin


async def _user(db, email: str, *, is_admin: bool = False) -> User:
    u = User(name=email.split("@")[0], email=email, is_admin=is_admin)
    db.add(u)
    await db.commit()
    return u


@pytest.mark.anyio
async def test_grant_promotes_and_is_idempotent(db_session):
    await _user(db_session, "a@test.com")

    first = await set_admin(db_session, "a@test.com", grant=True, dry_run=False)
    assert "granted" in first

    row = (await db_session.execute(
        select(User).where(User.email == "a@test.com")
    )).scalar_one()
    assert row.is_admin is True

    second = await set_admin(db_session, "a@test.com", grant=True, dry_run=False)
    assert "already" in second


@pytest.mark.anyio
async def test_dry_run_writes_nothing(db_session):
    await _user(db_session, "b@test.com")

    msg = await set_admin(db_session, "b@test.com", grant=True, dry_run=True)
    assert "dry-run" in msg

    row = (await db_session.execute(
        select(User).where(User.email == "b@test.com")
    )).scalar_one()
    assert row.is_admin is False


@pytest.mark.anyio
async def test_unknown_email_raises(db_session):
    with pytest.raises(LookupError):
        await set_admin(db_session, "nobody@test.com", grant=True, dry_run=False)


@pytest.mark.anyio
async def test_revoke_refuses_to_remove_the_last_admin(db_session):
    """脚本存在的意义就是消灭「必须手工 SQL 才能救回来」的状态，
    所以它自己绝不能把管理员数清零。"""
    await _user(db_session, "solo@test.com", is_admin=True)

    with pytest.raises(ValueError, match="last admin"):
        await set_admin(db_session, "solo@test.com", grant=False, dry_run=False)

    row = (await db_session.execute(
        select(User).where(User.email == "solo@test.com")
    )).scalar_one()
    assert row.is_admin is True


@pytest.mark.anyio
async def test_revoke_works_when_another_admin_remains(db_session):
    await _user(db_session, "one@test.com", is_admin=True)
    await _user(db_session, "two@test.com", is_admin=True)

    msg = await set_admin(db_session, "two@test.com", grant=False, dry_run=False)
    assert "revoked" in msg

    row = (await db_session.execute(
        select(User).where(User.email == "two@test.com")
    )).scalar_one()
    assert row.is_admin is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && $PY -m pytest tests/test_grant_admin.py -v
```

期望：全部 FAIL，报 `ModuleNotFoundError: No module named 'scripts.grant_admin'`。

- [ ] **Step 3: 写脚本**

创建 `backend/scripts/grant_admin.py`：

```python
#!/usr/bin/env python3
"""按 email 提权 / 降权一个用户。**新环境的第一个管理员就靠它。**

在此之前全仓没有任何创建管理员的路径：``is_admin`` 只有模型默认值与迁移
server_default，唯一的写入口 ``PATCH /admin/users/{id}`` 自身就要求 admin 身份。
于是新环境部署完只能上生产手工跑 ``UPDATE users SET is_admin=true`` —— 与 07-25
事故同一类未受控的手工 SQL 操作面。

用法（vm212 api 容器内，DATABASE_URL 已由 deploy compose 注入）::

    docker compose exec api python scripts/grant_admin.py --email you@example.com          # dry-run
    docker compose exec api python scripts/grant_admin.py --email you@example.com --apply

降权::

    docker compose exec api python scripts/grant_admin.py --email x@example.com --revoke --apply

``--dry-run`` 是默认行为，必须显式 ``--apply`` 才写库。降权时拒绝清零管理员
（否则就会造出这个脚本本来要解决的那个死锁）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def set_admin(db, email: str, *, grant: bool, dry_run: bool) -> str:
    """Promote/demote by email. Returns a human-readable audit line.

    Raises LookupError if the email is unknown, ValueError if revoking would
    leave the deployment with zero admins.
    """
    from sqlalchemy import func, select

    from app.models.user import User

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        raise LookupError(f"no user with email {email!r}")

    if user.is_admin == grant:
        state = "already an admin" if grant else "already not an admin"
        return f"{email} ({user.id}) is {state} — nothing to do"

    if not grant:
        admin_count = (await db.execute(
            select(func.count(User.id)).where(User.is_admin.is_(True))
        )).scalar() or 0
        if admin_count <= 1:
            raise ValueError(
                f"refusing to revoke the last admin ({email}); "
                "promote someone else first"
            )

    verb = "granted" if grant else "revoked"
    if dry_run:
        return f"[dry-run] would have {verb} admin on {email} ({user.id})"

    user.is_admin = grant
    await db.commit()
    return f"{verb} admin on {email} ({user.id})"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="target user's email address")
    parser.add_argument("--revoke", action="store_true", help="demote instead of promote")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; without it the script is a dry-run")
    args = parser.parse_args()

    from app.database import async_session

    async with async_session() as db:
        try:
            print(await set_admin(db, args.email, grant=not args.revoke,
                                  dry_run=not args.apply))
        except (LookupError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && $PY -m pytest tests/test_grant_admin.py -v
```

期望：5 条全 PASS。

- [ ] **Step 5: 写文档**

创建 `docs/ADMIN_BOOTSTRAP.md`：

```markdown
# 管理后台：新环境引导

## 第一个管理员从哪来

`is_admin` 默认 `false`，而唯一的线上写入口 `PATCH /admin/users/{user_id}` 自身就要求
admin 身份——所以新部署的环境里没有任何人能通过界面拿到管理员权限。用脚本：

    docker compose exec api python scripts/grant_admin.py --email you@example.com

默认是 dry-run，只打印将要发生什么。确认无误后加 `--apply`：

    docker compose exec api python scripts/grant_admin.py --email you@example.com --apply

前提是该 email 已经注册过（先在前端正常注册/OAuth 登录一次）。

### 降权

    docker compose exec api python scripts/grant_admin.py --email x@example.com --revoke --apply

脚本拒绝降掉最后一名管理员。注意后台的 `PATCH /admin/users/{id}` 目前**没有**这个约束，
两名管理员互相降权仍可能把数量降到零——真降到零就只能用本脚本救（它按 email 找人，
不需要任何现存管理员）。

## 两个哨兵账号

`residents.creator_id` 是 `users.id` 的外键，有两个非人类所有者，都在
`backend/app/services/system_users.py` 里定义：

| 常量 | 值 | 谁用它 | 建行的地方 |
|---|---|---|---|
| `SYSTEM_CREATOR_ID` | `00000000-…-0001` | seed 内置角色班底 | `seed_residents.ensure_system_user()` |
| `ADMIN_CREATOR_ID` | `system` | admin 控制台建的预设居民 | `system_users.ensure_admin_creator_user()` |

两者都由 bootstrap 服务（`alembic upgrade head && python -m seed.reset_builtin_residents`）
建出来；`POST /admin/residents/presets` 另外会在插入前自愈调用一次，所以即使 seed 没跑过
也不会外键违约。

这两个账号**永远不该有余额**：`coin_service.reward_creator_passive` 跳过
`NON_USER_CREATOR_IDS` 里的每一个 id。想确认历史上有没有被误铸过：

    docker compose exec api python scripts/audit_system_minting.py

该脚本是纯只读的，只报数不改数据。

## 相关

- 端点与风险全貌：`docs/plans/2026-07-27-admin-immediate-fixes.md`
- 立绘审核后台的运维手册：`docs/RESIDENT_SPRITE_OPERATIONS.md`
```

- [ ] **Step 6: README 加指针**

`README.md` 的「### 验证」小节（第 253-257 行）当前是：

```markdown
### 验证

- 打开 http://localhost:5173 应看到游戏界面
- 访问 http://localhost:8000/health 应返回 `{"status": "ok"}`
- 访问 http://localhost:8000/docs 查看 API 文档
```

在最后一条之后追加一行（**只加这一行，不要重排 README**）：

```markdown
- 管理后台 `/admin` 需要 `is_admin` 账号——第一个管理员怎么来见 [docs/ADMIN_BOOTSTRAP.md](docs/ADMIN_BOOTSTRAP.md)
```

- [ ] **Step 7: 提交**

```bash
git add backend/scripts/grant_admin.py backend/tests/test_grant_admin.py \
        docs/ADMIN_BOOTSTRAP.md README.md
git commit -m "feat(scripts): add grant_admin so a fresh deploy can get its first admin

is_admin had no creation path anywhere — not in scripts, seed, alembic,
deploy or docs — while the only write endpoint requires admin itself, so
bootstrapping meant hand-running UPDATE on production: the same
uncontrolled manual-SQL surface as the 07-25 incident. dry-run by default,
refuses to revoke the last admin.

Verified-by: <粘贴 pytest 最后一行真实输出>"
```

---

## 收尾验证

八个 task 全部完成后：

- [ ] **全量后端测试，对比基线失败集**

```bash
cd backend && $PY -m pytest tests/ -q 2>&1 | tail -10
```

判定：与「执行前基线」双向差集，**零新增失败**。有新增就定位到具体 task 修掉，不要放行。

- [ ] **全量前端测试 + 类型 + lint**

```bash
cd frontend && npm run test 2>&1 | tail -10
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
```

- [ ] **确认本批次没有偷偷带迁移**

```bash
git diff --stat master...HEAD -- backend/alembic/
```

期望：**空输出**。有任何迁移文件出现就是违反全局约束，停下报告。

- [ ] **确认提交历史是一 step 一 commit**

```bash
git log --oneline master..HEAD
```

期望：约 10-12 条，每条对应上面的一个 commit step，message 里带真实 `Verified-by:`。

- [ ] **走一遍真实用户路径**（`verify-before-done`，build 绿 ≠ 完成）

起本地后端 + 前端，用一个真实 admin 账号登录 `/admin`，至少走到：
1. 「炼化监控」tab —— 确认活跃会话区块不再崩（Task 3 + 4）
2. 「系统配置」tab 的 LLM 分组 —— 确认 api_key 显示为掩码、没有「显示」按钮，且点一次保存后重新进入该 tab，密钥仍然可用（Task 7）
3. 「居民编辑」→ 新建预设居民 —— 确认返回 200 且居民出现在列表里（Task 5）

把这三步的**真实截图或 curl 输出**贴进收尾报告。

- [ ] **停下，报告，不要自行 push / 合并 / 部署**

全局约束：对外/不可逆动作需 Jimmy 另行授权。收尾报告里说明：本批次改了 deploy compose 的端口绑定，**下次部署时**才会生效，vm212 当前运行实例不受影响。
