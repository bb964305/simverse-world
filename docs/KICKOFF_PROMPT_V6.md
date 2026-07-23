# Kickoff V6 — 修复批次合并部署 + 切换按量 key（deepseek-v4-flash）

你在 simverse-world 仓库本机环境。目标两件事：① 把 burn-in 修复批次 `fix/burnin-batch-1` 验证、合并、部署 vm212；② 把 LLM 从百炼 Coding Plan（qwen3.7-plus）切到**按量计费 deepseek-v4-flash**，补齐 F-02 真实账单对账。全部完成 = burn-in 阶段 3（48h 定版）就绪，是否开跑等 Jimmy 拍板。所有 vm212 操作前备份 `.env`；每步结果记入 PROGRESS.md。

## 前置检查

0. **git index 漂移归位**：主仓库 `git status` 呈现大片假 staged（MM/D，沙盒 commit-tree 后遗症，PROGRESS 有两次先例）。先 `git diff HEAD --stat` 确认物理文件与 HEAD 一致：文件齐全 → `git reset --mixed`；有缺失 → `git reset --hard HEAD`。`backend/skills_world_dev.db` 的改动直接 checkout 丢弃。

## 任务 1 — fix/burnin-batch-1 验证合并部署

分支在 `.claude/worktrees/fix-burnin-batch-1`（branch `fix/burnin-batch-1`，7 提交 `b494061..4ebbc21`）：gossip 燃料回填 related_resident_id / 社交半径扩大+plan 社交加权 / 夜间归巢 / forge stage 预算闸+计量 session 标签 / Phaser 首载画布 / chat·social 锁 TTL / conftest dummy env。

1. **Review 7 提交**：逐个 `git show` 过 diff。重点核对：a) gossip 回填不会把玩家 user_id 误写进 related_resident_id；b) 社交半径 10/14/6→18/24/10 没有破坏既有 perceive 测试的距离断言；c) 夜间归巢是零 LLM 规则路径（不烧预算）；d) forge 预算闸的 402 路径与既有 `forge_blocked` 语义一致。
2. **合并**：`fix/burnin-batch-1` rebase 到 `feat/rate-limiting-p1` 最新（或 merge，冲突少者优先）。确认**无新 Alembic 迁移**（`ls backend/alembic/versions` 对比）；若有则补链尾校验。
3. **全量验证（CI 口径）**：后端 pytest 全量零排除（基线 711+，conftest dummy env 提交后裸环境也应绿）+ 前端四连 tsc(strict)/vitest/eslint 0 问题/build。**worktree 分支写于阶段 2 期间，必须在合并后的 HEAD 上重跑**，不要只信 worktree 里的旧结果。
4. **push 双 ref**（feat + master）→ 盯 CI 双 ref 绿。
5. **部署 vm212**：惯例 = 备份 `backend.bak.<ts>` → `git archive` 树覆盖 → rebuild api+agent-worker → `/health`、`/metrics`（带 token）、AgentLoop started 三查。
6. **seed items（burn-in 发现的必修数据项）**：`docker compose exec api python -m app.seed.shop_items`，然后 `SELECT count(*) FROM items;` 应为 8。顺手确认 achievements 表已 seed（12 行）。
7. **修复生效冒烟**（部署后观察 2-4h，appendix 命令沿 BURNIN_PLAN §2）：
   - 自然互聊 ≥1 次/半天（此前两天零次），chat_wrapup parse_ok=t
   - 新产 event 记忆里 related_resident_id 非空率 >0（gossip 燃料恢复）
   - UTC sleep_hour 前居民出现 GO_HOME 动作、不再就地冻结
   - 生产页首登 Phaser 画布完整渲染（浏览器实测）

## 任务 2 — 切换按量 key：deepseek-v4-flash

**硬前提（Jimmy 提供）**：百炼按量计费 API Key，见附录 A。到手前先做步骤 1（纯代码，不依赖 key）。

