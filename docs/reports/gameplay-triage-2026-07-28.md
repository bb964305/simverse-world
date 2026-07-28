# 2026-07-28 玩家实测问题诊断 · 8 条报告 → 37 条根因

> 来源：项目所有者实际游玩后报告 8 条问题，5 组 agent 并行定位（含生产库只读核对与 API 日志比对）。
> 基线 `master = ed9d42f`，vm212 alembic `051`。
> 每条的根因都是**读过代码确认**的，不是推测；能在生产上只读验证的都贴了真实读数。

## 玩家报告 → 诊断映射

| 玩家报告 | 展开成 | 最高优先级 |
|---|---|---|
| #1 每次进游戏要重选角色 | #1 / #1b / #1c | P1 |
| #2 议案投票没实装 | #2 / #2b / #2c / #2d / #4b | **P0** |
| #3 辩论很随机、无法新建 | #3-1 / #3-2 / #3-3 / #3-4 / E3 | **P0** |
| #4 赛季页 React #31 | #4 / E7 | **P0** |
| #5 每次要重新认领住房 | #5 / #5a | P2 |
| #6 村落日报不更新 | #6-1 / #6-2 / #6-3 / E10 | **P0** |
| #7 居民记忆有问题 | #7-a ~ #7-e | P1 |
| #8 带路请求无后续行动 | #8 | P1 |
| （排查中额外发现） | E1~E12 | **P0** |

## 三条最反直觉的结论

1. **#2「议案投票没实装」是误判** —— 后端 `POST /polls/{id}/vote` 与前端投票按钮**都实装了**，是被 #4 的整页崩溃挡死了。修好 #4，投票立刻可用。`votes` 表恒为 0 就是这么来的。
2. **#3 辩论「很随机」不是随机** —— 辩论生命周期**根本没有驱动器**，永远卡在 `announced` 状态，玩家押注的金币被**永久冻结**。这是资金问题，不是体验问题。
3. **赛季系统从来没有开季入口** —— `seasons` 表 0 行，所有记分静默变成 no-op。

---

### #1 · 同一个人用两个 OAuth 入口登录 = 两行 users = 每换一次入口就要重新选角色

**类型** `MISSING_FEATURE` ｜ **优先级** `P1` ｜ **工作量** 新功能

**根因**

OAuth 登录只按 provider id 查用户，没有任何账号绑定/合并：`backend/app/services/linuxdo_auth.py:98-116` 只按 `User.linuxdo_id == str(ld_user.id)` 查，查不到就 `User(email=f"{ld_user.username}@linux.do", ...)` 新建；`backend/app/services/github_auth.py:83-95` 同理，只按 `User.github_id` 查，新建时 `email=gh_user.email or f"{gh_user.login}@github.users"`。两条路径都不会按邮箱/已有账号做关联。

而「是否已选过角色」的判定是 per-user 行的：`backend/app/services/onboarding_service.py:30-33` 返回 `needs_onboarding = user.player_resident_id is None`。所以同一个人从 GitHub 换到 Linux.do，命中的是另一行 users，`player_resident_id` 当然是 NULL → 必然重新走一遍选角色。

生产库实证（只读）：
```sql
SELECT id,name,email,linuxdo_id,github_id,created_at FROM users WHERE name='不做了睡大觉';
 176a210c-3b83-4ec5-b520-1bdf359b4ab8 | 不做了睡大觉 | stawky@linux.do        | 132315 |          | 2026-07-23 05:41:47
 11769050-f93a-4236-bb08-2855375a07ce | 不做了睡大觉 | stakeswky@github.users |        | 64798754 | 2026-07-17 03:21:50
```
同一个显示名、两行 users，各自绑了各自的化身：
```sql
SELECT u.email,u.player_resident_id,r.slug,r.created_at FROM users u JOIN residents r ON r.id=u.player_resident_id;
 stawky@linux.do        | c3c149e7-... | p-赵启文 | 2026-07-28 12:34:33.788
 stakeswky@github.users | 8c470622-... | p-沈静书 | 2026-07-28 05:24:21.924
```
API 日志（`docker logs -t deploy-api-1`）逐秒对得上，两次都是 200（即两次都是该 user 行的"首次" onboarding，不是重复提交被拒）：
```
2026-07-28T05:24:21.949Z "POST /onboarding/load-preset" 200 OK   ← github 那行
2026-07-28T12:34:12.552Z "GET  /onboarding/check"        401     ← 旧 token 过期，core.ts 触发 logout
2026-07-28T12:34:33.807Z "POST /onboarding/load-preset" 200 OK   ← 改用 linux.do 登录 → 另一行 user → 又选一次
```
注意 12:34:12 那个 401：`backend/app/config.py:44` `jwt_expire_minutes: int = 1440`（24h），token 一到期 `frontend/src/services/api/core.ts:49-56` 收到 401 就 `useGameStore.getState().logout()` 清 localStorage 打回登录页——玩家在登录页随手换了一个 OAuth 按钮，就掉进另一个身份。所以「每天进游戏都要重选一次」是可复现的日常节奏，不是玩家记错。

补充：持久化本身**是实现了的、并且工作正常**（`onboarding_service.py:99` 写 `user.player_resident_id = resident.id` + `:103` commit；`check_onboarding_needed` 读它）。同一行 user 内不会反复要求选角色。

**复现**

1. 浏览器 A：用 GitHub 登录 https://simverse.world → 选一个预设角色 → 进游戏。此时 `users(github_id=<你的id>).player_resident_id` 已写入。
2. 清 localStorage（或等 24h token 自然过期，`core.ts:49-56` 会自动 logout）。
3. 回到 /login，改点 Linux.do 登录（同一个人）。
4. `AuthCallbackPage.tsx:35` 无条件 `navigate('/onboarding')` → `OnboardingPage.tsx:74-78` 调 `GET /onboarding/check`。
5. 必现：返回 `needs_onboarding: true`，角色选择网格再次弹出。反向操作（Linux.do → GitHub）同样必现。

**修法**

做 OAuth 账号绑定，让同一个人只有一行 users：

(a) 后端加绑定入口。在 `backend/app/routers/auth.py` 增加 `GET /auth/{provider}/link/start` + 回调，回调里带**当前登录用户的 JWT**（state 里塞 user_id，服务端签名校验），命中后不是新建 User，而是把 `user.github_id` / `user.linuxdo_id` 写到当前行；provider id 已被别人占用则 409。

(b) 改登录路径的查找顺序。`backend/app/services/linuxdo_auth.py:98` 与 `backend/app/services/github_auth.py:83` 现在是「按 provider id 查 → 查不到就建号」，中间补一层：
```python
user = (await db.execute(select(User).where(User.linuxdo_id == str(ld_user.id)))).scalar_one_or_none()
if not user and ld_user.email:              # 只在 provider 返回了已验证邮箱时才认
    user = (await db.execute(select(User).where(User.email == ld_user.email))).scalar_one_or_none()
    if user:
        user.linuxdo_id = str(ld_user.id)   # 自动认领，落库
if not user:
    user = User(...)                        # 保持原逻辑
```
注意 linux.do 现在写的是伪邮箱 `{username}@linux.do`（`linuxdo_auth.py:115`）、github 写 `{login}@github.users`（`github_auth.py:94`），两者永远不会相等——所以 (b) 只在 provider 真的给了已验证邮箱时才有用，主力还得靠 (a) 的显式绑定。

(c) 前端在 `/profile` 设置页的账号分组（`AllSettingsResponse.account` 已经返回了 `github_bound` / `linuxdo_bound`，见 `backend/app/routers/settings.py:75-82`）加两个「绑定 GitHub / 绑定 Linux.do」按钮，把 (a) 的入口露出来。

(d) 存量数据：`stawky@linux.do` 与 `stakeswky@github.users` 两行需要人工确认后合并（把 linuxdo_id 挪到保留行，另一行的化身/资产迁移或作废）。合并脚本要单独走，**不要和开关变更同一次上线**。

---

### #1b · player_resident_id 悬空（化身被删但指针还在）会把玩家永久锁死：既进不了 onboarding，也没有角色

**类型** `BUG` ｜ **优先级** `P1` ｜ **工作量** 一个函数

**根因**

「是否已选过角色」只判 NULL、不判化身是否还存在：
`backend/app/services/onboarding_service.py:30-33`
```python
return {
    "needs_onboarding": user.player_resident_id is None,
    "player_resident_id": user.player_resident_id,
}
```
同时重建通道被无条件堵死：`backend/app/services/onboarding_service.py:53-54`
```python
if user.player_resident_id:
    raise ValueError(f"User {user_id} already has a player resident")
```
于是指针一旦悬空，三条路全断：
- `GET /onboarding/check` → `needs_onboarding=false` → `frontend/src/pages/OnboardingPage.tsx:75-77` 直接 `navigate('/play')`，玩家再也看不到选角色页；
- `GET /settings` → `backend/app/routers/settings.py:85-87` 的 `get_player_resident` 返回 None（`backend/app/services/settings_service.py:193-200` 按 id 查，查不到返回 None）→ `character=null` → `frontend/src/pages/GamePage.tsx:27-33` 拿不到 sprite_key，用默认皮；`PATCH /settings/character` 走 `settings.py:60-62` 直接 404；
- `POST /onboarding/create-character` / `load-preset` → 400 `already has a player resident`。
还有 `backend/app/routers/residents.py:334` 的位置保存、`backend/app/ws/handlers/connection.py:144-152` 的出生点/皮肤、`backend/app/ws/handlers/player_chat.py:87-92` 的玩家聊天，全部静默降级。

这不是假设——07-25 事故就是这一类：`backend/seed/reset_builtin_residents.py:19-24` 的 docstring 自己记着「2026-07-25 16:53 一个手写 roster 迁移直接调 purge_residents 传自己的 id 列表，销毁了 12 个玩家角色」。当时靠 `reset_builtin_residents.py:158-164` 那段手写 `update(User).values(player_resident_id=None)` 才没留下悬空指针（顺带把玩家踢回重选角色，这就是玩家 07-28 两次重选里更早那次的来源）。任何**不走这个函数**的删除（手工 SQL、admin 面板、以后新写的清理脚本）都会留下悬空指针，直接把玩家锁死。

生产库现状（只读核对，当前没有悬空，属于潜在缺陷）：
```sql
SELECT count(*) users_total, count(player_resident_id) with_prid FROM users;  -- 46 | 2
SELECT u.player_resident_id, r.id FROM users u LEFT JOIN residents r ON r.id=u.player_resident_id WHERE u.player_resident_id IS NOT NULL;
-- 2 行，r.id 均非 NULL → 无悬空
```

**复现**

1. 任意一个已完成选角色的账号（例如 `users.id='176a210c-...'`，`player_resident_id='c3c149e7-...'`）。
2. 在库里直接删掉那行 residents（生产库 users.player_resident_id **没有 FK 约束**，见 #1c，DELETE 不会被拦），或走任何绕过 `purge_residents` 的清理脚本。
3. 该账号登录 → `GET /onboarding/check` 返回 `{"needs_onboarding": false, "player_resident_id": "c3c149e7-..."}` → 前端跳 /play。
4. 必现：游戏里没有自己的角色；`/profile` 设置页角色分组空白；`PATCH /settings/character` 404；想重新选角色 → `POST /onboarding/load-preset` 400 `already has a player resident`。玩家自己无法脱困。

**修法**

两处改，都在 `backend/app/services/onboarding_service.py`：

(1) `check_onboarding_needed`（第 23-33 行）改成 JOIN 校验存在性，顺手自愈：
```python
async def check_onboarding_needed(db, user_id: str) -> dict:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")
    rid = user.player_resident_id
    if rid is not None:
        exists = (await db.execute(
            select(Resident.id).where(Resident.id == rid)
        )).scalar_one_or_none()
        if exists is None:
            # 化身已不存在 —— 悬空指针，清掉让玩家重新选，别把人锁死
            logger.warning("dangling player_resident_id user=%s resident=%s, clearing", user_id, rid)
            user.player_resident_id = None
            await db.commit()
            rid = None
    return {"needs_onboarding": rid is None, "player_resident_id": rid}
```

(2) `create_player_resident` 的第 53-54 行同样改成「指针在但行不在 → 放行重建」：
```python
if user.player_resident_id:
    still_there = (await db.execute(
        select(Resident.id).where(Resident.id == user.player_resident_id)
    )).scalar_one_or_none()
    if still_there:
        raise ValueError(f"User {user_id} already has a player resident")
    user.player_resident_id = None   # 悬空，允许重建
```

配套加两个测试：`backend/tests/test_onboarding.py` 里造一个「user.player_resident_id 指向已删 resident」的 fixture，断言 check 返回 needs_onboarding=True 且库里指针被清、断言 create-character 能成功重建。

---

### #1c · 生产库 users.player_resident_id 根本没有 FK 约束——模型声明了，003 迁移没建，schema drift

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一个函数

**根因**

模型这样声明（`backend/app/models/user.py:28-32`）：
```python
# use_alter breaks the users<->residents FK cycle ...
player_resident_id: Mapped[str | None] = mapped_column(
    String, ForeignKey("residents.id", use_alter=True), nullable=True
)
```
但生产库这一列是 Alembic 建的，而 `backend/alembic/versions/003_foundation_upgrade.py:26` 只建了裸列，没建约束：
```python
op.add_column("users", sa.Column("player_resident_id", sa.String(), nullable=True))
```
后续 51 个 revision 里没有任何一个补过（`grep -rn player_resident_id backend/alembic/versions/*.py` 只命中 003 的 add/drop 两行）。

生产库只读核对，`users` 上只有 PK 和 3 个 UNIQUE，没有任何 FOREIGN KEY：
```sql
SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='users'::regclass;
 uq_users_linuxdo_id | UNIQUE (linuxdo_id)
 users_email_key     | UNIQUE (email)
 users_github_id_key | UNIQUE (github_id)
 users_pkey          | PRIMARY KEY (id)
```
后果有两层：
- 引用完整性完全靠人手维护——只有 `backend/seed/reset_builtin_residents.py:158-164` 那一段 `update(User).values(player_resident_id=None)` 在扫尾。任何手工 SQL / 新脚本删 residents 都能造出 #1b 的悬空指针，数据库一声不吭；
- dev（SQLite + `create_all`）有 FK、prod 没有 → 单测覆盖不到这条路径，本地永远测不出来。

注意 07-27B 那批已经给 `resident_sprite_runs` 的三个 users FK 补了 `ondelete=SET NULL`（commit 104f2b1），这条是同一类问题里被漏掉的一个。

**复现**

只读一条 SQL 即可证实，无需构造：
```
docker exec deploy-db-1 psql -U postgres -d skills_world \
  -c "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='users'::regclass;"
```
输出里没有任何 `FOREIGN KEY (player_resident_id)`。对照 `backend/app/models/user.py:30-32` 声明了 FK → 模型与生产 schema 不一致，且 `alembic check` / autogenerate 因为 `use_alter` 的关系一直没把这条 diff 报出来。

**修法**

新增迁移 `backend/alembic/versions/052_add_users_player_resident_fk.py`：

```python
revision = '052_add_users_player_resident_fk'
down_revision = '051_add_civic_standing_history'

def upgrade() -> None:
    # 先清悬空，否则 ADD CONSTRAINT 会失败（当前生产 0 行，属幂等保险）
    op.execute("""
        UPDATE users u SET player_resident_id = NULL
        WHERE u.player_resident_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM residents r WHERE r.id = u.player_resident_id)
    """)
    op.create_foreign_key(
        "fk_users_player_resident_id", "users", "residents",
        ["player_resident_id"], ["id"], ondelete="SET NULL",
    )

def downgrade() -> None:
    op.drop_constraint("fk_users_player_resident_id", "users", type_="foreignkey")
```
同步把 `backend/app/models/user.py:30-32` 改成 `ForeignKey("residents.id", use_alter=True, ondelete="SET NULL", name="fk_users_player_resident_id")`，让模型和迁移的约束名/语义对齐。

`ondelete="SET NULL"` 是刻意的：删化身不该连坐删账号，指针置空后配合 #1b 的修法，玩家下次进游戏会被正常引导去重新选角色，而不是锁死。

上线纪律：这是纯迁移，**必须和任何行为/开关变更分开一次上线**（07-25 事故就在「迁移+行为变更同批」这个窗口里）。

---

### #5 · 每次进入游戏都要重新认领住房 —— 与 #1 同源：换了账号就是换了化身，新化身 home_location_id 必为 NULL

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一个函数

**根因**

住房本身**是固化的**，生产库读数为证：
```sql
SELECT slug, resident_type, home_location_id, home_decor_json FROM residents WHERE resident_type='player';
 p-沈静书 | player | apt_moon | []       ← github 那个账号，认领已落库
 p-赵启文 | player |          |          ← linux.do 那个账号，从来没认领过
```
API 日志里对应两次成功写入，之后再没有任何 PUT：
```
docker logs deploy-api-1 | grep 'PUT /residents/.*/home/decor'
  2 × "PUT /residents/p-%E6%B2%88%E9%9D%99%E4%B9%A6/home/decor" 200 OK   （= p-沈静书）
```
公开接口也确认固化：`GET https://simverse-api.proxypool.eu.org/residents` 里 `p-沈静书 → apt_moon`。

代码侧也确认 `home_location_id` 只会被写、不会被清空——全仓 `grep -rn "home_location_id\s*=" backend/app` 只有 4 处创建时赋值（`routers/residents.py:190,307`、`forge/pipeline.py:171`、`forge/legacy_pipeline.py:157,310`）和 1 处认领赋值（`backend/app/services/home_decor_service.py:128`），没有任何一处置 NULL。

所以「每次重新认领」的真实机制是 #1：换 OAuth 入口 → 另一行 users → `onboarding_service.create_player_resident` 建一个**全新的** Resident，而 `backend/app/services/onboarding_service.py:68-74` 明确传 `assign_housing=False`：
```python
district, spawn_x, spawn_y, _home = await allocate_resident_location(
    db, requested_location_id=CENTRAL_PLAZA_LOCATION_ID,
    preferred_tile=preferred_spawn,
    default_location_id=CENTRAL_PLAZA_LOCATION_ID,
    assign_housing=False,
)
```
新化身 `home_location_id=NULL` → `frontend/src/components/DecorEditor.tsx:139-140` 的 `showButton = !open && (insideHome || !mine.home)` / `buttonLabel = mine.home ? '🛋️ 装修' : '🏠 认领住房并装修'` 又变回「🏠 认领住房并装修」。玩家看到的就是「又要认领一次」。

次要放大因素（同一账号内也会误导）：认领成功后按钮只在自己家 bbox 内才出现（`DecorEditor.tsx:139` 的 `insideHome`），而全站没有任何「回家」入口（`grep -rn '回家\|goHome' frontend/src` 无命中，只有小地图点击传送），玩家站在广场上完全看不到住房存在的证据，很容易判定为「没认领上」。

**复现**

1. 用 GitHub 登录 → 选角色 → 点左下「🏠 认领住房并装修」→ 认领成功（库里 `residents.home_location_id='apt_moon'`）。
2. 退出，改用 Linux.do 登录同一个人（步骤同 #1）。
3. 重新选角色后进游戏。
4. 必现：左下角按钮又是「🏠 认领住房并装修」——因为这是另一行 users 下的另一个 Resident，`home_location_id` 为 NULL。

同账号内**不复现**：同一账号认领后刷新页面，`DecorEditor.tsx:40-48` 从 `GET /residents` 读回 `home_location_id`，按钮变成「🛋️ 装修」（生产数据 `p-沈静书 → apt_moon` 已验证）。

**修法**

主因在 #1，修完账号绑定这条自然消失，本条不需要单独动持久化逻辑。另外两处值得顺手补：

(1) 认领后给玩家一个可见的「家」的入口。在 `frontend/src/components/TopNav.tsx`（或 `MinimapOverlay`）加一个「🏠 回家」按钮，仅当 `mine.home` 非空时显示，点击复用现成的传送通道：
```ts
const b = HOUSING_BOUNDS[mine.home]
bridge.emit('minimap:teleport', {
  tileX: Math.round((b[0] + b[2]) / 2),
  tileY: Math.round((b[1] + b[3]) / 2),
})
```
（`minimap:teleport` 已由 `frontend/src/game/GameScene.ts:463-465` 接住，并在 `:519` 落库位置，不用新写传送逻辑。）

