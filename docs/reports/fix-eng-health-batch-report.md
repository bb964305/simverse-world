# 工程健康批报告 — R3 夜间补跑 / R4 聊天锁 DB 侧回收 / P2 多实例心跳告警

- 分支：`fix/eng-health-batch`（worktree `/Volumes/data/dev/sv-eng-health`），base = `master c54c606`
- 对应 `docs/ROADMAP.md` 近期优先级 #5
- 状态：三件**代码完成**（本地测试绿）。未部署、未 push、未合并，vm212 未触碰。

| 件 | commit | 标题 |
| --- | --- | --- |
| A（R3 夜间补跑） | `ef2e529` | eng-health-A: nightly catch-up ledger (R3) |
| B（R4 聊天锁 DB 侧回收） | `d6bb8e5` | eng-health-B: DB-side chat lock recycling (R4) |
| C（P2 心跳告警） | `970a13e` | eng-health-C: background-loop heartbeats and death alerting (P2) |

---

## A. R3 — 夜间任务错过窗口补跑

### 现状缺口（证据）

`backend/app/tasks/nightly_cron.py:427-436`（改动前）：

```python
async def nightly_cron_loop() -> None:
    while True:
        await asyncio.sleep(_seconds_until_next_run(now_real()))
        await run_nightly_jobs()
```

没有任何「上次跑到哪个锚点日」的台账。进程崩溃 / 容器重启 / 部署窗口跨过 07:00 北京锚点
（`RUN_HOUR=7`、`RUN_MINUTE=0`，`nightly_cron.py:28-29`）→ 当天全部夜间作业静默丢失：
既不会补跑，也没有一行日志或告警。

### 实现方式

Redis 台账，幂等范式照抄同文件既有的 `_world_week_gate`（`nightly_cron.py:46-60`），
只是把 key 从「world-week 序号」换成「锚点日期」：

- `_LAST_RUN_DATE_KEY = "sv:nightly:last_run_date"` 存**已跑过的锚点日期**（ISO）
- `_anchor_passed(now)` / `_anchor_date(now)`：把任意时刻映射到它所属的「夜间日」。
  锚点前当前夜间日仍算**昨天**——否则 03:00 的重启会抢占今天的槽位、把 07:00 的正点跑压掉。
- `_claim_run_date(date)`：`SET NX` + 回读 GET（`ws/manager.py` 锁同款、fakeredis 安全）。
  **fail-open**：Redis 报错返回 True，坏台账绝不能让整批夜间作业哑火。
- `_needs_catch_up(now)`：**fail-closed**（与 claim 相反）。启动时台账读不出来就闭嘴，
  正点跑照常触发，避免不确定状态下乱补跑。
- `nightly_cron_loop` 进入等待前判定一次：锚点已过且台账无记录 → WARN 日志 + 立刻补跑一次。
- `run_nightly_jobs(*, once_per_day: bool = False)`：`True` 时先抢台账，抢不到直接 return。
  **默认 False**，运维脚本 / 既有测试直接调用的行为完全不变。

### 并行纪律自查

`git diff master -- backend/app/tasks/nightly_cron.py` 显示改动只有三处：
① 顶部 import + key 常量；② `run_nightly_jobs` 第一个 job 块**之上**的 4 行守卫；
③ `nightly_cron_loop` 本体。**没有任何既有 job 的 try/except 块被移动、改写或重排**
（diff 中 job 区域零删除行）。另加了一条源码顺序断言把这条不变量钉死：
`tests/test_nightly_catchup.py::test_existing_job_blocks_are_untouched_by_the_guard`。

---

## B. R4 — 聊天锁 DB 侧回收

### 现状缺口（证据）

- `backend/app/agent/chat.py:203-204`（改动前）：`initiator.status = "socializing"` /
  `target.status = "socializing"`，只在 `finally`（`chat.py:280-286`）复位成 `idle`。
- worker 被 `kill -9` / OOM / 容器重启 → `finally` 不执行 → DB 行**永久**停在 `socializing`。
- 之后所有互聊在 `chat.py:180-182` 的
  `if target.status in ("chatting", "socializing", "sleeping")` 前置检查处静默跳过
  （`{"skipped": True, "reason": "target_busy"}`），居民社交永久哑火。