1. **补价目表**：`app/llm/pricing.py` 前缀表加 `"deepseek-v4-flash": (0.14, 0.28, ...)`（¥1/¥2 per MTok，按 7.2 汇率折 USD，缓存价按官方缓存命中折扣填，注释写明汇率假设与日期）。顺手加 `qwen3.7-plus`（历史数据口径）。不补的后果：fallback haiku $1/$5，成本虚高 ~7 倍 → 预算熔断误触发。+测试，push 部署（可并入任务 1 部署批次）。
2. **落配**：vm212 `.env` 备份后改三键：
   ```
   LLM_BASE_URL=https://dashscope.aliyuncs.com/apps/anthropic
   LLM_API_KEY=sk-<百炼按量 key>
   LLM_MODEL=deepseek-v4-flash
   ```
   `LLM_THINKING` 保持 false/缺省（client 会发 `thinking:{"type":"disabled"}`——deepseek-v4-flash **默认开思考**，思维链按输出计费，不关则成本与延迟翻倍）。`VIDEO_LLM_MODEL=kimi-k2.5` 不动（同端点第三方列表在册）。`BACKGROUND_LLM_MODEL` 留空（= effective_model）。
3. **E-06 式探针（recreate 前，裸 curl）**：`POST https://dashscope.aliyuncs.com/apps/anthropic/v1/messages` 验证：200；`usage.input_tokens/output_tokens` 在场；thinking disabled 时响应无 thinking block；一条含 JSON 指令的请求可解析。429/403 直接停下报告（key 地域/欠费问题）。
4. **recreate + 计量自检**：`docker compose up -d --force-recreate api agent-worker` → 注册 smoke 账号真聊 2 轮 → `llm_usage` 出 `model='deepseek-v4-flash', source='usage', parse_ok=t`，cost_usd 走新价目（一次 player_chat 应为 e-5~e-4 量级，不是 e-3）。
5. **JSON 可靠性金丝雀（2h）**：E-05 只验过 qwen，deepseek 未验。观察窗口内 parse_ok ≥95%、attempt>1 ≤5%，互聊 wrapup 手动触发 1 次全链过。不达标 → 回滚 `.env`（换回备份）并停下报告，不要自行调 prompt。
6. **F-02 真实账单对账（≥1h 后，账单分钟级/观测小时级）**：`scripts/burnin_report.py` 的 cost 合计 vs 百炼[模型观测](https://bailian.console.aliyun.com/?tab=model#/model-telemetry) token 数 vs [账单明细](https://billing-cost.console.aliyun.com/finance/expense-report/expense-detail-by-instance)实际扣费。比值记入 PROGRESS（目标 0.8–1.2；token 数已知可信 source=usage，这次补的是**钱**的口径）。
7. **（可选，趁便宜）熔断演练**：`BUDGET_GLOBAL_DAILY_USD=0.05` recreate，观察 THROTTLE→RULE_ONLY→PLAYER_ONLY 三级降级与次日恢复，销掉 burn-in 遗留验证项。演练完调回 1.5。

## 边界

- 两任务串行：先任务 1 合并部署稳定，再切 key——否则出问题分不清是修复批次还是新模型。
- 探针/金丝雀任一环节红 → 回滚 `.env` 备份，报告后等指示；不要在生产 `.env` 上反复试错。
- deepseek-v4-flash 行为面（对话文风、决策质量）与 qwen3.7-plus 的差异只记录不调参，留阶段 3 系统评估。
- 阶段 3 开跑需 Jimmy 明确说"开"。

## 附录 A — 给 Jimmy：按量计费 key 配置指南（deepseek-v4-flash）

1. **开通/登录百炼**：https://bailian.console.aliyun.com ，地域选**华北2（北京）**——deepseek-v4-flash 等第三方模型的 Anthropic 兼容接口**仅北京地域**提供。
2. **创建 API Key**：控制台右上角头像 → API-KEY（或 https://help.aliyun.com/zh/model-studio/get-api-key ）→ 创建，归属选默认业务空间。得到 `sk-` 开头的 key。这与 Coding Plan key 是两套体系，互不影响。
3. **确保可扣费**：https://usercenter2.aliyun.com/home 充值（按量自动扣费，分钟级出账；新账号 deepseek 系列通常有免费额度，用完自动转按量）。价格：deepseek-v4-flash 输入 ¥1/百万 token、输出 ¥2/百万 token；我们默认关思考模式。按 burn-in 实测量级（~$0.2/天）折算约 **¥2-15/天**，先充 ¥50 足够阶段 3。
4. **交付**：把 key 发给 Claude Code（或自己写入 vm212 `/opt/skills-world/deploy/.env` 的 `LLM_API_KEY`）。端点和模型名由 Claude Code 按上文任务 2 落配，你只需要给 key。
5. **对账入口**（任务 2.6 会用到）：模型观测 https://bailian.console.aliyun.com/?tab=model#/model-telemetry （小时级更新）+ 账单明细（费用中心 → 按实例）。
