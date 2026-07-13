# BURNIN_PLAN — vm212 开 agent loop 试运行（burn-in）执行方案

> 执行环境：vm212 `/opt/skills-world`（测试环境，Jimmy 已确认可自由操作），API 端口 8100，compose 服务 = `db`（pgvector/pg16）/ `redis` / `api` / `agent-worker`（另有 cloudflared、ollama 容器）。写作基线 2026-07-13：`alembic_version=031`、`AGENT_ENABLED=false`、LLM = 百炼 Coding Plan qwen3.7-plus（`/apps/anthropic` 中转）、Sentry 已接线（`SENTRY_ENVIRONMENT=vm212-test`）、embedding 走本地 ollama（qwen3-embedding:0.6b）。
>
> 数字与阈值出处：`docs/research/COST_RESEARCH_REPORT.md`（成本基线与预测区间）、`backend/app/llm/budget.py`（熔断三级阈值）、`backend/app/models/llm_usage.py`（计量字段）、`docs/PROGRESS.md`（跳过项清单）。

## 0. 目标：burn-in 要回答的三个问题

1. **成本**：真实跑起来后 $/居民·天 是否落在预测区间？基线 = E-11 定格的 15 居民 $0.88–1.00/天（$0.0587–0.0667/居民·天）；已落地 E-09/E-10 + E-04/E-05 + E-02 三大杠杆后，理论稳态 ≈ 基线的 **45–55%**（REPORT §一.6），即 **$0.0264–0.0367/居民·天**。
2. **行为面**：一批"需要真 LLM、沙盒里只走了模板/兜底路径"的功能（§5 清单）在 qwen3.7-plus 上是否产出合格内容——尤其 JSON 结构化输出的 `parse_ok` 率（研究期 haiku 是 13/13，qwen 未验证）。
3. **稳定性**：agent-worker 不进重启循环、熔断三级能兜住失控、计量管线本身不丢数。

## 1. 前置条件清单（开闸前逐项打勾）

### 1.1 按量计费 LLM key（硬前提，没有就不开）

现 `.env` 的 key 是百炼 **Coding Plan**（条款禁止后端自动化调用，PROGRESS 发现区有记录）。开 loop 前必须换成按量计费 key：

```
# /opt/skills-world/deploy/.env
LLM_API_KEY=<按量计费 key>
LLM_BASE_URL=<按量端点，例如百炼 /apps/anthropic 兼容端点>
LLM_MODEL=<端点侧模型 id，如 qwen3.7-plus>
```

- [ ] key 已充值/额度确认，且在供应商控制台能看到实时消费（阶段 3 对账要用）。
- [ ] 顺手完成 REPORT「待 Jimmy」F-02：记录该端点是否回传 `response.usage`（看 llm_usage 的 `source` 列是 `usage` 还是 `estimated`）。

### 1.2 预算参数建议值

| 键（.env） | 阶段 1 金丝雀 | 阶段 2/3 | 说明 |
|---|---|---|---|
| `BUDGET_GLOBAL_DAILY_USD` | **0.5** | **1.5**（代码默认） | 全局日预算（UTC 日）。1.5 = 15 居民基线 $0.9–1.0 × 1.5。金丝雀收紧到 0.5：3–5 居民预期日成本 ~$0.1–0.2，占用 20–40%，正常不触熔断；失控时很快撞 80% 降级线 |
| `BUDGET_USER_DAILY_USD` | 0.5 | 0.5 | per-user 玩家可见调用日上限，超了 WS 回 `budget_exceeded` |
| `BUDGET_FORGE_REQUEST_USD` | 0.15 | 0.15 | forge 单次上限（起跑闸门） |
| `LLM_METERING_ENABLED` | true | true | **必须为 true**——熔断读 `SUM(cost_usd)`，计量关了熔断 fail-open 直接失效（budget.py 设计如此） |
| `BACKGROUND_LLM_MODEL` | 空 | 空（备用降档） | 空 = 背景与玩家可见同模型；回滚时才设（§4.3） |

熔断三级（`app/llm/budget.py`，全局预算占用分数）：**≥80% THROTTLE**（tick 间隔 ×2）→ **≥95% RULE_ONLY**（decide 强制跟计划零 LLM + 暂停互聊发起）→ **≥100% PLAYER_ONLY**（背景全停，只留玩家可见调用）。任何查询失败 **fail-open 回 NORMAL**——所以熔断不是保险丝的最后一道，§3 的人工阈值才是。

