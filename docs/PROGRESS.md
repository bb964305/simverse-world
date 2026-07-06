# PROGRESS

## Phase 0 — 止血（OPTIMIZATION_PLAN §2）
- [x] P0-1 AgentLoop 每 tick 独立 session（loop.py）— `1e884c8`。偏差：`_handle_action`（含居民互聊）随规格示例移入信号量+session 内，互聊期间会占用一个并发槽（原实现在信号量外）；外层短 session 只查 `id+meta_json` 列而非整个 ORM 对象
- [x] P0-5 embedding 失败返回 None + 清洗存量零向量 — `51df6d3`。偏差：存量零向量清洗未走一次性 migration，改为补偿任务首轮执行（幂等，pgvector 用 `<#>` 快路径）；额外发现并修复 `Memory.embedding` JSON 列 None 落库为 JSON 'null' 而非 SQL NULL 的问题（加 `none_as_null=True`，否则"保持 NULL"的前提不成立）
- [x] P0-6 create_all 加环境开关 — `ec086a3`。`auto_create_tables` 默认 False；deploy Dockerfile CMD 前置 `alembic upgrade head`。注意：本地开发若不跑 alembic 需在 .env 设 `AUTO_CREATE_TABLES=true`
- [x] P0-4b jwt_secret 默认值检测，生产拒绝启动 — `ad2fa85`。新增 `debug` 开关（默认 False=强制校验）；本地开发需 `DEBUG=true` 或设置真实 JWT_SECRET；tests conftest 已注入 DEBUG=true。注意：.env.example 里的示例 JWT_SECRET 值不在拒绝名单（仅拒绝代码默认值）
- [x] 全局 logger.error 补 exc_info=True — `931c9b8`。8 处 except 块内的 logger.error 全部补齐；portrait_service 两处非 except 上下文的 error 调用有意不加
- [x] Dockerfile 改为 pip install .（消除依赖漂移）— `6174e13`。顺带修复：pyproject 补 `python-multipart` 依赖（原来只在 Dockerfile 手写清单里）+ 补 hatchling `packages=["app"]` 配置（否则包根本无法构建，`pip install -e .` 也因此恢复可用）；wheel 构建已验证含 agent YAML configs

## Phase 1 — 扛并发与安全（§2/§3）
- [ ] P0-2 WS 事务边界拆分 + database.py 连接池参数
- [ ] P0-4a python-jose → PyJWT
- [ ] P0-4c WS token 改首条 auth 消息
- [ ] P0-4d SSRF 防护（私网段拒绝）+ 上传 magic bytes 校验 + passlib → bcrypt
- [ ] P1-2 共享 httpx AsyncClient
- [ ] 限流：WS 聊天频控 + REST slowapi 兜底

## Phase 2 — 扩展性与成本（§2/§3）
- [ ] P0-3a Agent Worker 独立进程 + compose 服务
- [ ] P0-3b ConnectionManager 状态 Redis 化 + pub/sub 广播
- [ ] P1-1 LLM 计量（llm_usage 表）+ 预算熔断 + 分级模型 ← 🔥 功能的闸门
- [ ] P1-4 前端路由懒加载 + manualChunks
- [ ] P1-5 apiFetch 超时/取消 + Forge 轮询改 WS 推送

## 底座周（FEATURE_SPECS §1，迁移 012-016）
- [ ] S1 世界事件总线（world_events + event_cron + 三注入点）
- [ ] S2 事件钩子 bus + 成就引擎（achievements 表 + 6 个埋点）
- [ ] S3 商店管线（items/purchases + shop_service）
- [ ] S4 通知中心（notifications + NotificationDrawer）
- [ ] S5 LocationTracker（location_visits + move 钩子）

## 批次 1 — 体感与经济
- [ ] A3 居民主动找玩家 · [ ] A5 村落日报 🔥 · [ ] D2 消耗场景 · [ ] D1 成就（12 个 seed）

## 批次 2 — 玩法纵深
- [ ] A1 人生目标 🔥 · [ ] B1 委托任务 🔥 · [ ] B2 位置偶遇 · [ ] D3 每日循环 · [ ] E1 情绪引擎 · [ ] E4 目击记忆

## 批次 3 — 内容与传播
- [ ] C1 灵魂卡片 · [ ] C2 关系图谱 · [ ] A2 世界事件运营化 · [ ] A4 居民创作 🔥 · [ ] E8 探索图鉴 · [ ] E7 时间胶囊

## 批次 4 — 记忆放大器
- [ ] E2 梦境 🔥 · [ ] E3 谣言传播 🔥 · [ ] E14 周报 🔥 · [ ] E11 关注动态流 · [ ] E10 合影 · [ ] E5 TTS 🔥

## 赛季装配（一次版本发布）
- [ ] 迁移 022（seasons 全家）· [ ] E12 赛季榜 · [ ] C3 剧本季 🔥 · [ ] E9 辩论擂台 🔥 · [ ] E13 目标投资

## Phase 3 — 持续项（穿插进行，不阻塞主线）
- [ ] P1-3 分页与索引补齐 · [ ] P1-6 大文件拆分 · [ ] CI（GitHub Actions）· [ ] 前端测试底座 · [ ] Sentry + /metrics

## 发现（施工中发现的新问题，不当场处理）
- 4 个预先存在的测试失败（HEAD 基线复现，与 P0-1 无关）：`test_forge.py::test_forge_answers_advance_to_generating`、`test_map_integration.py::test_decide_prompt_includes_remembered_residents`、`test_portrait.py::test_generate_portrait_success`（portrait_url 为 None）、`test_preset_import.py::test_seed_presets_creates_residents`（district 默认值断言 'free'，代码已改 'central_plaza'）——测试与代码漂移
- `Memory.embedding` ORM 类型为 JSON 但迁移 004 实际列是 `vector(1024)`，类型不一致：PG 上依赖 asyncpg 文本转型，sqlite 上是 JSON 文本。历史 sqlite 开发库中可能残留 JSON 'null' 文本行（非 SQL NULL），补偿任务扫不到；建议后续统一为 pgvector 的 SQLAlchemy 类型
- OPTIMIZATION_PLAN P0-5 提到的 qwen3-embedding 2560→1024 维截断问题（应在请求中显式传 dimensions）未在本次处理，规格的修复清单未包含它
- ~~`pip install -e .` 在 backend 下失败：hatchling 缺打包配置~~ 已在 `6174e13`（Dockerfile 任务）中修复
