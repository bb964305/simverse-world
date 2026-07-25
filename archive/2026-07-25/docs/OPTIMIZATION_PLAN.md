# Simverse World 项目分析与优化方案

> 分析日期：2026-07-06 · 范围：backend/（约 12,500 行 + 381 个测试）、frontend/（约 9,400 行）、deploy/
> 所有问题均标注 `文件:行号` 证据，可直接定位。

---

## 一、总体评价

**项目优点（值得保持的部分）：**

- 后端按领域分包（agent / memory / personality / forge / llm / ws），边界清晰；Agent 采用 YAML 插件化五阶段管线（perceive→plan→decide→execute→memorize），扩展性设计好
- 数据库迁移规范：Alembic 链完整，`004_add_memories_table.py:46-51` 正确创建了 pgvector HNSW 索引
- 测试量可观：381 个后端测试用例，覆盖 agent、memory、forge、admin 等核心域
- LLM 客户端做了模块级缓存（`llm/client.py`），system/user 双通道设计合理
- 提交历史遵循 Conventional Commits，工程习惯好

**核心矛盾（一句话诊断）：**

> 这是一个**架构上只能跑单进程**的系统：Agent 循环、WebSocket 连接管理、居民锁、每日行为计数全部依赖进程内存；同时 **DB 会话生命周期与 LLM 长调用深度耦合**，默认连接池下十几个并发聊天用户就会把数据库连接耗尽。功能完成度高（v1.4），但生产稳定性、安全基线和成本控制存在系统性欠账。

**成熟度评估：** 功能 8/10 · 架构可扩展性 4/10 · 安全 4/10 · 可观测性 3/10 · 前端工程化 5/10

---

## 二、P0 — 正确性与稳定性（必须优先修复）

### P0-1 AgentLoop 多个并发任务共享同一个 AsyncSession

**证据：** `backend/app/agent/loop.py:58` 开启一个 session，`loop.py:99` 用 `asyncio.gather` 并发跑所有居民 tick（信号量放行 5 个并发，`config.py:73`），所有 tick 共用这一个 `db`。

**问题：** SQLAlchemy `AsyncSession` **不是并发安全的**。5 个并发 tick 在同一 session 上交错执行查询/写入，会触发 `InterfaceError: another operation is in progress`、脏读、以及一个 tick 的 rollback 吞掉另一个 tick 的写入。当前没有大规模爆发只是因为 LLM 调用占了大部分时间、真正撞车概率低——这是隐性数据损坏源。

**修复：** 每个 `guarded_tick` 内部自建 session：

```python
async def guarded_tick(resident_id: str):
    async with semaphore:
        async with async_session() as db:
            resident = await db.get(Resident, resident_id)
            action_result = await resident_tick(db, resident)
            ...
```

外层 `_tick_round` 只用一个短 session 拉取居民 ID 列表后立即关闭。**工作量：0.5 人日。**

### P0-2 WebSocket 聊天期间长期占用数据库连接

**证据：** `backend/app/ws/handler.py:133` 在消息循环内 `async with async_session() as db:` 包住整个消息处理，包括 LLM 流式输出（约 `handler.py:260-291`）。一次 LLM 流式回复 10–60 秒，期间该 DB 连接一直被占用。`database.py:5` 创建 engine 未配置池参数（asyncpg 默认 `pool_size=5, max_overflow=10`）。

**问题：** 15 个用户同时和 NPC 聊天 → 连接池耗尽 → 所有 API 请求排队超时，整站假死。这是当前最可能在线上出现的事故。

**修复：**
1. 拆分事务边界：LLM 调用前完成读操作并关闭 session；流式结束后开新 session 写 Message/commit
2. `database.py` 显式配置：`pool_size=20, max_overflow=20, pool_pre_ping=True, pool_recycle=1800`
3. `handler.py:637` 这个文件本身太大，按消息类型拆成 handlers 目录（顺带完成）

**工作量：2 人日。**

### P0-3 全部关键状态在进程内存，系统被锁死在单 worker

**证据：**
- `deploy/backend/Dockerfile:22`：`--workers 1`
- `main.py:31-32`：`heat_cron_loop` 和 `agent_loop` 在 lifespan 里随 API 进程启动 → 开多 worker 会跑出 N 份 Agent 循环，LLM 成本 ×N、行为重复 ×N
- `agent/tick.py:15-16`：`_daily_counts` 进程内 dict，重启清零、多实例互不可见
- `ws/manager.py:6-11`：在线表、位置表、居民聊天锁 `chatting`、排队 `chat_queue`、社交锁全在内存