### 1.3 METRICS_TOKEN（/metrics 经 CF tunnel 公网可见，必须设）

```bash
cd /opt/skills-world/deploy
# 生成并写入 .env
echo "METRICS_TOKEN=$(openssl rand -hex 24)" >> .env
# 生效后验证：无头 401、带头 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/metrics            # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer <token>" \
     http://127.0.0.1:8100/metrics                                                # 200
```

### 1.4 对账脚本就位 + 计量自检

`backend/scripts/burnin_report.py`（本次新增）要进 api 容器。正常走部署（rebuild 后 `COPY . .` 自带）；不想 rebuild 就：

```bash
docker compose exec api mkdir -p /app/scripts
docker cp /opt/skills-world/backend/scripts/burnin_report.py \
          $(docker compose ps -q api):/app/scripts/burnin_report.py
docker compose exec api python scripts/burnin_report.py --days 2 --residents 15   # 跑通即可
```

- [ ] 开闸前先跟一个真人玩家聊 2 轮，确认 `llm_usage` 有 `player_chat` 新行（计量链路活着）。

### 1.5 金丝雀居民圈定（阶段 1 用）

loop 只 tick `status != 'sleeping'` 的居民（`loop.py::_tick_round`）。把非金丝雀居民置为 sleeping：

```bash
docker compose exec db psql -U postgres -d skills_world <<'SQL'
-- 备份原状态（阶段 2 恢复用）
CREATE TABLE IF NOT EXISTS burnin_status_backup AS SELECT id, slug, status FROM residents;
-- 圈 3-5 个金丝雀（换成实际 slug；建议含 klaus + 一对关系较近的居民以便触发互聊/gossip）
UPDATE residents SET status='sleeping'
 WHERE slug NOT IN ('klaus', '<slug2>', '<slug3>');
SQL
```

恢复（进阶段 2 时）：

```sql
UPDATE residents r SET status = b.status FROM burnin_status_backup b WHERE r.id = b.id;
DROP TABLE burnin_status_backup;
```

注意：阶段 1 期间**不要和非金丝雀居民开聊**（聊天会把居民从 sleeping 拉回活跃）。

### 1.6 AGENT_ENABLED 翻开步骤

```bash
cd /opt/skills-world/deploy
cp .env .env.bak-burnin-$(date +%Y%m%d-%H%M)      # 必须先备份（回滚锚点）
# 编辑 .env：AGENT_ENABLED=true + §1.1/1.2/1.3 的键
docker compose up -d --force-recreate api agent-worker   # env_file 变更要 recreate
docker compose ps                                        # 两服务 Up、无重启
docker compose logs --since 2m agent-worker | grep "AgentLoop started"
# 期望：AgentLoop started (interval=60s, N agent configs loaded)
```

补充说明：`AGENT_ENABLED` 只闸 agent loop 本体；agent-worker 里的 heat/event/nightly cron 在此之前就一直在跑（digest 冷启动走零 LLM 兜底）。api 容器 `RUN_BACKGROUND_TASKS=false`，不跑任何 loop。

## 2. 观测方案

### 2.1 观测矩阵