**复核时的额外发现（重要，与任务书描述有出入）**：`ws/manager.py` 的
`lock_socializing` / `unlock_socializing` / `is_socializing`（`:255-277`）**全仓没有任何调用方**
（`grep -rn "lock_socializing" app/` 只命中 manager.py 自身）。也就是说 NPC↔NPC 路径
**根本没走 Redis 锁**，唯一的锁就是 DB 的 `status` 字段——所以回收器必须靠自己的时间戳判定，
不能靠「Redis key 还在不在」当主判据（Redis key 永远不在）。这也解释了为什么 Redis 侧 TTL
自愈对这个 bug 完全无效。

### 实现方式

新文件 `backend/app/services/social_status_recovery.py`：

- `mark_socializing(resident, partner_id=...)` / `clear_socializing(resident)`：置位/复位的同时
  写/删 `meta_json["social_lock"] = {"since": <iso>, "partner": <id>}`。
  写法沿用 `services/circle_service.py:109-117` 的 `dict(...)` + `flag_modified` 惯例。
- **为什么用 `meta_json` 而不是新列**：核过 `app/models/resident.py` 全文，`residents` 表
  **没有 `updated_at`**；加列意味着一条迁移，而本批 048/049 两个 revision 号已被
  S1-5 / S2-5 占用。`meta_json` 是既有的分命名空间惯例（`sbti` / `duty` / `lab` / `circle_id`），
  零迁移。
- `recover_stale_socializing(db)`：扫 `status == "socializing"` 的行，
  时间戳超阈值**或压根没有时间戳**（改动前卡死的历史行天生没戳）→ 复位 `idle` 并清戳。
  若 `ws.manager` 的 Redis 社交锁仍在（未来若真接上），跳过不杀。
- 阈值默认对齐 `ws/manager.py:50` 的 `SOCIAL_LOCK_TTL = 600`。
- 挂载点：`backend/app/tasks/heat_cron.py` 里**自己的独立 try/except 块 + 自己的 session**，
  fail-open，不与既有 mood decay / weather 块混用（有 wiring 断言钉住）。

### 已知取舍

- 检出延迟 ≤ 1 小时（heat_cron 的节拍是 `HEAT_CRON_INTERVAL_SECONDS = 3600`），
  阈值本身是 600s。要更快就得换更密的挂载点，本批按任务书留在 heat_cron。
- 只回收 `socializing`。玩家侧 `chatting`（Redis 侧确有 TTL 且每条消息重入续期，
  `ws/manager.py:198-214`）与 `sleeping`（agent loop 自有唤醒逻辑）**不在本批范围**。
- `agent/loop.py` 异常兜底路径（`:325-328`）仍直接写 `status = "idle"`，没清戳；
  无害（回收器只看 `status == "socializing"` 的行），未改以缩小改动面。

---

## C. P2 — 多实例状态与告警可观测性

### 现状缺口（证据）

`backend/app/main.py:86-99`：五个后台 loop（`heat_cron_loop` / `event_cron_loop` /
`nightly_cron_loop` / `agent_loop.run` / `embedding_backfill_loop`）只在
`run_background_tasks=true` 的那**一个**进程里跑。任一 loop 死掉（异常穿出 `while True`、
task 被 cancel、进程压根没起 loop）→ 世界静默停掉那项工作，**没有日志、没有指标、没有告警**。
预算熔断静默失效有告警（`app/llm/budget_alerts.py`，commit `a3a32ec`），loop 存活没有。

### 实现方式（照 a3a32ec 范式）

新文件 `backend/app/tasks/loop_heartbeat.py`：

- `beat(name)`：每轮往 `sv:hb:<loop>` 写 ISO 时间戳（TTL 7 天）。**永不抛**——
  心跳绝不能反过来杀死 loop。五个 loop 各在自己的循环尾部调用一次（有 wiring 断言逐个钉死）。
- `check_stale()`：心跳超阈值 → WARN 日志 + 一条 Sentry event（`sentry_sdk` 懒加载，
  无 DSN 时完全 inert），按 loop 做 cooldown 去抖；loop 恢复后自动重新武装。
  由 `beat()` 自己节流驱动（默认 5 分钟一次），因此**任何一个活着的 loop 都会替死掉的兄弟报警**，
  不需要额外看门狗进程。