(2) `frontend/src/components/DecorEditor.tsx:37-48` 的 `useEffect` 依赖只有 `[token]`，全程只拉一次。补一个订阅：监听 WS 的 `decor_updated`（`home_decor_service.py:144-150` 已经在广播，payload 里带 `home_location_id`），命中自己的 slug 时刷新 `mine`，避免多标签页/长时间挂机后本地 `mine.home` 与库不一致。

---

### #5a · 「认领住房」根本没有独立功能，只是打开装修编辑器时的一次隐式副作用写入

**类型** `MISSING_FEATURE` ｜ **优先级** `P2` ｜ **工作量** 一个模块

**根因**

全链路只有一条认领路径，而且是装修接口的副作用：
- `backend/app/services/onboarding_service.py:68-74` 建玩家化身时 `assign_housing=False`，新玩家一律无房；
- 没有任何 `POST /housing/claim` 之类的接口（`backend/app/routers/` 下无 housing/home 路由，只有 `home_decor.py`）；
- 唯一的写入点是 `backend/app/services/home_decor_service.py:120-128`：
```python
async def set_home_decor(db, resident, user_id, items):
    if resident.home_location_id is None:
        home = await assign_player_home(db)
        if home is None:
            raise DecorError("全镇住房已满，暂时无法认领住房")
        resident.home_location_id = home
```
服务自己的 docstring 就写着这是偏离规格的将就实现（`home_decor_service.py:11-16`「Deviation from spec: onboarding creates player residents with assign_housing=False, so the first decor write lazily claims a home」）；
- 前端对应地在 `frontend/src/components/DecorEditor.tsx:72-85` 靠一次空 PUT 触发认领：
```ts
let resp = await getHomeDecor(mine.slug)
if (!resp.home_location_id) {
  resp = await putHomeDecor(mine.slug, [])   // 空的全量替换 = 认领一套房
  ...
}
```
后果：认领没有确认、没有选房、没有反馈、没有「我家在哪」的持久化 UI；分配策略是 `assign_home()`（`backend/app/agent/map_data.py:450-464`）按 `_HOUSING_ORDER` 挑第一个没满的，玩家完全没有选择权。生产现状 `apt_star` 5/5 已满、其余 8 处各 1 人，玩家实际拿到的是「系统随手塞的一间」。

**复现**

1. 新账号完成选角色（`POST /onboarding/load-preset`）。
2. 查库：`SELECT home_location_id FROM residents WHERE slug='p-<你的名字>';` → NULL。全站没有任何「认领住房」的菜单/接口。
3. 唯一入口是进游戏后左下角那个「🏠 认领住房并装修」按钮，点它 → 后台其实发的是 `PUT /residents/{slug}/home/decor {"items":[]}`。
4. 服务器直接替你选好房子，玩家全程无选择、无确认。

**修法**

把认领从装修副作用里拆出来，做成一等功能：

(1) 新增 `backend/app/routers/housing.py`：
- `GET /housing/available` → 复用 `app.agent.map_data.get_housing_locations()` + `home_decor_service.assign_player_home` 里那段占用统计，返回 `[{location_id, name, type, capacity, occupied, bounds}]`；
- `POST /housing/claim {location_id?}` → 鉴权取 `user.player_resident_id` 对应的 Resident；已有 home 直接返回当前值（幂等）；`location_id` 给了就校验该处 `occupied < capacity` 再写，没给就退回 `assign_player_home(db)`；全满返回 400「全镇住房已满」。

(2) `backend/app/services/home_decor_service.py:125-128` 的隐式认领改成显式拒绝：`if resident.home_location_id is None: raise DecorError("还没有住房，请先认领住房")`，让装修接口只做装修。

(3) 前端：`frontend/src/components/DecorEditor.tsx:72-85` 里那段空 PUT 删掉；新增一个 `HousingClaimDialog` 组件，列出 `GET /housing/available`、让玩家挑一间、调 `POST /housing/claim`，成功后沿用现有的 `bridge.emit('camera:pan', ...)` 把人送过去。`DecorEditor.tsx:140` 的按钮拆成两个：无房时是「🏠 认领住房」（开认领弹窗），有房时是「🛋️ 装修」。

(4) 可选但推荐：`backend/app/services/onboarding_service.py:73` 的 `assign_housing=False` 保持不变（让玩家自己挑房比系统硬塞好），但在 onboarding 完成后的引导里加一步「去认领你的住房」。

---

### #4 · 赛季页整页崩溃：PollCard 把 option 对象当 React child 渲染（Minified React error #31）

**类型** `BUG` ｜ **优先级** `P0` ｜ **工作量** 一个函数（后端 1 个投影函数 ~10 行 + 前端 2 行 JSX + 1 处类型 + 2 个测试）

**根因**

前后端对 poll option 的形状认知不一致，前端按 string[] 渲染，后端实际给的是 dict。

1) 后端 `/polls/open` 把 `options_json` 原样吐出：`backend/app/services/script_service.py:140`
   `"options": poll.options_json or [],`
   而 Poll 行在全库只有一个写入口 —— `backend/app/services/civic_service.py:74` 的 `propose()`，它在 `civic_service.py:62` 构造的元素恒为 dict：
   `{"label": o["label"], "effect": o.get("effect"), "npc_votes": 0}`
   再在 `civic_service.py:69/73` 往 `opts[0]` 挂 `_proposer_slug` / `_eligible_at_open`，`civic_service.py:189` 挂 `_npc_voters`。

2) 前端类型撒谎：`frontend/src/services/api/world.ts:94` 写的是 `options: string[]`（该文件从 bc8f1fe 起就没改过）。

3) 崩溃点：`frontend/src/pages/SeasonsPage.tsx:177`
   `{opt}{chosen && <span ...>✓已投</span>}`
   `opt` 是对象 → React 抛 #31。已在**线上产物**中逐字确认：`https://simverse.world/assets/SeasonsPage-mBwfgq0G.js` 里 `children:[e,i&&..."✓已投"]`，`e` 就是未取 `.label` 的原始元素。

4) 崩溃被 `frontend/src/App.tsx:87` 的 `<ErrorBoundary>` 接住，`frontend/src/components/ErrorBoundary.tsx:54` 直接把 `error.message` 打在页面上 —— 这正是玩家看到的那串「object with keys {label, effect, npc_votes, _proposer_slug, _npc_voters}」。整页（含排行榜）被替换成「💥 页面出错了」。

生产实证（只读）：
```
$ curl -s https://simverse-api.proxypool.eu.org/polls/open
"options":[{"label":"赞成兴建","effect":{...},"npc_votes":20,"_proposer_slug":"jiang-lin","_npc_voters":[...]}, ...]
```
keys 顺序与玩家报错串**逐字一致**（第一张 poll 的第一个 option 就炸）。

注意：这不是「偶发」，是**结构性必现** —— 全库没有任何路径会写 string 形状的 option。

**复现**

1. 登录 https://simverse.world（/seasons 是 ProtectedRoute，App.tsx:92）
2. 点 TopNav「🏆 赛季」（TopNav.tsx:262）
3. 只要 `/polls/open` 返回 ≥1 张 poll 就必现整页 ErrorBoundary
生产当前有 3 张 open poll（closes_at 2026-07-31 23:29:43+00），所以 100% 必现：
```
$ docker exec deploy-db-1 psql -U postgres -d skills_world -c \
  "SELECT id,status,left(question,40),closes_at FROM polls WHERE status='open';"
0f01163a...|open|在南苑空地兴建一座邮局   |2026-07-31 23:29:43+00
1dd6aa2e...|open|在东岸花园兴建一座剧院   |2026-07-31 23:29:43+00
8e96c1dd...|open|镇长选举:谁来当下一任镇长?|2026-07-31 23:29:43+00
```

**修法**

两层都要改（后端定形状 + 前端防御），并补测试。

**A. 后端 —— `backend/app/services/script_service.py`，`open_polls()` 内改 line 138-141**（同时修掉 #4b 的泄漏）：
```python
def _public_option(o) -> dict:
    """投影成对外形状。options_json 元素恒为 civic_service.propose 写的 dict，
    且 opts[0] 上挂着 effect/_proposer_slug/_npc_voters/_eligible_at_open 等
    内部 blob —— 一个都不许出网。string 分支只为兜历史/回滚数据。"""
    if isinstance(o, str):
        return {"label": o, "npc_votes": 0}
    return {
        "label": str(o.get("label", "")),
        "npc_votes": int(o.get("npc_votes") or 0),
    }

# out.append 里：
"options": [_public_option(o) for o in (poll.options_json or [])],
```
安全性已核：`open_polls()` 的消费方只有两处 —— `backend/app/routers/polls.py:38` 与 `backend/app/routers/townhall.py:82`（后者只读 `p["question"]` 做 ELECTION_TAG 过滤，前端 TownHallPanel.tsx:171 只读 `.label`），都不依赖 `effect`。`_close_one`/`office_audit.py:177` 读的是 ORM 上的 `poll.options_json`，不受影响。

**B. 前端类型 —— `frontend/src/services/api/world.ts:90-97`**：
```ts
export interface PollOption { label: string; npc_votes?: number }
export interface PollData {
  id: string
  season_id: string | null   // 生产实测返回 null，原来写的 string 也是错的
  question: string
  options: PollOption[]
  closes_at: string | null
  my_vote?: number
}
```

**C. 前端渲染 —— `frontend/src/pages/SeasonsPage.tsx:155` 与 `:177`**：
```tsx
{poll.options.map((opt, idx) => {
  const label = typeof opt === 'string' ? opt : (opt?.label ?? `选项 ${idx + 1}`)
  const chosen = state?.kind === 'voted' && state.idx === idx
  ...
      {label}{chosen && <span style={{ marginLeft: 8, fontWeight: 600 }}>✓已投</span>}
```
保留 string 分支，使后端未部署/回滚时页面仍不炸。

**D. 测试（否则会再犯）**：
- 新增 `frontend/src/pages/SeasonsPage.test.tsx`：mock `getOpenPolls` 返回 `options: [{label:'赞成',effect:{...},npc_votes:20,_proposer_slug:'x',_npc_voters:['a']}]`，断言渲染出「赞成」且不抛。
- 后端补 `assert '_npc_voters' not in json.dumps(resp)` 断言。
参照物已存在：`frontend/src/components/TownHallPanel.test.tsx:23` 用的就是 `{label:'修'}` 对象形状 —— 两个组件对同一 API 的形状认知本就分裂，SeasonsPage 缺测试是这个 bug 活到生产的直接原因（frontend/src/pages 下只有 LandingPage/LoginPage 两个 test）。

---

### #2 · 议案投票：后端和前端都实装了，但唯一带投票按钮的入口被 #4 的崩溃全挡死 —— 所以 votes 表永远是 0

**类型** `BUG` ｜ **优先级** `P0` ｜ **工作量** 一行（随 #4 的修复一并生效），但验收需完整跑一遍真实投票路径

**根因**

不是功能缺失。链路逐段核过，**只差最后一步渲染**：

- 投票接口存在且在线：`backend/app/routers/polls.py:67` `POST /polls/{poll_id}/vote`，已在 `backend/app/main.py:182` 注册。生产实测无 token 返回 **401**（不是 404），证明路由活着：
  `curl -X POST https://simverse-api.proxypool.eu.org/polls/0f01163a-.../vote -d '{"option_idx":0}'` → `401`
- 落库逻辑完整：`backend/app/services/script_service.py:154 cast_vote()` 做了 open 校验、截止校验、option_idx 越界校验、重复投票预检 + 唯一约束兜底。
- 计票会算玩家票：`backend/app/services/civic_service.py:485-495 _close_one()`，`tally[i] = npc_votes + player_votes[i]`；`close_due_polls`（civic_service.py:467）扫的是**全部** open poll，镇长选举也在内。
- 前端 API 客户端存在：`frontend/src/services/api/world.ts:111 votePoll()`，带 Authorization（core.ts:15）。
- 前端投票 UI 存在：`frontend/src/pages/SeasonsPage.tsx:116-188 PollCard`，`onClick={() => vote(idx)}`（:160）、乐观锁 `locked`、`my_vote` 回填、`already voted` 分支处理，都写了。

**唯一断点**：`PollCard` 的 option 循环在渲染 label 那一行就抛 React #31（见 #4），组件树在 onClick 挂上去之前就被 ErrorBoundary 换掉了 —— 按钮从来没在 DOM 里出现过。所以「一张玩家票都没有」是崩溃的必然结果，不是接口缺失。

生产实证（只读）：
```
$ docker exec deploy-db-1 psql -U postgres -d skills_world -c 'SELECT count(*) FROM votes;'
 votes_total
 0
```
对照：同期 NPC 票有 20/19/17 张（存在 options_json[i].npc_votes 里，不走 votes 表），说明 NPC 侧治理闭环是通的，缺的纯粹是玩家侧的那个按钮能不能画出来。

**复现**

同 #4：进 /seasons → 整页 ErrorBoundary → 页面上根本没有可点的选项按钮。
反证接口可用：拿任意有效 token 直接打
`POST /polls/0f01163a-3a2c-4851-a00a-da501260e06a/vote {"option_idx":1}` 会返回 `{"ok":true}` 并写入 votes（**请勿在 vm212 上执行，会写库**）。

**修法**

**修完 #4 的 A/B/C 三处，#2 自动通。**不需要新写任何投票逻辑。

修完后必须做的运行时验证（不是跑单测就算完）：
1. 本地或预发起前端，登录后进 /seasons，确认 3 张议案渲染出中文 label；
2. 点一个选项 → Network 面板看到 `POST /polls/{id}/vote` 200 `{"ok":true}`；
3. 刷新页面，确认 `my_vote` 回填出「✓已投」（走的是 script_service.py:143-150 那段）；
4. 再点一次，确认走 `already voted on this poll` 分支显示「已投过」（SeasonsPage.tsx:133）；
5. 本地库 `SELECT * FROM votes;` 见到真实行。

补一条回归护栏：后端加集成测试 —— propose 一张 civic poll（dict 选项）→ 打 `/polls/open` → 断言 `options[0]` 只有 `label`/`npc_votes` 两个 key，杜绝形状再次漂移。

---

### #4b · 内部字段泄漏：/polls/open 与 /townhall/overview 把 effect / _proposer_slug / _npc_voters 全量吐给未登录客户端

**类型** `BUG` ｜ **优先级** `P1` ｜ **工作量** 一个函数（与 #4 的 A 步同一处改动）+ townhall.py:107 一处同类投影

**根因**

与 #4 同一行根因：`backend/app/services/script_service.py:140` 原样返回 `options_json`，而 `civic_service` 把三类内部数据都挂在这个 blob 上：
- `civic_service.py:69` `_proposer_slug` —— 哪个 NPC 提的案
- `civic_service.py:189` `_npc_voters` —— **每张票的投票人 slug 全名单**（25 个 slug，含玩家角色名如「夜风侦探-46ff1f」「部署回归图灵0724」）
- `civic_service.py:62` `effect` —— 未落地建筑的 `bounds`/`center`/`entrance` 坐标与描述，本应在议案通过后才公开
- `civic_service.py:73` `_eligible_at_open` —— 法定人数分母快照

泄漏面**不止赛季页一个接口**，`/townhall/overview` 走同一个 `open_polls()`（`backend/app/routers/townhall.py:82`），生产实测：
```
$ curl -s .../townhall/overview | jq '.open_polls[0].options[0] | keys'
['label','effect','npc_votes','_proposer_slug','_npc_voters']
```
两个接口都**不要求鉴权**（polls.py:34 的 auth 是可选的，仅用于回填 my_vote），任何人 curl 即得。

**复现**

匿名 `curl -s https://simverse-api.proxypool.eu.org/polls/open`（或 `/townhall/overview`）→ 响应里直接出现 `_npc_voters` 全名单与未落地建筑坐标。无需登录。

**修法**

就是 #4 的 A 步 —— 在 `open_polls()` 里用 `_public_option()` 做**白名单投影**（只留 `label` + `npc_votes`），而不是黑名单剔除 `_` 前缀字段（黑名单挡不住将来新增的非下划线内部键，比如 `won`/`final_votes`/policy 的 `META_*`）。

若产品上希望公示提案人，应显式加一个 `proposer_name`（查 Resident.name 后返回展示名），而不是把 slug 直接漏出去。

额外核一处：`backend/app/routers/townhall.py:107` 的 `_recent_election` 也是 `"options": opts` 原样返回（已结票的选举 poll），当前生产 `recent_election` 为 null 所以没暴露，但一旦有已结束选举就会漏同样的 `_npc_voters`。建议一并用同一个 `_public_option()` 投影（结票场景额外放行 `won`/`final_votes`）。

---

### #2b · 市政厅「议案投票」tab 只有展示没有投票按钮（设计即只读）

**类型** `MISSING_FEATURE` ｜ **优先级** `P2` ｜ **工作量** 方案 1 一行；方案 2 一个模块（抽共享组件 + townhall.py 传 user_id + 两处接入 + 测试）

**根因**

如果玩家说的「议案投票」指的是市政厅那个同名 tab（`frontend/src/components/TownHallPanel.tsx:16` `{ key: 'poll', label: '议案投票' }`），那它**从来就没打算能投**：
- 组件头注释 `TownHallPanel.tsx:5-8` 明写「read-only 市政厅 … there are NO write actions here」，弹窗副标题 `TownHallPanel.tsx:87` 也写着「只读公示」。
- `PollTab`（`TownHallPanel.tsx:158-178`）只渲染 question + label 药丸 + 截止时间，没有 onClick、没有 import votePoll。
- 客户端 `frontend/src/services/api/townhall.ts:3-5` 注释同样声明「there are no write endpoints here」。

注意 PollTab 的 label 取值写的是 `String((o as { label?: unknown }).label ?? '')`（TownHallPanel.tsx:171）—— 它**正确处理了对象形状**，所以市政厅这个 tab 不崩，只是不能投。这也反证 SeasonsPage 是漏改的那一个。

结论：tab 名叫「议案投票」但实际是「议案公示」，命名本身就在误导玩家 —— 玩家报「没实装」完全合理。

**复现**

游戏内打开 🏛️ 市政厅 → 切到「议案投票」tab → 看到 2 条议案（选举被 townhall.py:83-85 的 ELECTION_TAG 过滤掉了）与选项药丸，全程没有任何可点元素。

**修法**

二选一，先拍板再动手：

**方案 1（低成本，立刻消歧义）**：把 tab 文案从「议案投票」改成「议案公示」（`TownHallPanel.tsx:16`），并在 PollTab 底部加一行跳转 —— `<button onClick={() => { setOpen(false); navigate('/seasons') }}>前往投票 →</button>`，把玩家导到真正能投的赛季页。

**方案 2（对齐玩家预期）**：把 SeasonsPage 的 `PollCard` 抽成共享组件 `frontend/src/components/PollCard.tsx`（含 votePoll 调用 + 已投状态 + my_vote 回填），SeasonsPage 与 TownHallPanel PollTab 同时引用。前提是 `/townhall/overview` 的 poll 也带 `my_vote` —— 后端 `townhall.py:82` 调 `open_polls(db)` 时**没传 user_id**，需改成从 Authorization 解析用户后传入（照抄 `polls.py:34-37` 那 4 行）。

推荐方案 2 —— 玩家是在市政厅场景里找投票入口的，跳转到另一个页面体验割裂。

---

### #2d · 开放议案不显示当前票数，玩家投完看不到任何反馈

**类型** `MISSING_FEATURE` ｜ **优先级** `P2` ｜ **工作量** 一个函数（后端一次 group-by 聚合 + 前端一段渲染）

**根因**

数据一直有，只是没人渲染：`options_json[i].npc_votes` 生产实测已累到 20/19/17（见 #4 的 SQL），但
- `frontend/src/pages/SeasonsPage.tsx:155-180` 的按钮只画 label，不画票数；
- `frontend/src/components/TownHallPanel.tsx:163-177` 的药丸同样只画 label；
- 玩家票（votes 表）在结票前**根本没有任何接口聚合**，`script_service.py:138-141` 的响应里没有 per-option 玩家票计数字段，只有 `my_vote`（自己投了哪个）。