| 观测什么 | 工具 / 命令 | 频率 | 关注点 |
|---|---|---|---|
| 成本 / scenario 分布 / parse_ok / attempt 放大 | `docker compose exec api python scripts/burnin_report.py --days 2 --residents <N>` | 阶段 1 每 30min；阶段 2/3 每 2–4h + 每日早晚各一次 | $/居民·天 vs 两个预测区间；scenario 构成 vs 基线（互聊 46% > decide 32% > 玩家 11% > plan 7.5%，REPORT §一.1——**decide 占比应显著低于 32%**，否则 skip-decide 没生效）；parse_ok ≥85%；attempt>1 ≤10%（E-19：重试放大正常值 ×1.05–1.14） |
| 快速成本总览（免进容器） | `curl -s -H "Authorization: Bearer $ADMIN_JWT" "http://127.0.0.1:8100/admin/llm-usage/summary?hours=24"` | 随手 | 同上的粗粒度版（无 parse/attempt 维度） |
| 熔断分级触发 | burnin_report 的「预算占用 %」行（阈值 80/95/100）；行为佐证 = agent.events 时间戳节奏（THROTTLE 后 tick 间隔 60s→120s） | 每次跑报告顺带 | 熔断**没有专门日志行**，靠占用 % + 节奏推断；若占用 >100% 而调用仍在增长 → 熔断失效（见 §3） |
| tick 时长 / loop 健康 | `docker compose logs --since 30m agent-worker \| grep -E "tick_round error\|Tick error"`；tick 节奏看 agent.events 行间隔 | 阶段 1 每 30min；之后每 2–4h | 注意拓扑限制：`sv_agent_tick_round_seconds` 记在 agent-worker 进程里，而 **/metrics 只由 api 进程暴露**（worker 无 HTTP），所以 vm212 上该指标在 /metrics 恒为 0，以日志节奏为准 |
| /metrics（api 进程侧） | `curl -s -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:8100/metrics \| grep -E 'sv_llm_(calls\|errors\|parse_failures)_total\|sv_ws_online\|sv_db_pool'` | 每 2–4h | 只覆盖 api 进程的调用（player_chat/forge）；背景调用的度量看 llm_usage 表。`sv_llm_errors_total` 涨 = 传输/API 层报错 |
| Sentry 错误率 | sentry.io → `simverse-backend`，filter `environment:vm212-test`；tag `component` 区分 api / agent-worker | 每 2–4h + 告警邮件 | 新 issue 类型、同 issue 事件量爆发 |
| agent.events 行为抽样 | `docker compose logs --since 1h agent-worker 2>&1 \| grep 'agent.events' \| tail -30` | 阶段 1 每 30min；之后每天 2–3 次 | 单行 JSON（ts/resident/action/target/reason/suppress_chat）。看：动作分布是否多样（不全是 WANDER/空转）、reason 是否合理、互聊是否发生 |
| worker 稳定性 | `docker compose ps`；`docker inspect --format '{{.RestartCount}}' $(docker compose ps -q agent-worker)` | 每次看日志顺带 | RestartCount 必须不增长（历史教训：embedding 短路曾引发整 worker 重启循环，PROGRESS `6562651`） |
| 行为可见性 | 浏览器开生产前端看地图：居民移动、互聊气泡、状态/心情 emoji | 阶段 1 至少一次全程盯 20min | 玩家视角世界是"活的"且不刷屏 |
| 内存/swap（ollama 共存风险） | `free -h; docker stats --no-stream` | 每天 1–2 次 | vm212 仅 1.8G 内存，ollama cap 1.4G；swap 持续抖动 → `EMBEDDING_ENABLED=false` 止血（PROGRESS 已留退路） |

### 2.2 口径提醒

- 预算窗口、burnin_report 日界都是 **UTC**（北京时间 -8h）；nightly cron 跑在 **00:30 UTC = 08:30 北京时间**。
- `cost_usd` 是 Anthropic 列表价折算的**估计值**（qwen 经中转、真实价目未验证）；burn-in 期间同时记录供应商控制台真实消费，阶段 3 算出比值。
- 玩家聊天、forge 与背景调用共享同一个全局日预算——burn-in 期间别做大规模 forge 压测，会污染对账。

## 3. 金丝雀阈值与响应

内建自动响应（无需人工）：80%/95%/100% 三级熔断（§1.2）；per-user 超额回 `budget_exceeded`；forge 超额 402；LLM 失败处处有规则兜底（decide 走 plan、辩论自动平局退款、digest 冷启动兜底）。

人工阈值（按严重度排序，命中就执行「响应」列，不讨价还价）：