**问题：** 单 worker 是吞吐天花板（一个事件循环同时扛 API + 全部 WS + Agent 循环 + 热度 cron）；且任何一次部署重启都会丢锁、丢队列、重置行为限额。

**修复（分两步走）：**
1. **短期（1 人日）：** 把 `agent_loop` / `heat_cron` 从 API 进程剥离成独立入口（`python -m app.agent.main`），deploy compose 加一个 `agent-worker` 服务。API 即可安全开多 worker。
2. **中期（3–5 人日）：** ConnectionManager 的锁/队列/在线态迁到 Redis（项目已依赖 Redis），广播改 Redis pub/sub，WS 层即可水平扩展。`_daily_counts` 改 Redis `INCR` + 按日过期。

### P0-4 安全基线欠账（4 项）

| # | 问题 | 证据 | 修复 |
|---|------|------|------|
| a | `python-jose>=3.3` 允许装到带 CVE-2024-33663（算法混淆）/33664（JWT bomb DoS）的版本；该库长期少维护 | `pyproject.toml:13`、`services/auth_service.py:3` | 迁移到 `PyJWT`（API 几乎同形，半天）；至少钉 `>=3.4` |
| b | JWT 密钥有弱默认值且不强制覆盖，生产忘配 = 任何人可伪造管理员 token | `config.py:7` `jwt_secret: str = "dev-secret-change-in-production"` | Settings 加 validator：非 debug 模式下检测到默认值直接拒绝启动 |
| c | WS 用 query string 传 token，会进 nginx/CF 访问日志 | `ws/handler.py:26` | 改为连接后首条 auth 消息，或 `Sec-WebSocket-Protocol` 携带；配合短时效一次性 ticket 更佳 |
| d | SSRF：管理端「测试 LLM 连接」对用户提供的任意 `base_url` 发起服务端 POST，可探测内网（如云元数据 169.254.169.254） | `services/settings_service.py:271`（同类面还有自定义 LLM base_url、portrait base_url） | 解析目标 IP，拒绝私网/环回/链路本地段；或维护域名白名单 |

另有两项中危一并处理：媒体上传只校验客户端声明的 `content_type`（`media/service.py:61-80`），应加 magic bytes 嗅探 + 图片重编码；`passlib` 已停止维护且被迫钉死 `bcrypt==4.0.1`（`pyproject.toml:14-15`），建议直接用 `bcrypt` 库替换 CryptContext。**合计工作量：2–3 人日。**

### P0-5 Embedding 失败时写入零向量，污染记忆检索

**证据：** `memory/embedding.py:71,83-84` 批量失败时返回 `[[0.0]*1024]`，调用方照常入库；检索 SQL `memory/service.py:263-266` 用余弦距离 `<=>`。

**问题：** 零向量的余弦距离是 NaN（除零），会导致排序结果不可预期；Ollama 一次宕机就往库里灌一批"毒记忆"，且无法与"真实向量"区分。另外 `embedding.py:38-41` 把 qwen3-embedding:4b 的原生 2560 维**直接截断**到 1024——该模型支持 MRL 套娃维度，但应在请求里显式指定 `dimensions`，而不是事后截断（截断后也未重新归一化，虽然余弦距离不受影响，但语义质量需要验证）。

**修复：** 失败返回 `None` 并让 `embedding` 字段保持 NULL（检索 SQL 已有 `embedding IS NOT NULL` 过滤，`service.py:265`）；加一个补偿任务扫描 NULL 向量重算。**工作量：0.5 人日 + 一次数据清洗（`DELETE`/重算 embedding 全零的行）。**

### P0-6 生产环境仍在跑 `create_all`，架空 Alembic

**证据：** `main.py:27-28` lifespan 无条件执行 `Base.metadata.create_all`，注释写着 "dev mode" 但生产同样执行。

**问题：** 新模型会绕过迁移直接建表，导致迁移链与真实 schema 漂移；某天 `alembic upgrade` 会在别人的环境上炸。

**修复：** 用环境开关（`settings.auto_create_tables`，默认 False）；生产入口只跑 `alembic upgrade head`（可放 Docker entrypoint）。**工作量：0.5 人日。**

---

## 三、P1 — 性能与成本

