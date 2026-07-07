# PROGRESS

## Phase 0 — 止血（OPTIMIZATION_PLAN §2）
- [x] P0-1 AgentLoop 每 tick 独立 session（loop.py）— `1e884c8`。偏差：`_handle_action`（含居民互聊）随规格示例移入信号量+session 内，互聊期间会占用一个并发槽（原实现在信号量外）；外层短 session 只查 `id+meta_json` 列而非整个 ORM 对象
- [x] P0-5 embedding 失败返回 None + 清洗存量零向量 — `51df6d3`。偏差：存量零向量清洗未走一次性 migration，改为补偿任务首轮执行（幂等，pgvector 用 `<#>` 快路径）；额外发现并修复 `Memory.embedding` JSON 列 None 落库为 JSON 'null' 而非 SQL NULL 的问题（加 `none_as_null=True`，否则"保持 NULL"的前提不成立）
- [x] P0-6 create_all 加环境开关 — `ec086a3`。`auto_create_tables` 默认 False；deploy Dockerfile CMD 前置 `alembic upgrade head`。注意：本地开发若不跑 alembic 需在 .env 设 `AUTO_CREATE_TABLES=true`
- [x] P0-4b jwt_secret 默认值检测，生产拒绝启动 — `ad2fa85`。新增 `debug` 开关（默认 False=强制校验）；本地开发需 `DEBUG=true` 或设置真实 JWT_SECRET；tests conftest 已注入 DEBUG=true。注意：.env.example 里的示例 JWT_SECRET 值不在拒绝名单（仅拒绝代码默认值）
- [x] 全局 logger.error 补 exc_info=True — `931c9b8`。8 处 except 块内的 logger.error 全部补齐；portrait_service 两处非 except 上下文的 error 调用有意不加
- [x] Dockerfile 改为 pip install .（消除依赖漂移）— `6174e13`。顺带修复：pyproject 补 `python-multipart` 依赖（原来只在 Dockerfile 手写清单里）+ 补 hatchling `packages=["app"]` 配置（否则包根本无法构建，`pip install -e .` 也因此恢复可用）；wheel 构建已验证含 agent YAML configs