后果：即使修完 #4/#2，玩家点完按钮只会看到一个「✓已投」，既不知道自己那票占多少分量，也不知道议案会不会过 —— 治理玩法没有反馈回路。

**复现**

修完 #4 后进 /seasons 投票 → 按钮只变色打勾，页面上找不到任何票数/进度条。对照 `/polls/open` 响应里其实带着 `npc_votes: 20`。

**修法**

**后端**：在 #4 的 `_public_option()` 里保留 `npc_votes`（已含在上面给的实现里），并在 `open_polls()` 的 `out.append` 之后补一段玩家票聚合，与现有 `my_vote` 查询合并成一次查询：
```python
# 现有 my_vote 查询同一批 poll_id，顺手 group by 出 per-option 玩家票
rows = (await db.execute(
    select(Vote.poll_id, Vote.option_idx, func.count())
    .where(Vote.poll_id.in_([p["id"] for p in out]))
    .group_by(Vote.poll_id, Vote.option_idx)
)).all()
player = {}
for pid, idx, n in rows:
    player.setdefault(pid, {})[idx] = n
for p in out:
    counts = player.get(p["id"], {})
    for i, o in enumerate(p["options"]):
        o["player_votes"] = counts.get(i, 0)
        o["total_votes"] = o["npc_votes"] + counts.get(i, 0)
```
注意这段要放在 `my_vote` 那段（script_service.py:143-150）之外，因为它**不依赖 user_id**（匿名也该看到票数）。

**前端**：`SeasonsPage.tsx` 的选项按钮右侧加 `{opt.total_votes} 票` + 一条按占比的背景进度条；`PollData` 的 `PollOption` 加 `player_votes?: number; total_votes?: number`。

口径先拍板：是否要在投票期间实时公开票数（可能诱导从众投票）。若要藏，就只在 `my_vote != null` 时展示。

---

### #2c · 玩家发起议案（POST /polls/propose）前端完全没有入口

**类型** `MISSING_FEATURE` ｜ **优先级** `P3` ｜ **工作量** 一个功能（API 客户端 + 表单弹窗 + 校验/错误态 + 测试；后端零改动）

**根因**

后端 `backend/app/routers/polls.py:41-64` 实现了 `POST /polls/propose`（含 ≥2 选项校验、非 admin 自动剥离 effect 变成咨询性投票、civic_polls_enabled 关闸返回 403），但全前端搜不到任何调用方：
```
$ grep -rn 'propose|/polls' frontend/src --include=*.ts --include=*.tsx | grep -v test
frontend/src/services/api/world.ts:108:  return apiFetch('/polls/open')
frontend/src/services/api/world.ts:112:  return apiFetch(`/polls/${pollId}/vote`, ...)
```
即 API 客户端里连 `proposePoll()` 这个函数都没写。当前生产 3 张议案的提案人全是 NPC（`_proposer_slug` = jiang-lin / zhou-dahe），玩家侧提案量必然为 0。

**复现**

全站找不到任何「发起议案 / 提案」按钮；`frontend/src/services/api/world.ts` 无 propose 相关导出。

**修法**

1. `frontend/src/services/api/world.ts` 加：
```ts
export interface ProposeOption { label: string }
export function proposePoll(topic: string, options: ProposeOption[], days?: number):
  Promise<{ ok: boolean; poll_id: string }> {
  return apiFetch('/polls/propose', {
    method: 'POST',
    body: JSON.stringify({ topic, options, days }),
  })
}
```
2. 在 SeasonsPage 投票区上方（或 #2b 方案 2 落地后的市政厅 PollTab 里）加「＋ 发起议案」按钮 → 弹窗表单：议题一行 + 动态选项列表（默认 2 行，可增删）+ 天数选择。提交前本地拦 `options.length < 2`（对齐 polls.py:52）。
3. 错误分支要处理 403「civic polls are disabled」（polls.py:63），文案给「当前镇务征询已关闭」。
4. 玩家提的案 effect 会被后端剥成 null（polls.py:57），是**咨询性投票**，UI 上要明说「本案仅作民意征询，不会自动改变小镇规则」，否则玩家会误以为自己能直接改世界。

注意这是纯新增功能，建议独立立项，不要塞进 #4/#2 的修复里 —— 那两个是止血，这个是开新玩法。

---

### #3-1 · 辩论生命周期没有任何驱动器，永远卡在 announced，玩家押注的币被永久冻结

**类型** `BUG` ｜ **优先级** `P0` ｜ **工作量** 一个函数（约 30 行）+ event_cron 6 行接线，不动 schema；含测试 0.5 天

**根因**

run_live() 在 backend/app/services/debate_service.py:143、settle() 在 backend/app/services/debate_service.py:213，两者在 app 代码里零调用方（全仓 grep run_live 只命中定义 + backend/tests/test_debates.py:158/172）。生产代码里唯一被调用的是 create_debate()（backend/app/services/debate_service.py:51），调用方只有 backend/app/services/civic_service.py:768。event_cron / nightly_cron / heat_cron / agent loop 都没有推进 announced→live→voting→settled 的代码。作者自己在 backend/app/services/debate_service.py:57-59 注释承认 'the debate lifecycle stops here today (no live/settle driver in app code)'，backend/app/services/opinion_service.py:11 也写了 'debate lifecycle is only half-wired today'。后果：debate 建出来就是 announced，不会产生辩词、不会开投票、不会结算，押注钱已 charge 但 payout 永远 NULL。

**复现**

curl -s https://simverse-api.proxypool.eu.org/debates 返回唯一一场 1c00ba36-f507-4287-ab0e-b0042f73f540，status=announced，transcript=[]。生产库只读：SELECT count(*),min(starts_at),max(starts_at) FROM debates; → 1 | 2026-07-26 06:05:34.078936+00 | 同上。SELECT status,count(*) FROM debates GROUP BY status; → announced|1。SELECT debate_id,amount,payout FROM debate_stakes; → 1c00ba36…|10|NULL，玩家 10 SC 冻结已 2 天。

**修法**

在 backend/app/services/debate_service.py 新增 drive_due_debates(db)：查 status=='announced' 且 starts_at <= now-30min 的行逐个 await run_live(db,d)；再查 status=='voting' 且 starts_at <= now-90min 的行逐个 await settle(db,d.id)，每个 for 体内单独 try/except 记 warning。然后在 backend/app/tasks/event_cron.py 第 61 行（C3 script/season 块）之后接线：from app.services.debate_service import drive_due_debates; moved = await drive_due_debates(db)，外面包 try/except。注意 Debate 表只有 starts_at/settled_at（backend/app/models/debate.py:30-31），没有记录进入 voting 的时刻，所以 settle 判据用 starts_at+30min+60min 推算；想更干净就加一列 phase_at（新 alembic revision），但上面这版不动 schema 即可上线。上线后第一个 tick 会把卡住的 1c00ba36 推走，玩家 10 SC 自动解冻。

---

### #3-2 · 玩家无法新建辩论：后端没有 POST /debates，前端也没有入口

**类型** `MISSING_FEATURE` ｜ **优先级** `P1` ｜ **工作量** 一个模块：后端 1 路由 + 前端 1 API + 1 表单组件，约 1 天（含节流/收费与测试）

**根因**

backend/app/routers/debates.py 一共只注册 4 条路由：GET ''(:45)、GET /{debate_id}(:51)、POST /{debate_id}/stake(:59)、POST /{debate_id}/vote(:69)，没有创建路由。create_debate() 只被 backend/app/services/civic_service.py:768 内部调用，从未暴露到 HTTP 层。前端同理：frontend/src/services/api/world.ts:145-168 只有 getDebates/getDebate/stakeDebate/voteDebate；frontend/src/pages/DebatesPage.tsx 全页无发起表单，列表空态只有一句『暂无辩论，等居民们吵起来再来看吧』。属于从来没实现过，不是坏掉。

**复现**

curl -s https://simverse-api.proxypool.eu.org/openapi.json 里 debate 相关 path 只有 /debates(get)、/debates/{debate_id}(get)、/debates/{debate_id}/stake(post)、/debates/{debate_id}/vote(post)。curl -X POST https://simverse-api.proxypool.eu.org/debates -d '{}' → HTTP 405（Method Not Allowed，而非 401/422），证明路由不存在。

**修法**

1) backend/app/routers/debates.py 在 _view 之后加 POST ''：Pydantic CreateBody{topic, resident_a_slug, resident_b_slug}；校验 2<=len(topic)<=60、两个 slug 不同、两个 slug 都能在 residents 表里查到且 is_autonomous；节流 SELECT count(*) FROM debates WHERE status IN ('announced','live','voting') 大于等于 3 就 400；收费 await charge(db,user.id,50,'debate_create')；然后 d = await create_debate(db, topic, a, b) 并 return _view(d)。每人每日次数用 Redis key sv:debate_create:{user_id}:{date} SETNX+EXPIRE。2) frontend/src/services/api/world.ts 加 createDebate(topic,a,b) 走 apiFetch('/debates',{method:'POST',...})。3) frontend/src/pages/DebatesPage.tsx 左栏标题下加『＋发起辩论』按钮 + topic 输入框 + 两个居民下拉，提交后 loadList() 并 setSelectedId(新 id)；错误映射在 DETAIL_ZH（backend/app/routers/debates.py 返回的 detail 串）里补 'too many debates in progress' → '当前进行中的辩论已达上限'。

---

### #3-3 · 辩论产生频率被钉死在每 7 天最多 1 场，且只有 1 个居民能触发

**类型** `WORKING_AS_DESIGNED` ｜ **优先级** `P1` ｜ **工作量** (a) 一行/一个 config 旋钮；(b) 一个函数（约 40 行）+ nightly 接线；(c) 见 #3-2

**根因**

生产里 debate 只有唯一一条产生链，每一环都在收窄：(1) backend/app/tasks/event_cron.py:43-50 只在世界事件 phase=='end' 时尝试 spawn（phase 字符串见 backend/app/services/world_event_service.py:108）；(2) backend/app/services/civic_service.py:733-771 maybe_spawn_lecture_debate，:737 要求 settings.civic_polls_enabled（backend/app/config.py:527 默认 True，vm212 .env 未覆盖，这环没卡），:740 要求 payload_json['duty']=='lecturer'，全世界只有『公开课』带这个标记；(3) 公开课由 backend/app/services/duty_service.py:305-331 _work_lecturer 产生，:309 cooldown_days = perk(resident,'lecture_cooldown_days',7)，生产 residents.meta_json 实测 gu-mingyuan 的 perks 就是 {'lecture_cooldown_days': 7}，即同一讲师 7 个真实日只能开一次课；(4) 上面还叠 backend/app/services/duty_service.py:126-141 on_work 的 Redis 冷却 DUTY_WORK_COOLDOWN_HOURS=20（同文件:41）以及『那一 tick 恰好选中 WORK』的概率；(5) 全镇 11 个 duty 里 lecturer 唯一，只有 gu-mingyuan 持有。理论上限=每 7 天 1 场，实际更少。设计如此，但参数值导致玩家侧表现为『很随机、隔很久才有一场』。

**复现**

生产库只读：SELECT id,title,starts_at,ends_at,payload_json FROM world_events WHERE title LIKE '%公开课%'; → 只有 1 行『顾明远的公开课』2026-07-26 00:05:06+00 → 06:05:06+00，payload {'location_id':'academy','duty':'lecturer'}。SELECT count(*) FROM debates; → 1。公开课 1 条对辩论 1 条严格 1:1，辩论 starts_at 06:05:34 正好落在公开课 ends_at 06:05:06 之后第一个 event_cron tick（60s 周期）。居民 roster 是 2026-07-25 16:53 重建的，07-26 00:05 是第一次可开课，下一次要等 08-02。

**修法**

(a) 最小旋钮：把 lecture_cooldown_days 从 7 降到 1~2 —— 改 residents.meta_json seed，或把 backend/app/services/duty_service.py:309 的默认值 7 换成 settings.lecture_cooldown_days（进 config.py + .env.example）。(b) 解耦触发源：在 backend/app/services/debate_service.py 加 maybe_spawn_daily_debate(db)，用现成的 issue_stances 表选题（取当日 |stance| 分歧最大的 issue_key 当 topic，取该 issue 上正负两端的两个居民做 a/b，backend/app/services/opinion_service.py 已有 issue_key 规范化与 stance 读写），并加『进行中的辩论 < N 场才开』的节流；然后在 backend/app/tasks/nightly_cron.py 的 civic 块之后接线，形态照抄 :213-221 那段 try/except。(c) 配合 #3-2 让玩家自己开。建议 (a)+(c) 先上，(b) 作为内容侧增强。

---

### #3-4 · 辩题永远是『关于「<讲师名字>」的争论』——把讲师名当成了讲题

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一个函数（两处各改 5 行）

**根因**

backend/app/services/civic_service.py:766 写的是 topic = event.get('title','小镇议题').replace('的公开课','')，而这个 title 由 backend/app/services/duty_service.py:324 生成为 f'{resident.name}的公开课'，里面根本不含讲题、只有讲师姓名 + 固定后缀。后缀被 replace 掉剩下就是『顾明远』，于是 backend/app/services/civic_service.py:768 拼出 f'关于「{topic}」的争论' = 『关于「顾明远」的争论』。讲题本身一直没被建模：backend/app/services/duty_service.py:325 的 description 是写死的一句话，:326 的 payload_json 只有 location_id/duty。

**复现**

生产库只读：SELECT topic FROM debates; → 关于「顾明远」的争论。玩家在 /debates 页看到的辩题就是这句，而两位辩手 su-xiaoman / zhao-qiwen 跟顾明远毫无关系。

**修法**

两步：(1) backend/app/services/duty_service.py:322-329，加一个静态题库 LECTURE_TOPICS（成本 0），random.choice 出 subject，把 title 改成 f'{resident.name}的公开课：{subject}'、description 里带上讲题、payload_json 加 'topic': subject；(2) backend/app/services/civic_service.py:766 改成优先读 payload.get('topic')，其次 event['title'].split('：',1)[-1]，若结果等于『title 去掉的公开课后缀』（说明是老格式数据）就退化成通用议题字符串而不是讲师名。

---

### #6-1 · 村落日报正文被写成空字符串：compose_digest 绕过 llm.client.chat()，thinking 没关 + max_tokens 800 把输出吃光

**类型** `BUG` ｜ **优先级** `P0` ｜ **工作量** 一个函数（compose_digest 重写约 20 行）+ 顺手改 generate_weekly_recap；半天含测试

**根因**

backend/app/services/digest_service.py:153-167 的 compose_digest 直接调 client.messages.create(model=..., max_tokens=800, ...)（:156），绕过了 backend/app/llm/client.py:116 的 chat() 包装。而 chat() 是全仓唯一会加 kwargs['thinking'] = {'type':'disabled'} 的地方（backend/app/llm/client.py:148-149，条件 not settings.llm_thinking；backend/app/config.py:55 llm_thinking 默认 False，vm212 .env 未设 LLM_THINKING）。backend/app/llm/client.py:129 的 docstring 本身就写着 'Use this instead of client.messages.create() directly.'。结果 digest 这条路径的推理没被关掉，800 token 预算被吃掉，响应里没有可用 text block；backend/app/services/digest_service.py:32-36 那份本地 _extract_text 找不到带 .text 的块就 return ''。:215 拿到空串后 :217-222 原样构造 Digest 并 commit，全程没有非空校验。另外 800 这个上限对 DIGEST_SYSTEM（:26-29）要求的『3-5 段、不超过 600 字中文 + 标题』本来就不够。

**复现**

生产库只读，把 token 用量和正文长度对齐，相关性 100%：SELECT ts, output_tokens, (SELECT length(content_md) FROM digests d WHERE d.scope='village' AND d.created_at BETWEEN u.ts - interval '5 s' AND u.ts + interval '5 s') AS digest_len FROM llm_usage u WHERE scenario='digest' AND model='deepseek-v4-flash' ORDER BY ts; → 07-16|412|601, 07-17|801|0, 07-18|633|453, 07-19|762|566, 07-20|676|483, 07-21|801|409, 07-22|801|256, 07-23|730|533, 07-24|801|0, 07-25|801|0, 07-26|801|0, 07-27|801|546。4 条空正文（07-17/24/25/26）全部落在 output_tokens=801（max_tokens 800 触顶）的 7 条里；5 条未触顶的全部有正文。触顶但非空的 07-27 正文也断在半句：SELECT right(content_md,60) → '…阳光从云缝里漏下来，照在铁匠铺的铜扣上。周大河拍拍我的肩膀'（无句末标点）。公告栏同步遭殃：SELECT title,length(content_md) FROM bulletin_posts WHERE kind='digest' 在 07-24/25/26 三条也是 len=0。

**修法**

改 backend/app/services/digest_service.py：顶部 import 换成 from app.llm.client import chat as llm_chat 和 from app.llm.metering import Meter，加常量 DIGEST_MAX_TOKENS = 2000；compose_digest 内部改为 text = (await llm_chat(DIGEST_SYSTEM, [{'role':'user','content':_build_prompt(day, material)}], model=settings.effective_model, max_tokens=DIGEST_MAX_TOKENS, owner='system', meter=Meter(scenario='digest'))).strip()，删掉原来的 client.messages.create + record_usage 两行（llm_usage 由 wrapper 统一记）。标题解析逻辑（:162-166）保持不变。这样一次拿到三件事：thinking:{'type':'disabled'} 被带上、用 backend/app/llm/client.py:83 的 extract_text（显式跳过 ThinkingBlock）、计量统一。本地 _extract_text（:32-36）和 get_client 在 village 路径上可删；generate_weekly_recap（:330）是同一个毛病，建议一并改。红线提醒：这是行为变更，不要和补数据的写操作放同一次上线。

---

### #6-2 · 空正文照样落库，且 (scope,date,user_id) 幂等把空行永久钉死，日报再也不会自愈

**类型** `BUG` ｜ **优先级** `P0` ｜ **工作量** 一个函数（generate_village_digest 重构约 25 行）+ 1 个异常类；半天含测试

**根因**

backend/app/services/digest_service.py:211-229 只判断了 material['has_material']（冷启动兜底文案），没有判断 LLM 返回的 content 是否为空，:217-222 无条件 db.add(Digest(...)) 并 commit。落库后 :204-208 的幂等早返回只看行是否存在、不看正文是否为空（existing is not None 就 return existing），配合表上的 uq_digest_scope_date_user 唯一约束，一天写空一次就永久空。而 generate_village_digest 的唯一调用方是 backend/app/tasks/nightly_cron.py:159（每天 07:00 北京只跑一次），没有任何 admin/脚本入口能重生成。玩家侧链路：backend/app/routers/digest.py:32-38 GET /digest/latest 按 date 倒序取最新一条 → frontend/src/components/DigestModal.tsx:48-52 拿到 content_md='' 后渲染 <ReactMarkdown>{''}</ReactMarkdown> 得到空白，且因为 digest 对象非 null，:53 那句『还没有日报，明天早上再来看看吧』也不会显示，于是玩家看到只有日期、正文全空的面板，连开几天都一样。

**复现**

生产库只读：SELECT date,title,length(content_md) AS len,created_at FROM digests WHERE scope='village' ORDER BY date; → 2026-07-24|2026-07-24 村落日报|0|2026-07-24 00:30:09+00；2026-07-25|…|0|2026-07-25 15:33:04+00；2026-07-26|…|0|2026-07-26 23:00:09+00；2026-07-27|雷雨中的小镇…|546|2026-07-27 23:00:10+00。即从 07-24 00:30 UTC 写下空行起，/digest/latest 连续返回空正文，直到 07-27 23:00 UTC（=07-28 07:00 北京）才出有内容的一条 —— 玩家连着约 4 天打开日报都是空白面板。cron 本身没问题：07-10~07-27 每天一行共 18 行无缺口。

**修法**

