# 执行计划：P1-5 + 底座周（S1–S5）

> 跨会话接续用的规划文档。真相进度仍以 `docs/PROGRESS.md` 为准；本文只是这两块的施工设计。
> 规格出处：P1-5 见 `docs/OPTIMIZATION_PLAN.md` §145-149；S1–S5 见 `docs/FEATURE_SPECS.md` §1（迁移 012-016）。

## 锁定的决策（2026-07-09 与 Jimmy 确认）

1. **P1-5 quick-forge 进度也改 WS**：不保留轮询兜底。quick 那条链（内存 session、历史上子进程跑 LLM）由**父进程（API 进程内）代发** `manager.send`——即父进程感知子进程/后台任务的 stage 推进后再广播，子进程本身不直连 WS。
2. **底座周内部按依赖序施工**：`S1 → S4 → S2 → S3 → S5`（非数字序）。硬依赖边：S2 成就解锁调用 S4 `notify()` 与新建的 `app/events/bus.py`；S5 经该 bus `emit`。规格 §40 与 S5 注明允许非号码序落地，`down_revision` 指向落地时实际链头即可。
3. 计划落成本文档，然后直接开工 P1-5。

## 顺序总览

`P1-5 → S1 → S4 → S2 → S3 → S5`

P1-5 与底座周互不依赖，先做（是 PROGRESS 的下一个未勾选任务）。一次一个任务，做完勾 PROGRESS 再取下一个。

---

## P1-5 — 前端网络层健壮性（约 1.5 人日）

### A. apiFetch 超时/取消 + 集中登出
- `services/api.ts`：`apiFetch` 加 `signal: AbortSignal.timeout(15000)`；若调用方传了 `options.signal`，用 `AbortSignal.any([caller, timeout])` 合流以支持组件卸载取消。超时抛可识别错误（`name==='TimeoutError'` 或包装成友好文案）。
- 401 集中登出：改调已存在的 `useGameStore.getState().logout()`（清 token/user/state），**删掉** `window.location.href='/login'` 的直接跳转（并发请求下会多次跳转、丢状态）。登出后由 `ProtectedRoute` 自然重定向到 `/login`。
- 注意：`api.ts` 现在读 `localStorage.getItem('token')`，`logout()` 会清 store 与 localStorage 两处，保持一致。

### B. Forge 进度轮询 → WS 推送
**后端**（`app/forge/pipeline.py`、`app/routers/forge.py`）：
- deep（`_run_deep`）：每个 stage 切换后 `await manager.send(user_id, {"type":"forge_progress","forge_id","stage","status"})`；`done`/`error` 发 `forge_done`/`forge_error`。`_run_deep` 是 API 进程内 async 后台任务，可直接 push。
- guided（`forge_answer` 的 `_run_pipeline` 后台任务）：同样在生成阶段推进处 push。
- quick（`/quick` 的 `_run`）：按决策 1a，父进程代发 —— 若 quick 走子进程，父任务在子进程状态变化时代为 `manager.send`；若已是进程内 async，则同 deep。
- `ForgeSession` 需带 `user_id`（deep-start 已传；guided/quick 补齐到 session 或闭包捕获）。
- WS 出站消息为 dict；`app/ws/protocol.py` 目前只建**入站**模型，无需改（除非决定给出站也建模，则同步补）。

**前端**（`services/ws.ts` + 三个 forge 组件）：
- `ws.ts` onmessage 加 `forge_progress`/`forge_done`/`forge_error` 分支（本仓所有新 WS 出站类型的前端接入点都在这里），派发给 `onWSMessage` listeners。
- `components/forge/{DeepForge,QuickForge,ForgeChat}.tsx`：`onWSMessage` 订阅、按 `forge_id` 过滤，删除三处 `setInterval` 轮询与其 `clearInterval`/`pollRef`。保留一次性 `GET /status` 兜底拉取（首屏/重连补状态）。