## Phase 1 — 扛并发与安全（§2/§3）
- [x] P0-2 WS 事务边界拆分 + database.py 连接池参数 — `c19eb61`。handler.py 拆为 `app/ws/handlers/{connection,context,movement,chat,rating,player_chat}`（原文件删除，main.py 改从 `app.ws.handlers` 导入；测试补丁点改 `app.ws.handlers.chat.ModelRouter`）。chat_msg 拆成两个短 session：LLM 流式期间不持有连接。偏差/顺带：① 发现 player_chat auto 回复同样在 session 内调 LLM，一并拆分——`PlayerChatService.prepare_route`（纯 DB）+ 模块级 `generate_auto_reply`（无 session）；`route_message` 保留为组合封装，test_player_chat 零改动；② 连接池参数仅对非 sqlite URL 生效（sqlite 不用 QueuePool）；③ 遗留：chat_msg 媒体记忆 `add_memory` 在第二个 session 内含 embedding HTTP 调用（秒级，远小于 LLM 流式），可随 P1-2 共享 httpx client 一并优化
- [x] P0-4a python-jose → PyJWT — API 同形(`import jwt` + encode/decode 签名不变),exp datetime 原生支持,过期/篡改拒绝已验证。注意:PyJWT 对 <32 字节 HMAC key 发 InsecureKeyLengthWarning,dev 默认密钥(31 字节)会触发,仅 DEBUG 模式可见,生产已被 P0-4b 强制真实密钥
- [x] P0-4c WS token 改首条 auth 消息 — 后端 `_authenticate`:accept 后等首条 `{"type":"auth","token":...}`(10s 超时),成功回 `auth_ok`;`?token=` query 保留为废弃回退(带 warning 日志,老客户端刷新前不断线),后续可移除;`manager.connect` 改为 `register`(accept 归 handler 所有)。前端 ws.ts 在 onopen 发 auth 消息,URL 不再带 token。新增 tests/test_ws_auth.py 覆盖 6 条握手分支;tsc --noEmit 通过。未做:规格附注的一次性 ticket 方案(可与限流一起再评估)
- [x] P0-4d SSRF 防护（私网段拒绝）+ 上传 magic bytes 校验 + passlib → bcrypt — 新增 `services/url_guard.py`（getaddrinfo 解析→拒绝非 is_global 地址；DEBUG 模式跳过 IP 段检查以保本地 Ollama，scheme/host 语法检查始终生效）。接入点：用户自定义 LLM 保存（PATCH /settings/llm，400）、连接测试（/settings/llm/test，返回 error）、管理端 config 所有 `*base_url` 键。media 上传加 magic bytes 嗅探（jpeg/png/gif/webp + mp4/mov/webm），扩展名以嗅探结果为准而非声明类型。passlib→直接 bcrypt（旧 $2b$ 哈希验证兼容已确认；显式 72 字节截断与 passlib 行为一致）；pyproject 去 passlib、bcrypt 解钉为 >=4.1。已知边界：URL 校验在保存/请求时解析 DNS，rebinding 需 pinned-IP transport（记录在 url_guard docstring，超 P0-4d 范围）；图片重编码未做（需 Pillow，规格正文列为可选加强）。测试 +11（url_guard 8 + media 嗅探 3），test_settings 的 pwd_context 引用同步更新
- [x] P1-2 共享 httpx AsyncClient — `0ee0787`。`app/http.py` get_client()/close_client()，lifespan 关停时关闭；8 处调用点迁移（embedding×2、research_stage、github/linuxdo OAuth、settings LLM 测试、portrait、forge_monitor/dashboard 健康检查），client 级 timeout 原值下沉为 per-request。偏差：① trust_env=False 统一生效——forge_monitor/settings_service/portrait_service 三处原先会吃 proxy env（本机实测 HTTP_PROXY=127.0.0.1:1082 会劫持 localhost 出网，统一禁用反而是修复）；② 顺带补了 github_auth 的 exchange_code 测试（原先无覆盖）；③ 6 个测试文件 patch 点从 `app.<mod>.httpx.AsyncClient` 改为 `app.<mod>.get_client`
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
- **成本优化研究完成（2026-07-07）**：28 条实验，产出在 `docs/research/`（REPORT=结论与 P1-1 建议、LOG=实验台账、DIRECTOR_ROADMAP/CALLMAP=Opus 统筹产物）。关键输入给 P1-1：计量字段清单、熔断阈值、杠杆排序（计划优先跳过 decide 省 29-37% 为最大项）；缓存/Batch 判定为不可用杠杆。**开放问题需 Jimmy**：部署机 .env 的 LLM_BASE_URL（验证端点是否 Anthropic 原生，F-02）。顺手修清单：互聊 max_tokens 100→150（截断污染 history 风险）、互聊 history 双注入（chat.py 一行）、玩家聊天 chat_messages 无截断。
- 4 个预先存在的测试失败（HEAD 基线复现，与 P0-1 无关）：`test_forge.py::test_forge_answers_advance_to_generating`、`test_map_integration.py::test_decide_prompt_includes_remembered_residents`、`test_portrait.py::test_generate_portrait_success`（portrait_url 为 None）、`test_preset_import.py::test_seed_presets_creates_residents`（district 默认值断言 'free'，代码已改 'central_plaza'）——测试与代码漂移
- ~~`Memory.embedding` ORM 类型为 JSON 但迁移 004 实际列是 `vector(1024)`~~ **已修**（`045cd5a`）：当时猜"PG 靠 asyncpg 文本转型可行"被 vm212 实测证伪（连 NULL 都插不进，整个记忆管线在 PG 上死透）；现用 EmbeddingVector 方言分派类型。sqlite 残留 JSON 'null' 文本行的提醒仍有效
- **vm212 端到端验证（2026-07-07）暴露并修复 4 个"仅真实 PG 会炸"的 bug**：迁移双头（`6e54e48`）、003 六处 Integer/String 类型漂移+012 同步迁移（`8662175`）、注册 FK 插入顺序（`d3e3a55`，github_auth 早修过但 register/linuxdo 没跟上）、embedding 列类型（`045cd5a`）。根因共性：sqlite 测试（FK 不强制+create_all 不走迁移）掩盖全部四类问题。**建议 Phase 3 CI 加一个 testcontainers-postgres 的迁移+注册冒烟 job**
- vm212 部署状态：`/opt/skills-world`，API 端口 8100，pgvector/pg16，LLM=百炼 Coding Plan qwen3.7-plus（`/apps/anthropic`）。**AGENT_ENABLED=false**（Coding Plan 条款禁止后端自动化调用，防封 key/烧配额；玩家聊天链路不受影响）。要开 agent loop：改 `/opt/skills-world/deploy/.env` 后 `docker compose up -d api`；上生产须换按量计费 key
- OPTIMIZATION_PLAN P0-5 提到的 qwen3-embedding 2560→1024 维截断问题（应在请求中显式传 dimensions）未在本次处理，规格的修复清单未包含它
- ~~`pip install -e .` 在 backend 下失败：hatchling 缺打包配置~~ 已在 `6174e13`（Dockerfile 任务）中修复