backend/app/services/digest_service.py 的 generate_village_digest 两处改：(1) 幂等条件加『正文非空』：if existing is not None and (existing.content_md or '').strip(): return existing —— 只有有正文才算已完成；(2) compose 之后判空：if not content.strip(): logger.error('digest compose returned empty text for %s (material=%s)', day, material['stats']) 并抛自定义异常 DigestComposeEmpty（backend/app/tasks/nightly_cron.py:161-162 已有 try/except 会记 'Nightly village digest failed'，至少留告警而不是静默写空）；(3) 落库分支拆成两路：existing is not None 时走 UPDATE 回填（existing.title/content_md/stats_json 赋值后 commit+refresh，避开唯一约束），否则才 INSERT，IntegrityError 分支（:224-228）保留。加固建议：compose_digest 失败时在同一次调用里用 1.5 倍 max_tokens 重试一次。

---

### #6-3 · 存量 4 天空日报无法自愈，也没有任何手动重生成入口

**类型** `MISSING_FEATURE` ｜ **优先级** `P2` ｜ **工作量** 一行到一个函数：admin 路由 8 行，或脚本 30 行

**根因**

generate_village_digest（backend/app/services/digest_service.py:201）的唯一调用方是 backend/app/tasks/nightly_cron.py:159（每天一次且只传 today）。backend/app/routers/digest.py 只有 3 个 GET（:19 /weekly/me、:32 /latest、:41 ''），没有任何 POST/regenerate；backend/scripts/ 下 23 个脚本也没有 digest 相关。即便按 #6-2 修好逻辑，2026-07-17/24/25/26 这 4 条已存在的空行也只会在同一 day 再次被调用时才走回填分支，而历史日期永远没人喂。

**复现**

grep -rn generate_village_digest backend/ --include=*.py 只命中 backend/app/tasks/nightly_cron.py:159、backend/app/services/digest_service.py:201 定义、以及 backend/tests/test_digest.py。curl -s https://simverse-api.proxypool.eu.org/openapi.json 里 digest 相关只有 3 个 get。

**修法**

二选一：(1) 在现有 admin 路由里加 POST /admin/digest/regenerate?date=YYYY-MM-DD，内部 await generate_village_digest(db, date_type.fromisoformat(date)) 并 return serialize(d)，鉴权沿用现成的 require_admin 依赖；(2) 或写 backend/scripts/regenerate_digest.py，参数 --date YYYY-MM-DD [--force]，内部先把该行 content_md 清成 '' 再调 generate_village_digest（配合 #6-2 的回填分支）。上线顺序按红线：先发 #6-1/#6-2 的代码修复并观察一晚新日报非空，再单独发一次补数，不要在同一次变更里既改行为又写库。

---

### #8 · 居民答应「带我去某地」后无任何行动 —— 系统层根本没有「带玩家去某地」这个能力

**类型** `MISSING_FEATURE` ｜ **优先级** `P1` ｜ **工作量** 护栏版一行（prompt.py 一处）；完整功能一个模块：新 action + 新表/迁移 + escort_service + prompts 扩字段 + decide/execute 两处接线 + 前端一个 WS 事件渲染，约 6 个 step

**根因**

对话与行动完全解耦，且动作集里不存在任何以玩家为目标的动作。四条证据链，全部读过代码确认：

1) 动作枚举里没有带路。`backend/app/agent/actions.py:6-33` — ActionType 只有 16 个成员：CHAT_RESIDENT / CHAT_FOLLOW_UP / GOSSIP / WANDER / VISIT_DISTRICT / GO_HOME / OBSERVE / EAVESDROP / REFLECT / JOURNAL / WORK / STUDY / IDLE / NAP / RESEARCH / EAT。没有 ESCORT/GUIDE/FOLLOW。`actions.py:40` 注释写死 `target_slug: Resident slug if social action` —— 目标只能是居民，玩家不是合法目标。

2) 感知层看不见玩家。`backend/app/agent/phases/perceive/basic.py:22-35` 只 `select(Resident)` 填 `ctx.nearby_residents`；玩家仅在 `perceive/basic.py:45-52` 经 `witness_service.record_witnesses` 写一条「路过」记忆，从不进入可交互目标集。`backend/app/agent/prompts.py:37-45` 的 DECISION_USER「附近的居民」因此永远只列居民。

3) 玩家对话产生零 side effect。`backend/app/ws/handlers/chat.py:254-283` 走 `ModelRouter.chat_with_media` 纯文本流式；`backend/app/llm/client.py:116-125` 的 `chat()` 与 `stream_chat()` 签名里没有 `tools` 参数，全仓 grep `tool_calls|function_call|tools=` 在 app/llm、app/media、app/ws 下零命中。玩家一次对话的全部副作用只有：扣币、写 Message 行、`reward_creator_passive`、以及 end_chat 后的事后记忆抽取。没有任何动作派发。

4) 即使 LLM 的承诺被写进了记忆，决策循环也读不到。`backend/app/agent/phases/decide/basic.py:341-347` `_load_memories` 只取 `limit=10` 的最新 event 记忆；生产实测 tick 循环每居民每小时写 18.8~23.8 条 event 记忆（见 repro SQL），承诺记忆约 30 分钟内就被冲出决策窗口。`backend/app/agent/phases/plan/basic.py` 的规划侧取 `limit=20` 再过滤 `importance > 0.5`，而这条承诺记忆生产实测 importance=0.4，被硬过滤掉。

5) 本次玩家谈话对象 `p-沈静书` 是 `resident_type='player'` 的玩家化身（生产库 residents 表），`backend/app/models/resident.py:93-111` 的 `is_autonomous` 混合属性把 player 类型排除在 SIM_RESIDENT_TYPES 之外，`app/agent/loop.py:60/138/314` 全部按 is_autonomous 过滤 —— 该 resident 从未被 tick 过（生产库 agent_action 记忆数 = 0）。就算有 ESCORT 动作，这个对象也不会动。

结论：这不是坏掉的功能，是从来没实现过的功能。LLM 在对话里答应了，系统层没有对应能力。

**复现**

必现，100%。

最小路径：WS 连上 → `{"type":"start_chat","resident_slug":"<任意居民>"}` → `{"type":"chat_msg","text":"带我去五金店找镇长"}` → LLM 会答应 → `{"type":"end_chat"}`。观察：`residents.status` 不变、没有 `resident_move` 广播、没有 commission/plan 行新增、居民坐标不动。

生产实证（只读）：
```sql
-- 玩家 不做了睡大觉(11769050-...) 的三次会话
SELECT c.id,u.name,r.slug,c.turns,c.started_at,c.ended_at
FROM conversations c JOIN residents r ON r.id=c.resident_id
LEFT JOIN users u ON u.id=c.user_id ORDER BY c.started_at;
-- c912b020 | 不做了睡大觉 | p-沈静书 | 3 | 05:34:56 | 05:35:35

-- 承诺被记下来了，但仅此而已
SELECT r.slug,m.source,round(m.importance::numeric,3),m.content
FROM memories m JOIN residents r ON r.id=m.resident_id WHERE m.source='chat_player';
-- p-沈静书 | chat_player | 0.400 | 沈静书主动答应带玩家去五金店找镇长，并描述了具体路线。

-- tick 写入速率，证明决策窗口 limit=10 只覆盖约 30 分钟
SELECT r.slug,count(*) n,round(count(*)/24.0,1) per_hour FROM memories m
JOIN residents r ON r.id=m.resident_id
WHERE m.type='event' AND m.created_at>=now()-interval '24 hours'
GROUP BY 1 ORDER BY n DESC;
-- a-lan 572/23.8, he-qiaoyun 565/23.5, shen-jingshu 452/18.8 ...
```

**修法**

两档，建议先上护栏再排功能。

【护栏版，当天可上，止血】改 `backend/app/llm/prompt.py` 的 `assemble_system_prompt`（第 39 行起），在末尾 `parts.append("请始终保持角色扮演...")` 之前插一段能力边界声明：
```python
parts.append(
    "能力边界：你只能说话。你无法带玩家移动、无法替玩家取送东西、无法代玩家去找人。"
    "玩家问路时请描述路线和地标，不要说『我带你去』『跟我来』这类你做不到的承诺。"
)
```
一行级改动，立刻消除「答应了却不动」的落差。

【功能版，模块级，六步】
1. `backend/app/agent/actions.py`：追加第 17 个成员 `ESCORT_PLAYER = "ESCORT_PLAYER"`（append-only，别插在中间）；在 `get_available_actions` 末尾加分支——查到该居民有 open 的 escort intent 时才 append。
2. 新建表 `resident_intents(id, resident_id, user_id, kind, payload_json, status, expires_at, created_at)` + `backend/app/services/escort_service.py`。形状照抄 `backend/app/services/commission_service.py`（现有 commission 是居民→玩家发委托，正好是反方向，可直接镜像 optimistic UPDATE + 完成检测那套）。
3. 意图落库：`backend/app/ws/handlers/chat.py:417` 的 `extract_chat_memories` 里已经有一次 JSON 抽取 LLM 调用。扩 `backend/app/memory/prompts.py` 的 `EXTRACT_EVENTS_SYSTEM`，让它额外输出 `"promises": [{"kind":"escort","target_location":"五金店"}]`；用 `app/agent/map_data.get_location_by_id` + 名称查找解析成 location_id，写 resident_intents 行。零新增 LLM 调用。
4. `backend/app/agent/phases/decide/basic.py`：在 `execute()` 第 76 行 `_maybe_needs_action` 旁边加同级的 `_maybe_escort(ctx)`，命中时返回 `ActionResult(ActionType.ESCORT_PLAYER, target_slug=loc_id, target_tile=get_valid_target_tile(loc_id), reason="带路")`，优先级建议放在 needs 之下、weather 之上。
5. `backend/app/agent/phases/execute/basic.py`：把 ESCORT_PLAYER 并入 `_MOVEMENT_ACTIONS` 走 `find_path`；到达后把 intent 置 done，并新增 WS 广播 `{"type":"resident_escort", ...}` 供前端渲染「居民在前面带路」。
6. `p-*` 玩家化身：因为它永远不 autonomous，要么在 `handle_start_chat` 里对 `resident_type='player'` 的目标注入「你只能说话」提示，要么禁止对玩家化身发 escort intent。

---

### #7-a · 语义检索的候选池被静态 importance 截断到 30 条 —— 85% 的记忆永远召不回（居民只会翻来覆去讲那几件事）

**类型** `BUG` ｜ **优先级** `P1` ｜ **工作量** 一个函数（_search_events_scored 候选阶段重写）+ 一个五行辅助函数 + 一处调用点 + 一条回归测试

**根因**

`backend/app/memory/service.py:364` —— `_search_events_scored` 的候选来自 `_fetch_event_candidates(resident_id, cap=max(limit*3, 30))`，而 `_fetch_event_candidates`（service.py:308-321）的排序是 `ORDER BY importance DESC, created_at DESC`。也就是说：余弦相关度（0.45 权重）和 recency（0.30 权重）只在「按静态 importance 取出的前 30 条」这个已经固定的集合内部重排。真正的 pgvector 查询 `search_events_vector`（service.py:384-422）写好了但全仓零调用点（grep 确认，只有定义处一行），是死代码。

后果：一条语义上完全命中玩家提问的记忆，只要 importance 没进全局前 30，无论玩家怎么问都召不回来。

第二处相关缺陷：`backend/app/ws/handlers/chat.py:116-119` 的 `handle_start_chat` 调 `retrieve_context` 时不传 `query_text`（默认 ""），于是 `_retrieve_events`（service.py:341）的 `if settings.realism_enabled and query_text` 短路失败，直接落到 `_search_events(resident_id, "", limit)` → 每次会话的第一条消息拿到的是纯 importance 前 10，零相关度、零 recency。

测试为什么没抓到：`backend/tests/test_realism_memory_retrieval.py:44-63` 每个 resident 只造 2~3 条记忆，从未越过 30 这条线，所以「语义相关低分记忆排前」的断言恒真。

**复现**

必现。任何记忆数 > 30 的居民，跟他聊他 importance 排名 31 名之后的任何往事，他都想不起来。

生产实证（只读，居民 zhao-qiwen，玩家实际聊过的那个）：
```sql
WITH c AS (
  SELECT importance, created_at, source, embedding IS NOT NULL emb
  FROM memories WHERE type='event' AND archived_at IS NULL
    AND resident_id=(SELECT id FROM residents WHERE slug='zhao-qiwen')
  ORDER BY importance DESC, created_at DESC LIMIT 30)
SELECT count(*) n, min(importance) min_imp, count(*) FILTER (WHERE emb) with_emb,
       min(created_at) oldest, max(created_at) newest FROM c;
-- 30 | 0.985 | 25 | 2026-07-25 18:20 | 2026-07-28 12:46

SELECT count(*) total, count(*) FILTER (WHERE importance>=0.9) ge09
FROM memories WHERE type='event' AND archived_at IS NULL
  AND resident_id=(SELECT id FROM residents WHERE slug='zhao-qiwen');
-- 1606 | 230
```
读数：可召回池 30 条 / 全部 1606 条 = 1.9%。门槛 importance ≥ 0.985 —— 连 0.98 的记忆都进不去。1576 条（98.1%）永远召不回。即使只算 ≥0.9 的「重要」记忆也有 200 条被挡在外面。

**修法**

改 `backend/app/memory/service.py` 的两处：

1. 候选阶段换成三路召回取并集，别再用单一 importance 排序：
```python
async def _search_events_scored(self, resident_id, query_embedding, limit=10):
    K = max(limit * 20, 200)
    by_vec = await self.search_events_vector(resident_id, query_embedding, limit=K)  # 复活现有死代码
    by_imp = await self._fetch_event_candidates(resident_id, cap=K)
    by_recent = await self._fetch_recent_candidates(resident_id, cap=K)  # 新增：ORDER BY created_at DESC
    candidates = {m.id: m for m in (*by_vec, *by_imp, *by_recent)}.values()
    # 下面 0.45/0.30/0.25 的打分循环原样保留
```
`search_events_vector` 已在 service.py:384-422 写好且 `except` 分支会退回 `_search_events`，sqlite 测试环境自动降级，不会炸。`_fetch_recent_candidates` 就是 `_fetch_event_candidates` 换个 order_by，五行。

2. `backend/app/ws/handlers/chat.py:116-119`：给 start_chat 的 `retrieve_context` 传一个 query_text 种子（比如已有的 relationship.content，或玩家名 + 「打招呼」），别让会话第一条消息走相关度盲区。

3. 补一条 >30 条记忆的回归测试进 `backend/tests/test_realism_memory_retrieval.py`：造 50 条高 importance 噪声 + 1 条低 importance 但语义命中的，断言后者能被召回。这是当前测试套件的空洞。

---

### #7-b · 75% 的记忆没有 embedding，且旧记忆永远补不上 —— backfill 按最新优先，老的被饿死

**类型** `BUG` ｜ **优先级** `P1` ｜ **工作量** 一个函数（backfill_missing_embeddings 换排序 + 改批量）+ 两个常量

**根因**

写入侧完全不算 embedding：`backend/app/agent/phases/memorize/basic.py:145-152` 的 `add_memory(...)` 调用没有 `embedding=` 参数 → tick 循环写的每一条 agent_action 记忆 embedding 都是 NULL。同样漏掉的还有 `app/services/gossip_service.py:123`、`app/services/witness_service.py:93`、`app/services/greeting_service.py:74/141`、`app/services/duty_service.py:370`。只有 `extract_events`（service.py:471）和 `_persist_wrapup_side`（service.py:615）显式算了。

补偿侧算不过来，而且顺序反了：`backend/app/tasks/embedding_backfill.py` —— `BACKFILL_INTERVAL_SECONDS = 3600`（第 20 行）、`BACKFILL_BATCH_SIZE = 50`（第 21 行）→ 上限 1200 行/天。而世界每天写 4500~5500 条 event 记忆，缺口约 4 倍。更致命的是第 61 行 `.order_by(Memory.created_at.desc())`：每小时永远只捞「最新的 50 条 NULL」。由于每小时新产生约 190 条 NULL，这 50 条永远是刚写的，比它老的那一批一辈子轮不到。

后果：`_cosine`（service.py:32-45）对 embedding 为 None 的记忆直接返回 0.0，这些记忆的相关度项恒为 0，语义召回对 3/4 的语料静默失效。

**复现**

必现，且随时间恶化。

生产只读实证：
```sql
SELECT type, source, count(*) n, count(embedding) with_emb
FROM memories GROUP BY 1,2 ORDER BY n DESC;
-- event|agent_action  |12391| 3051   (24.6%)
-- event|chat_resident | 2948| 2948   (100%,  extract 路径有算)
-- event|gossip        |  542|  165

SELECT count(*) FROM memories WHERE type='event' AND embedding IS NULL;
-- 9834  ← NULL 积压

-- 关键：按天看，老数据的覆盖率不会随时间上升，证明 created_at DESC 饿死了老行
SELECT date_trunc('day',created_at) d, source, count(*) n, count(embedding) emb
FROM memories WHERE type='event' GROUP BY 1,2 ORDER BY 1 DESC;
-- 07-28 agent_action 2254/574 | 07-27 4503/1085 | 07-26 4374/1061 | 07-25 1260/331
```
07-25 的数据放了三天，覆盖率还是 26%，一条都没补上。而每天新增 embedded 数（约 1000~1085）恰好等于 backfill 的 50×24=1200 上限 —— agent_action 的 embedding 100% 来自 backfill，写入侧一条都没算。

**修法**

改 `backend/app/tasks/embedding_backfill.py`，三处：

1. 第 61 行排序反过来，改成 FIFO 排干积压：
```python
.order_by(Memory.created_at.asc())
```
2. 用已存在的批量接口替掉逐行循环（`app/memory/embedding.py:120` 的 `generate_embeddings_batch` 现成可用），并把吞吐拉到写入速率之上：
```python
BACKFILL_BATCH_SIZE = 400
BACKFILL_INTERVAL_SECONDS = 600
# backfill_missing_embeddings 内：
rows = list(result.scalars().all())
embs = await generate_embeddings_batch([m.content for m in rows])
for m, e in zip(rows, embs):
    if e is not None: m.embedding = e; fixed += 1
```
400×6/小时 = 2400/小时，既能覆盖约 190/小时的新增，也能在两天内排干 9834 条积压。

3.（可选，更彻底）`backend/app/agent/phases/memorize/basic.py:145` 直接传 `embedding=await generate_embedding(memory_content)`。注意这会给每个 tick 加一次 ollama 往返；如果不想拖慢 tick，就只做 1+2，让 backfill 变成真正跑得赢的补偿通道。

---

### #7-c · 玩家对话抽出的 event 记忆永远不带 related_user_id —— 四个玩家可见功能静默失效

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一行（签名）+ 一行（透传）+ 一行（调用点），共三处；建议补一条断言 related_user_id 非空的测试

**根因**

`backend/app/memory/service.py:424-486` 的 `extract_events()` 签名里根本没有 user_id 参数，第 472-480 行的 `add_memory(...)` 只传了 `related_resident_id=related_id`，`related_user_id` 恒为 None。调用方 `backend/app/ws/handlers/chat.py:446-451` 手里明明有 `user_id`（同函数第 419 行的参数），却无处可传 —— 它只传了 `source="chat_player"`。

只有 relationship 记忆走 `update_relationship_via_llm`（chat.py:455-460）显式带了 `user_id`，所以每对（居民,玩家）终生只有 1 条记忆带得上 user 指针。

四个下游消费者因此静默失效，全部读过代码确认：
- `backend/app/events/achievements.py:195-204`：`remembered` / `memory_keeper_10` 挂在 `memory_written_about_user` 上，而该事件由 `service.py:100-103` 的 `if related_user_id:` 触发。事件记忆全 NULL → 「被 10 条居民记忆记住」几乎不可达。
- `backend/app/services/digest_service.py:305`：周报「居民记得关于你的事」查 `Memory.related_user_id == user_id`，永远查不到真正的对话内容。
- `backend/app/services/dream_service.py:70`：`involves = next((m.related_user_id for m in material ...))` → 玩家永远不会出现在居民的梦里。
- `backend/app/personality/evolution.py:258-266`：`personality_shifted` 通过触发记忆的 `related_user_id` 归因给玩家；而触发记忆恒为刚抽出的 event（`_run_evolution_hooks`, service.py:488-519），user_id 恒 NULL → `soul_shaper` 成就永远无法归属到人。

**复现**

必现。与任意居民对话 → end_chat → 查该居民新写的 event 记忆，related_user_id 必为 NULL。