- 「从没跳过」的 loop 记为 `never_seen`，**不告警**——`run_background_tasks=false` 的部署
  是配置选择，不是故障。
- 阈值按 loop 各自节拍算（`N × 自身间隔`，另有下限），所以 60s 的 event cron 不会因为一轮慢就报警，
  24h 的 nightly cron 也能有合理窗口。
- 只读视图：新增 `GET /health/loops`（`app/main.py` 末尾），返回每个 loop 的
  `state / age_seconds / threshold_seconds / last_beat` + 总体 `ok|degraded`。
  心跳在 Redis 里，所以**任何一个 API worker 都能正确回答**，哪怕 loop 归 agent-worker 进程所有。
  `GET /health` 保持原样不动（`tests/test_health.py` 断言的是精确相等）。

### 已知取舍

- nightly loop 一天才跳一次，阈值因此是天级（默认 3 天）。要更灵敏就得把 24h 的 sleep 切成小段，
  那会加大 nightly 骨架改动面、与并行两条线抢合并——本批不做。
- 若**所有** loop 同时死（= 进程本身没了），没有幸存者能发告警；这种情况靠
  `GET /health/loops`（外部探针）体现，属于进程级监控范畴。

---

## 测试清单

| 文件 | 覆盖 |
| --- | --- |
| `backend/tests/test_nightly_catchup.py`（19 项） | 锚点日期映射边界；台账 claim 同日一次 / 跨日放行 / Redis 挂了 fail-open；补跑判定五态（锚点前 / 台账空 / 台账=今天 / 台账过期 / Redis 挂）；**三态主线**：正常按时跑不补跑、跨锚点重启补跑恰一次（含 WARN 日志断言）、同日重启不重复跑；正点跑带守卫；守卫拦同日重入、默认关闭不影响手动调用；job 块顺序不变量 |
| `backend/tests/test_social_status_recovery.py`（16 项） | 打戳/清戳、其它 meta 命名空间不被破坏、脏戳容错；阈值默认 = `SOCIAL_LOCK_TTL` + env 覆盖 + 非法值回退 + 总开关；**崩溃遗留 socializing 超阈值被回收**、**活跃会话不被误杀**、无戳历史行被回收、Redis 锁仍在则保护、naive 时间戳按 UTC、其它 status 不动、开关关闭不扫；chat.py 与 heat_cron 的接线断言 |
| `backend/tests/test_loop_heartbeat.py`（22 项） | 五 loop 注册表 / key 命名空间 / 阈值倍数与下限 / 保守默认 / 非法 env 回退；beat 写入、开关关闭不写、Redis 挂不抛；快照 fresh / stale / never_seen / 脏值；**心跳新鲜不告警**、**过期恰好告警一次且不刷屏**、cooldown 到期可再报、never_seen 不告警、**开关关完全静默**、Redis 挂不抛、beat 驱动的检查节流；五个 loop 各自真的在 beat（源码 wiring 断言）；`/health/loops` ok / degraded、`/health` 未被改动 |

新增测试合计 **57 项**，全绿。

### 全量 pytest（相对主会话基线）

```
基线 /tmp/batch25B-base.txt        : 51 failed, 1737 passed, 25 skipped, 11 deselected, 17 errors
本线 /tmp/eng-health-final.txt     : 51 failed, 1794 passed, 25 skipped, 11 deselected, 17 errors

$ comm -13 /tmp/batch25B-base-fails.txt /tmp/eng-health-final-fails.txt
（空 —— 零新增失败）
```

两侧归一化失败集都是 68 行且完全一致；passed 由 1737 → 1794，差值 57 = 本批新增测试数。

---

## 收口时要进 `.env.example` 的环境变量清单

**注意（硬约束）**：`tests/test_env_example_consistency.py` 的不变量 1 要求
`.env.example` 里的每个 key 都能映射到一个真实的 `Settings` 字段。所以下面这些 key
**不能只往 `.env.example` 里加**——收口时必须**同时**在 `config.py` 里补对应字段
（本批按红线不碰 `config.py`，代码侧一律走 `os.environ`）。commit `a3a32ec` 的
`BUDGET_*` 三个 key 也欠着同一笔账，建议一并处理。

