# Kickoff Prompt V3（从 P1-4 起步，适用 Opus 4.8）

> 用法：在新 Cowork 会话中选中本仓库文件夹，把下面分隔线以下的全文粘贴为首条消息。
> 与 V2 的区别：P0 全部 + P1-1（🔥 功能闸门：计量/熔断/分级/4 杠杆/顺手修）已全部完成；下一个未勾选任务是 **P1-4（前端分包）**，随后 P1-5。并入本轮踩实的沙盒环境坑清单。

---

你是 Simverse World 仓库的执行工程师，按既定路线图逐步完成剩余优化与全部新功能。

**规格文档**：`docs/OPTIMIZATION_PLAN.md`（P0/P1/P2 优化，P1-4 见 §133-143、P1-5 见 §145-149）、`docs/FEATURE_SPECS.md`（29 个功能的可开工规格）、`AGENTS.md`（仓库规范）、`docs/PROGRESS.md`（**唯一进度真相**，已存在，不要重建）。

## 每次会话的固定流程

1. 读 `docs/PROGRESS.md`
2. 取**第一个未勾选任务**（当前应为 **P1-4**），宣布本次目标。一次只做一个任务，做完再取下一个
3. 动工前：先读该任务在规格文档中的完整条目，再读涉及的源码文件，确认规格与代码现状一致
4. 实现 → 按「完成定义」自测 → 提交 → 在 PROGRESS.md 勾选，并附一行说明（提交哈希 + 实际改动与规格的偏差，若有）
5. 会话结束前（或上下文吃紧时）：提交所有已完成工作，更新 PROGRESS.md，使任何新会话可无缝接续

## 顺序（不可跳）

**P1-4 → P1-5 → 底座周 S1–S5 → 批次 1→2→3→4 → 赛季装配。** 带 🔥 的功能的硬闸门（P1-1）已完成，🔥 功能已可开工，但仍按路线图顺序推进（先清 P1-4/P1-5 前端债，再进底座周）。

## 本次任务：P1-4 前端代码分割（OPTIMIZATION_PLAN §133-143，约 1 人日）

现状：`src/App.tsx` 所有页面 eager import（`LoginPage/GamePage/ForgePage/ProfilePage/OnboardingPage/AdminPage/AuthCallbackPage`，7 个 page 全在 `src/pages/`）；`vite.config.ts` 只有 5 行、无任何 build 配置；全库无 `React.lazy`。Phaser（≈1.4MB）、`@uiw/react-md-editor`、admin 面板全进首屏主包，登录页也要下整个游戏引擎。

要做：
- `App.tsx`：把重页面改 `React.lazy(() => import(...))` + `<Suspense fallback={...}>`。至少 `GamePage`（带 Phaser）、`AdminPage`、`ForgePage`（带 md-editor）；`LoginPage`/`AuthCallbackPage` 属首屏可保持 eager。注意这些 page 现在是**具名导出**（`export function GamePage`），`React.lazy` 需要 default export——用 `lazy(() => import('./pages/GamePage').then(m => ({ default: m.GamePage })))` 适配，别去改各 page 的导出方式（超范围）。
- `vite.config.ts`：加 `build.rollupOptions.output.manualChunks`，至少把 `phaser` 单独拆一个 chunk（`manualChunks: { phaser: ['phaser'] }`），可考虑再拆 `react-md-editor`。
- 量化：装 `rollup-plugin-visualizer`（devDep），产出前后对比（首屏 JS 体积预期降 ≥60%）。把关键数字写进 PROGRESS 说明。

## 随后：P1-5 前端网络层健壮性（OPTIMIZATION_PLAN §145-149，约 1.5 人日）

现状：`src/services/api.ts` 的 `apiFetch` 无超时/无 AbortController/无重试；401 时直接 `window.location.href='/login'`（丢状态，并发下多次跳转）；Forge 全靠 `setInterval` 轮询（`DeepForge.tsx`、`QuickForge.tsx`、`ForgeChat.tsx`，最快 2s 一次），而系统本有 WS 通道。

要做：
- `apiFetch` 加 `AbortSignal.timeout(15000)` + 组件卸载可取消；401 集中经 store（`gameStore`）一次性登出，去掉散落的直接跳转。
- Forge 进度改走 WS 推送：**后端** pipeline 各阶段完成时 `manager.send(user_id, {...})`（`app/forge/pipeline.py` 各 stage 之后 / `app/routers/forge.py` 的后台任务里）；**前端** `services/ws.ts` 加对应分支，Forge 组件去掉三处 `setInterval` 轮询，改监听 WS。新 WS 出站消息要在 `app/ws/protocol.py`（如有出站建模）与 `services/ws.ts` 同步。

## 硬性规则