生产只读实证：
```sql
SELECT type, source, count(*) n, count(related_user_id) with_user
FROM memories GROUP BY 1,2 ORDER BY n DESC;
-- event       |chat_player| 5 | 0     ← 玩家说的话，一条都没关联到玩家
-- relationship|chat_player| 2 | 2     ← 只有关系记忆带得上
-- event       |witness    | 3 | 3
-- event       |greeting   | 2 | 2
-- event       |capsule    | 1 | 1
```
全库 17k+ 条记忆里，只有 8 条带 related_user_id，其中 6 条来自 witness/greeting/capsule 这些旁路。主通道（居民把你说的话记下来）贡献 0 条 event。

对应成就卡死：
```sql
SELECT code, progress_json, unlocked_at FROM user_achievements
WHERE user_id='11769050-f93a-4236-bb08-2855375a07ce';
-- memory_keeper_10 | {"count": 7, "target": 10} | NULL   ← 停在 7/10
```

**修法**

`backend/app/memory/service.py:424` 加一个 keyword-only 参数并透传：
```python
async def extract_events(
    self, resident, other_name, conversation_text, *,
    source: str = "chat_player",
    user_id: str | None = None,      # 新增
) -> list[Memory]:
```
第 473-480 行的 add_memory 调用加一行：
```python
    related_resident_id=related_id,
    related_user_id=user_id,          # 新增
```
调用方 `backend/app/ws/handlers/chat.py:446-451` 加一行 `user_id=user_id,`。

注意 `add_memory` 第 100-103 行会在 related_user_id 非空时 emit `memory_written_about_user`，所以成就/周报/梦境/人格归因四条线一起复活，不需要额外接线。

生产已有的 5 条历史行可以不管（量太小），也可以一条 UPDATE 回填 —— 但按红线「迁移与行为变更不同批」，回填要单独一次变更。

---

### #7-d · 居民自己的行动记忆 96% 是重复模板串，把决策/规划的记忆窗口刷爆

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一个函数（memorize 去重）+ 一个函数（_load_memories 换召回）+ 一行阈值调整

**根因**

`backend/app/agent/phases/memorize/basic.py:78-114` 的 `format_action_memory` 对每个 (action, 地点, 目标) 组合产出固定模板串，且 `BasicMemorizePlugin.execute`（同文件 145-152 行）每 tick 无条件 INSERT 一条，不做任何去重/合并。

两个直接后果：
- `backend/app/agent/phases/decide/basic.py:341-347` `_load_memories` 取 `get_memories(type="event", limit=10)`（纯 created_at 倒序），于是 DECISION_USER（`app/agent/prompts.py:37-45`）里的「最近的记忆」几乎永远是 10 行一模一样的字符串。决策 LLM 的上下文等于没有信息。
- `backend/app/agent/phases/plan/basic.py` 的 `recent_all = get_memories(..., limit=20)` 后接 `[m for m in recent_all if m.importance > 0.5][:5]` —— 同样被模板串占满，而且这个 `> 0.5` 硬阈值正好把 #8 那条 importance=0.4 的玩家承诺记忆过滤掉。

另外注意：整个 agent 侧（decide/plan）用的是原始 `get_memories` 递归时间序，完全没走 `retrieve_context` / `_retrieve_events` 的打分召回 —— 打分召回只在玩家对话路径用（grep `retrieve_context` 仅命中 ws/handlers/chat.py:116 与 220）。

**复现**

必现。

生产只读实证：
```sql
SELECT content, count(*) c FROM memories
WHERE type='event' AND source='agent_action'
GROUP BY 1 ORDER BY c DESC LIMIT 12;
-- 在星光公寓和 he-qiaoyun 聊天   | 1272
-- 在星光公寓和 luo-xiaozhou 聊天 | 1031
-- 在星光公寓和 a-lan 聊天        |  875
-- 在星光公寓和 zhao-qiwen 聊天   |  784
-- 在住宅A和 zhou-dahe 聊天       |  750
-- ...（前 12 个字符串合计 8496 行 / agent_action 总计 12391 行 = 68.6%）
```
单条字符串「在星光公寓和 he-qiaoyun 聊天」在库里存在 1272 份。decide 的 limit=10 窗口在星光公寓这种热点几乎必然被同一句刷满。

**修法**

两处：

1. 写入去重 —— `backend/app/agent/phases/memorize/basic.py` 的 `BasicMemorizePlugin.execute`，在 `add_memory` 之前查该居民最新一条 event 记忆，内容相同就不插新行，改为累加：
```python
last = (await ctx.db.execute(
    select(Memory).where(Memory.resident_id == ctx.resident.id, Memory.type == "event")
    .order_by(Memory.created_at.desc()).limit(1))).scalar_one_or_none()
if last is not None and last.content == memory_content and last.source == "agent_action":
    last.last_accessed_at = datetime.now(UTC)
    last.metadata_json = {**(last.metadata_json or {}),
                          "repeat": (last.metadata_json or {}).get("repeat", 1) + 1}
    await ctx.db.commit()
    ctx.memory_created = False
    return ctx
```
更彻底的做法是按「同内容 + 同世界日」合并成一条带次数的记忆。

2. 决策侧换成打分召回 —— `backend/app/agent/phases/decide/basic.py:341-347` 的 `_load_memories` 不再用 `get_memories`，改调 `MemoryService._retrieve_events(resident_id, query_text, limit)`，query_text 用当前 plan.reason + 当前地点名拼出来。这样 decide 拿到的是与当下情境相关的记忆，而不是复读机流水。

3. `backend/app/agent/phases/plan/basic.py` 里的 `importance > 0.5` 硬阈值建议改成「取前 N 条」而不是绝对阈值，否则在 importance 分位归一化（`_normalize_importance`, service.py:243-267）之下这个阈值语义会随语料漂移。

---

### #7-e · WS 断线时不抽取记忆 —— 玩家直接关标签页/断网，整段对话的记忆全丢

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一个函数（把 handle_end_chat 收尾抽成 _finalize_chat）+ 一处调用点

**根因**

记忆抽取只挂在显式 end_chat 上：`backend/app/ws/handlers/chat.py:409-414` 的 `handle_end_chat` 末尾才 `asyncio.create_task(extract_chat_memories(...))`。

而断线清理路径 `backend/app/ws/handlers/connection.py:179-218` 的 `_cleanup()` 只做三件事：解 resident 锁、重置 status、保存坐标。全函数 grep 无 `extract_chat_memories`、无 `MemoryService`（已验证：该文件只有第 129 行和 179 行两处 `_cleanup` 命中，无其它匹配）。`websocket_handler` 的 `finally`（connection.py:128-129）走的正是这条路。

前端侧确认只有一个出口会发 end_chat：`frontend/src/components/ChatDrawer.tsx:225` 的 `close()`。关标签页、切后台被系统杀、网络抖动、移动端锁屏 —— 都不走这里。

结果：Conversation 和 Message 行都落库了（chat.py:290-299 的 Session 2 每轮都写），但 Memory 一条不生成，relationship 也不更新。玩家下次来，居民对这段完全没印象。

**复现**

在前端与居民对话若干轮 → 不点关闭按钮，直接关标签页 / 拔网线 → 重新连上再找同一居民 → 该居民对上一段对话零记忆。

验证 SQL（对比 conversations 与其产出的记忆）：
```sql
SELECT c.id, c.turns, c.ended_at,
       (SELECT count(*) FROM memories m
        WHERE m.resident_id=c.resident_id AND m.source='chat_player'
          AND m.created_at BETWEEN c.started_at AND c.started_at + interval '5 min') mem
FROM conversations c ORDER BY c.started_at;
```
生产当前 3 条会话全部有 ended_at（玩家都点了关闭按钮），所以这条在生产上还没被触发过 —— 代码缺陷已确认，生产实例未确认。诚实标注。

**修法**

`backend/app/ws/handlers/connection.py` 的 `_cleanup()`（第 179 行）在 `if ctx.in_chat:` 分支里补上与 `handle_end_chat` 相同的收尾。最省事的做法是把 `handle_end_chat` 里从「更新 resident/conversation 收尾字段」到「create_task(extract_chat_memories)」这段抽成 `chat._finalize_chat(ctx)`，两边共用：
```python
# connection.py _cleanup 内
if ctx.in_chat:
    from app.ws.handlers import chat as chat_handler
    try:
        await chat_handler._finalize_chat(ctx, notify_client=False)
    except Exception:
        logger.warning("chat finalize on disconnect failed", exc_info=True)
    # 原有的解锁 / status 重置逻辑由 _finalize_chat 承担，去重
```
注意 `_finalize_chat` 里的 `manager.send(ctx.user_id, ...)` 在断线路径上要跳过（notify_client=False），rating 弹窗也不该触发；`extract_chat_memories` 本身是 fire-and-forget 且自带 `len(chat_messages) < 2` 早退，直接复用即可。

---

### E3 · 辩论生命周期无任何驱动——玩家押注的金币永久沉没

**类型** `MISSING_FEATURE` ｜ **优先级** `P0` ｜ **工作量** 一个模块（~40 行推进器 + 2 个 settings 旋钮 + 一次性退款脚本 + TDD 测试）

**根因**

`debate_service.run_live()`（backend/app/services/debate_service.py:143）和 `settle()`（同文件:207）在**整个 app/ 目录里没有任何调用点**，只有 backend/tests/test_debates.py:172 调过。源码自己写明了这件事：debate_service.py:57-58 注释「the debate lifecycle stops here today (no live/settle driver in app code)」。唯一的创建入口是 civic_service.py:768 `create_debate(...)`（由 tasks/event_cron.py:47 的 `maybe_spawn_lecture_debate` 触发），建完就停在 status='announced'。而 routers/debates.py:59 的 stake 接口是开放的，debate_service.py:88 会真扣币（`charge(db, user_id, amount, ...)`）。结果：押注扣钱→辩论永不 live/voting/settled→永不 payout。
生产实证：
```sql
SELECT id,status,pool_a,starts_at FROM debates;
 1c00ba36-f507-4287-ab0e-b0042f73f540 | announced | 10 | 2026-07-26 06:05:34+00
SELECT user_id,amount,reason,created_at FROM transactions WHERE reason LIKE 'debate%';
 176a210c... | -10 | debate_stake:1c00ba36... | 2026-07-28 12:36:13+00
```
建于 07-26，到 07-28 20:45 仍是 announced（2.3 天）。玩家 176a210c（stawky@linux.do）今天 20:36 扣了 10 币，无对应 debate_win/debate_refund 流水。

**复现**

1. 等 event_cron 触发一次 `maybe_spawn_lecture_debate`（或直接看现存的 1c00ba36）；2. `GET /debates` 拿到 status=announced 的辩论；3. `POST /debates/{id}/stake {"side":"a","amount":10}` → 200，soul_coin_balance -10；4. 无限期等待 → status 永远是 announced，无 payout。必现，与并发/时序无关。
附带现象：玩家重复点押注会拿到 400（debate_service.py:84「already staked on this debate」）——那个 400 本身是 WORKING_AS_DESIGNED（生产日志里今天两条 `POST /debates/.../stake 400`），前端该置灰按钮。

**修法**

在 backend/app/tasks/event_cron.py 的 60s 循环里补一段辩论推进器（与 :53 的 `settle_due_seasons` 并列）：
```python
from app.models.debate import Debate
from app.services import debate_service as ds
now = datetime.now(UTC)
# announced 满 STAKE_WINDOW（建议 settings 加 debate_stake_window_min=30）→ 跑 live
for d in (await db.execute(select(Debate).where(Debate.status == "announced"))).scalars():
    if (now - _aware(d.starts_at)).total_seconds() >= settings.debate_stake_window_min * 60:
        await ds.run_live(db, d)          # 内部失败会 _auto_draw_refund，安全
# voting 满 VOTE_WINDOW → settle
for d in (await db.execute(select(Debate).where(Debate.status == "voting"))).scalars():
    if (now - _aware(d.starts_at)).total_seconds() >= settings.debate_vote_window_min * 60:
        await ds.settle(db, d.id)         # 已幂等（:210 settled 直接返回）
```
另加一条兜底：启动时把 `status IN ('announced','live')` 且 `starts_at` 超过 24h 的辩论走 `_auto_draw_refund`（debate_service.py:186），把历史卡死的押金退回去——包括现在这笔 10 币。
生产存量处置：debate 1c00ba36 需要人工 `_auto_draw_refund`（只读排查阶段不动，交给上线时的一次性脚本）。

---

### E7 · 赛季系统从来没有开季入口——seasons 表 0 行，全部记分静默变 no-op

**类型** `MISSING_FEATURE` ｜ **优先级** `P1` ｜ **工作量** 一个模块（admin 路由 + nightly 自动开季 + 列表路由 + 迁移不需要，表已存在）

**根因**

全仓库 `Season(` 的构造只出现在 backend/app/models/season.py:10 的类定义处，**没有任何一处代码创建 Season 行**（无 seed、无 admin 路由、无 cron）。backend/app/routers/admin/ 下 17 个文件 grep 'season' 零命中。后果链：
1. `season_service._active_season_id()`（backend/app/services/season_service.py:32-40）恒返回 None；
2. `season_service.add_points()`（同文件:46-50）第一件事就是 `if not season_id: return 0` —— 所有经 season_scorer 上报的积分**全部静默丢弃**；
3. `script_service.settle_season_polls()`（backend/app/services/script_service.py:196）按 `Poll.season_id == season_id` 过滤，而生产 3 张 poll 的 season_id 全是 NULL，这条路径永远空转；
4. 只有读端和记分端存在，写端从未实现。
生产实证：`seasons=0, season_scores=0, season_scripts=0`（精确 count）；`GET /seasons/current` → `{"season":null}`；`GET /seasons/current/leaderboard` → `{"top":[],"season":null}`；`GET /seasons` → 404（routers/seasons.py 只有 /current 与 /current/leaderboard 两条路由，没有列表路由）。

**复现**

`curl https://simverse-api.proxypool.eu.org/seasons/current` → `{"season":null}`，任何时刻、任何账号都一样。玩家侧表现为「赛季页面永远空白 / 积分怎么刷都是 0」。

**修法**

补写端，两选一（建议都做）：
1. **管理端**：新增 backend/app/routers/admin/seasons.py，`POST /admin/seasons`（title/theme/starts_at/ends_at/status='active'）+ `POST /admin/seasons/{id}/settle` 调 `season_service.settle_season`。注意开完季必须调 `season_service._invalidate_active()`（同文件:42）打掉 60s 缓存，否则新赛季最多 1 分钟不可见。
2. **自动开季**：在 backend/app/tasks/nightly_cron.py 里，`settle_due_seasons` 结算完后，如果无 active season 就按 `settings.season_length_days` 自动开下一季。
再补 `GET /seasons` 列表路由（backend/app/routers/seasons.py，现在只有 :13 和 :22 两条）。
**不要**顺手把 `add_points` 改成「无赛季也记分」——那会污染 season_scores 的赛季语义；正确做法是让赛季真的存在。

---

### E4 · 人格演化预算按真实自然月计，与 k=4 世界时钟错配——11 名居民全部冻结到 8 月

**类型** `BUG` ｜ **优先级** `P1` ｜ **工作量** 一个函数（guard.py 的 check_monthly_budget + shift 冷却换算，加 2 个 TDD 用例 monkeypatch world_clock）

**根因**

backend/app/personality/guard.py:137-158 `check_monthly_budget()` 用的是**真实自然月**：
```python
now = datetime.now(UTC)
... extract("year", PersonalityHistory.created_at) == now.year,
    extract("month", PersonalityHistory.created_at) == now.month,
... return max(0, self.TOTAL_MONTHLY_CHANGE - used)   # TOTAL_MONTHLY_CHANGE = 8 (:32)
```
但世界跑在 `WORLD_CLOCK_K=4` 的加速时钟上，而 backend/app/world_clock.py 的模块 docstring 明确规定「Everything about resident life that has a '时间/星期/日历' meaning … reads world time」。人格演化是标准的居民生活语义，却读了真实时间 → 每 1 真实月 = 4 世界月，预算被压成「每世界月 2 次变更」。
生产实证（真实 7 月至今）：
```sql
SELECT r.slug, count(*) entries,
       sum((SELECT count(*) FROM json_object_keys(ph.changes_json))) dim_changes,
       min(ph.created_at), max(ph.created_at)
FROM personality_history ph JOIN residents r ON r.id=ph.resident_id
WHERE ph.created_at >= date_trunc('month', now()) GROUP BY 1;
-- 11 名居民里 10 名 dim_changes = 8/8（顶格），lin-wanqiu 6/8
-- 全部在 2026-07-25 17:35 ~ 2026-07-28 05:31 之间烧完
```
agent-worker 日志现在每分钟刷：`app.personality.evolution: Drift skipped for <id>: monthly budget exhausted`（evolution.py:50 / :146）。世界时间已到 2028-04-15，人格要等到真实 2026-08-01 才解冻 ≈ 还有 16 个世界日全员人格死锁。

**复现**

`ssh vm212 'cd /opt/skills-world/deploy && docker compose logs --since 10m agent-worker | grep "budget exhausted"'` → 每轮 tick 都有。任何 k>1 的部署跑满 8 次维度变更后必现。

**修法**

把 guard.py:137-158 的月窗口换成世界时间月：
```python
from app.world_clock import now_world, world_to_real
def _world_month_start_real() -> datetime:
    w = now_world()
    return world_to_real(w.replace(day=1, hour=0, minute=0, second=0, microsecond=0))

async def check_monthly_budget(self, resident_id, db) -> int:
    since = _world_month_start_real()      # created_at 存的是真实时间，所以窗口要换算回真实
    stmt = select(PersonalityHistory.changes_json).where(
        PersonalityHistory.resident_id == resident_id,
        PersonalityHistory.created_at >= since,
    )
    ...
```
注意 `created_at` 列存的是真实时间（models/personality_history.py 用 `now()` 默认值），所以是「把世界月首换算成真实时刻再比较」，不是「把 created_at 换算成世界时间」——后者要么全表扫要么加生成列，没必要。
同一处 `SHIFT_COOLDOWN_HOURS = 24`（guard.py:31）和 `MIN_DRIFT_INTERVAL`（:30，按记忆条数不受影响）也要一并核：24 小时冷却在 k=4 下等于 4 个世界日，同样偏紧，建议改成 `world_to_real(24h)` = 6 真实小时。

---

### E6 · 5 名居民全部叠在同一格 (54,74)——回家目标恒等于建筑的单个 entrance 格

**类型** `BUG` ｜ **优先级** `P1` ｜ **工作量** 一个模块（map_data 新函数 + 三处优先级调转 + 一个回填脚本 + TDD：同 loc 两人落点必须不同）

**根因**

回家目标解析有三处，全部落到同一个「建筑入口单格」：
- backend/app/agent/night_homing.py:17-24 `_home_target()`：先 `get_valid_target_tile(home_location_id)`，拿不到才用 `home_tile_x/y`；
- backend/app/agent/phases/execute/basic.py:106-111 GO_HOME 执行同样先走 `get_valid_target_tile(home_loc_id)`；
- backend/app/agent/actions.py:79-87 判断「是否已到家」也是拿 `entrance or center` 单点比较。
而 backend/app/agent/map_data.py:431-436：
```python
def get_valid_target_tile(loc_id):
    loc = LOCATIONS.get(loc_id)
    return loc.get("entrance", loc.get("center"))   # 返回单点
```
本来 `home_tile_x/home_tile_y` 应该给每个居民一个不同的落点，但 grep 全仓库，**没有任何代码写入 home_tile_x**（只有 night_homing.py:22 / execute/basic.py:110 / memorize/basic.py:50 / actions.py:89 四处读）。生产里 13 名居民的 home_tile_x/home_tile_y **全为 NULL**，fallback 分支永远不生效。
生产实证：
```sql
SELECT slug, home_location_id, home_tile_x, tile_x, tile_y FROM residents ORDER BY home_location_id;
 a-lan|apt_star|NULL|54|74   he-qiaoyun|apt_star|NULL|54|74
 jiang-lin|apt_star|NULL|54|74  luo-xiaozhou|apt_star|NULL|54|74
 zhao-qiwen|apt_star|NULL|54|74
```
而 `GET /world/locations` 里 apt_star（星光公寓）= bounds [51,65,62,75], center [56,70], **entrance [54,74]** —— 5 个人全部停在公寓大门那一格，从不进屋。