### P1-1 LLM 成本与可靠性没有治理层（最大的钱坑）

**现状：**
- 一次居民间自主对话 = 最多 8 轮串行 LLM 调用（`agent/chat.py:120,138`）+ 2 次关系更新（`chat.py:159,166`）+ 1 次总结（`chat.py:175`）≈ **11 次串行调用**。Agent 循环每 60 秒一轮、每居民每天 20 次行为上限（`config.py:72-74`），居民数量增长时成本线性爆炸
- Token 记账用字符数近似（`ws/handler.py:299` `tokens_used += len(full_reply)`），无法核算真实成本
- 全站无任何限流（grep 无 slowapi/limiter），恶意用户可用聊天接口刷穿 LLM 预算

**优化方案：**
1. **计量：** 从 Anthropic SDK response 的 `usage.input_tokens/output_tokens` 落库（新表 `llm_usage`：owner、model、场景、tokens、时间），管理面板出成本报表 — *1 人日*
2. **限流：** WS 聊天按 user 限频（如 20 msg/min），REST 用 slowapi 全局兜底；Agent 全局加"每小时 LLM 调用预算"熔断器，超预算自动降级为规则行为 — *1–2 人日*
3. **分级模型：** 居民闲聊/行为决策用最便宜模型，玩家可见对话用好模型——`llm/client.py` 已有 system/user 双通道，只需在 agent prompts 处传小模型名 — *0.5 人日*
4. **减少调用：** 居民对话 2 次关系更新可合并进总结调用（一次输出 JSON 同时给 summary/mood/关系变化），11 次 → 9 次；行为决策的 plan 阶段可对"无新事件"的居民跳过 LLM 直接走规则 — *1 人日*

### P1-2 httpx 客户端不复用

**证据：** 10 处 `async with httpx.AsyncClient(...)`（`memory/embedding.py:17,58`、`forge/research_stage.py:57`、`services/portrait_service.py:66` 等），每次调用新建连接池、重做 TLS 握手。

**修复：** 建 `app/http.py` 模块级共享 `AsyncClient`（lifespan 中关闭），embedding 这类高频路径收益最明显。**工作量：0.5 人日。**

### P1-3 数据库查询与索引

- **`routers/residents.py` 列表接口无任何分页**（全文件无 limit/offset）——居民数增长后每次全量返回，同时拖慢 API 与前端渲染；补 `limit+offset`（或游标）并拉齐其他列表接口
- `agent/loop.py:67-68` 每轮全量拉 `Resident`（含大 JSON 列 meta_json）——只 select 需要的列（id/slug/status/meta_json['sbti']），或居民过百后改分批
- 为高频过滤字段补索引：`residents.status`、`conversations.resident_id+rating`（评分聚合 `ws/handler.py:409-414` 每次全表聚合，可改增量维护）
- 打开慢查询日志（engine `echo` 换成 `log_min_duration_statement` 或 SQLAlchemy events 记 >200ms 查询）

**工作量：1–2 人日。**

### P1-4 前端：主包没有任何代码分割

**证据：** `App.tsx:1-9` 所有页面 eager import；`vite.config.ts` 仅 5 行无任何 build 配置；grep 全库无 `React.lazy`。Phaser（min 约 1.4MB）、`@uiw/react-md-editor`、全部 admin 面板全部进首屏主包——登录页用户也要下载整个游戏引擎。

**修复：**
```tsx
const GamePage = lazy(() => import('./pages/GamePage'))     // Phaser 随游戏页走
const AdminPage = lazy(() => import('./pages/AdminPage'))   // 管理端按需
const ForgePage = lazy(() => import('./pages/ForgePage'))   // md-editor 按需
```
加 `vite.config.ts` `manualChunks: { phaser: ['phaser'] }`，并装 `rollup-plugin-visualizer` 量化前后对比。预期首屏 JS 体积下降 60% 以上。**工作量：1 人日。**

### P1-5 前端网络层健壮性

**证据：** `services/api.ts:14-30` 的 `apiFetch`：无超时、无 AbortController、无重试；`api.ts:22-23` 401 时直接 `window.location.href = '/login'`（丢失当前状态，且在并发请求下会触发多次跳转）。Forge 全靠 `setInterval` 轮询（`DeepForge.tsx:49`、`QuickForge.tsx:45`、`ForgeChat.tsx:58`，最快 2s 一次），而系统本有 WS 通道。