| # | 信号 | 阈值 | 响应 |
|---|---|---|---|
| 1 | 日花费（burnin_report 或控制台账单） | **> 预算 2×**，或熔断该触发而调用仍在增长（llm_usage 行数照涨） | **立即停**：`docker compose stop agent-worker`（§4.1 一档）。这是"熔断失效"信号——第一嫌疑 = 计量断写（fail-open 回 NORMAL），查 llm_usage 是否还有新行、api/worker 日志的 metering 报错 |
| 2 | agent-worker 重启循环 | RestartCount 在 10min 内 +2 或 `docker compose ps` 显示 Restarting | **回滚**（§4.1 一档）→ 抓 `docker compose logs --tail 200 agent-worker` 定位 → 修复后重新进当前阶段 |
| 3 | parse_ok 率（burnin_report 任一主要 scenario：decide/plan/chat_wrapup/extract） | 失败 **>15%**（即 parse_ok <85%） | **暂停推进**（不进下一阶段，可不停 loop）：抽 llm_usage `parse_ok=false` 的行对应场景，人工重放 prompt 看 qwen 输出格式；嫌疑 = 围栏/前缀语（json_extract 已容忍大部分）。持续 >30% → 停 loop 修完再来 |
| 4 | attempt>1 占比 | **>20%**（正常放大 ×1.05–1.14，E-19） | 查重试风暴来源（按 scenario 定位），常与 #3 同根因；伴随成本超速时按 #1 处理 |
| 5 | tick 节奏 | agent.events 间隔明显 >60s 且预算占用 <80%（排除 THROTTLE 正常降频） | 端点延迟或并发不足：查 `sv_llm_errors_total`、latency（llm_usage.latency_ms）；临时 `AGENT_MAX_CONCURRENT` 5→3 或拉长 `AGENT_TICK_INTERVAL` |
| 6 | Sentry | 新 issue 类型出现，或单 issue >20 events/h | 按栈定位；影响行为面就暂停推进，只是噪音就记账继续 |
| 7 | 行为异常 | 全员同一动作 / 空转刷屏 / 互聊内容复读机 / 一对居民无限对聊 | 暂停推进，抽 agent.events + conversations 内容；互聊对滥发查 `AGENT_CHAT_COOLDOWN`(1800s) 是否生效 |
| 8 | 内存 | swap 持续增长、PG/api 变卡 | `EMBEDDING_ENABLED=false` + recreate agent-worker（只断记忆检索的语义路，行为面其余不受影响） |

## 4. 回滚预案（从快到慢三档 + 还原）

### 4.1 一键停（秒级）

```bash
cd /opt/skills-world/deploy
# 一档（最快，连 cron 一起停——digest/dream/goal eval 也停）：
docker compose stop agent-worker
# 二档（温和：只停 agent loop，保留 heat/event/nightly cron）：
#   .env 改 AGENT_ENABLED=false，然后
docker compose up -d --force-recreate agent-worker
```

注意二档下 nightly cron 的 LLM 任务（digest/dream/周评估）仍会在 00:30 UTC 调 LLM——预算熔断照管着，但若回滚原因是 #1（熔断失效），必须用一档。玩家聊天两档都不受影响（走 api 进程）。

### 4.2 预算参数收紧（不停世界，勒紧缰绳）

```
BUDGET_GLOBAL_DAILY_USD=0.2      # 让三级熔断立刻接管背景降级
BUDGET_USER_DAILY_USD=0.2
```
改完 `docker compose up -d --force-recreate api agent-worker`。适用：成本超速但行为正常、想留着观察降级路径时。

### 4.3 模型降档

```
BACKGROUND_LLM_MODEL=<端点认识的便宜档 id，如 qwen-flash / qwen-turbo>
```
只影响背景/system 调用（decide/plan/互聊/进化/夜间任务），玩家可见调用恒走 `LLM_MODEL`（config.py `background_model` 语义）。**必须填中转端点真实存在的 model id**（F-02 教训：别把 claude-* 发给百炼）。降档后行为面验收要重测（§5 是按主模型跑的）。

### 4.4 全量还原

```bash
cp .env.bak-burnin-<时间戳> .env
docker compose up -d --force-recreate api agent-worker
```
数据不需要回滚：llm_usage 是 append-only 遥测；burn-in 产生的 memories/conversations 是测试环境正常数据，留着不删。

## 5. 真 LLM 行为面验收清单（跳过项逐条）

通用要求：每项完成后在 llm_usage 里能找到对应 scenario 的行且 `parse_ok`（有 JSON 语义的）为 true；内容抽查由人工判断。手动触发统一用 api 容器（Redis pub/sub 会把 WS 帧送达玩家）。

### 5.1 进化 shift / drift（scenario=`evolution_shift` / `evolution_drift` / `evolution_sync`）

