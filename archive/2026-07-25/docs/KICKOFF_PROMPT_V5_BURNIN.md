# Kickoff V5 — vm212 burn-in 开跑（本机 CC 执行）

你在 simverse-world 仓库本机环境，负责按 `docs/BURNIN_PLAN.md` 在 vm212 启动 agent loop 试运行。运行手册是 BURNIN_PLAN（阈值/命令/验收全在里面），本文只是启动序列。所有 vm212 操作前备份 `.env`；每步结果记入 PROGRESS.md。

## 前置（一次性）

0. **向 Jimmy 要按量计费 LLM key**（OpenAI 兼容或 Anthropic 兼容端点均可）。没有 key 一切免谈——Coding Plan key 条款禁自动化，不得挪用。
1. **push 双 ref**：本地 master 应领先 origin 4 提交（`bd4bc9e`..`91463ff`：/import 路由 bug 修复 + CI 零排除 + METRICS_TOKEN + burn-in 工具包）。push 后盯 CI——**首次全量零排除口径**，若有环境性失败当场修。
2. **部署本轮代码到 vm212**（git archive 树覆盖惯例 + rebuild；无新迁移）。顺手设 `METRICS_TOKEN`（BURNIN_PLAN §1 有生成/验证命令）。

## 启动序列（BURNIN_PLAN §1/§6 阶段 1：金丝雀）

3. vm212 `.env`：换按量 key（`LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` 按端点填）+ 金丝雀预算 `BUDGET_GLOBAL_DAILY_USD=0.5` + 确认 `LLM_METERING_ENABLED=true`
4. 圈定金丝雀居民 3-5 个（其余置 sleeping，SQL 在 BURNIN_PLAN §1，含恢复表）
5. `AGENT_ENABLED=true` → `docker compose up -d --force-recreate api agent-worker` → grep "AgentLoop started"
6. 跑 2 小时，按 BURNIN_PLAN §2 观测矩阵巡检；对账：`docker compose exec api python scripts/burnin_report.py --days 1 --residents 5`
7. 任一金丝雀阈值（§3）触发 → 按 §4 回滚并停下来报告；全绿 → 恢复全量居民进阶段 2（24h，完成 §5 的 11 条行为面验收），预算调回 1.5

## 边界

- 不改代码逻辑；发现 bug 记 PROGRESS 发现区，属阻塞性的（如 worker 重启循环）先回滚再报告
- 成本数字与 E-09/E-11 区间偏差 >2× 时不要自行调价目表——记录原始数据等对账定版（F-02 口径）
- 阶段 3（48h 定版）开始前把阶段 1/2 的 burnin_report 输出存档进 `docs/research/`