**修复：** `apiFetch` 加 `AbortSignal.timeout(15000)` + 组件卸载取消；401 集中经 store 处理一次性登出；Forge 进度改走 WS 推送（后端 pipeline 各阶段完成时 `manager.send`），去掉三处轮询。**工作量：1.5 人日。**

### P1-6 巨型文件拆分（前后端）

| 文件 | 行数 | 拆法 |
|------|------|------|
| `frontend/src/components/profile/SettingsPanel.tsx` | 808 | 按 6 个分区拆子组件 + 提取 `useSectionForm` hook |
| `backend/app/ws/handler.py` | 637 | 按消息类型拆 `ws/handlers/{chat,movement,economy}.py` |
| `backend/app/services/forge_service.py` | 645 | 文件头部已自标 **DEPRECATED**（被 `app/forge/pipeline.py` 取代），但 `/forge/start|answer|status` 三个旧端点仍在走它——尽快迁移端点后删除，顺带归一 `llm/forge_prompts.py`(294 行) 与 `forge/prompts.py`(262 行) 两套 prompt |
| `frontend/src/services/api.ts` | 647 | 按域拆 `api/{forge,admin,resident}.ts`，共享 `apiFetch` |
| `frontend/src/components/admin/UsersPanel.tsx` | 620 | 表格/弹窗/操作拆分 |

**工作量：3–4 人日（可随功能迭代渐进做）。**

---

## 四、P2 — 工程化与体验

**可观测性（当前接近为零，建议尽早做）：**
- 异常日志全部 `logger.error("...: %s", e)` 无堆栈（`agent/loop.py:61`、`tick.py:60` 等）→ 统一加 `exc_info=True`；引入 structlog JSON 日志
- 接入 Sentry（前后端各半天）；加 `/metrics`（prometheus-fastapi-instrumentator）：LLM 延迟/失败率、tick 时长、WS 在线数、池占用
- Agent 行为已有广播，补一张 `agent_events` 表或结构化日志，方便回放调试"居民为什么做了这件事"

**前端质量底座：**
- 无 ErrorBoundary（grep 为零）→ 路由级兜底，Phaser 崩溃不应白屏整站
- 无前端测试 → 按 Roadmap v1.5 落地 Vitest + RTL，优先覆盖 stores 和 api 封装（纯逻辑、最划算）
- `GameScene.ts` 审计事件监听/timer 的 `shutdown` 清理（现仅 `destroyGame` 一个出口，`GameScene.ts:48-50`），React 严格模式下重挂载易泄漏
- TS 开启 `strict`（若未全开）+ CI 跑 `tsc --noEmit`

**部署与供应链：**
- `deploy/backend/Dockerfile:13-15` 手写依赖清单，与 `pyproject.toml` 已经漂移风险 → 改 `pip install .`；加多阶段构建、非 root 用户、`HEALTHCHECK`
- 依赖锁定：后端引入 `uv`/`pip-tools` 生成 lock；前端 CI 用 `npm ci`
- 建 GitHub Actions：lint + tsc + pytest + build，PR 必过（现在完全没有 CI）
- `.env.example` 与 `config.py` 字段做一致性检查脚本

**产品向（低成本高感知）：**
- WS 断线重连的指数退避 + 断线期间 UI 提示（现状需确认重连策略）
- 玩家移动广播目前逐条转发（`ws/handler.py:125-131`），玩家多时可改 100ms 节流合批
- 图片资源（精灵图/头像）加尺寸压缩与 CDN 缓存头

---

## 五、专题：目标架构（P0-3 的终态）

```
                ┌─────────────┐
   浏览器 ──────►│  CDN / CF    │ 前端静态资源（已分包）
                └──────┬──────┘
                       │ HTTPS / WSS
                ┌──────▼──────────────┐
                │  API 进程 × N        │  uvicorn --workers N
                │  (REST + WS 网关)    │  无进程内业务状态
                └──┬────────┬─────────┘
                   │        │ Redis pub/sub（广播）
                   │        │ Redis（锁/队列/在线态/限流/日计数）
            ┌──────▼──┐  ┌──▼───────────────┐
            │ Postgres │  │ Agent Worker × 1  │  独立进程跑 agent_loop
            │ +pgvector│  │ (可按居民分片扩展) │  + heat_cron
            └──────────┘  └──────────────────┘
```