**复现**

`curl https://simverse-api.proxypool.eu.org/residents | jq '.[]|{slug,tile_x,tile_y}'` → 现在就有 5 条 (54,74)。任何有 ≥2 名居民共用一个 home_location_id 的部署，在夜间归巢/GO_HOME 后必现。玩家侧表现为「地图上一堆人重叠成一个」「居民不进屋，全站在门口」。

**修法**

给每名居民分配建筑内部的**互异**落点，并让三条回家路径都优先用它。
1. 在 backend/app/agent/map_data.py 的 `assign_home()`（:450）里，选定 loc_id 之后顺手算一个稳定的内部格并回写：
```python
def pick_home_tile(loc_id: str, resident_id: str) -> tuple[int, int] | None:
    loc = LOCATIONS.get(loc_id)
    if not loc: return None
    x0, y0, x1, y1 = loc["bounds"]
    walkable = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
                if (x, y) in get_walkable_tiles()]
    if not walkable: return loc.get("entrance") or loc.get("center")
    # 稳定哈希，避免 PYTHONHASHSEED（同 civic_service._stable_unit 的理由）
    idx = int(hashlib.sha1(resident_id.encode()).hexdigest(), 16) % len(walkable)
    return walkable[idx]
```
   调用方在分房时写 `resident.home_tile_x, resident.home_tile_y = pick_home_tile(...)`。
2. 把 night_homing.py:17-24 的优先级**调转**——先 `home_tile_x/y`，没有才退到 entrance：
```python
def _home_target(resident):
    if resident.home_tile_x is not None and resident.home_tile_y is not None:
        return (resident.home_tile_x, resident.home_tile_y)
    if getattr(resident, "home_location_id", None):
        t = get_valid_target_tile(resident.home_location_id)
        if t: return (t[0], t[1])
    return None
```
   execute/basic.py:106-111 与 actions.py:79-93 同样调转。
3. 一次性回填脚本：给现有 13 名居民按 `pick_home_tile` 补 home_tile_x/y（纯数据写，不改行为，符合「迁移与开闸不同批」的红线——这次只回填，不动逻辑开关）。

---

### E5 · 计划槽缺 importance 键抛 KeyError，整条 phase 链被 break——a-lan 连续 2.5 小时不动

**类型** `BUG` ｜ **优先级** `P1` ｜ **工作量** 一个函数（plan/basic.py 归一化 + 读取侧 .get + tick.py 容错语义，3 个 TDD 用例）

**根因**

两段代码叠加：
1. backend/app/agent/phases/plan/basic.py:110-124 读当前时段计划时对 LLM 生成的 dict 做**硬下标**：
```python
ctx.current_plan = HourlyPlan(
    slot=p["slot"], hour_range=tuple(hr), action=p["action"],
    target=p.get("target"), location=p.get("location"),
    importance=p["importance"],          # ← KeyError 源头
    reason=p.get("reason", ""), status=p.get("status", "pending"))
```
   `slot`/`action`/`importance` 三个键没有默认值，而 plans 是 LLM 直出后原样落库的（同文件:249-253 `_generate_plan` 只补了 `status`，没做 schema 校验）。
2. backend/app/agent/tick.py:87-92 的容错是 **break 整条链**：
```python
for phase in phases:
    try: ctx = await phase.execute(ctx)
    except Exception as e:
        logger.warning("Phase failed for %s: %s", resident.slug, e)
        break            # ← perceive 后面的 decide/execute/memorize 全不跑
```
   plan 阶段一挂，该居民这一 tick 什么都不做。
生产实证：`docker compose logs --since 24h agent-worker | grep "Phase failed"` → 26 条，全是 `Phase failed for a-lan: 'importance'`，时间窗 2026-07-28 01:23:20 → 03:44:34（2 小时 21 分真实时间 ≈ 9.4 个世界小时），直到当天计划被重新生成才恢复。现在库里已无缺 importance 的槽（a-lan 的 daily_plans_json 在 11:32 重生成过），所以是间歇性、跟 LLM 输出质量绑定。

**复现**

让 plan LLM 对某个时段输出缺 `importance` 的 slot（或直接改库把某 slot 的 importance 删掉），等世界时钟走进那个 hour_range → 该居民每 tick 都刷一条 `Phase failed for <slug>: 'importance'` 并完全不动，直到跨世界日重新生成计划。

**修法**

两处都改，缺一不可。
1. **写入侧做归一化**（根治）——backend/app/agent/phases/plan/basic.py:249-253，落库前补齐必填键：
```python
plans = data.get("plans", [])
normalized = []
for i, p in enumerate(plans):
    hr = p.get("hour_range") or [0, 0]
    if not (isinstance(hr, list) and len(hr) == 2):
        continue                       # 时段都读不出来的槽直接丢
    normalized.append({
        "slot": p.get("slot", i),
        "hour_range": hr,
        "action": p.get("action", "IDLE"),
        "target": p.get("target"), "location": p.get("location"),
        "importance": int(p.get("importance", 5)),
        "reason": p.get("reason", ""), "status": "pending",
    })
resident.daily_plans_json = {"generated_date": today, "plans": normalized}
```
2. **读取侧兜底**——同文件:116-122 三个硬下标改成 `p.get("slot", 0)` / `p.get("action", "IDLE")` / `p.get("importance", 5)`，让历史脏数据也不炸。
3. **可选但强烈建议**：tick.py:87-92 的 `break` 改成 `continue` 只跳过失败的 phase（保 decide/execute 继续跑），并把 `logger.warning` 加上 `exc_info=True`——现在日志里只有一个裸键名 `'importance'`，没有栈，定位全靠猜。

---

### E8 · 三张 open poll 的 npc_votes 全是已删除旧居民投的幽灵票；镇长选举四个候选人一个都不存在

**类型** `BUG` ｜ **优先级** `P1` ｜ **工作量** 一个模块（_npc_voters 结构升级 + 撤票逻辑 + 结票候选人校验 + 存量重算脚本，注意读侧要兼容旧 list 格式）

**根因**

backend/app/services/civic_service.py:156-193 `run_npc_voting()` 只做**增量补票**，从不清理已离开的居民：
```python
voters = set(poll.options_json[0].get("_npc_voters", []))
for r in residents:                    # residents = 当前 is_civic_voter 集合
    if r.slug in voters: continue      # 已投过就跳过
    opts[idx]["npc_votes"] += 1
    voters.add(r.slug)
```
`npc_votes` 是累加计数器，`_npc_voters` 是只增不减的 slug 集合。07-25 的花名册重置（seed/reset_builtin_residents.py，含那次误删 12 个玩家角色的事故）把旧 25 人删干净了，但票留在了 options_json 里。`close_due_polls`（同文件:462）结票时直接 `int(o.get("npc_votes", 0)) + 玩家票`（:493-496），幽灵票原样计入。
生产实证：
```sql
SELECT resident_type, count(*) FROM residents GROUP BY 1;  -- npc=11, player=2
SELECT question, closes_at, status FROM polls;
```
三张 poll 的 `_npc_voters` 都是 25 人，其中 adam / isabella / klaus / mei / tamara / 夏洛克-福尔摩斯 / 夜风侦探 / 夜风侦探-46ff1f / 夜风侦探-a23160 / 格蕾丝-霍珀 / 阿达-洛芙莱斯 / 陈默 / 林晚秋 / 部署回归图元0724 共 14 个 slug 在 residents 表里查不到。
最严重的一张 `8e96c1dd 镇长选举:谁来当下一任镇长?`：四个候选 effect 分别指向 klaus(17票) / 夜风侦探(2) / isabella(5) / adam(1)——**四个候选人全部已不存在**。结票时 `_execute_outcome` → `install_mayor` 解析不到 → `_winner_lost_civic_rights`（civic_service.py:557）判 True → 走「流会」公告（:541-544）。所以不会崩，但镇长位置永久空缺，且 20+5=25 张幽灵票让 13 人小镇里的 2 个真玩家永远投不赢任何议案。
`closes_at` 已被 backend/scripts/postpone_open_polls.py 推到 2026-07-31 23:29:43（system_config.civic_poll_postpone_until 有标记），也就是还有 3 天就会以幽灵票结票。

**复现**

`curl https://simverse-api.proxypool.eu.org/polls/open` → 三张 poll 的 options[].`_npc_voters` 里能直接看到 residents 表查不到的 slug。玩家侧：任何一张 poll 上真人票 ≤2，NPC 侧 20-25 票，玩家投票不可能改变结果。

**修法**

分三步，**存量与逻辑分开两次变更**（照 no-migration-with-flag-flip 红线）。
1. **逻辑修（先上）**——在 civic_service.py:180 附近，用当前名册重算而不是信历史集合：
```python
live = {r.slug for r in residents}
voters = set(opts[0].get("_npc_voters", []))
stale = voters - live
if stale:                       # 已离开世界的投票人：撤票
    # 需要知道每个 stale slug 投的是哪一项 → _npc_voters 结构要从 list[str]
    # 升级成 dict[slug -> option_idx]，否则无法定向回滚
    ...
```
   最小改动方案：把 `_npc_voters` 从 `list[str]` 改成 `{slug: option_idx}`（读侧兼容旧 list），然后每晚先 `for slug, idx in list(voters.items()): if slug not in live: opts[idx]["npc_votes"] -= 1; del voters[slug]`，再走原有补票循环。
2. **结票兜底**——`close_due_polls`（:462）在算 tally 前，对 `effect.type in {"mayor","office","duty"}` 的选项校验 `effect["slug"]` 是否还在 `is_civic_voter` 集合里，不在就把该选项的 npc_votes 归零（而不是等到 `install_mayor` 阶段才流会）——否则一张全是幽灵候选的选举会「有胜者但流会」，公告文案对玩家是误导。
3. **存量数据（单独一次变更）**——一次性脚本重算这 3 张 poll 的 npc_votes：删掉 14 个不存在 slug 的票，重算 tally。镇长选举那张四个候选全废，建议直接 `status='closed'` 作废并让 `election_service.maybe_open_election`（:75-130，`election_last_opened='2026-07-24'`）在下一个 interval 用当前名册重开。

---

### E1 · users.player_resident_id 是无外键的裸字符串——化身指针可悬空（07-25 事故的同一个窗口）

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一行模型改动 + 一个迁移（含悬空清理），另需复核 reset_builtin_residents 的删除顺序

**根因**

`users` 表**一条外键都没有**：
```sql
SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='users'::regclass;
 uq_users_linuxdo_id | UNIQUE (linuxdo_id)
 users_email_key     | UNIQUE (email)
 users_github_id_key | UNIQUE (github_id)
 users_pkey          | PRIMARY KEY (id)
```
`\d residents` 的 Referenced by 里列了 civic_standing_history / conversations / memories / personality_history / resident_sprite_runs，**唯独没有 users**。models/user.py 的 `player_resident_id` 是 `mapped_column(String)`，没有 ForeignKey。
对比：同一批治理里 residents.creator_id 有 FK（migration 040 加了账号删除 → creator_id 置 NULL），resident_sprite_runs 的三个 users FK 刚在 commit 104f2b1 补了 ondelete=SET NULL。player_resident_id 是唯一漏网的。
目前靠 seed/reset_builtin_residents.py 的文档承诺（docstring：「users.player_resident_id pointers are nulled」）兜底，即**只有那一条删除路径**会清指针；任何其他 DELETE FROM residents 都会留下悬空指针。07-25 16:53 手写脚本绕过 `purge_residents` 守卫删掉 12 个玩家角色，就是在这个窗口里发生的。
当前生产读数（幸运，指针都还能解析）：
```sql
SELECT count(*) total, count(player_resident_id) with_ptr,
  count(*) FILTER (WHERE player_resident_id IS NOT NULL
    AND EXISTS (SELECT 1 FROM residents r WHERE r.id=u.player_resident_id)) resolvable
FROM users u;
 46 | 2 | 2
```
46 个账号里 41 个是测试/系统号（sv-test.dev / e2etest.com / burnin-smoke / @t.dev / skills.world），真人只有 5 个账号 / 4 个人：stawky@linux.do 与 stakeswky@github.users（同一昵称「不做了睡大觉」，两个账号各有一个化身 p-沈静书 / p-赵启文）、tlolee@github.users、engineering-wrench@github.users(Артём)、konstantine.zam@yandex.ru —— 后三个 last_login_date 全 NULL，注册后没回来过，所以没化身不是 bug 是没走完 onboarding。

**复现**

当前无法复现悬空（2/2 可解析）。构造路径：在 residents 上执行任何不经过 `reset_builtin_residents.purge_residents` 的 DELETE（例如 admin 删居民、手写迁移），对应 user 的 player_resident_id 立刻悬空，`/users/me` 与地图化身解析随即拿到 None。

**修法**

加迁移（单独一次变更，不与任何开关同批）：
```python
# alembic/versions/052_player_resident_fk.py
def upgrade():
    # 先清悬空，否则加约束会失败
    op.execute("""UPDATE users SET player_resident_id = NULL
                  WHERE player_resident_id IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM residents r WHERE r.id = users.player_resident_id)""")
    op.create_foreign_key("users_player_resident_id_fkey", "users", "residents",
                          ["player_resident_id"], ["id"], ondelete="SET NULL")
```
同步在 backend/app/models/user.py 把 `player_resident_id` 改成 `mapped_column(String, ForeignKey("residents.id", ondelete="SET NULL"), nullable=True)`。
注意 residents.creator_id 已有 `residents_creator_id_fkey → users(id)`，加反向 FK 会形成环——插入顺序上没问题（两边都 nullable），但 `reset_builtin_residents` 的删除顺序要复核：先删 residents 时 users 指针自动置 NULL，正是想要的语义，可以把 docstring 里那句手工 nulled 的代码删掉。

---

### E9 · 全部 13 名居民没有立绘也没有头像——RESIDENT_SPRITE_ENABLED 在生产 .env 里根本没设

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一行配置（但需先做一次 provider 连通性验证 + 一次 batch 生成，属于运维动作而非代码改动）

**根因**

backend/app/config.py:152 `resident_sprite_enabled: bool = False`，而 vm212 的 `/opt/skills-world/deploy/.env` 里 grep `sprite|portrait|image` 只有四行 PORTRAIT_LLM_*（BASE_URL/API_KEY/MODEL=gpt-image-2），**没有 RESIDENT_SPRITE_ENABLED**，所以取默认值 False，backend/app/tasks/resident_sprite_worker.py 整条流水线不启动。
生产实证：`resident_sprite_runs = 0`（精确 count）；`curl /residents` 13 条全部 `sprite_url: null, portrait_url: null`；lab_* 全部 23 张表也全 0 行。
玩家侧表现：地图上所有人都用 `sprite_key` 的默认素材（residents.sprite_key 默认值是 '伊莎贝拉'，实际库里是 约翰/简 等预设名），角色卡没有头像。
附注：.env 里那把 PORTRAIT_LLM_API_KEY 是明文写在部署目录的（`sk-pFfNt...`）——本次只读排查看到，建议一并轮换，不在本条修复范围。

**复现**

`curl https://simverse-api.proxypool.eu.org/residents | jq '[.[]|select(.sprite_url==null)]|length'` → 13（全部）。`ssh vm212 'grep -c RESIDENT_SPRITE_ENABLED /opt/skills-world/deploy/.env'` → 0。

**修法**

这是配置缺失不是代码缺陷，但要先确认代码路径可用再开闸：
1. 先在本地/vm212 用 `RESIDENT_SPRITE_ENABLED=true` 跑一遍 resident_sprite_worker，确认 `resident_sprite_provider_base_url` / `_api_key` / `_model`（config.py:154-157）这三个也必须一起配上——现在 .env 里只有 PORTRAIT_LLM_* 这套**不同前缀**的变量，说明立绘和头像是两条独立配置，立绘那条一个都没配。
2. 确认 `resident_sprite_request_cost_upper_bound_usd`（config.py:162，默认 0.0）——如果这是硬预算上限，0.0 意味着即使开了 enabled 也会被立刻拒；要一并设成实际单价上限。
3. 补齐后写进 deploy/.env 与 backend/.env.example，按「开闸单独一次变更」上线，跑一次 batch 给 13 名居民生成，核 `resident_sprite_runs` 从 0 变正数、`residents.sprite_url` 非空。
如果这条流水线本来就不打算在生产开，那要修的是**前端**：给 sprite_url/portrait_url 为 null 时的占位逻辑做体面兜底，而不是让玩家看到清一色默认皮。

---

### E10 · 日报按真实自然日生成并用真实日期命名，与 k=4 世界时钟错配——一份日报盖了 4 个世界日

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一个模块（三处日期源 + 素材窗口换算 + nightly 触发频率取舍 + 历史 digest 日期跳变的处置方案）

**根因**

backend/app/services/digest_service.py 三处都用真实时间：
- :202 `day = day or datetime.now(UTC).date()`
- :40 `start = datetime(day.year, day.month, day.day, tzinfo=UTC)`（素材窗口 = 1 真实日）
- :288 `today = datetime.now(UTC).date()`（个人周报）
而 backend/app/world_clock.py 的模块 docstring 把「日报叙事」明确列进必须读世界时间的清单：「Everything about resident life that has a 时间/星期/日历 meaning — 作息, 星期节律, **日报叙事**, 计划日期 — reads world time」。
后果：k=4 下一份「日报」实际覆盖 4 个世界日的事件，标题却写真实日期。
生产实证：
```sql
SELECT scope, date, left(title,30), created_at FROM digests ORDER BY created_at DESC LIMIT 4;
 village | 2026-07-27 | 雷雨中的小镇：镇志页角藏着古剑线索？ | 2026-07-27 23:00:10+00
 village | 2026-07-26 | 2026-07-26 村落日报                  | 2026-07-26 23:00:09+00
```
同一时刻 `world_date_key()` = **2028-04-15**（real 2026-07-28 20:45 CST，epoch 2026-01-01, k=4）。玩家看到的日报日期和居民计划里的 `generated_date`（a-lan 的 daily_plans_json 里就是 `"generated_date": "2028-04-15"`）对不上，差了近两年。
另注：`2026-07-26 村落日报` 这种标题是 compose_digest(:162) 的兜底 —— LLM 输出不以 `#` 开头时就用 `f"{day} 村落日报"`，22 份 digest 里有 4 份是兜底标题，说明 digest LLM 有稳定的格式失败率。

**复现**

`curl https://simverse-api.proxypool.eu.org/digest/latest | jq .digest.date` → `"2026-07-27"`；同时 `ssh vm212 'docker compose exec -T api python -c "from app import world_clock as wc; print(wc.world_date_key())"'` → `2028-04-15`。必现。

**修法**

digest_service.py 里把三处日期源换成世界时钟，素材窗口换成对应的真实区间：
```python
from app.world_clock import now_world, world_to_real

async def generate_village_digest(db, day=None):
    day = day or now_world().date()          # :202
    ...

async def gather_material(db, day):          # :39-40
    w_start = datetime(day.year, day.month, day.day, tzinfo=_zone())
    start = world_to_real(w_start)           # created_at 存真实时间 → 窗口换算回真实
    end   = world_to_real(w_start + timedelta(days=1))
```
`digests.date` 列的唯一约束是 `(scope, date, user_id)`（:205 的幂等键），改成世界日后**一个真实日会产生 4 个世界日的 digest**，但 nightly_cron 每真实日只跑一次（nightly_cron.py:159 `generate_village_digest(db)`）→ 每 4 个世界日只生成 1 份，会漏 3 天。所以配套要么把 nightly 触发改成按世界日（每 6 真实小时一次），要么显式接受「日报 = 每世界周一份」并改标题措辞。**这个取舍必须先定，不能只改日期字段** —— 只改字段会立刻在 digests 表里制造日期跳变（2026-07-28 → 2028-04-15），历史数据无法对齐。
个人周报 :277 `_week_sunday` / :288 同理，且 `world_week_index()`（world_clock.py:105）就是为这种场景准备的。

---

### E11 · /polls/open 把内部字段 _npc_voters / _proposer_slug 原样吐给未鉴权的公开接口

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一个函数（script_service.open_polls 出参白名单 + 一个断言 _ 前缀键不出现在响应里的测试）

