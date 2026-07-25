# PLAN P3 — Phase 3 全量收尾 + P2 工程化扩展

制定日期 2026-07-12。范围：PROGRESS.md「Phase 3 — 持续项」五项 + OPTIMIZATION_PLAN §四 中尚未落地的工程化项。原则：纯重构零行为变化，每步验收，PROGRESS 记账。

## 0. 现状基线（2026-07-12 调研，先于本计划）

与 PROGRESS.md 记录的出入——**五项中四项实际已有提交**：

| 项 | 状态 | 证据 |
|---|---|---|
| P1-3 分页与索引 | ✅ 主体已落，**已在 vm212**（alembic=030） | `fee7126`：慢查询日志（database.py events）、索引 030（residents.status / conversations.resident_id+rating）、residents 分页、loop 列裁剪 |
| 前端测试底座 | ✅ 已提交未推送 | `014fdb8`：Vitest+RTL、ErrorBoundary、store/api 测试 |
| CI | ✅ 已提交未推送，**从未在真 Actions 跑过** | `e02de65`：3 job（pytest / pgvector 迁移冒烟 / 前端四连），eslint 基线闸门 7err/3warn |
| Sentry + /metrics | ✅ 已提交未推送 | `30d7334`：observability.py（prometheus_client + 懒加载 sentry_sdk）、main.py+agent worker 接线、前端 monitoring.ts 动态 import |
| P1-6 大文件拆分 | ❌ **主体未做** | api.ts 1208 行、SettingsPanel 808、UsersPanel 620、forge_service.py 669（DEPRECATED 但 6 端点仍走它）、双 prompt 文件 294+262 |

其它事实：分支 `feat/rate-limiting-p1` 领先 origin/master 3 提交；有未提交 WIP 拆分半成品（`profile/settings/` 3 文件、`admin/users/helpers.ts`，尚无引用方）；TS strict 未开（tsconfig.app.json 无 strict 键）；eslint 基线 7err/3warn；WS 重连固定 3s 无退避无 UI 提示；无 .env 一致性检查；无 agent 结构化日志。docs 多个文件未纳 git（FEATURE_SPECS/OPTIMIZATION_PLAN/KICKOFF_PROMPT*）。

## 批次 0 — 记账与推送闸门

- **T0.1** PROGRESS.md：Phase 3 区补记四项已完成（含提交号与本表证据），P1-6 标记本计划接手
- **T0.2** push master+feat 同步（Kickoff V4 惯例）→ GitHub Actions 首跑 → 修环境问题至三 job 全绿。沙盒已验证可 fetch origin；push 凭据若不可用，出「本机执行清单」交 Jimmy
- **T0.3** docs 纳管决策：FEATURE_SPECS/OPTIMIZATION_PLAN/KICKOFF_PROMPT* 建议入库（工作资产，PROGRESS 多处引用）——待 Jimmy 确认后一并提交

## 批次 1 — P1-6 大文件拆分（主体）

纯重构，对外行为/API/视觉零变化。三个互不相交的工作面可并行：

- **T1.1 前端 SettingsPanel.tsx 808 行** → `profile/settings/` 按分区拆子组件 + `useSectionForm` hook（**续接 WIP**：shared.tsx/AccountSection/useSectionForm 已起头）
- **T1.2 前端 UsersPanel.tsx 620 行** → `admin/users/`（helpers.ts 已起头）：表格/弹窗/操作分文件
- **T1.3 后端 forge_service.py 669 行（DEPRECATED）**：`/forge/start|answer|quick|status` + settings.py/residents.py 的引用迁出或收敛到 `forge/legacy.py`；归一 `llm/forge_prompts.py`(294) 与 `forge/prompts.py`(262) 双套 prompt
- **T1.4 前端 api.ts 1208 行**（在 T1.1/T1.2 合入后做，避免 import 冲突）→ `services/api/{core,resident,forge,admin,social,economy}.ts`，`services/api.ts` 保留为 barrel re-export，全库调用点零改动

验收（每个 T 独立提交）：`tsc --noEmit`、`vitest`、eslint ≤ 基线、`vite build`（outDir /tmp，分包不回归）；后端 pytest 选择性套件零新增失败；拆后单文件 <400 行。

## 批次 2 — P1-3 扫尾

- **T2.1** 列表接口分页审计：全 routers 扫一遍无界 `select` 列表（admin 面板列表重点），拉齐 limit/offset 或游标；有界小表（如 LOCATIONS）记录豁免理由
- **T2.2** 评分聚合增量维护：030 已有 `conversations.resident_id+rating` 索引，评估现聚合是否已够（预期：够，记决策不做增量维护）

## 批次 3 — P2 工程化扩展

- **T3.1** TS `strict: true`（tsconfig.app.json）+ 修全部类型错误；CI 已跑 tsc 自动生效
- **T3.2** eslint 基线清零（7err/3warn → 0/0）：3 处 setState-in-effect、2 处 no-unused-vars、1 处 react-refresh、3 处 exhaustive-deps；CI 基线常量降为 0
- **T3.3** `.env.example` ↔ `config.py` 一致性检查：脚本 + pytest 用例（挂进 CI backend job）
- **T3.4** WS 断线重连指数退避（3s→30s cap + jitter）+ 断线期间 UI 提示条；恢复后自动消失
- **T3.5** GameScene 事件监听/timer 的 `shutdown` 清理审计（StrictMode 重挂载泄漏）
- **T3.6** agent 行为结构化日志：最小落法 = 在既有广播 chokepoint 打 JSON 结构化日志（logger `agent.events`），不建表（llm_usage+广播已覆盖回放需求的大半，建表待真实回放需求出现）——记决策

验收同批次 1 口径；T3.1/T3.2 完成后 CI 前端 job 的基线闸门收紧到 0。

## 批次 4 — 部署接线与验证（需外部依赖/Jimmy）

- **T4.1** Sentry：建项目拿前后端 DSN（**需 Jimmy**）→ vm212 `.env` 加 `SENTRY_DSN`、前端构建注入 → 触发测试事件验证上报
- **T4.2** vm212 验证 `/metrics` 输出与慢查询日志阈值生效；Prometheus 抓取配置（可选，有监控机再接）
- **T4.3** CI 三 job 首绿确认（若 T0.2 沙盒推不动则此处随本机推送一并做）

## 风险与已知坑

- **git mount EPERM 锁**：commit 若卡死，用 PROGRESS 发现区记录的 `/tmp` 索引 + commit-tree + 覆写 ref 绕过法
- **push 凭据**：沙盒 fetch 已通，push 未验证；不通则批次 0/4 的推送项转本机执行清单
- **forge 端点迁移**：前端 QuickForge/ForgeChat/DeepForge 与 17 个既有测试依赖现行为，迁移必须保持响应 schema 不变
- **TS strict 爆量**：若错误 >50 个，按目录渐进（先 stores/services 后 components），多提交推进
- **既有基线失败**：6 个预存网络/漂移测试文件与 1 个 flaky 用例维持排除口径（CI 注释已载明），本计划不修复它们（超范围）

## 总验收口径

全部批次完成后：pytest 选择性套件零新增失败；tsc（strict）/vitest/build 全过；eslint 0/0 且 CI 闸门=0；CI 三 job 绿；PROGRESS.md Phase 3 全勾 + 各项记账（提交号/偏差/验证）。