迁移顺序：先拆 Agent Worker（P0-3 短期）→ Redis 化 ConnectionManager 状态 → API 开多 worker → 压测验证。

---

## 六、实施路线图

| 阶段 | 内容 | 工作量 | 效果 |
|------|------|--------|------|
| **Phase 0（1–2 天）** | P0-1 session 隔离、P0-5 零向量、P0-6 create_all 开关、JWT 默认值校验、日志加 exc_info、Dockerfile 改装 pyproject | ~2 人日 | 消除数据损坏源与最尴尬的安全洞 |
| **Phase 1（1 周）** | P0-2 WS 事务边界 + 连接池、P0-4 剩余安全项（PyJWT/SSRF/上传校验/WS token）、P1-2 httpx 复用、限流 | ~5 人日 | 扛住真实并发，安全基线达标 |
| **Phase 2（2–3 周）** | P0-3 Agent Worker 剥离 + Redis 状态化、P1-1 LLM 计量/预算/分级、P1-4/5 前端分包与网络层、Forge 改 WS 推送 | ~10 人日 | 可水平扩展，LLM 成本可见可控，首屏提速 |
| **Phase 3（持续）** | P1-6 大文件拆分、CI、前端测试、可观测性三件套、慢查询治理 | 渐进 | 工程质量长期健康 |

---

## 七、今天就能做的 10 件快速赢

1. `agent/loop.py`：每个 tick 独立 session（P0-1，半天，防数据损坏）
2. `database.py`：加 `pool_size=20, max_overflow=20, pool_pre_ping=True`（3 行）
3. `config.py`：jwt_secret 默认值检测，生产拒绝启动（10 行）
4. `memory/embedding.py`：失败返回 None 而不是零向量（10 行）
5. `main.py`：`create_all` 加环境开关（5 行）
6. `pyproject.toml`：`python-jose` 换 `PyJWT`（auth_service.py 仅 2 处调用）
7. `App.tsx`：三个 `React.lazy` + Suspense（首屏立减 60%）
8. `api.ts`：`AbortSignal.timeout(15000)`（1 行/处）
9. 全局 `logger.error(..., exc_info=True)`（查找替换）
10. `deploy/backend/Dockerfile`：`pip install .` 替换手写依赖清单

---

## 八、新功能建议

> 原则：全部复用现有基建（记忆系统 / SBTI / AgentLoop / 经济 / WS），避开 Roadmap 已列项（物品交易、移动端、i18n、开放 API、附身机制）。凡带 🔥 的是 LLM 调用大户，**建议在 P1-1 成本治理落地后再上**。

### A. AI 居民能力（差异化核心）

| # | 功能 | 设计要点 | 复用基建 | 工作量 |
|---|------|----------|----------|--------|
| A1 | **居民人生目标与长线剧情** 🔥 | 在现有 DailyGoal/HourlyPlan 之上加 LifeGoal（如"开一家店""找到失散的朋友"），目标为行为决策提供偏置，阶段性达成/受挫写入反思记忆并触发人格演化事件 | `agent/phases/plan/`、`personality/evolution.py` | 3–4 人日 |
| A2 | **世界事件系统** | 全局事件（节日、暴雨、村落新闻）注入所有居民的 perceive 阶段 → 产生集体记忆和共同话题；管理端可手动投放事件，作为运营抓手 | agent 插件 perceive 阶段、admin 面板 | 2–3 人日 |
| A3 | **居民主动找玩家** | 基于关系记忆强度，居民在玩家上线时主动打招呼、留言或送小礼物；离线时走已有的 pending_message 队列 | `models/pending_message.py`、`memory/service.py` 关系层 | 2 人日 |
| A4 | **居民创作物** 🔥 | 居民按 SBTI 人格定期"创作"（日记、短诗、吐槽）发到公告栏，玩家可打赏收藏；让公告栏从静态变成活的内容流 | `routers/bulletin.py`、agent 新增 CREATE 行为 | 2 人日 |
| A5 | **村落日报（涌现叙事摘要）** 🔥 | 每日一次 LLM 汇总：当天居民对话摘要、关系变化、事件 → 生成一份可读的"小报"推给玩家；是记忆系统产出的最佳展示窗口，也是每日回访钩子 | 居民对话已有 summary 广播、`tasks/` 定时任务 | 2 人日 |

### B. 玩法系统