**根因**

backend/app/services/script_service.py:135 直接把整个 options blob 塞进响应：
```python
out.append({
    "id": poll.id, "season_id": poll.season_id, "question": poll.question,
    "options": poll.options_json or [],      # ← 未做字段白名单
    "closes_at": ...,
})
```
而 `options_json` 里除了玩家该看的 label/effect/npc_votes，还塞了两个下划线前缀的内部字段：`_npc_voters`（civic_service.py:189 写入，全量投票人 slug 列表）和 `_proposer_slug`（提案人）。routers/polls.py:29 的 `/polls/open` 是 auth 可选的公开接口。
生产实证：`curl https://simverse-api.proxypool.eu.org/polls/open`（无 token）直接返回完整的 25 人 `_npc_voters` 数组。这同时也把 E8 的幽灵名单暴露给了玩家——玩家能直接看到一堆世界里根本不存在的名字在投票。

**复现**

`curl -s https://simverse-api.proxypool.eu.org/polls/open | grep -o '_npc_voters' | head` → 有命中，无需任何鉴权。

**修法**

在 script_service.py:130-137 出参处做白名单，而不是靠调用方过滤：
```python
_PUBLIC_OPTION_KEYS = ("label", "effect", "npc_votes", "won", "final_votes")

def _public_option(o: dict) -> dict:
    return {k: o[k] for k in _PUBLIC_OPTION_KEYS if k in o}

out.append({
    "id": poll.id, "season_id": poll.season_id, "question": poll.question,
    "options": [_public_option(o) for o in (poll.options_json or [])],
    "closes_at": ...,
})
```
用白名单不用黑名单（不要写「删掉以 _ 开头的键」）——将来 civic_service 再往 blob 里塞内部字段时白名单自动挡住。
注意 `policy_service.META_OUTCOME`（civic_service.py:513 写进 opts[0]）也是内部字段，同样会被白名单挡掉，符合预期。

---

### E12 · 四个 office 全部无人在任（mayor/town_clerk/postman/doctor），seed/appointment 策略没有补位驱动

**类型** `BUG` ｜ **优先级** `P2` ｜ **工作量** 一个模块（seed 补位脚本 + election 重开 + overview 空缺语义 + 一条 bootstrap 后置 invariant）

**根因**

生产实证：
```sql
SELECT id, office_key, holder_slug, fill_strategy, term_started_at FROM offices;
 1 | mayor      | (空) | election    | (空)
 2 | town_clerk | (空) | seed        | (空)
 3 | postman    | (空) | seed        | (空)
 4 | doctor     | (空) | appointment | (空)
```
四行都建于 2026-07-25 03:47:59，holder_slug 全空。原任者是 07-25 花名册重置删掉的旧居民（seed/reset_builtin_residents.py 的删除清单没有把 offices.holder_slug 一起清，但也没有重新填）。
- `mayor`（fill_strategy=election）依赖 election_service，但唯一那张选举 poll 的四个候选全是幽灵（见 E8），结票必流会 → 永久空缺；
- `town_clerk` / `postman`（fill_strategy=seed）—— 「seed」意味着应该由种子数据填，但 reset_builtin_residents 只重建 residents，不碰 offices；
- `doctor`（appointment）需要镇长任命，而没有镇长 → 死锁。
下游可见影响：`GET /townhall/overview` 返回 `"mayor": null`；civic_service.py:777 的 `_clerk_announce`（镇务公告以文书身份发布）没有 holder，公告落到「系统」名下；digest_service 的 `_pin_digest_bulletin`（:169-180）注释里那套「有 chronicle editor 就以她的名义发布」在 duties 层是有人的（shen-jingshu），但 offices 层四个全空，两套「职位」概念并存本身就容易对不上。

**复现**

`curl https://simverse-api.proxypool.eu.org/townhall/overview | jq .mayor` → `null`。任何依赖 offices.holder_slug 的读都拿到空。必现。

**修法**

按 fill_strategy 分别补驱动：
1. **seed 类（town_clerk / postman）**——在 backend/seed/reset_builtin_residents.py 重建花名册的收尾处，或新增 `seed/ensure_office_holders.py`，按 office_key 从当前 `is_civic_voter` 名册里选一个（可复用 duty 的选人规则）写入 holder_slug + term_started_at。这条最简单也最该先做，因为它不依赖选举。
2. **election 类（mayor）**——先修 E8（作废幽灵选举 poll），再让 `election_service.maybe_open_election`（backend/app/services/election_service.py:75-130）用当前名册重开一次；注意它的 `election_last_opened='2026-07-24'` 和 `election_interval_days` 决定了下次开票时间，作废旧 poll 后需要把这个 key 一并清掉才会立刻重开（:114-124 的 elapsed 判断）。
3. **appointment 类（doctor）**——依赖镇长，等 2 落地后由镇长 agent 或 admin 接口任命；在此之前建议 `/townhall/overview` 对空缺职位返回明确的「空缺」语义而不是 null，让前端能显示「虚位以待」而不是渲染成空白。
另建议加一条 invariant 测试：`offices` 里 fill_strategy='seed' 的行在 bootstrap 完成后 holder_slug 必须非空——现在 bootstrap（docker-compose.yml 的 `alembic upgrade head && python -m seed.reset_builtin_residents`）跑完是绿的，但世界是空的，正是「build 绿 ≠ 完成」。

---

### E2 · 玩家化身不参与 agent loop、无日行动计数——设计如此，不是缺陷

**类型** `WORKING_AS_DESIGNED` ｜ **优先级** `P3` ｜ **工作量** 不修（若要做则是一个新功能：受限行为集 + 新谓词 + 开关，跨 loop/人口口径多处）

**根因**

backend/app/models/resident.py:92-111 的 `is_autonomous` 混合属性明确写了：「Player avatars are registered members of the world, but are never autonomous actors.」实现是 `resident_type in SIM_RESIDENT_TYPES`（civic_membership.py 的常量），`player` 不在其中。agent loop 的三处取数（loop.py:60 `_metabolize_sleepers`、:138 `_tick_round` 主查询、:194/:220 tick 前复查）全部 `WHERE Resident.is_autonomous`，所以两个玩家化身 p-沈静书 / p-赵启文 从不进入 tick。
生产交叉证据（Redis）：`sv:daily_actions:2028-04-15:*` 只有 11 个 key，全部对应 11 个 npc 的 id，两个 player 化身没有计数器。
把这条单列出来，是因为它会被误报成「我的角色不动 / 我的角色不干活」——那不是 bug，是产品设计。如果玩家期望化身有自主行为，那是**新功能**（需要给 player 类型定义受限的自主行为集），不是修复。

**复现**

用有化身的账号登录，观察 p-沈静书 / p-赵启文 在地图上永远保持玩家最后操作的位置与 idle 状态。对照 `docker compose logs agent-worker | grep agent.events` 里只有 11 个 npc slug。

**修法**

不修。若产品确实要「化身在玩家离线时也有生活」，那是新增功能，路线是：
1. 新增 `resident_type='player'` 专用的受限行为集（只允许 IDLE/GO_HOME/OBSERVE 之类无社会后果的动作），不要直接把 player 塞进 `SIM_RESIDENT_TYPES` —— 那会连带把化身拉进 `is_civic_voter` 之外的一堆人口口径（reputation_service.py:146、civic_service.py:744、townhall.py:51 都用 is_autonomous 作人口分母），影响面远超预期。
2. 在 loop.py:138 的主查询里用一个新谓词（如 `is_ticked`）并集 `is_autonomous | (resident_type=='player' and settings.player_avatar_autopilot)`。
3. 必须有开关，且默认关——玩家化身自动行动会产生玩家没授权的社交后果。

---

## 各组补充说明

### persistence

## 组 A 的总结论

**不是「代码没实现持久化」。** 两条持久化链路都实现了、也都在生产上验证过能落库：
- 角色：`onboarding_service.py:99` 写 `user.player_resident_id`，`check_onboarding_needed` 读它。生产库 2 行 user 都正常绑着化身，`LEFT JOIN residents` 无悬空。
- 住房：`home_decor_service.py:128` 写 `resident.home_location_id`，全仓没有任何一处把它置 NULL。生产库 `p-沈静书 → apt_moon` 已固化，公开接口 `GET /residents` 也返回该值。

**真正的根因是身份分裂 + 一次历史事故的余波，两者叠加让玩家以为「每次都要重来」：**
1. （当日 12:34 那次，日志+DB 双证）同一个人有 GitHub 和 Linux.do 两套身份、两行 users，没有账号绑定 → 换个登录按钮就是新玩家。24h token 过期（`config.py:44`）+ 401 自动 logout（`core.ts:49-56`）保证了他每天都要重新点一次登录按钮，也就每天都有机会点错。
2. （更早那次）07-25 16:53 的手工 roster 迁移绕过 `find_targets` 直接调 `purge_residents`，销毁 12 个玩家化身并把 `users.player_resident_id` 清空（`reset_builtin_residents.py:158-164`）。该函数现在已有 `_assert_no_players` 防呆（`reset_builtin_residents.py:85-115`，commit d8f955b），自动路径不会再犯。

所以工作量分配建议：**#1（账号绑定，新功能）是唯一真正要投人力的**，#1b/#1c 是各 1 个函数/1 个迁移的防呆，#5 修完 #1 自动消失，#5a 是产品补完。

## 排查中发现的、玩家没报但相关的问题

1. **`deploy/backend/docker-compose.yml:45` 的 bootstrap 每次 `docker compose up` 都跑 `python -m seed.reset_builtin_residents`。** 现在有 `_assert_no_players` 兜底所以安全，但这个「每次部署自动跑一个会 DELETE 十几张表的脚本」的设计本身很脆——防呆一旦被后来的改动绕过（比如又有人手写 id 列表调 `purge_residents`），玩家数据当场蒸发。建议 bootstrap 只保留 `alembic upgrade head`，roster reset 改成手动一次性运维命令。生产 bootstrap 容器最后一次运行是 2026-07-25 17:12（就是事故当天），07-28 01:25 那次部署只重启了 api，没重跑 bootstrap。

2. **`residents` 表全 11 个内置 NPC 的 `created_at` 都是 `2026-07-25 16:53:47`**（只有 `luo-xiaozhou` 是 17:15）——事故的时间戳直接印在数据里。所有 NPC 的记忆/关系/对话历史都是那一刻之后重新长出来的，世界的「历史纵深」实际上只有 3 天。这会影响任何依赖 NPC 长期记忆的玩法评估，做拟真度验收时别把这当基线。

3. **46 行 users 里只有 2 行完成了选角色**（`count(player_resident_id)=2`）。除去 ~20 行测试账号（`svtest_*` / `burnin-*`），真实注册用户里完成 onboarding 的比例仍然极低。考虑到 `HomeRoute`（`frontend/src/App.tsx:32-35`）在有 token 时**直接渲染 GamePage、完全不检查 onboarding**，用户从 landing 页点进 `/` 就能无角色地进世界——这可能是转化漏斗漏在这里，值得单独查。

4. **`frontend/src/pages/LoginPage.tsx:50` 用的是 `navigate('/onboarding')` 而不是 `{ replace: true }`**（AuthCallbackPage 用了 replace）。密码登录后按浏览器返回键会回到登录页，行为不一致。一行的事。

5. **`backend/app/services/home_decor_service.py:47` 的 `_decor_seen` 是进程内字典缓存**，api 跑多 worker（`main.py` 起了 parent + server process）时每个 worker 各有一份，NPC「注意到装修变化」的彩蛋会在不同 worker 上重复触发/漏触发。不影响本次报告的两条，但属于同一批代码里的既有问题。

### poll-ui

## 结论速览\n\n| 编号 | 性质 | 一句话 |\n|---|---|---|\n| #4 | 缺陷 | `SeasonsPage.tsx:177` 把 option **对象**当 React child，整页崩；后端 `script_service.py:140` 原样吐 `options_json` 是源头 |\n| #2 | 缺陷（**不是功能缺失**） | 投票后端 + 前端**都实装了**，被 #4 的崩溃挡在按钮出现之前，所以 `votes` 表恒为 0 |\n| #4b | 缺陷 | 同一行导致 `_npc_voters` 全名单 / 未落地建筑坐标匿名可拉，`/townhall/overview` 一样漏 |\n| #2b/#2c/#2d | 功能缺失 | 市政厅 tab 只读、无提案 UI、不显示票数 |\n\n**最省事的止血**：改 3 个文件共约 15 行（script_service.py 加投影函数、world.ts 改类型、SeasonsPage.tsx 取 `.label`），#4 + #2 + #4b 一起消掉。\n\n## 玩家没报但相关的问题\n\n**1. 赛季页会把「镇长选举」当普通议案列出（口径不一致，需拍板）**\n`backend/app/routers/townhall.py:83-85` 的 `_open_proposals` 明确过滤掉 `ELECTION_TAG`（= 「镇长选举」，`election_service.py:31`），但 `backend/app/routers/polls.py:38` 的 `/polls/open` **没有任何过滤**。生产实测 `/polls/open` 返回 3 张而 `/townhall/overview` 返回 2 张，差的就是那张选举票。\n后果：#4 修完之后，玩家会在赛季页看到「镇长选举:谁来当下一任镇长?」并可以直接投票 —— 这可能是想要的（玩家参与选举），也可能是意外（选举本该有独立入口和资格规则）。**动 #4 之前先确认这是不是预期**，否则一次修复顺带开了一个没设计过的玩法入口。\n\n**2. `PollData.season_id` 类型错误**\n`frontend/src/services/api/world.ts:92` 声明 `season_id: string`，生产实测 3 张 poll 全是 `\"season_id\": null`（civic poll 不挂赛季）。当前没触发崩溃只因为没人渲染这个字段，但类型是假的，建议随 #4 的 B 步一起改成 `string | null`。\n\n**3. 赛季相关的两条死代码路径**\n`script_service.py:189 settle_season_polls()` 只处理 `Poll.season_id == season_id` 的票 —— 而全库唯一的 Poll 写入口 `civic_service.propose()`（civic_service.py:74）**从不设置 season_id**。也就是说这个函数在生产上永远扫到 0 行，赛季结算里的 poll 环节是空转；实际结票走的是 `civic_service.close_due_polls()`。建议确认后删掉或补上 season_id 归属，否则后续维护者会照着这条死路径改。\n\n**4. 形状分歧没有 single source of truth（这是本组问题的元凶）**\n同一个 `/polls/open` 响应，`TownHallPanel.tsx:171` 按对象读（`(o as {label?}).label`）、`SeasonsPage.tsx:177` 按字符串读、`townhall.ts:22` 声明成 `Array<Record<string, unknown>>`、`world.ts:94` 声明成 `string[]` —— 四处四种理解。根治办法是给 `/polls/open` 加 FastAPI `response_model`（Pydantic `PollOut`/`PollOptionOut`），让后端 schema 成为唯一真相，前端类型从它生成。当前 `backend/app/routers/polls.py` 三个端点**都没有 response_model**，返回值是裸 dict，FastAPI 不做任何序列化收口 —— 这也是 #4b 泄漏能发生的结构性原因。\n\n**5. SeasonsPage 零测试覆盖**\n`frontend/src/pages/` 下只有 `LandingPage.test.tsx` 和 `LoginPage.test.tsx`。而 `TownHallPanel.test.tsx:23` 用的 mock 数据恰恰是**正确的对象形状** —— 说明当时写 TownHall 的人知道形状，但 SeasonsPage（bc8f1fe 引入，之后只被 79e43b9/db6d498 碰过时间格式和分享卡）从未被这个认知覆盖到。修复必须带上 SeasonsPage 的渲染测试，否则同类漂移还会再发生一次。\n\n## 已核过、确认**不是**问题的点（省得重复排查）\n\n- **投票落库/计票逻辑本身没坏**：`cast_vote`（script_service.py:154）的 open/截止/越界/重复投票四道校验齐全，唯一约束兜底也在；`_close_one`（civic_service.py:485-495）确实把玩家票加进 `npc_votes` 一起 tally，`close_due_polls`（civic_service.py:467）扫全部 open poll（选举也算）。\n- **路由注册没问题**：`main.py:179/182` 都 include 了，线上 `POST /polls/{id}/vote` 无 token 返回 401 而非 404。\n- **没有隐藏的玩家投票资格闸门**：全库 grep `civic_right|voting_right`，只有 `civic_service.py:557 _winner_lost_civic_rights` 一处，且只作用于**当选 NPC** 的结票复核，不拦玩家投票。\n- **存量 poll 缺 `_eligible_at_open` 不影响**：生产 3 张 poll 的 `opts[0]` 确实没有该键（F2 之前开的），但 `_policy_threshold_verdict`（civic_service.py:634-637）有回落实时计数的分支，且整段只在 `polis_policy_approval_enabled` 为真时才走 —— 该开关 `config.py:601` 默认 `False`。非问题。\n- **Admin 治理面板不受影响**：`GovernanceInsightsPanel.tsx:144` 只渲染 `poll.options.length`，不碰元素本身。\n\n## 生产只读验证清单（全程未做任何写操作）\n\n```\n# vm212，容器内 psql，只读 SELECT\ndocker exec deploy-db-1 psql -U postgres -d skills_world \\\n  -c \"SELECT id,status,left(question,40),closes_at,json_array_length(options_json) FROM polls ...\"  → 3 open\n  -c \"SELECT count(*) FROM votes;\"                                                                  → 0\n  -c \"SELECT id, options_json::text FROM polls WHERE status='open';\"                                → 全对象形状\n# 公网 API\ncurl -s https://simverse-api.proxypool.eu.org/polls/open            → 泄漏 _npc_voters/_proposer_slug/effect\ncurl -s https://simverse-api.proxypool.eu.org/townhall/overview     → 同样泄漏\ncurl -X POST .../polls/{id}/vote（无 token）                        → 401（接口存在）\n# 线上前端产物\ncurl -s https://simverse.world/assets/SeasonsPage-mBwfgq0G.js       → children:[e,...] 逐字命中崩溃点\n```\n

### debate-digest

## 结论速览

| 编号 | 性质 | 一句话 |
|---|---|---|
| #3-1 | 缺陷 P0 | 辩论状态机没人推，永远 announced，玩家 10 SC 冻结 |
| #3-2 | 功能缺失 P1 | 从来没有 POST /debates，玩家不可能自己开辩论 |
| #3-3 | 设计如此但参数病态 P1 | 唯一产生链=公开课结束，7 天冷却 × 只有 1 个讲师 |
| #3-4 | 缺陷 P2 | 辩题拼成了讲师姓名 |
| #6-1 | 缺陷 P0 | compose_digest 绕过 chat()，thinking 没关 + max_tokens 800，正文被截成空 |
| #6-2 | 缺陷 P0 | 空正文照样落库 + 幂等钉死，连续 4 天空白面板 |
| #6-3 | 功能缺失 P2 | 没有重生成入口，存量 4 天空日报只能人工补 |

日报的 cron 确实在跑（backend/app/tasks/nightly_cron.py:533 nightly_cron_loop，18 天无缺口，最近一条 2026-07-27 23:00:10 UTC）。所以 #6 不是 cron 没跑、也不是 fail-open 吞掉，而是「跑了、写了、写了个空的、而且永远不再重试」。agent-worker 容器 2026-07-28 01:23:19 重启过，日志被截断，所以拿不到当时的 digest 日志行，改用 llm_usage 表还原。

## 玩家没报但顺手挖到的相关问题

**1. debate_stakes 有孤儿行且没有 FK（P2）**
SELECT s.id,s.debate_id,s.amount,s.payout,(d.id IS NULL) AS orphan FROM debate_stakes s LEFT JOIN debates d ON d.id=s.debate_id; → 3b7490d4…|51be50ba-efea-4780-8351-2f121d062a52|20|20|t（对应 debate 已不存在）。backend/app/models/debate.py:39 的 debate_id 只是 String + index，没有 ForeignKey/ondelete，07-25 清库时 debates 被删而 stakes 留下。跟 commit 104f2b1 刚补的 resident_sprite_runs FK 是同一类问题，建议一并补 ForeignKey('debates.id', ondelete='CASCADE')。