- **触发**：与同一居民密集互动制造事件记忆。guard 条件（`app/personality/guard.py`）：drift 需自上次以来 **≥15 条事件记忆**；shift 有 **24h 冷却** + 高重要度触发记忆；两者共享月度维度变更预算。阶段 2 的自然互聊 + 玩家对话通常够触发 drift；shift 可用高强度对话（表白/冲突类话题）催化。
- **预期**：15 维评分小幅漂移（drift）或显著跳变（shift）落库；`personality_shifted` 触发 feed push + D1 soul_shaper 归因；prompt 中人格描述随之变化。
- **通过标准**：≥1 次 drift 或 shift 评估调用 parse_ok=true 且维度变化写回；guard 生效（同居民 24h 内无第二次 shift；月预算裁剪日志可见）；变化方向与触发记忆语义相符（人工判断）。

### 5.2 E2 真 LLM 组梦（scenario=`dream`）

- **触发**：自然 = 居民当日 ≥3 条新记忆 + 00:30 UTC nightly（概率 0.5、全局每晚上限 10）。手动：
  ```bash
  docker compose exec api python -c \
    "import asyncio; from app.services.dream_service import run_nightly_dreams; print(asyncio.run(run_nightly_dreams()))"
  ```
- **预期**：memories 出现 `type='dream'` 行（第一人称、prompt 限 80 字、素材 = 当日 top3 + 一条旧记忆的荒诞混搭）；梦涉及玩家 → `dream_generated` 事件 + S4「有人梦到了你」通知 + D1 dreamt_of；次日对话 prompt 注入「昨晚你做了个梦：…」。
- **通过标准**：梦内容通顺、素材可溯源（对得上当日记忆）、不是记忆原文复读；与该居民对话时能自然提到梦。

### 5.3 E3 gossip 失真改写（scenario=`gossip`）

- **触发**：居民互聊收尾时 0.3 概率传第三者记忆；失真概率 `min(0.2×hops, 0.8)`，hops≥4 终止。金丝雀圈里放一对关系近的居民 + 一条 importance≥0.6 的三方事件记忆，等互聊自然发生（阶段 2 内多次机会）。
- **预期**：听者出现 `source='gossip'` 记忆；未失真 = 原样转述（不调 LLM，llm_usage 无行）；失真 = LLM 改写（有 scenario=gossip 行），主干保留、一个细节被夸大/改错；`metadata_json.origin_memory_id` 串链。
- **通过标准**：≥1 条失真改写案例，与 origin 记忆对读「主干同、细节变」；admin 谣言链 API（`/admin` gossip 路由，RumorChainPanel）能回溯完整链。

### 5.4 A1 目标周评估（scenario=`goal_eval`）

- **触发**：自然 = 周日 00:30 UTC nightly。burn-in 窗口不含周日就手动：
  ```bash
  docker compose exec api python -c \
    "import asyncio; from app.tasks.nightly_cron import run_weekly_goal_eval; asyncio.run(run_weekly_goal_eval())"
  ```
  前提：居民有 active life goal（没有先跑 `seed/backfill_goals.py` 规则化建目标）。
- **预期**：LLM 读本周 importance≥0.5 记忆 top30，输出 `{progress_delta, milestone, verdict}`；进度累积、里程碑追加；achieved/failed → resolved + importance 0.9 reflection 记忆（顺带喂 5.1 的跳变评估）。
- **通过标准**：parse_ok=true；progress_delta 与本周实际行为强度合理相关（一周没动静不该 +30%）；milestone 文案具体、非模板腔。

### 5.5 A5 日报 LLM 组稿（scenario=`digest`）

- **触发**：自然 = 每日 00:30 UTC（有素材才调 LLM；素材 = 当日互聊记忆 top10 + 人格变更 + 活跃事件 + heat top3）。手动全套 nightly（幂等）：
  ```bash
  docker compose exec api python -c \
    "import asyncio; from app.tasks.nightly_cron import run_nightly_jobs; asyncio.run(run_nightly_jobs())"
  ```
- **预期**：digests 表新行 `content_md` 为连贯中文日报（**不是**冷启动兜底文案）；`digest_ready` 广播（前端报纸图标红点）；置顶公告贴出现。
- **通过标准**：日报事实与当日 llm_usage/记忆对得上（不编造居民/事件）；文风可读；同日重跑不重复生成（幂等约束）。