- **完成定义（前端为主）**：`npm run lint` 与 `npx tsc --noEmit` 通过（lint 基线 7 errors/3 warnings 为预先存在，不要顺手清）；能跑 `npm run build` 就跑并核对分包/体积（sandbox 现为 **Node v22**，大概率能 build；若 rolldown 原生 binding 报 `MODULE_NOT_FOUND` 即命中已知基线问题，降级为 lint+tsc 并记入 PROGRESS）。**后端改动（P1-5 的 WS 推送）必须带 pytest 用例**，`cd backend` 全绿（既有基线失败除外，见下）。
- **提交**：Conventional Commits（`feat(frontend): ...` / `perf(frontend): ...`），一个任务一个或多个小提交，禁止混合大提交。
- **接线检查**：新 WS 消息在 `app/ws/protocol.py` 建模型、前端 `services/ws.ts` 加分支；新路由注册进 `app/main.py`。
- **冲突处理**：规格与代码现实不符时以代码为准，小偏差自行适配并记录；影响架构的大冲突停下来向我提问（给出选项和你的建议）。
- **禁止**：规格范围外的顺手重构；跳过测试标绿；修改 `.env`/密钥；新问题记入 PROGRESS.md「发现」区，不当场展开。

## 沙盒环境坑（前人验尸报告，动工前读一遍——这些坑真的会吃掉时间）

- **挂载盘禁止删文件 + 禁 hardlink**。后果连锁：
  - **Python venv 必须建在挂载盘外**（如 `/tmp/svenv`），且 `export UV_LINK_MODE=copy`，否则 `uv pip install` 因无法替换/删文件而失败。仓库要求 Python ≥3.11、宿主可能是 3.10（缺 `datetime.UTC`），用 `uv venv --python 3.12 /tmp/svenv` + `uv pip install --python /tmp/svenv/bin/python -e ".[dev]"`。装 `pytest-timeout` 便于隔离无外网的网络用例（`--timeout=15 --timeout-method=signal`）。
  - **前端 `node_modules`**：`npm install` 若报无法删/替换文件，同理需绕挂载盘（可 `npm install --no-bin-links` 或把 install 目标放挂载盘外再软链——先试直接 install，失败再绕）。
  - **git 提交**：每次 git 写操作会留下无法删除的 0 字节 `.git/index.lock`/`.git/HEAD.lock`/`.git/config.lock`，阻塞下一次写。对策：每条 git 写命令前先把锁挪走——`for f in .git/index.lock .git/HEAD.lock; do [ -e "$f" ] && mv "$f" "$f.gc.$(date +%s%N)"; done`。`git add` 时的 `warning: unable to unlink .git/objects/*/tmp_obj_*` 是挂载盘删不掉临时对象，**无害**（对象已写入）。若 reflog 锁报错，临时 `git config core.logAllRefUpdates false` 提交完再 `git config --unset`。这些 `.lock.gc.*` 残留可让我在自己电脑上 `rm`。
- **测试数据库**：conftest 用内存 sqlite；但用到**全局 engine** 的用例（lifespan、ws handler 直连 `async_session`）会命中挂载盘上的 dev `skills_world_dev.db`，**该文件现在读即 `sqlite3.OperationalError: disk I/O error`（挂载盘限制，非改动引入）**。跑这类用例要 `export DATABASE_URL="sqlite+aiosqlite:////tmp/xxx.db" AUTO_CREATE_TABLES=true` 指向 `/tmp` 可写库（需要表就先跑一次 `Base.metadata.create_all`）。`DEBUG=true` 必设（否则 P0-4b 拒绝默认 JWT 密钥）。
- **既有基线失败（不是你打破的，别顺手修）**：`test_portrait::test_generate_portrait_success`、`test_preset_import::test_seed_presets_creates_residents`（测试与代码漂移）；`test_import`、`test_map_integration`、`test_research_stage`、`test_resident_edit`（沙盒无外网的网络用例，会超时）。全套件其余零失败。
- **vm212 约束**：LLM key 是百炼 Coding Plan（条款禁止后端自动化调用）——`AGENT_ENABLED` 必须保持 false，禁止在 vm212 开 agent loop 或跑批量脚本烧配额；玩家聊天链路（交互式）可用于 E2E 验证。生产端点是百炼中转、`effective_model=qwen3.7-plus`（非 Anthropic 原生），改任何"模型 id"相关逻辑前记住这点（F-02）。

## 已建成的地基（背景，P1-4/P1-5 一般用不到，但别踩坏）

P0 全绿、P1-1 完成（`37fb631`→`6b30b7b`）：Redis 化 ConnectionManager + pub/sub；LLM 计量表 `llm_usage` + 全调用点 `Meter` 接线；三级预算熔断器（`app/llm/budget.py`，按 `SUM(cost_usd)` 分级，fail-open）；decide 计划优先跳过 + 规则中断；互聊收尾 5→1 合并；history 双注入修复 + 玩家聊天滑窗；分级路由（`background_model` 默认=effective_model，ops 可 pin）。迁移 013 的全链 + 真 PG 复验仍**留 vm212**（沙盒无 pgvector）。

现在开始：读 PROGRESS.md，认领第一个未勾选任务（P1-4）。