| # | 功能 | 设计要点 | 复用基建 | 工作量 |
|---|------|----------|----------|--------|
| B1 | **委托任务系统** 🔥 | 居民根据自身目标发布委托（"帮我带话给 X""陪我聊聊 Y 话题"），玩家完成得 Soul Coin + 关系值；把居民关系网变成玩法内容，天然衔接 A1 | 经济系统、memory 关系层、公告栏做任务板 | 4–5 人日 |
| B2 | **位置偶遇事件** | 玩家走进 20 个命名位置时概率触发与附近居民的情境对话（图书馆偶遇正在看书的居民）；让地图感知系统产生玩家可感的价值 | `agent/map_data.py`、WS 位置上报（`ws/handler.py` move） | 2 人日 |
| B3 | **家园装修** | 居民住房（home_location_id 已落库）开放内饰摆放；作为物品系统（Roadmap v1.5）的第一个消耗场景先行验证 | housing 迁移、Phaser 场景 | 3–4 人日 |

### C. 社交与 UGC

| # | 功能 | 设计要点 | 复用基建 | 工作量 |
|---|------|----------|----------|--------|
| C1 | **灵魂卡片分享** | 居民档案（persona/SBTI/头像/名言）一键生成分享图卡 + 导出/导入 JSON；低成本裂变传播，也是开放 API（v1.6）的轻量前置 | forge 产物、portrait 头像 | 2 人日 |
| C2 | **关系图谱可视化** | 用关系记忆数据渲染村落社交网络力导向图（挚友/死对头/暧昧），玩家能直观看到自己在村落中的位置；数据现成，纯前端增量 | memory 关系层、前端 d3/canvas | 2–3 人日 |
| C3 | **剧本季** 🔥 | 每季社区投票选主题（如"神秘失踪案"），运营通过 A2 事件系统注入叙事线索，居民行为受剧本引导，玩家集体解谜；对齐 v2.0 Stardust Town 愿景的低成本试水 | A2 世界事件、公告栏、投票新增 | 5+ 人日 |

### D. 留存与经济

| # | 功能 | 设计要点 | 复用基建 | 工作量 |
|---|------|----------|----------|--------|
| D1 | **成就系统** | 首次对话、被居民写进记忆、触发人格跳变、连续登录……成就发 Soul Coin；事件源都已存在（memory/evolution/reward），只缺聚合展示 | conversation/memory/personality_history 表 | 2–3 人日 |
| D2 | **Soul Coin 消耗场景** | 当前经济**只发不收**（对话奖励、每日签到、创作者被动收益），通胀是时间问题：加改名卡、头像重绘、传送券、委托加急、给居民送礼等回收口 | `services/coin_service.py` charge 已有 | 2 人日 |
| D3 | **每日循环强化** | 签到升级为连续签到阶梯 + "每日话题"（今天去和 X 聊聊 Y，完成额外奖励），与 A5 日报形成"看日报→领任务→聊天→明日看结果"的闭环 | `daily_reward_service.py` | 1–2 人日 |
| D4 | **创作者仪表盘** | 角色对话量/评分/收益曲线，给创作者持续优化角色的反馈回路；数据全在库里，纯查询+图表 | conversation/transaction 表 | 2 人日 |

### E. 第二波候选池（进一步扩展）

