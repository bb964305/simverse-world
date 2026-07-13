# Kickoff Prompt V2（从 P0-3b / P1-1 起步，适用 Opus 4.8）

> 用法：在新 Cowork 会话中选中本仓库文件夹，把下面分隔线以下的全文粘贴为首条消息。
> 与 V1（docs/KICKOFF_PROMPT.md）的区别：并入成本研究产物作为 P1-1 实施规格、更新已知坑清单、加入 vm212 测试约束。

---

你是 Simverse World 仓库的执行工程师，按既定路线图逐步完成剩余优化与全部新功能。

**规格文档**：`docs/OPTIMIZATION_PLAN.md`（P0/P1/P2 优化）、`docs/FEATURE_SPECS.md`（29 个功能的可开工规格）、`AGENTS.md`（仓库规范）、`docs/PROGRESS.md`（唯一进度真相，已存在，不要重建）。

**成本研究产物（P1-1 的实施规格，优先级高于 OPTIMIZATION_PLAN 中对应段落）**：
- `docs/research/COST_RESEARCH_REPORT.md` §三 = llm_usage 字段清单、熔断阈值、路由策略的最终设计稿
- `docs/research/COST_RESEARCH_LOG.md` = 28 条实验台账（每条建议的证据）
- `docs/research/CALLMAP.md` = 全部 LLM 调用点 file:line 级 ground truth，改调用点前先对照它

## 每次会话的固定流程

1. 读 `docs/PROGRESS.md`
2. 取**第一个未勾选任务**（当前应为 P0-3b），宣布本次目标。一次只做一个任务，做完再取下一个
3. 动工前：先读该任务在规格文档中的完整条目，再读涉及的源码文件，确认规格与代码现状一致
4. 实现 → 按「完成定义」自测 → 提交 → 在 PROGRESS.md 勾选，并附一行说明（提交哈希 + 实际改动与规格的偏差，若有）
5. 会话结束前（或上下文吃紧时）：提交所有已完成工作，更新 PROGRESS.md，使任何新会话可无缝接续

## 顺序（不可跳）

P0-3b（Redis 化 + pub/sub）→ **P1-1（🔥 功能的闸门）** → P1-4 → P1-5 → 底座周 S1–S5 → 批次 1→2→3→4 → 赛季装配。带 🔥 的功能在 P1-1 完成前一律不开工。

## P1-1 专项要求

P1-1 = LLM 计量 + 预算熔断 + 分级模型路由，实施以 REPORT §三 为准：

- `llm_usage` 表字段照设计稿全量落（scenario 枚举、owner、attempt_no、parse_ok、cache_* 预留、source=usage|estimated 降级路径、cost_usd 计算列）
- **按 attempt 记账而非 success**（E-19）；`Conversation.tokens_used += len(full_reply)` 直接废弃（E-03）
- 动工第一步先换 JSON 解析器：`re.search(r'\{[^{}]+\}')` → 剥围栏 + 平衡括号提取（E-05 证明现解析器连嵌套 JSON 都抓不了），全调用点统一
- 熔断三级动作（80% 背景降频 / 95% 背景规则化 / 100% 只保玩家可见）+ per-user 日预算 + forge 单请求上限；每级都有规则兜底，不允许白屏
- 路由：背景调用锁 haiku，玩家可见调用模型可配置

**P1-1 同批实施 4 个已验证杠杆**（每个独立提交，附台账编号）：
1. decide 计划优先跳过 + 规则级中断检测（E-09/E-10，全服省 29–37%）——中断信号只用 TickContext 已有数据（新增空闲邻居/新高重要度记忆），零 LLM；必须带行为回归测试
2. 互聊收尾 5 调用 → 1 合并调用（E-04/E-05，收尾省 41%）——带解析失败重试 1 次兜底
3. 互聊 history 双注入修复（E-02，一行：CHAT_REPLY_SYSTEM 去 {history} 槽）
4. 玩家聊天滑窗 `ctx.chat_messages[-10:]`（E-08）

顺手修（同批捎带，独立小提交）：互聊回复 max_tokens 100→150、summary 150→200（E-17/E-26 截断污染风险）；add_memory 内容入库限长 80 字（E-28）；deep forge extraction 输入加截断（E-20）。

**明确不做**：prompt 前缀缓存、Batch API（E-07/E-29 三重否定，已从优化清单剔除）；背景模型降档（已全 haiku，无更低档）。

## 硬性规则（继承 V1）

- **完成定义**：后端改动必须带 pytest 用例，`cd backend && python3 -m pytest tests/` 全绿（4 个预先存在的失败除外，见下）；前端 `npm run lint && npx tsc --noEmit` 过（build 在 Node v25 有基线问题，见下）；迁移 `alembic upgrade head` 与 `downgrade -1` 双向实测；LLM 调用点必须过预算熔断器；能用模板/规则实现的不用 LLM
- **提交**：Conventional Commits（`feat(agent): ...`），一个任务一个或多个小提交，禁止混合大提交
- **接线检查**：新 router 注册进 `app/main.py`；新 WS 消息在 `app/ws/protocol.py` 建模型、前端 `services/ws.ts` 加分支；新模型确保被 Alembic 迁移覆盖
- **冲突处理**：规格与代码现实不符时以代码为准，小偏差自行适配并记录；影响架构的大冲突停下来向我提问（给出选项和你的建议）
- **禁止**：规格范围外的顺手重构；跳过测试标绿；修改 `.env`/密钥；新问题记入 PROGRESS.md「发现」区，不当场展开

## 已知坑（前人验尸报告，动工前读一遍）

- **sqlite 测试会掩盖 PG 问题**（FK 不强制、create_all 不走迁移、类型漂移）——已因此炸过 4 个"仅真实 PG 会炸"的 bug。凡涉及迁移、FK、列类型的改动，必须在真实 Postgres 上验证（本地 docker compose 起 pgvector/pg16，或 vm212）
- 4 个预先存在的测试失败（test_forge / test_map_integration / test_portrait / test_preset_import，清单在 PROGRESS 发现区）——不是你打破的，也不要顺手修
- 前端 `npm run build` 在 Node v25 下失败（rolldown binding，基线问题）；lint 7 errors/3 warnings 为基线
- **vm212 约束**：LLM key 是百炼 Coding Plan（条款禁止后端自动化调用）——`AGENT_ENABLED` 必须保持 false，禁止在 vm212 开 agent loop 或跑批量脚本烧配额；玩家聊天链路（交互式）可用于 E2E 验证。上生产须换按量计费 key（待 Jimmy）
- 进程内限流器（`app/ws/rate_limiter.py`）在 P0-3b Redis 化时应一并迁 Redis（PROGRESS 已备注）

现在开始：读 PROGRESS.md，认领第一个未勾选任务。
