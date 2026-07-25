# 生产修缮三件套报告 — fix/prod-hygiene-batch(2026-07-25)

分支 `fix/prod-hygiene-batch`,base = master `d27fce7`。三件独立小修全部**纯本地开发 + 测试**完成;
生产执行步骤见文末,等服务器恢复后另行执行。**未合并、未 push、未部署、未连任何远程库。**

## 结论先行

| 任务 | 状态 | commit |
|---|---|---|
| A · 生产居民 SBTI backfill 工具(Roadmap #11) | ✅ 完成 | `e0e2d59` feat(A/sbti) |
| B · heat_cron tz 混比修复(Roadmap #12,8a0449c 起既有) | ✅ 完成 | `003968c` fix(B/heat) |
| C · 预算熔断静默失效告警(Roadmap #6) | ✅ 完成 | `a3a32ec` feat(C/budget) |

全量 pytest 硬门(相对 master 基线零新增失败):**通过**,见下文对比。

## A · SBTI backfill 工具

**问题**:vm212 现有 26 位居民带 forge 期算出的 `meta_json.sbti.type`,但 `dimensions` 稀疏
(0/26 有 A2 键)→ `civic_service._npc_choice` 的 `dims.get("A2","M")` 恒读 M,
守序/叛逆分支永不触发,NPC 投票系统性堆向 option 0(docs/PROGRESS.md S0 遗留跟进 (a))。
agent-S 已修 seed 预设(只对未来新世界生效);本工具修**已存在于库里**的居民。

**产物**:`backend/scripts/sbti_backfill.py`

- **规则模式(默认,零 LLM)**:有已知 `type` 的居民,按 `sbti_service.TYPE_PATTERNS`
  里该 type 自己的 pattern 反推补全缺失维(`match_type()` 的精确逆操作,
  与 seed/preset_characters.py `_inject_sbti` 同一套数学)。已有维值保留只补洞;
  `type` 身份永不翻转;`similarity/exact` 对声明 type 重算保持块内一致。
- **`--llm`(可选)**:规则修不了的(完全缺 sbti / 特殊 type HHHH·DRUNK 无 pattern)
  走 `sbti_service.compute_sbti` 从 persona 三层文本重算——每次调用经
  `record_usage(scenario="sbti")` 计入 llm_usage;每人调用前查
  `background_tier`,PLAYER_ONLY(全局日预算耗尽)即停,余下标记
  `skip_budget_exhausted`。
- **默认 dry-run** 只打差异报告不写库;`--apply` 才写;`--slug` 可指定单人(可重复);
  幂等——已齐全 15 维的一律 `skip_complete`,重跑收敛。

**测试**(`backend/tests/test_sbti_backfill.py`,10 个):seeded sqlite 覆盖
缺 sbti / 部分维(有 type 无 A2)/ 已齐全 三态;dry-run 不落库断言;apply+幂等;
--slug 过滤;--llm(mock compute_sbti);预算熔断停机;LLM 失败不写库。
另做了 CLI 冒烟(一次性 sqlite):dry-run → apply(补 13 维,type=MUM 不变)→
重跑全 skip_complete → --slug 过滤,全部 EXIT=0。

## B · heat_cron tz 混比修复

**问题**(既有,8a0449c 即有):`heat_service.recalculate_heat` 用 **naive** 的
`seven_days_ago` 与 `resident.last_conversation_at` 比较;生产 Postgres 上 asyncpg 对
`timezone=True` 列返回 **aware** datetime → `heat_service.py:64` 抛
`TypeError: can't compare offset-naive and offset-aware datetimes`,**整轮小时级
heat 重算全灭**(热度、popular/sleeping 状态迁移全部跳过)。sqlite 开发库返回 naive,
所以本地一直测不出来。

**修法**(`backend/app/services/heat_service.py`):按仓库既有惯例
(`civic_service.close_due_polls` 的 `replace(tzinfo=UTC)` 分支)——

- SQL 侧窗口查询保留 naive-UTC 绑定(生产行为不变);
- Python 侧比较新增 aware 的 `sleep_cutoff`;
- 存量脏数据(naive 行)统一 `last.replace(tzinfo=UTC)` 归一后 aware 比较。

**回归测试**(`backend/tests/test_heat.py::test_mixed_aware_naive_last_conversation_no_crash`):
sqlite 上用 load/refresh 事件 + `set_committed_value`(不弄脏实例)模拟 asyncpg 的
aware 形状,一轮里混入 aware-陈旧 / naive-陈旧 / aware-新鲜 三居民——
修复前红(TypeError 复现于 heat_service.py:64,有红跑证据),修复后绿:
不抛、陈旧双双入睡、新鲜保持 idle。

## C · 预算熔断静默失效告警

**问题**:熔断器按设计 fail-open(计量抖动不能冻结世界),副作用是 spend 查询
**永久性坏掉**时熔断器永远静默报 NORMAL,成本失控零信号(Roadmap #6)。

**产物**:`backend/app/llm/budget_alerts.py` + 两处接线(fail-open 语义不变):

1. **计量读数失败**:`budget.background_tier` / `user_over_budget` 的 spend SUM()
   抛错 → WARN 日志 + Sentry event(懒加载 sentry_sdk,无 SENTRY_DSN 完全惰性;
   按调用点冷却限流,库挂了不会刷屏)。
2. **llm_usage 停摆看门狗**:AGENT_ENABLED=true + 计量开 + loop 被观察运行 ≥N 分钟,
   而 llm_usage 连续 N 分钟零新增 → WARN + Sentry。挂在 `AgentLoop.run` 每轮末尾
   (`maybe_check_usage_stall`:未武装时零开销、自开短会话、DB 探测 ≥60s 间隔、永不抛)。

**配置走环境变量 os.environ(不改 config.py,任务红线)**:

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `BUDGET_ALERTS_ENABLED` | `true` | 一键总开关(`false` 全关) |
| `BUDGET_ALERT_COOLDOWN_MIN` | `30` | 同类告警最小间隔(分钟,日志+Sentry 同门) |
| `BUDGET_USAGE_STALL_MIN` | `1440` | 停摆窗口 N 分钟;`0` 单独关看门狗 |

停摆默认 24h 是刻意保守:日行动 cap 触顶休眠会让世界合法静默 ~21h
(burn-in 记录),更短窗口会在健康的夜里误报。

**测试**(`backend/tests/test_budget_alerts.py`,14 个):broken-session 计量失败
(fail-open 不变 + WARN + sentry 桩)、一键关、冷却、停摆(旧行/空表/新鲜行)、
观察窗宽限、零阈值关、agent/metering 门、24h 默认保守性、maybe_check 武装判定 +
最小间隔 + 注入 session factory、无 DSN 空转。

**收口注意(留给主会话)**:三个键登记进 `.env.example` 时,
`tests/test_env_example_consistency.py` 不变量 1 要求 example 键都是 Settings 字段——
需要同时把键补进 `config.py` Settings 或调整该测试的白名单机制
(该测试在本基线本就有一个预存失败)。

## 全量 pytest:基线 vs 收尾

同机同命令 `uv run pytest -q`(backend/,uv 环境;本机无 redis/testcontainers):

| 运行 | commit | 结果 |
|---|---|---|
| 基线 | `d27fce7`(master base) | **51 failed, 1642 passed**, 25 skipped, 11 deselected, 17 errors(4:31) |
| 收尾 | `a3a32ec`(A+B+C 后) | **51 failed, 1667 passed**, 25 skipped, 11 deselected, 17 errors(4:22) |

- **新增失败:0**。failed/error 集合逐条 diff 完全一致(68 条预存:全部 lab-v2
  相关需真 redis/testcontainers,外加 1 条预存 `test_env_example_consistency`)。
  唯一文本差异是 `test_lab_runtime_v2_store_auth` 一条参数化用例的 ID 内嵌了
  每次运行新生成的 JWT(nbf/exp 时间戳),是**同一条预存失败**,非新增。
- passed 1642 → 1667:+25 恰为本批新增测试数(A 10 + B 1 + C 14)。
- 硬门语义 = 相对 base 零新增失败(非 literal 0 failed),**通过**。

失败清单存档:`/tmp/hygiene_baseline_failures.txt` / `/tmp/hygiene_final_failures.txt`。

## 红线遵守

- 未触碰 `config.py` / `nightly_cron.py` / `civic_service` / `election_service` /
  `duty_service`(git diff 全程只含:scripts/sbti_backfill.py、tests/test_sbti_backfill.py、
  services/heat_service.py、tests/test_heat.py、llm/budget_alerts.py、llm/budget.py、
  agent/loop.py、tests/test_budget_alerts.py、本报告);
- 未碰 docs/PROGRESS.md;未连远程库(冒烟用一次性 /tmp sqlite);未合并/push/部署。

---

## 服务器恢复后的生产执行步骤

> 前提:vm212 恢复可达;本分支经主会话评审合并后随下一次后端部署上线。
> B(heat tz)与 C(预算告警)是纯代码变更,**随部署自动生效**,无需单独操作;
> C 的三个环境变量不设即用保守默认,想调再在 compose env / .env 里加。
> A 需要按下面四步人工推进。

### 0. 部署(B/C 随此上线)

```bash
# 本地(合并后的部署分支)
./deploy/backend/deploy.sh <user@vm212>
# 服务器上确认容器起来、alembic 无新迁移(本批零迁移,应保持 045)
docker compose ps && docker compose exec api alembic current
```

### 1. backfill dry-run(只读,出差异报告)

```bash
# vm212,api 容器内(DATABASE_URL 已由 compose 注入)
docker compose exec api python scripts/sbti_backfill.py > /tmp/sbti_dryrun.txt
cat /tmp/sbti_dryrun.txt
```

### 2. 人工审阅(Jimmy)

- 预期形状:26 位左右居民,大多 `would_fill`(source=rule,type 不变只补洞);
  `needs_llm` 的应只有完全缺 sbti 或特殊 type 的;`skip_complete` 是已齐全的。
- 核对几个熟悉居民(isabella/klaus/mei…)的 `补 N 维` 列表是否合理,
  特别确认 **A2 都被补上**、已有维值没被改写、type 没翻转。
- 若 `needs_llm` 数量可观且想一并修:确认当日预算余量
  (`docker compose exec api python scripts/burnin_report.py --days 1`),
  再决定第 3 步是否带 `--llm`。

### 3. 真跑(写库)

```bash
# 纯规则(推荐先跑这个,零成本零风险)
docker compose exec api python scripts/sbti_backfill.py --apply | tee /tmp/sbti_apply.txt
# 若审阅后决定连缺 sbti 的一起修(走 LLM,计入 llm_usage,预算耗尽自动停):
docker compose exec api python scripts/sbti_backfill.py --apply --llm | tee -a /tmp/sbti_apply.txt
# 复跑一次应全 skip_complete(幂等自证):
docker compose exec api python scripts/sbti_backfill.py
```

### 4. 复验

**4a. 数据画像复验**(A2 覆盖率应到位):

```sql
-- psql 到生产库
SELECT count(*)                                                    AS total,
       count(*) FILTER (WHERE (meta_json::jsonb)->'sbti' IS NULL)  AS missing_sbti,
       count(*) FILTER (WHERE (meta_json::jsonb)->'sbti'->'dimensions' ? 'A2') AS has_a2
FROM residents;
-- 期望:has_a2 = total - missing_sbti(--llm 全修则 missing_sbti=0)
```

**4b. 投票分布复验**(问题的最终验收):等下一个/几个 civic poll 周期结束后——

```sql
SELECT id, question, status,
       opt->>'text'      AS option_text,
       opt->>'npc_votes' AS npc_votes
FROM polls, jsonb_array_elements(options_json::jsonb) AS opt
WHERE status = 'closed'
ORDER BY closes_at DESC
LIMIT 20;
```

期望:NPC 票不再系统性 100% 堆 option 0——守序居民(A2=H)聚向维持现状项、
叛逆(A2=L)偏变更项,分布随议题有分化。若仍全堆 0,再查 `_proposer_slug`
亲和加成是否主导(那是另一个机制,不在本批 scope)。

**4c. B 的生产验证**:部署后看一小时,日志里不再出现
`Heat cron error: can't compare offset-naive and offset-aware datetimes`,
且 `Heat cron: N status changes` 正常出现(有状态变化时)。

**4d. C 的生产验证**(可选主动演练):临时设 `BUDGET_USAGE_STALL_MIN=5` 并停掉
LLM 出口几分钟,确认 WARN 日志 + Sentry event 出现;演练完删掉该变量回到 24h 默认。