**DoD**：`npx tsc --noEmit` + `npm run lint`（基线 7err/3warn）通过；`npm run build` 分包正常（挂载盘 dist 清理 EPERM 属环境限制，产物写 `/tmp` 复核）；后端 WS 推送带 pytest（`cd backend` 全绿，既有基线失败除外）。

**风险/待验证**：quick forge 的子进程/内存 session 细节需开工时按实际代码确认；父进程代发若需轮询子进程状态，尽量用事件而非 busy-poll。

---

## 底座周共享原语（先于依赖它的任务落地）

- **`app/events/bus.py`**（名义属 S2，实为基建）：进程内同步发布器约 30 行 —— `_handlers: dict[str, list[Callable]]`、`on(event)` 装饰器、`async emit(db, event, **kw)` 逐个 await handler、单个失败 `logger.warning(exc_info=True)` 不阻断。S2/S5 及后续 B1/D1/D3 都 import。建议作为 S2 的第一个提交独立落地。
- **cron / 后台消费者的进程归属**：P0-3b 后 deploy = `api`(RUN_BACKGROUND_TASKS=false, UVICORN_WORKERS=2) + `agent-worker`(跑 loop)。`event_cron`（S1）与 location 消费者（S5）**只在 RUN_BACKGROUND_TASKS=true 的单实例（agent-worker）跑**，广播经 Redis pub/sub 扇出到各 API worker 的 socket，避免多 worker 重复广播。这是接线要点。

---

## S1 世界事件总线（迁移 012 `world_events`）

- **表**：`models/world_event.py` —— id(uuid pk)、type(String20: festival|weather|news|custom|script)、title(200)、description(Text, 注入 prompt 文案)、payload_json(JSON)、starts_at、ends_at、created_by(FK users.id nullable)、is_active(index)。
- **三消费点**：① `agent/schemas.py::TickContext` 加 `world_events: list[dict]`，`phases/perceive/basic.py` 查 `is_active=True`（每 tick 一次、模块级缓存 60s）写 ctx，`phases/decide/basic.py` prompt 加「当前世界事件：{titles}」；② `llm/prompt.py::assemble_system_prompt` 追加活跃事件段落（同缓存）；③ cron 翻转 `is_active` 时 `manager.broadcast({"type":"world_event","event":{...},"phase":"start"|"end"})`。
- **cron**：`app/tasks/event_cron.py`，仿 `heat_cron.py`，每 60s 扫 starts_at/ends_at 翻转 + 广播；main.py lifespan 注册（随 agent-worker 走）。
- **API**：`routers/admin/events.py` `GET/POST/PATCH/DELETE /admin/events`（require_admin）；公共 `GET /events/active`。注册进 `app/main.py`。
- **测试**：`tests/test_world_events.py` 覆盖 cron 翻转与两处 prompt 注入。
- **验收**：投放后 60s 内全端广播；活跃期居民决策 prompt 与玩家对话 prompt 含事件文案。

## S4 通知中心（迁移 015 `notifications`）

- **表**：id、user_id(index)、kind(String30: resident_greeting|achievement|capsule_delivered|commission|feed|system)、title、body、payload_json、read_at(nullable)、created_at。
- **服务**：`services/notification_service.py::notify(db, user_id, kind, title, body, payload)` —— 写表；若 `manager.active` 在线则同时 `manager.send(user_id, {"type":"notification",...})`。
- **API**：`GET /notifications?unread_only=&cursor=`、`POST /notifications/read {ids}`。
- **前端**：`TopNav.tsx` 铃铛+未读角标；`services/ws.ts` 加 `notification` 分支；新组件 `components/NotificationDrawer.tsx`。
- **验收**：离线产生的通知上线后可拉取；已读状态持久。

## S2 事件钩子 + 成就引擎（迁移 013 `achievements` + `user_achievements`）