**2. 顾明远在 7 天冷却期内每次 WORK 都白跑（P3）**
backend/app/services/duty_service.py:315-321 冷却期内 _work_lecturer 返回 None；backend/app/services/duty_service.py:126-141 的 on_work 只在 result is not None 时才设 Redis 冷却和发工资。于是 lecturer 在 7 天里每个 WORK tick 都跑一次 DB 查询、什么都不产出、也拿不到工资，跟其他 duty（20h 冷却就有产出）体验不一致。

**3. 日报素材窗口的历史坑已修，但耦合很脆（无需动作）**
07-10~07-24 的 digests.stats_json 里 chat_count/shift_count 全是 0。原因是当时 nightly 锚点是 UTC 00:30，而 generate_village_digest(day=None) 取 datetime.now(UTC).date() 即当天，backend/app/services/digest_service.py:40-41 的窗口 [当天00:00, 次日00:00) 在生成时才开了 30 分钟。改成北京 07:00 锚点后（backend/app/tasks/nightly_cron.py:29 RUN_HOUR=7，即 UTC 23:00），now(UTC).date() 自然变成前一天，窗口是完整 24 小时，07-25 起 chat_count 跳到 10（limit 上限）。已修复，但这是靠时区巧合成立的隐式耦合，将来谁再动 RUN_HOUR 会再次咬人，建议把 day 显式写成 now_real().date() - timedelta(days=1)。

**4. gather_material 的 events 查询不带日期过滤（P3）**
backend/app/services/digest_service.py:56-58 只按 WorldEvent.is_active.is_(True) 取，不限当天。因为天气事件（backend/app/tasks/weather.py）几乎永远有一条 active，has_material 事实上恒为 True，:211 的冷启动兜底文案基本永远不触发 —— 这也是为什么 4 天空日报没退化成兜底文案而是直接空串。修 #6-2 时顺手给 events 加 starts_at < end AND ends_at >= start 更诚实。

**5. 其他 client.messages.create() 直调点（P2，与 #6-1 同源，会连累 #3-1）**
grep -rn "messages.create(" backend/app 命中 20+ 处绕过 chat() 的调用：backend/app/services/digest_service.py:156/330、backend/app/services/debate_service.py:172、backend/app/services/dream_service.py:78/89、backend/app/services/gossip_service.py:41、backend/app/services/goal_service.py:105、backend/app/services/sbti_service.py:205、backend/app/services/sprite_service.py:114、backend/app/forge/*.py 全家。它们同样不会传 thinking:{'type':'disabled'}，也各自复制了一份 _extract_text。其中 backend/app/services/debate_service.py:172 是 max_tokens=200 配中文 60 字辩词 —— 一旦 #3-1 的驱动器上线，很可能重演 digest 的空输出：_extract_text 返回 ''，辩词全空但流程照常走完并结算。建议修 #3-1 之前先把 debate_service 这处换成 llm_chat()。

### agent-behavior

## 玩家没报、但排查中确认的相关问题

**1. 玩家实际是在和自己的化身对话（可能是 #8 体验落差的放大器）**
生产库 residents 表：`p-沈静书 | 沈静书 | resident_type=player | creator_id=11769050-... | created_at=2026-07-28 05:24:21`，而 conversations 表里 `c912b020 | 不做了睡大觉(11769050) | p-沈静书 | 3 turns | 05:34:56`。也就是说玩家 05:24 在 onboarding 里造了一个叫「沈静书」的玩家化身，10 分钟后和它聊天，那句「带你去五金店」是这个化身说的，不是 NPC `shen-jingshu`（NPC 是 2026-07-25 seed 的独立 resident，1449 条记忆）。

两点值得拍板：
- `start_chat`（`backend/app/ws/handlers/chat.py:37`）对 `resident_type='player'` 的目标不做任何区分，走的是完全一样的 NPC 人格对话路径。玩家化身按设计永不 autonomous（`app/models/resident.py:93-111`），所以它做出的任何承诺 100% 无法兑现 —— 比 NPC 更严重。
- 世界里出现了同名的 NPC「沈静书」和玩家化身「沈静书」（slug 不同、name 相同）。前端如果按 name 展示，玩家分不清在跟谁说话。建议 onboarding 建化身时对 name 做一次与现有 residents 的查重。

**2. 同名双账号**
`SELECT id,name,player_resident_id FROM users WHERE name='不做了睡大觉'` 返回两行：`11769050-...`（化身 p-沈静书）和 `176a210c-...`（化身 p-赵启文，创建于 12:34）。同一个人很可能注册了两次（或两个 OAuth 源没打通）。记忆、成就、关系全部按 user_id 分裂 —— 玩家会感到「居民不认识我了」，而这跟记忆系统本身无关。值得查 `app/services/github_auth.py` / `linuxdo_auth.py` 的账号合并逻辑。

**3. `search_events_vector` 是死代码**
`backend/app/memory/service.py:384-422` 写了完整的 pgvector 余弦查询（含 `archived_at IS NULL` 过滤和 fallback），全仓零调用点。生产用的是 pgvector/pgvector:pg16 镜像、`memories.embedding` 是 `vector(1024)`、ollama 跑 `qwen3-embedding:0.6b` 且 `OLLAMA_EMBED_DIMENSIONS=1024` —— 维度是对得上的，pgvector 检索本可以直接用。#7-a 的修法正好把它复活。

**4. embedding 维度/环境已核实无误，不是问题源**
`OLLAMA_EMBED_MODEL=qwen3-embedding:0.6b`（原生 1024 维）+ `OLLAMA_EMBED_DIMENSIONS=1024` + `EmbeddingVector` 在 PG 上映射 `PGVector(1024)`（`app/models/memory.py:38-52`），`_fit()`（`app/memory/embedding.py:47-54`）无需截断。`chat_resident` 来源的记忆 embedding 覆盖率 100%（2948/2948），证明 ollama 端点是通的。所以 #7-b 纯粹是写入侧漏算 + backfill 吞吐/排序的问题，不是 embedding 服务坏了。

**5. 07-25 事故后的居民记忆不是空的**
`SELECT r.slug, count(m.id) FROM residents r LEFT JOIN memories m ON m.resident_id=r.id GROUP BY 1` → 11 位 NPC 每人 1320~1646 条。归档也没过度：`count(archived_at)` 全表为 0（`realism_evict_importance_floor=0.35` + `idle_days=90`，世界才跑 3 天，nightly_cron.py:453 的 evict 还没到条件）。所以「重新 seed 后记忆全空」这条假设排除。

**6. 测试盲区**
`backend/tests/test_realism_memory_retrieval.py` 全部用例每个 resident 只造 2~3 条记忆，永远不跨 30 条候选池上限，所以 #7-a 那个截断缺陷在 CI 里恒绿。补回归测试时务必造 50+ 条噪声。

## 声明范围
- 全程只读，未修改仓库任何文件；vm212 上只跑了 `psql -c SELECT` / `docker compose ps` / `grep .env`，无任何写操作。
- 所有根因均已读到具体行；#7-e 标注为「代码缺陷已确认，生产实例未触发」，没有编造证据。

### prod-runtime

## 组 E · 生产运行时全景（2026-07-28 20:45 CST 快照，vm212 只读）

排查期间**未对 vm212 做任何写操作**，仅 SELECT / docker compose logs / curl。仓库文件未修改。

### 0. 环境基线

| 项 | 读数 |
|---|---|
| 容器 | api / agent-worker / db(pgvector pg16) / redis8 全 Up，api+worker 已跑 11h |
| alembic | `051_add_civic_standing_history`（与本地 master 一致） |
| 真实时间 | 2026-07-28T20:45:22+08:00 |
| **世界时间** | **2028-04-15T11:01:29+08:00**（world_hour=11, weekday=5, week_index=119） |
| 世界时钟 | `WORLD_EPOCH=2026-01-01T00:00:00+08:00`, `WORLD_CLOCK_K=4` |
| 心跳 | 5 条 loop 全活：agent=12:52:40 / heat=12:23:21 / embedding_backfill=12:31:52 / event / nightly=01:23:19(每日一拍，正常) |

### 1. 数据全景（精确 count，非 n_live_tup）

**核心 8 表**：users=46, residents=13, memories=16554, conversations=3, messages=22, debates=1, polls=3, **votes=0**

**全部非空表**（降序）：
```
memories 16554 | llm_usage 15159 | transactions 243 | world_events 103 | personality_history 63
feed_events 60 | resident_relations 48 | users 46 | user_achievements 44 | notifications 40
location_visits 30 | bulletin_posts 26 | digests 22 | messages 22 | daily_quests 21
resident_goals 18 | policies 17 | purchases 14 | residents 13 | achievements 12 | items 12
forge_sessions 8 | resident_treasuries 7 | commissions 4 | offices 4 | conversations 3
polls 3 | system_config 3 | debate_stakes 2 | issue_stances 2 | alembic_version 1
debates 1 | time_capsules 1 | town_treasuries 1
```

**空表（41 张）—— 直接证据**：
- `seasons` / `season_scores` / `season_scripts` = 0 → **赛季功能从未开季**（见 E7，代码里根本没有建季入口）
- `votes` = 0 → 3 张 poll 开着，**真人票一张没有**（NPC 票走 options_json 不入 votes 表；见 E8）
- `resident_sprite_runs` = 0 → 立绘流水线从未跑过（见 E9）
- `follows` = 0 / `goal_investments` = 0 / `world_change_proposals` = 0 / `world_revisions` = 0 / `dynamic_locations` = 0 / `dynamic_mechanics` = 0 → 社交关注、目标投资、世界改造三条线零使用
- `civic_standing_history` = 0 / `coin_holds` = 0 / `coin_hold_entries` = 0 → 07-27B 刚落地的政绩/冻结机制尚无数据
- `outbox_events` = 0 / `pending_messages` = 0
- **全部 23 张 `lab_*` = 0** → 实验楼在生产完全未启用
- `purchases`=14 但 `items`=12、`achievements`=12 有数据 → 商店/成就是唯一被玩家真实触达的子系统

### 2. 玩家化身现状（#1/#5 关键读数）

```sql
SELECT count(*) total, count(player_resident_id) with_ptr,
       count(*) FILTER (WHERE player_resident_id IS NOT NULL
         AND EXISTS (SELECT 1 FROM residents r WHERE r.id=u.player_resident_id)) resolvable
FROM users u;
-- 46 | 2 | 2
```

**46 → 2 这个数字会被严重误读，务必按下面口径引用**：

- 46 个账号里 **41 个是测试/系统号**（`sv-test.dev` / `e2etest.com` / `@test.dev` / `burnin-smoke*` / `@t.dev` / `@t.io` / `*smoke*` / `skills.world`）。
- **真人账号只有 5 个，对应 4 个自然人**：
  | 账号 | 邮箱 | 化身 | 最后登录 |
  |---|---|---|---|
  | 不做了睡大觉 | stawky@linux.do | `c3c149e7`(p-赵启文) | 2026-07-28 |
  | 不做了睡大觉 | stakeswky@github.users | `8c470622`(p-沈静书) | 2026-07-28 |
  | tlolee | tlolee@github.users | 无 | NULL |
  | Артём | engineering-wrench@github.users | 无 | NULL |
  | Konstantin Z. | konstantine.zam@yandex.ru | 无 | NULL |
- 后 3 个 `last_login_date` 全 NULL —— 注册后没回来过，**没化身是没走完 onboarding，不是 bug**。
- 2 个指针**全部可解析**，当前无悬空。但 `users` 表**一条外键都没有**（见 E1），指针是裸字符串，07-25 那次事故就发生在这个窗口。

**residents 构成**：`npc=11, player=2, ugc=0`。**零个玩家创建的居民存活** —— forge_sessions 有 8 条（6 条 done），forge_creation 流水 9 笔共 +450 币，但产物全部在 07-25 花名册重置中消失，且这 8 条 session 全属于测试号。

### 3. worker 在做什么（`docker compose logs --since 2h agent-worker`）

**节奏正常，没有挂死**。每 ~60s 一轮 tick，每轮 4-8 名居民出动作，日志形如：
```
12:39:25 {"resident":"su-xiaoman","action":"VISIT_DISTRICT","target_slug":"central_plaza","target_tile":[75,56],"reason":"去凑热闹"}
12:41:58 {"resident":"chen-tiesheng","action":"CHAT_RESIDENT","target_slug":"zhou-dahe","reason":"想聊天"}
```

24h 内**只有两类异常**：
1. `app.personality.evolution: Drift/Shift skipped for <id>: monthly budget exhausted` —— 每轮都刷，**全员人格演化已冻结**（见 E4）
2. `WARNING app.agent.tick: Phase failed for a-lan: 'importance'` —— 24h 内 26 次，集中在 01:23:20~03:44:34，期间 a-lan 完全不动（见 E5）

**除此之外零 error / 零 traceback。**

一个值得其它组注意的观察：日志里 `CHAT_RESIDENT` 且 `target_slug: null` 反复出现（zhou-dahe、zhao-qiwen），即「想聊天但没有对象」也会消耗一次行动，理由文本还是长篇 LLM 输出——这是一次白烧的 decide 调用。

### 4. API 错误面

- `--since 2h api | grep -iE "error|exception|500"` → **零命中**
- `--since 168h`（7 天）→ traceback / internal server error **0 次**
- 7 天内非 2xx 分布：`401×8, 404×5, 400×2, 405×1` —— 没有 5xx
- 今天两条 `POST /debates/{id}/stake → 400`：玩家 176a210c 20:36:13 成功押注 10 币后又点了两次，被 debate_service.py:84「already staked」挡下。**400 本身正确，但那 10 币永远拿不回来**（E3）

**结论：API 层是健康的。玩家看到的问题全部在业务逻辑/数据层，不在服务可用性。**

### 5. 前端实际请求（公开接口 curl 实测）

| 接口 | 状态 | 返回要点 |
|---|---|---|
| `GET /residents` | 200 | 13 条，**全部 `sprite_url:null` + `portrait_url:null`** |
| `GET /world/locations` | 200 | 正常，含 academy/tavern/apt_star 等 |
| `GET /debates` | 200 | 1 条，`status:"announced"`, `pool_a:10`, `transcript:[]` |
| `GET /digest/latest` | 200 | date=`2026-07-27`（真实日期，世界是 2028-04-15） |
| `GET /events/active` | 200 | 2 条：festival「集市日」+ weather「雷阵雨」 |
| `GET /townhall/overview` | 200 | **`"mayor": null`**；duties 有 5 个 holder（与 offices 表全空并存） |
| `GET /polls/open` | 200 | 3 张，**泄漏 `_npc_voters` / `_proposer_slug`** |
| `GET /seasons/current` | 200 | `{"season":null}` |
| `GET /seasons/current/leaderboard` | 200 | `{"top":[],"season":null}` |
| `GET /bulletin` | 200 | 正常，hot_residents 含 p-沈静书 |
| `GET /seasons` `/polls` `/world` `/world/state` | 404 | 路由不存在（seasons 只有 /current 两条） |
| `GET /commissions?status=open` `/feed` | 401 | 需 token，未测 |
| `https://simverse.world` | 200 | 前端可访问 |

**地图上的可见异常**：13 名居民里 **5 个坐标完全相同 (54,74)** —— zhao-qiwen / he-qiaoyun / a-lan / luo-xiaozhou / jiang-lin，全是 apt_star（星光公寓）住户，(54,74) 正是该建筑的 `entrance` 单格（bounds `[51,65,62,75]`, center `[56,70]`）。见 E6。

### 6. 世界时钟与预算（决定「居民不动」是 bug 还是休眠）

**结论：不是预算休眠，也没触顶。**

| 项 | 读数 | 上限 | 占比 |
|---|---|---|---|
| LLM 今日花费 | **$0.6871**（4819 calls） | `BUDGET_GLOBAL_DAILY_USD=10.0` | **6.9%** |
| 昨日 | $0.7482（5369 calls） | 10.0 | 7.5% |
| agent 日行动（世界日 2028-04-15） | 最高 43（cce3af14），最低 16 | `AGENT_MAX_DAILY_ACTIONS=100` | **43%** |

11 个 `sv:daily_actions:2028-04-15:*` 计数器：`43, 37, 35, 33, 29, 27, 25, 25, 23, 19, 16` —— **没有一个接近 100**。

今日 LLM 按场景：`chat_turn 2268 / decide 2103 / chat_wrapup 284 / plan 44 / gossip 37 / evolution_drift 37 / player_chat 11 / goal_eval 11 / evolution_shift 9 / dream 6 / evolution_sync 3 / extract 2 / update_rel 2 / digest 1 / reflect 1`。

对比 07-25 之前（$0.0006~$0.019/天），**07-26 起花费跳到 $0.6+/天** —— 那是花名册重置后世界真正跑起来的信号，不是异常。

**因此：任何「居民不动 / 世界像挂了」的玩家报告，都不能归因到预算休眠。** 真实原因在 E5（phase 链 break）、E4（人格冻结）、E6（挤成一格看起来没动）。

### 7. 供其它组交叉引用的额外读数

- **居民聊天不落 conversations/messages**：`chat_turn` 今日 2268 次 LLM 调用，但 `conversations=3 / messages=22`。居民互聊全部写进 `memories`（`source='chat_resident'` 3041 条），`conversations/messages` 只承载玩家↔居民私聊。**玩家看不到居民之间聊了什么**，除非走 memories 相关接口。
- **feed_events 只有 60 条且不含社交**：kinds = `wage 20 / duty_output 20 / personality_shift 10 / goal_milestone 4 / goal_achieved 2 / creation 2 / work_listed 2`。世界里最高频的活动（聊天、移动、观察）**完全不进 feed**。任何「动态页很空 / 感觉世界没在动」的报告，根因在这里。
- **玩家真实互动量**：3 段对话（全部 2026-07-28 05:31~05:36，同一个玩家），22 条消息（user 11 / assistant 11）。其中 `c109ef9d` 与 chen-tiesheng 的对话 `turns=0` 却有 `rating=1` —— **0 轮对话可以打分**，值得单独查（chat_service 的 rating 入口没有 turns>0 前置）。
- **经济流水**（transactions 按 reason 前缀）：`chat 64笔 -64 / creator_passive 56 +56 / signup_bonus 44 +4400 / daily_login_reward 27 +250 / achievement 20 +450 / purchase 14 -170 / forge_creation 9 +450 / player_chat 3 -3 / good_rating 3 +15 / debate_stake 2 -30 / debate_win 1 +20`。注册奖励 4400 币占了绝对大头，实际消耗只有 -267 —— **币几乎没有沉淀出口**。
- **debate_stakes 有孤儿行**：`3b7490d4` 指向 debate `51be50ba`，该 debate 在 debates 表里不存在（07-25 被删）。`debate_stakes.debate_id` 同样**没有外键**（与 E1 同类问题）。
- **system_config 三行**：`election_last_opened="2026-07-24"` / `civic_poll_postpone_until="2026-07-31T23:29:43+00:00"` / `town_last_spend_at="2026-07-27T23:01:14"`。三张 poll 的 `closes_at` 全是 `2026-07-31 23:29:43+00` —— **3 天后会以幽灵票结票**，E8 有时间压力。
- **digest LLM 格式失败率**：22 份 digest 里 4 份标题是兜底模板 `"YYYY-MM-DD 村落日报"`（digest_service.py:162 的 `if text.startswith("#")` 分支没命中），约 18%。
- **residents.home_tile_x / home_tile_y 全表 NULL** —— grep 确认**全仓库没有任何代码写入这两列**，只有 4 处读。是彻底的死字段（E6 的直接成因）。
- **`.env` 明文密钥**：`/opt/skills-world/deploy/.env` 里 `PORTRAIT_LLM_API_KEY=sk-pFfNt...` 是明文。本次只读排查顺带看到，建议轮换 —— 不在任何一条 finding 的修复范围内，单独处理。

### 8. 一句话结论

**服务是健康的（7 天零 5xx，5 条 loop 全活，预算用了 7%，行动 cap 用了 43%）；坏的全是业务闭环 —— 辩论押注有入口没出口（币永久沉没）、赛季从来没开过季、人格预算按错时钟算导致全员冻结、五个居民挤在公寓门口一格、三张 poll 被已删除居民的幽灵票锁死。玩家看到的「世界很空 / 不动 / 投票没意义」，每一条都能落到上面某个 file:line。**