### 5.6 E9 辩论 live（scenario=`debate`）

- **触发**：无路由/cron，容器内分两步（中间留押注/投票窗口，前端 /debates 页实时观战）：
  ```bash
  # 第 1 步：创建（status=announced，此时去前端押注 10–200 SC）
  docker compose exec api python - <<'PY'
  import asyncio
  from app.database import async_session
  from app.services.debate_service import create_debate

  async def main():
      async with async_session() as db:
          d = await create_debate(db, "村里该不该修一座钟楼", "klaus", "<slug2>")
          print("debate_id:", d.id)
  asyncio.run(main())
  PY

  # 第 2 步：开播 6 轮（换入上面的 debate_id）
  docker compose exec api python - <<'PY'
  import asyncio
  from app.database import async_session
  from app.models.debate import Debate
  from app.services.debate_service import run_live

  async def main():
      async with async_session() as db:
          d = await db.get(Debate, "<debate_id>")
          d = await run_live(db, d)          # 6 轮直播 → 开票
          print("status:", d.status, "turns:", len(d.transcript_json or []))
  asyncio.run(main())
  PY
  ```
  投票后结算：`from app.services.debate_service import settle; await settle(db, "<debate_id>")`。
- **预期**：6 轮 `debate_turn` WS 帧实时到达前端；transcript 落库；任一轮 LLM 失败 → 自动平局全额退款（这条**不**应触发）。
- **通过标准**：双方发言立场分明、有来有回（不是各说各话）、符合各自人格；押注→结算 SC 对账正确（burn-in 至少完整走一次 stake→vote→settle）。

### 5.7 E6 weather 对话自然提及

- **触发**：weather 马尔可夫机自动产段（world_events kind=weather，S1 cron 翻转）；等到雨/雪/暴风段（`SELECT type,title,is_active FROM world_events WHERE type='weather' ORDER BY starts_at DESC LIMIT 3;`），与居民开聊并观察 decide 行为。
- **预期**：活跃 weather 事件经 S1 单一注入点进 chat/perceive/decide prompt；居民对话自然带出天气；decide 收到按 kind 的软提示（雨天更倾向室内动作，但**不改作息**——测试锁定的语义）。
- **通过标准**：对话提及自然（不是复读注入文本「当前世界事件：…」）；雨天 agent.events 的动作分布可见室内倾斜；前端粒子渲染与事件一致。

### 5.8 互聊收尾合并 chat_wrapup（E-04 新管线首验，scenario=`chat_turn` / `chat_wrapup`）

- **触发**：金丝雀互聊自然发生即可（这是 P1-1 子提交 5 的新管线，真 LLM 下首次验证）。
- **预期**：一次合并调用产出双方记忆 + 关系 + summary/mood；解析失败重试 1 次（attempt_no=2），再失败走通用兜底。
- **通过标准**：`chat_wrapup` parse_ok ≥85%；attempt>1 占比低；抽 3 场对话对读：双方记忆视角正确（不串人）、summary 贴合对话、关系变化方向合理。qwen 上嵌套 JSON 解析是研究期最大不确定点（E-05），这条是 burn-in 的重点观察对象。

### 5.9 decide 计划优先跳过（E-09 首验，scenario=`decide`）

- **触发**：自然运行即可（`skip_decide_when_planned` 在 3 个 agent YAML config 全开）。
- **预期**：有新鲜计划的 tick 规则化执行零 LLM；仅中断信号（新高重要度记忆/附近空闲社交对象）触发 LLM 复议。
- **通过标准**：burnin_report 里 decide 成本占比**显著低于基线的 32%**；agent.events 中可见"跟计划"与"被打断改选"两类 reason 共存；行为不呆板（E-10 语义：无事件全遵从、有事件约半数改主意）。

### 5.10 A3 挚友 LLM 个性化问候 —— **未实现，降级验收**

跳过项原文（PROGRESS 批次 1 / PLAN_P3 后续轮）：挚友 LLM 个性化问候留 vm212 生产化。当前实现 = 模板池问候（零 LLM）。burn-in 验收降级为：有关系记忆的玩家上线后，24h 未问候的 idle 居民发模板问候（WS `resident_greeting` 气泡 + 离线走通知中心；挚友 importance≥0.85 概率送礼）。**LLM 个性化版本记入 burn-in 后跟进项**，不阻塞定版。