- **表**：Achievement(code pk String50、title、description、icon、points、reward_sc、hidden)；UserAchievement(id、user_id FK index、code FK、progress_json nullable、unlocked_at nullable、UniqueConstraint(user_id, code))。
- **bus**：先落 `app/events/bus.py`（见共享原语）。
- **埋点（首批 6）**：`chat_completed`(ws end_chat 分支, kw user_id/resident_id/turns)、`memory_written_about_user`(memory/service.py add_memory related_user_id 非空)、`personality_shifted`(personality/evolution.py 跳变提交处)、`login_streak`(daily_reward_service claim)、`location_first_visit`(S5)、`commission_completed`(B1，占位)。
- **检查器**：`app/events/achievements.py` 每成就一个纯函数注册到对应事件；解锁 → `coin_service.reward` + S4 `notify` + WS `{"type":"achievement_unlocked","code","title","reward_sc"}`。
- **API/seed**：`GET /achievements`（定义+我的进度合并）；`backend/seed/achievements.py`。
- **验收**：首次对话触发 `first_chat` 解锁+到账+toast；重复触发幂等（unique + upsert）。

## S3 商店管线（迁移 014 `items` + `purchases`）

- **表**：Item(id、code unique String50、kind String20: consumable|gift|decor|cosmetic、name、description、icon、price_sc、payload_json、active)；Purchase(id、user_id index、item_code、qty、total_sc、context_json nullable、created_at)。
- **服务**：`services/shop_service.py::purchase(db, user_id, item_code, qty, context)` —— 查 Item → `charge()`(reason=`purchase:{code}`，不足 400) → 写 Purchase → 按 kind 分发效果处理器（`shop_effects.py` 注册表，D2/B3/A3 各注册自己的效果函数）。
- **API**：`GET /shop/catalog`、`POST /shop/purchase {item_code, qty, context}`；管理端 `GET/POST/PATCH /admin/items`。
- **验收**：余额不足购买返回 400 且无副作用（事务内）；购买成功 transactions 有记录。

## S5 位置进入检测 LocationTracker（迁移 016 `location_visits`）

- **表**：id、user_id(index)、location_id(String50)、first_visited_at、visit_count、last_visited_at、UniqueConstraint(user_id, location_id)。
- **实现**：`services/location_tracker.py` —— 启动从 `agent/map_data.py` 20 个命名位置构建 tile→location 查找表（bbox 展开 dict, O(1)）；`ws/handler.py` move 快路径加 `location_tracker.on_move(user_id, tile_x, tile_y)`（**纯内存**比较上次 location，未变化立即返回，不在热路径查库）；进入新位置投递 `asyncio.Queue`，单后台消费者批量 upsert（visit_count）并在**首访**（由 DB upsert 结果判定，非仅内存）`emit("location_first_visit")` + 调 B2 偶遇判定（占位）。
- **验收**：move 处理 P99 无可测退化（队列异步化）；首访写库一次、成就事件恰一次。

---

## 横切约束（全程）

- **迁移 012-016**：单表 up/down 在 sqlite 隔离实测；**全链 `alembic upgrade head` + 真 PG 复验留 vm212**（沙盒无 pgvector）。`down_revision` 串到落地时实际链头。
- **测试**：后端改动必带 pytest；沙盒用 `/tmp` 可写 DB（挂载盘 dev DB 读即 disk I/O error）、`--timeout-method=signal` 隔离无外网用例、`DEBUG=true`（否则拒默认 JWT 密钥）。既有基线失败（portrait/preset_import + 无外网 network 用例）不顺手修。
- **接线检查**：新 WS 出站类型前端统一接入 `services/ws.ts` onmessage；新路由注册进 `app/main.py`；`protocol.py` 只建入站模型。
- **提交**：Conventional Commits，小提交，一任务一或多个小提交，禁大混合提交。挂载盘 git 锁：每次 git 写前挪走 `.git/*.lock`。
- **vm212**：`AGENT_ENABLED=false`（Coding Plan 条款），成就/事件取材尽量纯 SQL 无 LLM。

## 工作量估算

P1-5 ~1.5 人日；S1/S4/S3 各 ~1、S2 ~1.5、S5 ~1 —— 底座周约 5.5 人日；两块合计 ~7 人日。