| # | 方向 | 功能 | 设计要点 | 复用基建 | 工作量 |
|---|------|------|----------|----------|--------|
| E1 | AI | **居民情绪引擎** | 把对话结果里已有的 mood（positive/neutral/negative）持久化为连续情绪值，随事件衰减/累积，影响对话语气、行为权重（低落时宅家、兴奋时找人聊）；规则驱动为主，几乎零 LLM 成本 | `agent/chat.py` mood、SBTI 权重 | 2 人日 |
| E2 | AI | **梦境系统** 🔥 | 居民睡眠时段把当日高重要度记忆混合生成"梦"，醒来后可讲给玩家听、偶尔梦到玩家；记忆系统最有情感冲击力的展示口 | scheduler sleeping 状态、memory 检索 | 2 人日 |
| E3 | AI | **八卦与谣言传播** 🔥 | 居民对话时概率转述"二手记忆"并带失真（LLM 改写），谣言可沿关系网多跳传播；玩家能观察到"消息传着传着变了样"的涌现现象 | `resident_chat`、memory 关系层 | 3 人日 |
| E4 | AI | **目击记忆** | 居民"看到"玩家在世界中的行为（路过谁家、和谁聊过）写入低重要度事件记忆，下次对话会提起"昨天看你去图书馆了"；用 WS 位置数据即可，无需视觉 | `ws/handler.py` move 广播、`map_data.py` | 2 人日 |
| E5 | AI | **居民语音（TTS）** 🔥 | 按 SBTI 人格映射音色，对话回复可朗读；沿用 media/model_router 的多提供商路由模式接 TTS 端点 | `media/model_router.py` 模式 | 2–3 人日 |
| E6 | 玩法 | **天气与季节** | 全局天气影响居民作息（雨天减少外出）、地图色调（Phaser tint）与对话话题（作为 A2 世界事件的常驻数据源） | scheduler、A2 事件、Phaser | 2–3 人日 |
| E7 | 玩法 | **时间胶囊信件** | 玩家写信指定未来日期，由选定居民届时"送达"并附上居民自己的一句话；情感留存钩子，天然拉长回访周期 | `pending_message` + 定时任务 | 1–2 人日 |
| E8 | 玩法 | **探索图鉴** | 20 个命名位置的探索度、位置彩蛋（首次到访触发居民讲解此地典故），集齐发成就；给"闲逛"这个动作赋予目标 | `map_data.py`、D1 成就 | 2 人日 |
| E9 | 社交 | **居民辩论擂台** 🔥 | 定期两位人格对立的居民就话题公开辩论（复用居民对话引擎，改为多轮立场制），玩家用 Soul Coin 站队投票，胜方支持者分池；观赏性 + 经济回收双收 | `agent/chat.py`、coin_service | 3–4 人日 |
| E10 | 社交 | **合影纪念** | 与居民在场景中合影，Phaser 截图 + 像素相框 + 居民一句话签名，生成可分享图片；与 C1 卡片共用分享管线 | Phaser snapshot、portrait | 1–2 人日 |
| E11 | 社交 | **关注与动态流** | 关注居民后，其重要动态（人格跳变、达成目标、发布创作）进入玩家的个人动态页；把 Agent 系统的产出变成可订阅内容 | agent 广播事件、A4 创作物 | 2–3 人日 |
| E12 | 留存 | **赛季排行与徽章** | 按季重置的对话/探索/委托榜单，赛季徽章永久保留；搭配 C3 剧本季形成节奏 | 现有排行（search/heat）、D1 | 2 人日 |
| E13 | 留存 | **居民养成投资** | 玩家出资赞助居民的人生目标（A1），达成后获得分成与专属纪念记忆；给 Soul Coin 一个长线沉淀池 | A1 目标系统、coin_service | 2–3 人日 |
| E14 | 留存 | **每周个人回顾** 🔥 | A5 日报的个人版："本周你和 12 位居民聊过，X 把你写进了 3 条记忆，你的人格标签是……"，周日推送；低频高质的召回钩子 | memory、conversation 聚合 | 1–2 人日 |

### 建议的上线批次

1. **第一批（低成本高感知，~1 周）：** A3 居民主动找玩家、A5 村落日报、D2 消耗场景、D1 成就——不依赖新表结构大改，立刻提升"世界是活的"体感并止住经济通胀
2. **第二批（玩法纵深，~2 周）：** A1 人生目标 + B1 委托任务（两者互为燃料）、B2 位置偶遇、D3 每日循环
3. **第三批（增长与叙事）：** C1 分享卡片、C2 关系图谱、A2 世界事件 → C3 剧本季
4. **候选池（E1–E14）按需抽调：** E1 情绪引擎 / E4 目击记忆 / E7 时间胶囊 / E10 合影几乎零 LLM 成本，可随时插队；E2 梦境、E3 谣言、E14 周报是记忆系统最好的"效果放大器"，建议在记忆嵌入闭环（Roadmap v1.5）验证后上；E9 辩论 + E12 赛季 + E13 投资构成中期经济与内容节奏

> ⚠️ 依赖关系：第一批可与优化 Phase 1 并行；**A1/B1/C3 等 🔥 项务必等 P1-1（LLM 计量+预算熔断）就位**，否则居民数量 × 目标驱动行为会让成本不可控。

---

*附注：本方案基于静态代码审查；Phase 1 完成后建议用 locust/k6 做一次 50 并发聊天压测验证 P0-2 的修复效果，并用 rollup-plugin-visualizer 量化 P1-4 的分包收益。*