### 5.11 C1 LLM validation 反滥用门 —— **未实现，相邻验收**

跳过项原文：C1 import 的完整 LLM validation 门用敏感词表轻校验替代。burn-in 相邻验收：跑一次 **deep forge** 全管线，确认 `forge_validate`（以及 forge_router/build/extract/refine）在真 LLM 下 parse_ok=true、产出居民质量可接受——这覆盖了同款 validation prompt 的模型兼容性。C1 import 的 LLM 门记入跟进项。

## 6. 分阶段计划

| | 阶段 1 金丝雀 | 阶段 2 全量 | 阶段 3 稳态定版 |
|---|---|---|---|
| 规模/时长 | 3–5 居民 × 2h | 15 居民 × 24h | 15 居民 × 48h |
| 预算 | `BUDGET_GLOBAL_DAILY_USD=0.5` | 1.5 | 1.5（不许中途改） |
| 观测节奏 | 每 30min（§2.1 全套） | 每 2–4h + 早晚报告 | 每日 2 次 |
| 报告命令 | `burnin_report.py --days 1 --residents 5` | `--days 2 --residents 15` | 结束跑 `--days 2 --residents 15` 定版 |

**阶段 1（金丝雀，3–5 居民 × 2h）**
- 进入条件：§1 全部打勾。
- 重点：计量/熔断/worker 稳定性 + 5.8/5.9 首验 + 至少盯 20min 地图。注意 `AGENT_MAX_DAILY_ACTIONS=20` 在 60s tick 下约 30–60min 烧完（E-11 副产物），2h 里后段行为变稀**是预期不是故障**。
- 退出标准（全满足才进阶段 2）：worker 零重启；`decide`/`chat_wrapup` parse_ok ≥85%；成本时速外推 ≤ 阶段 2 预算；Sentry 无新增 error 类 issue；agent.events 动作分布正常。
- 收尾：恢复居民状态（§1.5 restore SQL）+ 预算调回 1.5。

**阶段 2（全量 15 居民 × 24h）**
- 必须跨一个 00:30 UTC nightly 周期（digest/dream/胶囊投递）。
- 完成 §5 验收清单主体（5.1–5.9；周评估不逢周日就手动触发 5.4）。
- 退出标准：$/居民·天 ≤ 基线上沿 $0.0667（超了说明杠杆没生效，先查 5.9）；parse_ok ≥85%；熔断如被触发，降级/恢复行为与设计一致；§5 清单 5.1–5.9 全过（5.10/5.11 记跟进项）。

**阶段 3（48h 稳态 + 成本对账定版）**
- 参数冻结连续 48h（跨两个完整 UTC 日 + 一个周日更佳，顺带自然验 5.4）。
- 结束动作：
  1. `burnin_report.py --days 2 --residents 15` 输出存档（贴进 PROGRESS）；
  2. 抄录供应商控制台同窗口真实账单，计算 `真实账单 / cost_usd 估计` 比值，回填 REPORT F-02 开放问题；
  3. 定版决策：`BUDGET_GLOBAL_DAILY_USD` 终值（建议 = 实测日成本 × 1.5）；`BACKGROUND_LLM_MODEL` 是否降档；玩家可见调用是否升 Sonnet 档（E-18：全服 +19%）；扩容余量核对（E-13：5% 互聊率下并发悬崖 ~86 居民，15 居民远离悬崖）。
- 任一阶段命中 §3 阈值：按响应列处理；回滚后从**该阶段起点**重来（不用从阶段 1 重来，除非改了 key/模型/解析器这类地基）。

## 7. 已知限制（burn-in 期不修，记账即可）

- `sv_agent_tick_round_seconds` 因进程拓扑在 /metrics 不可见（§2.1）；熔断分级无专门日志行——两者都值得 burn-in 后补一行日志/暴露口，先记跟进。
- forge quick 的子进程 LLM 调用未计量（P1-1 已知遗漏）——burn-in 期间少用 quick forge，或接受账面略欠。
- weather 事件 ~6 条/天累积 world_events 无清理（PLAN_P3 后续轮发现区）——48h 量级无害。
- 计量/熔断全链 fail-open：这是"世界永不白屏"的设计代价，人工阈值 #1 是补位（§3）。