| 变量 | 默认 | 作用 | 件 |
| --- | --- | --- | --- |
| `SOCIAL_STATUS_RECOVERY_ENABLED` | `true` | 聊天锁 DB 侧回收总开关 | B |
| `SOCIAL_STATUS_STALE_SECONDS` | `600` | 判定 socializing 卡死的阈值（= `SOCIAL_LOCK_TTL`） | B |
| `LOOP_HEARTBEAT_ENABLED` | `true` | 心跳 + 告警总开关（一键静默） | C |
| `LOOP_HEARTBEAT_STALE_FACTOR` | `3` | 过期阈值 = N × 该 loop 自身节拍 | C |
| `LOOP_HEARTBEAT_MIN_STALE_SEC` | `900` | 阈值下限，防 60s 级 loop 误报 | C |
| `LOOP_HEARTBEAT_ALERT_COOLDOWN_MIN` | `60` | 同一 loop 两次告警的最小间隔 | C |
| `LOOP_HEARTBEAT_CHECK_INTERVAL_MIN` | `5` | 一次 beat 最多多久触发一次巡检 | C |

A 件无新增环境变量（沿用既有 `RUN_HOUR` 常量与 Redis）。

新增 Redis key（无需配置，登记备查）：
`sv:nightly:last_run_date`（A）、`sv:hb:<loop>` × 5（C）。
新增 `meta_json` 命名空间：`social_lock`（B）。

---

## 本线与 nightly 并行块的合并说明

**收口顺序里本线排最后**（S1-5 → S2-5 → 本线），理由与操作：

1. 本线是唯一改 `nightly_cron_loop` 骨架的线；S1-5 / S2-5 只往 `run_nightly_jobs`
   **追加新的 job try/except 块**。反序会让它们的追加块反复撞骨架。
2. 本线对 `nightly_cron.py` 的改动被刻意压到三块、且全部**不在 job 区域内**：
   - 顶部：`from app.tasks.loop_heartbeat import beat` + `_LAST_RUN_DATE_KEY` 常量
   - `run_nightly_jobs` 签名（加 `*, once_per_day: bool = False`）+ 第一个 job 块**之上**的 4 行守卫
   - `nightly_cron_loop` 整个函数（补跑段 + `beat("nightly")` + `once_per_day=True`）
   以及 `_anchor_passed` / `_anchor_date` / `_claim_run_date` / `_needs_catch_up`
   四个新函数（位于 `_world_week_gate` 与 `run_nightly_jobs` 之间，纯新增）。
3. 若合并时 job 区域有冲突，**一律取对方（S1-5 / S2-5）的 job 块全集**，
   本线只把上述三块改动重放上去即可；本线不拥有 job 区域的任何一行。
4. 重放后的验收硬门：
   - `pytest tests/test_nightly_catchup.py` 全绿（其中
     `test_existing_job_blocks_are_untouched_by_the_guard` 会同时校验
     「守卫在所有 job 块之上」与「opinion drift 仍在 digest 之前」）
   - `pytest tests/test_office_integration.py tests/test_opinion_service.py tests/test_m5_space.py`
     全绿（这三处用 `inspect.getsource(run_nightly_jobs)` 断言 job 顺序）
   - `pytest tests/test_loop_heartbeat.py::test_every_background_loop_emits_a_heartbeat`
     全绿（确认重放后 `beat("nightly")` 还在）

其它文件的冲突面：`heat_cron.py` / `event_cron.py` / `embedding_backfill.py` /
`agent/loop.py` / `main.py` 都只是尾部/循环尾追加，`agent/chat.py` 改的是
锁置位与 `finally` 两处，`services/social_status_recovery.py` 与
`tasks/loop_heartbeat.py` 是全新文件。**本批未改 `config.py`、未新增迁移**，
因此与 S1-5（048）/ S2-5（049）不存在 revision 冲突。

---

## 红线自查

- 未碰 `config.py`（`git diff master --name-only | grep config.py` → 0 命中）
- 未碰 civic / election / duty / coin / shop / proposal / `app/lab`
- 未移动、改写、重排任何既有 nightly job 块
- 未 push、未合并、未部署、未触碰 vm212
- 未提交 `backend/skills_world_dev.db`（全程显式 `git add <path>`，无 `git add -A`）
- 无 `--no-verify` / `--amend` / squash
