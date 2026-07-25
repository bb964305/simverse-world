# 只读运维审计报告 · 2026-07-25B

- 环境:vm212,`/opt/skills-world`,compose 项目 `/opt/skills-world/deploy`,alembic=047。
- 审计时刻:`2026-07-25 11:54:21 UTC`(`ssh vm212 date -u`)。
- 服务状态:`db` Up 10h (healthy) / `redis` Up 10h (healthy) / `api` Up 8h / `agent-worker` Up 8h。
- 性质:**纯只读**。全部 psql 走 `BEGIN; SET TRANSACTION READ ONLY; ... COMMIT;`;
  未执行任何 UPDATE/DELETE/INSERT/DDL、未跑 alembic、未改任何远端文件、未 up/down/restart/build 任何容器、
  未 kill 任何进程。唯一在容器内执行的脚本是任务书点名的只读脚本 `scripts/burnin_report.py`。

---

## 0. 判据(取数前写死,禁事后挪门)

### A 项(R5 · SBTI 回填后 NPC 投票分布复验)

样本门槛(先于任何判绿/判红):

- **S1**:回填时点之后 closed 且含 NPC 票的 poll 数 `N_post >= 8`,且回填时点之前的对照 poll 数 `N_pre >= 5`。
  任一不满足 → 结论一律写「样本不足,需再等 N 轮 poll」,不得判绿也不得判红。

指标(NPC 票与玩家票分开统计,不混算):

- **C1 option-0 占比**:全部 NPC 票中投给 `option_idx = 0` 的比例 `P0`。
  判绿要求:`P0_post <= 0.45` **且** `P0_pre - P0_post >= 0.15`(下降 ≥ 15 个百分点)。
- **C2 单选项垄断 poll 占比**:某张 poll 的 NPC 票 100% 落在同一个选项 → 记为「垄断 poll」,占比 `M`。
  判绿要求:`M_post <= 0.20` **且** `M_pre - M_post >= 0.20`。
- **C3 归一化熵**:每张 poll 的 NPC 票分布归一化熵 `H / ln(K)`(K=选项数)。
  判绿要求:`median(H/lnK)_post >= 0.60`。

判绿总条件:S1 满足 **且** C1、C2、C3 全部满足。任一不满足 → 判红或「样本不足」。

### B 项(P3 · llm_usage 与真实账单对账)

- **C4 偏差率**:`|sum(llm_usage.cost_usd) - 真实账单金额| / 真实账单金额 <= 5%` 判为对得上。
  若拿不到供应商侧真实账单(本次为只读审计、无账单系统访问权限)→ 如实写「无法对账,缺账单侧数据」,
  只给 llm_usage 单边口径数字,禁用估算冒充实测。
- **C5 预算占用**:按天 `sum(cost_usd)` 对全局日预算 $10 的占用率;标出占用率 > 80% 的异常日。
- **C6 单位成本**:`$/居民·天 = 当日 cost_usd / 26`。

---

## A. R5 — SBTI 回填后的 NPC 投票分布复验

### A.1 结论(先行)

> **样本不足,无法判定。回填之后产生的 NPC 票 = 0 张,`N_post = 0`,远低于门槛 8。
> 按 §0 的 S1 规则,本项既不判绿也不判红。**
>
> 并且:现网 3 张 poll 的 NPC 票已被 `_npc_voters` 幂等集合锁定,**永远不会重投**;
> 下一张自动产生的新 poll 最早在 **2026-08-21** 左右(选举周期 28 天)。
> 也就是说,这道门在**不做任何干预的前提下还要等约 27 天**才有第一批可判样本。

附加的静态推演(见 A.5)显示:即使等到新 poll,**用今天的 A2 数据重跑现网评分函数,option-0 占比仍是
13/14 = 92.9%(建设类 poll)和 14/14 = 100%(镇长选举)**,几乎必然继续踩红 C1/C2/C3。
回填修好了「数据缺失」,但没有修掉「option-0 垄断」这个行为。

### A.2 原始命令与输出:poll / 票据盘点

```
ssh vm212 "cd /opt/skills-world/deploy && docker compose exec -T db psql -U postgres -d skills_world" <<'SQL'
BEGIN; SET TRANSACTION READ ONLY;
SELECT count(*) AS polls_total,
       count(*) FILTER (WHERE status='closed') AS closed,
       min(closes_at) AS first_close, max(closes_at) AS last_close
FROM polls;
SELECT status, count(*) FROM polls GROUP BY status ORDER BY 2 DESC;
COMMIT;
SQL
```

```
 polls_total | closed |          first_close          |          last_close
-------------+--------+-------------------------------+-------------------------------
           3 |      0 | 2026-07-27 23:29:43.791651+00 | 2026-07-27 23:29:43.884661+00
(1 row)

 status | count
--------+-------
 open   |     3
(1 row)
```

**全库只有 3 张 poll,`closed` = 0。** 判据里「回填后 closed 的 poll ≥ 8 / 回填前 ≥ 5」两侧同时为 0。

poll 明细与票据:

```
BEGIN; SET TRANSACTION READ ONLY;
SELECT id, status, closes_at, left(question,40) AS q, json_array_length(options_json) AS k
FROM polls ORDER BY closes_at;
SELECT p.id AS poll_id, o.ord-1 AS option_idx, (o.val->>'npc_votes')::int AS npc_votes
FROM polls p, LATERAL json_array_elements(p.options_json) WITH ORDINALITY AS o(val, ord)
ORDER BY p.id, o.ord;
SELECT count(*) AS votes_rows FROM votes;
SELECT poll_id, option_idx, count(*) FROM votes GROUP BY 1,2 ORDER BY 1,2;
COMMIT;
```

```
                  id                  | status |           closes_at           |             q              | k
--------------------------------------+--------+-------------------------------+----------------------------+---
 0f01163a-3a2c-4851-a00a-da501260e06a | open   | 2026-07-27 23:29:43.791651+00 | 在南苑空地兴建一座邮局     | 2
 1dd6aa2e-935f-4f15-96c5-6cc54c569b97 | open   | 2026-07-27 23:29:43.823983+00 | 在东岸花园兴建一座剧院     | 2
 8e96c1dd-c087-4b86-b965-615de396b8f1 | open   | 2026-07-27 23:29:43.884661+00 | 镇长选举:谁来当下一任镇长? | 4
(3 rows)

               poll_id                | option_idx | npc_votes
--------------------------------------+------------+-----------
 0f01163a-3a2c-4851-a00a-da501260e06a |          0 |        14
 0f01163a-3a2c-4851-a00a-da501260e06a |          1 |         0
 1dd6aa2e-935f-4f15-96c5-6cc54c569b97 |          0 |        14
 1dd6aa2e-935f-4f15-96c5-6cc54c569b97 |          1 |         0
 8e96c1dd-c087-4b86-b965-615de396b8f1 |          0 |        14
 8e96c1dd-c087-4b86-b965-615de396b8f1 |          1 |         0
 8e96c1dd-c087-4b86-b965-615de396b8f1 |          2 |         0
 8e96c1dd-c087-4b86-b965-615de396b8f1 |          3 |         0
(8 rows)

 votes_rows
------------
          0
(1 row)

 poll_id | option_idx | count
---------+------------+-------
(0 rows)
```

- NPC 票:3 张 poll 全部 **14/14 压在 option 0**,`P0 = 1.000`,垄断 poll 占比 `M = 3/3 = 1.000`,
  归一化熵 `H/lnK = 0`(3 张全部)。这正是回填前描述的症状。
- 玩家票:`votes` 表 **0 行**。玩家侧完全没有参与,所以「NPC 票 vs 玩家票」的分离统计里玩家侧无数据可分析。

### A.3 这 14×3 张票是回填**之前**投的 —— 时间线证据

**(1) 投票时点 = 2026-07-24 23:29:43 UTC。**
`civic_service.propose()` 设 `closes_at = now + civic_poll_days`,`config.py:511 civic_poll_days = 3`。
三张 poll 的 `closes_at` 为 `2026-07-27 23:29:43.79/.82/.88`,倒推创建时刻 `2026-07-24 23:29:43 UTC`;
`system_config.election_last_opened` 的 `updated_at = 2026-07-24 23:29:43.909591+00` 与之完全吻合。
`nightly_cron.py` 里 `seed_civic_agenda`(:149)→ `maybe_open_seasonal_election`(:163)→ `run_npc_voting`(:170)
在**同一次 cron 里前后脚执行**,锚点 `RUN_HOUR = 7` 按 `now_real()`(Asia/Shanghai)即 UTC 前一日 23:00。
因此这 3 张 poll 的 NPC 票就是那一次 cron 在 `2026-07-24 23:29~23:30 UTC` 一次性投完的。

**(2) 回填最早只可能在 2026-07-25 03:41 UTC。**

```
ssh vm212 "date -u; ls -l --time-style=full-iso /opt/skills-world/backend/scripts/sbti_backfill.py;
           for d in /opt/skills-world/backend.bak*; do printf '%s: ' $d; ls $d/scripts/sbti_backfill.py 2>/dev/null || echo MISSING; done"
```

```
Sat Jul 25 11:54:21 UTC 2026
-rw-rw-r-- 1 root root 10614 2026-07-25 03:41:00.000000000 +0000 /opt/skills-world/backend/scripts/sbti_backfill.py
/opt/skills-world/backend.bak-closeout-20260725-034254: MISSING
/opt/skills-world/backend.bak.1783484063: MISSING
/opt/skills-world/backend.bak.1783907900: MISSING
/opt/skills-world/backend.bak.1783929342: MISSING
/opt/skills-world/backend.bak.1783956749: MISSING
/opt/skills-world/backend.bak.20260717-120046: MISSING
/opt/skills-world/backend.bak.deploy-1784102102: MISSING
/opt/skills-world/backend.bak.deploy-1784395910: MISSING
/opt/skills-world/backend.bak.m1m6-20260725-000220: MISSING
/opt/skills-world/backend.bak.realism-20260723-045223: MISSING
```

回填脚本在**全部 10 个历史 backend 快照里都不存在**(含本次部署前的快照
`backend.bak-closeout-20260725-034254`),在现网 backend 上的落盘时间是 `2026-07-25 03:41:00 UTC`;
容器随后在 `2026-07-25 03:48` 重启(api 日志首行 `2026-07-25T03:48:29Z`)。

**(3) 结论:投票早于回填 4 小时 11 分。** 且 `run_npc_voting` 用
`options_json[0]['_npc_voters']` 做幂等(`civic_service.py:145-172`,已投过的 slug 直接 `continue`),
这 3 张 poll 的 `_npc_voters` 已含全部 14 个 NPC slug,**永远不会再产生新的 NPC 票**。

⇒ `N_post = 0`,`N_pre = 3`(且 3 张都还没 closed)。S1 双向不满足 → **样本不足,不判绿也不判红。**

### A.4 回填本身(数据层面)确实生效了

```
BEGIN; SET TRANSACTION READ ONLY;
SELECT count(*) AS residents_total,
       count(*) FILTER (WHERE meta_json->'sbti' IS NOT NULL) AS has_sbti,
       count(*) FILTER (WHERE meta_json->'sbti'->'dimensions'->>'A2' IS NOT NULL) AS has_A2
FROM residents;
SELECT resident_type, count(*) FROM residents GROUP BY 1;
SELECT meta_json->'sbti'->'dimensions'->>'A2' AS a2, count(*)
FROM residents WHERE resident_type='npc' GROUP BY 1 ORDER BY 2 DESC;
COMMIT;
```

```
 residents_total | has_sbti | has_a2
-----------------+----------+--------
              26 |       26 |     26

 resident_type | count
---------------+-------
 npc           |    14
 player        |    12

 a2 | count
----+-------
 M  |    10
 L  |     3
 H  |     1
```

- 26/26 居民有完整 `sbti.dimensions.A2` —— 与 ROADMAP 说法一致,**数据缺口确已补上**。
- 但会投票的只有 `resident_type='npc'` 的 **14 人**(与每张 poll 的 14 票吻合;12 个 player 型居民不投)。
- 这 14 个 NPC 的 A2 分布是 **M=10 / L=3 / H=1**。

NPC slug 级明细(不含任何隐私字段):

```
       slug       | a2 |   t    | duty
------------------+----+--------+------
 夏洛克-福尔摩斯  | H  | THAN-K | (null)
 isabella         | L  | FAKE   | (null)
 klaus            | L  | FAKE   | (null)
 阿达-洛芙莱斯    | L  | FAKE   | (null)
 adam             | M  | OJBK   | (null)
 mei              | M  | MUM    | (null)
 tamara           | M  | DEAD   | (null)
 夜风侦探         | M  | OJBK   | (null)
 夜风侦探-46ff1f  | M  | OJBK   | (null)
 夜风侦探-a23160  | M  | OJBK   | (null)
 林晚秋           | M  | OJBK   | (null)
 格蕾丝-霍珀      | M  | OJBK   | (null)
 部署回归图灵0724 | M  | SEXY   | (null)
 陈默             | M  | MUM    | (null)
```

poll 的提案人:

```
                  id                  | proposer  |             q
--------------------------------------+-----------+----------------------------
 0f01163a-3a2c-4851-a00a-da501260e06a | jiang-lin | 在南苑空地兴建一座邮局
 1dd6aa2e-935f-4f15-96c5-6cc54c569b97 | zhou-dahe | 在东岸花园兴建一座剧院
 8e96c1dd-c087-4b86-b965-615de396b8f1 | (无)      | 镇长选举:谁来当下一任镇长?
```

### A.5 静态推演:即使等到新 poll,option-0 垄断大概率**不会**消失

以下**不是实测**,是把**现网代码 + 今天的 DB 数据**代入 `civic_service._npc_choice`
(`backend/app/services/civic_service.py:180-227`,纯规则、零 LLM、零随机)得到的确定性结果。
标注为「静态推演」,不冒充实测。

评分规则要点:

- `a2 == "H" and not eff` → `+1.0`;`a2 == "L" and eff` → `+0.5`;**A2 = "M" → 对所有选项都 +0**;
- duty ∈ (shop_keeper/tavern_hub/cafe_host) 且 effect 文本含 shop/market/经济/price → `+0.8`;
- 提案人关系加成只在 `by_slug`(= NPC 字典)里找得到 proposer 时才生效;
- 收尾 `best = max(range(len(opts)), key=lambda i: (scores[i], -i))` —— **平局时确定性取最小 index,即 option 0**。

代入现状:

| 输入事实 | 来源 | 对结果的影响 |
|---|---|---|
| 14 个 NPC 的 `duty.key` 全为 NULL | A.4 表 | duty 加成恒不触发 |
| proposer `jiang-lin` / `zhou-dahe` 不在 NPC 名单里 | A.4 两表 | `by_slug.get()` 返回 None,关系加成恒不触发 |
| 建设类 poll:opt0「赞成兴建」有 effect,opt1「暂缓,维持现状」effect 为 null | options_json | H 走 opt1,L 走 opt0 |
| 镇长选举 poll:4 个选项**全部**带 effect(`{"type":"mayor",...}`) | options_json | 无「无 effect 选项」,H 分支失效;L 对 4 项同时 +0.5 → 平局 |

推演结果:

- **建设类 poll(2 张)**:M(10)平局 → opt0;L(3)+0.5 → opt0;H(1)+1.0 → opt1。
  ⇒ **13/14 = 92.9% 在 option 0**,`H/lnK ≈ 0.371`;非垄断,但仍远超 C1 的 0.45 门。
- **镇长选举 poll**:M / L / H 三档全部落到 option 0。⇒ **14/14 = 100%**,`H/lnK = 0`,仍是垄断 poll。

即回填把 option-0 占比从 100% 降到最好情况 92.9%,**离 C1 要求的 ≤45% 差得很远**。

根因不在数据而在评分函数:

1. `dims.get("A2","M")` 的**默认值问题被回填修好了,但 A2=M 本身在评分里就是零信号**,而 10/14 的 NPC 恰好是 M;
2. 平局兜底 `-i` 是**确定性偏向 index 0**,没有任何抖动 / 随机 / 个体偏好参与;
3. 镇长选举这类「每个选项都有 effect」的 poll,结构上让 A2 的 H / L 分支双双失效,SBTI 再准也无法产生分化。

### A.6 下一批可判样本什么时候来

```
BEGIN; SET TRANSACTION READ ONLY;
SELECT key, value, updated_at FROM system_config WHERE key LIKE 'election%';
SELECT id, status FROM seasons ORDER BY 1 LIMIT 5;
COMMIT;
```

```
         key          |    value     |          updated_at
----------------------+--------------+-------------------------------
 election_last_opened | "2026-07-24" | 2026-07-24 23:29:43.909591+00
(1 row)

 id | status
----+--------
(0 rows)
```

- `civic_service.CIVIC_AGENDA` 只有 **2 个 topic**(邮局、剧院),`seed_civic_agenda` 按 question 去重,
  两个都已开过 ⇒ **建设类 poll 不会再自动产生**。
- 无 active season ⇒ 选举走 off-season 节奏 `election_interval_days = 28`(`config.py:515`),
  `election_last_opened = 2026-07-24` ⇒ **下一次自动选举 ≈ 2026-08-21**。
- 剩下唯一的新 poll 来源是玩家 / 运维通过 `backend/app/routers/polls.py:60` 的提案 API 手动开一张
  —— 那是写操作,**本次审计不做**,列入 §C 待办。

诚实结论:**样本不足,需再等 ≥1 轮「回填之后新开的」poll;按现有自动节奏是 ~27 天后(2026-08-21);
若人工开 1 张新 poll,则下一次 nightly(每天 07:00 Asia/Shanghai = 23:00 UTC)即可产生 14 张 post 票。**

补充说明(不改本次判定):要达到我在 §0 写死的 `N_post >= 8` 张 poll,现有自动节奏下需要 ~7 个月,
该门槛在当前世界节奏下事实上不可达。**本次判定仍严格按 §0 原口径执行(结论 = 样本不足)**;
建议后续把口径调整为「≥3 张回填后新开的 poll 且 ≥42 张 NPC 票」这类可达形态,由主会话决定是否采纳。

---

## B. P3 — llm_usage 与真实账单对账

### B.1 结论(先行)

- **C4 无法判定:拿不到供应商(DashScope/百炼)侧的真实账单数据。** 本次为只读审计,
  无账单控制台凭据,服务器与仓库内也没有任何已抄录的真实账单数字。按 §0 的 C4 规则,
  **如实写「无法对账,缺账单侧数据」,不做任何估算冒充实测。**
- **可以给出的是 llm_usage 单边口径 + 一次内部一致性核对**:
  **2026-07-15 之后(deepseek-v4-flash 按量端点)的每一天,`sum(cost_usd)` 与按 `pricing.py` 官方 tariff
  重算的结果逐日 100% 相等(ratio 恒为 1.00)**,说明计量管线与价目表一致,没有漂移。
- **C5 通过**:15 天内日预算占用率最高 **8.8%**(2026-07-24,$0.8785 / $10),**无任何一天 > 80% 熔断线**。
- **C6**:`$/居民·天` 最高 **$0.0338**(07-24),多数日在 $0.005–0.018。
- **发现一处历史口径污染**:07-15 之前的 250 行 `qwen3.7-plus` 记录是按 **claude-haiku 兜底价**
  ($1/$5 per Mtok)写入的,比 qwen 按量 tariff **虚高 13.3–17.5 倍**;这批行也永远无法与账单对上
  (那段时间走的是百炼 Coding Plan 订阅制,根本不按 token 计费)。

### B.2 原始命令与输出:按天聚合

```
ssh vm212 "cd /opt/skills-world/deploy && docker compose exec -T db psql -U postgres -d skills_world" <<'SQL'
BEGIN; SET TRANSACTION READ ONLY;
SELECT count(*) AS rows, min(ts) AS first_ts, max(ts) AS last_ts, sum(cost_usd) AS total_usd FROM llm_usage;
SELECT date_trunc('day', ts)::date AS day, count(*) AS calls, round(sum(cost_usd)::numeric,4) AS cost_usd,
       round((sum(cost_usd)/10.0*100)::numeric,2) AS pct_of_10usd_budget,
       round((sum(cost_usd)/26.0)::numeric,5) AS usd_per_resident_day,
       sum(input_tokens) AS in_tok, sum(output_tokens) AS out_tok
FROM llm_usage GROUP BY 1 ORDER BY 1;
COMMIT;
SQL
```

```
 rows  |           first_ts            |            last_ts            |     total_usd
-------+-------------------------------+-------------------------------+-------------------
 22393 | 2026-07-10 15:46:32.452143+00 | 2026-07-25 11:56:30.345346+00 | 3.547496630000006

    day     | calls | cost_usd | pct_of_10usd_budget | usd_per_resident_day | in_tok  | out_tok
------------+-------+----------+---------------------+----------------------+---------+---------
 2026-07-10 |     4 |   0.0038 |                0.04 |              0.00015 |    1380 |     493
 2026-07-13 |   140 |   0.4738 |                4.74 |              0.01822 |   92138 |   76340
 2026-07-14 |   105 |   0.1901 |                1.90 |              0.00731 |   59695 |   26076
 2026-07-15 |   127 |   0.0200 |                0.20 |              0.00077 |   77028 |   12825
 2026-07-16 |   226 |   0.0256 |                0.26 |              0.00099 |  143487 |   19679
 2026-07-17 |  1100 |   0.1312 |                1.31 |              0.00505 |  784263 |   76098
 2026-07-18 |  1164 |   0.1366 |                1.37 |              0.00525 |  819443 |   77951
 2026-07-19 |  1207 |   0.1425 |                1.43 |              0.00548 |  861785 |   77885
 2026-07-20 |  2324 |   0.2742 |                2.74 |              0.01054 | 1662754 |  147223
 2026-07-21 |  1460 |   0.1746 |                1.75 |              0.00672 | 1059554 |   93386
 2026-07-22 |  1389 |   0.1664 |                1.66 |              0.00640 | 1006018 |   90863
 2026-07-23 |  3406 |   0.4519 |                4.52 |              0.01738 | 2615761 |  306026
 2026-07-24 |  6298 |   0.8785 |                8.79 |              0.03379 | 5366259 |  453855
 2026-07-25 |  3443 |   0.4781 |                4.78 |              0.01839 | 2838502 |  287531
(14 rows)
```

(2026-07-25 为进行中的一天,截至 11:56 UTC。)

**C5 判定:通过。** 最高占用 8.79%(07-24),距 80% throttle 线还有 9 倍余量,15 天内无异常日超线。
**C6:** 最高 $0.0338 / 居民·天。

### B.3 原始命令与输出:burnin_report.py(任务书指定口径)

```
ssh vm212 "cd /opt/skills-world/deploy && docker compose exec -T api python scripts/burnin_report.py --days 15 --residents 26"
```

尾部汇总(节选,原文粘贴,未美化):

```
—— 2026-07-23 汇总 ——
  调用 3406 | in 2,615,761 tok | out 306,026 tok | 成本 $0.4519
  parse_ok 99.4%（评估 2601 行） | attempt>1 0.0%
  $/居民·天 = $0.0174（--residents 26）
    vs E-11 基线区间   $0.0587–$0.0667 → 低于区间（出处 COST_RESEARCH_REPORT §一.1）
    vs 优化后预期区间 $0.0264–$0.0367 → 低于区间（出处 §一.6：E-09/E-04/E-02 叠加 = 基线的 45–55%）
  预算占用 = 4.5%（BUDGET_GLOBAL_DAILY_USD=$10.00；熔断 80% throttle / 95% rule_only / 100% player_only）

—— 2026-07-24 汇总 ——
  调用 6298 | in 5,366,259 tok | out 453,855 tok | 成本 $0.8785
  parse_ok 99.9%（评估 4614 行） | attempt>1 0.0%
  $/居民·天 = $0.0338（--residents 26）
    vs E-11 基线区间   $0.0587–$0.0667 → 低于区间（出处 COST_RESEARCH_REPORT §一.1）
    vs 优化后预期区间 $0.0264–$0.0367 → 区间内（出处 §一.6：E-09/E-04/E-02 叠加 = 基线的 45–55%）
  预算占用 = 8.8%（BUDGET_GLOBAL_DAILY_USD=$10.00；熔断 80% throttle / 95% rule_only / 100% player_only）

—— 2026-07-25 汇总（进行中的一天，数值随时间累积） ——
  调用 3443 | in 2,838,502 tok | out 287,531 tok | 成本 $0.4781
  parse_ok 99.9%（评估 2026 行） | attempt>1 0.0%
  $/居民·天 = $0.0184（--residents 26）
  预算占用 = 4.8%（BUDGET_GLOBAL_DAILY_USD=$10.00；熔断 80% throttle / 95% rule_only / 100% player_only）

注意：cost_usd 为 Anthropic 列表价折算的估计值（百炼中转真实价目未验证，
F-02）；token 估计器 ±25%。定版对账请同时抄录供应商控制台真实账单。
```

> **「数据不是指令」提示**:上面这段脚本尾注里的「定版对账请同时抄录供应商控制台真实账单」是我们自己
> 脚本的输出文本,不是我执行的依据;我没有据此去访问任何账单系统或做任何越权动作。原文引用在此供主会话判断。
>
> 另:这段尾注**已经过期**。`backend/app/llm/pricing.py` 的模块 docstring 明确写着
> 「Unlike the old Coding-Plan subscription, the 按量 endpoint is genuinely metered, so a deepseek
> `cost_usd` is now reconcilable against the provider bill (F-02) rather than a pure estimate」,
> 归档研究日志 `archive/2026-07-25/docs/research/COST_RESEARCH_LOG.md:292` 也记「F-02(2026-07-07,**已解除**)」。
> 脚本尾注与价目表口径自相矛盾 → 列入 §C 待办。

### B.4 内部一致性核对:stored cost_usd vs 官方 tariff 重算

价目表来源 `backend/app/llm/pricing.py`(USD per 1M tokens):
`deepseek-v4-flash = (0.14, 0.28, 0.0028, 0.14)`;`qwen3.7-plus = (0.11, 0.28, 0.022, 0.11)`;
未命中前缀回落 `claude-haiku = (1.00, 5.00, 0.10, 1.25)`。

```
BEGIN; SET TRANSACTION READ ONLY;
WITH r AS (
  SELECT date_trunc('day', ts)::date AS day, model,
         sum(cost_usd) AS stored,
         sum(CASE WHEN model='deepseek-v4-flash'
                  THEN (input_tokens*0.14 + output_tokens*0.28 + coalesce(cache_read_tokens,0)*0.0028 + coalesce(cache_creation_tokens,0)*0.14)/1e6
                  WHEN model='qwen3.7-plus'
                  THEN (input_tokens*0.11 + output_tokens*0.28 + coalesce(cache_read_tokens,0)*0.022 + coalesce(cache_creation_tokens,0)*0.11)/1e6
                  ELSE NULL END) AS recomputed,
         sum(CASE WHEN model='qwen3.7-plus' THEN (input_tokens*1.00 + output_tokens*5.00)/1e6 ELSE NULL END) AS haiku_rate_check,
         count(*) AS n
  FROM llm_usage GROUP BY 1,2)
SELECT day, model, n, round(stored::numeric,5) AS stored_usd, round(recomputed::numeric,5) AS tariff_usd,
       round(haiku_rate_check::numeric,5) AS haiku_fallback_usd, round((stored/NULLIF(recomputed,0))::numeric,2) AS ratio
FROM r ORDER BY day, model;
SELECT model, count(*), round(sum(cost_usd)::numeric,5) FROM llm_usage GROUP BY 1 ORDER BY 3 DESC;
COMMIT;
```

```
    day     |       model       |  n   | stored_usd | tariff_usd | haiku_fallback_usd | ratio
------------+-------------------+------+------------+------------+--------------------+-------
 2026-07-10 | qwen3.7-plus      |    4 |    0.00385 |    0.00029 |            0.00385 | 13.27
 2026-07-13 | qwen3.7-plus      |  140 |    0.47384 |    0.03151 |            0.47384 | 15.04
 2026-07-14 | qwen3.7-plus      |  105 |    0.19008 |    0.01387 |            0.19008 | 13.71
 2026-07-15 | deepseek-v4-flash |  126 |    0.01405 |    0.01405 |                    |  1.00
 2026-07-15 | qwen3.7-plus      |    1 |    0.00596 |    0.00034 |            0.00596 | 17.47
 2026-07-16 | deepseek-v4-flash |  226 |    0.02565 |    0.02565 |                    |  1.00
 2026-07-17 | deepseek-v4-flash | 1100 |    0.13121 |    0.13121 |                    |  1.00
 2026-07-18 | deepseek-v4-flash | 1164 |    0.13661 |    0.13661 |                    |  1.00
 2026-07-19 | deepseek-v4-flash | 1207 |    0.14252 |    0.14252 |                    |  1.00
 2026-07-20 | deepseek-v4-flash | 2324 |    0.27416 |    0.27416 |                    |  1.00
 2026-07-21 | deepseek-v4-flash | 1460 |    0.17459 |    0.17459 |                    |  1.00
 2026-07-22 | deepseek-v4-flash | 1389 |    0.16640 |    0.16640 |                    |  1.00
 2026-07-23 | deepseek-v4-flash | 3406 |    0.45193 |    0.45193 |                    |  1.00
 2026-07-24 | deepseek-v4-flash | 6298 |    0.87851 |    0.87851 |                    |  1.00
 2026-07-25 | deepseek-v4-flash | 3445 |    0.47849 |    0.47849 |                    |  1.00
(15 rows)

       model       | count |  round
-------------------+-------+---------
 deepseek-v4-flash | 22145 | 2.87412
 qwen3.7-plus      |   250 | 0.67372
(2 rows)
```

读法:

- **deepseek-v4-flash 全部 11 天 ratio = 1.00**(22,145 行,累计 **$2.87412**)。存库值与按官方按量 tariff
  重算的值完全相等 ⇒ 计量管线与 `pricing.py` 一致,**这部分金额就是可以直接拿去和 DashScope 账单逐日比对的口径**。
- **qwen3.7-plus 全部 4 天 ratio = 13.27–17.47**,且 `stored_usd` 与 `haiku_fallback_usd` 逐日**完全相等**
  ⇒ 这 250 行当时是按 claude-haiku 兜底价写进去的,没有按 qwen 价目重算过。
  按 qwen 按量 tariff 应为合计 **$0.04601**,实际存 **$0.67372**,**虚高 14.6 倍**。
  但那段时间走的是百炼 Coding Plan **订阅制**(固定摊销),按 token 的两个数字**都不代表真实支出**。
- ⇒ 未来真正对账时应当**只对 2026-07-15 起、model = deepseek-v4-flash 的 $2.87412**;
  07-15 之前的 $0.67372 需单独按订阅费口径处理,不能混进偏差率计算。
- 汇率假设:`pricing.py` 用 7.2 CNY/USD(2026-07-15)。若结算汇率不同,偏差按比例平移,对账时需一并核对。

### B.5 端点 / 预算配置(只读,密钥已打码)

```
ssh vm212 "cd /opt/skills-world/deploy && grep -E '^(LLM_|BUDGET_|AGENT_ENABLED|WORLD_)' .env | sed -E 's/(KEY|TOKEN|SECRET)=.*/\1=<redacted>/'"
```

```
LLM_BASE_URL=https://dashscope.aliyuncs.com/apps/anthropic
LLM_API_KEY=<redacted>
LLM_MODEL=deepseek-v4-flash
AGENT_ENABLED=true
BUDGET_GLOBAL_DAILY_USD=10.0
BUDGET_USER_DAILY_USD=0.5
BUDGET_FORGE_REQUEST_USD=0.15
LLM_METERING_ENABLED=true
WORLD_CLOCK_K=4
WORLD_EPOCH=2026-01-01T00:00:00+08:00
```

确认:现网确实跑在 DashScope **按量** Anthropic 兼容端点上,模型 deepseek-v4-flash,全局日预算 $10,计量开启。

### B.6 07-24 成本尖峰的成因(部分归因,未完全定位)

```
BEGIN; SET TRANSACTION READ ONLY;
SELECT date_trunc('day', ts)::date AS day, scenario, count(*) AS calls, round(sum(cost_usd)::numeric,4) AS usd
FROM llm_usage WHERE ts >= '2026-07-22' AND scenario IN
  (SELECT scenario FROM llm_usage WHERE ts >= '2026-07-24' GROUP BY 1 ORDER BY sum(cost_usd) DESC LIMIT 5)
GROUP BY 1,2 ORDER BY 2,1;
COMMIT;
```

```
    day     |    scenario     | calls |  usd
------------+-----------------+-------+--------
 2026-07-22 | chat_turn       |   448 | 0.0365
 2026-07-23 | chat_turn       |   694 | 0.0482
 2026-07-24 | chat_turn       |  1572 | 0.1356
 2026-07-25 | chat_turn       |  1352 | 0.1238
 2026-07-22 | chat_wrapup     |    57 | 0.0155
 2026-07-23 | chat_wrapup     |   198 | 0.0410
 2026-07-24 | chat_wrapup     |   354 | 0.0808
 2026-07-25 | chat_wrapup     |   248 | 0.0603
 2026-07-22 | decide          |   816 | 0.1040
 2026-07-23 | decide          |  2089 | 0.2873
 2026-07-24 | decide          |  3811 | 0.5813
 2026-07-25 | decide          |  1561 | 0.2470
 2026-07-22 | evolution_drift |    44 | 0.0051
 2026-07-23 | evolution_drift |   229 | 0.0270
 2026-07-24 | evolution_drift |   374 | 0.0449
 2026-07-25 | evolution_drift |   156 | 0.0195
 2026-07-22 | plan            |    12 | 0.0043
 2026-07-23 | plan            |    27 | 0.0089
 2026-07-24 | plan            |    55 | 0.0198
 2026-07-25 | plan            |    58 | 0.0215
```

- 尖峰主因是 `decide`(07-22 → 07-24:816 → 2089 → 3811 次,$0.104 → $0.287 → $0.581,占当日约 66%),
  其次 `chat_turn`(448 → 694 → 1572)。形态是**居民 agent 活动量整体翻倍**,不是单一场景失控,
  也不是重试风暴(见下)。
- 具体触发源(部署 / 开闸 / 世界时钟 k=4 下的行动帽节奏)**未定位**,不下结论。

质量指标(解析失败与重试):

```
    day     | parse_fail | retries
------------+------------+---------
 2026-07-18 |          0 |       0
 2026-07-19 |          6 |       0
 2026-07-20 |          0 |       0
 2026-07-21 |          0 |       0
 2026-07-22 |          1 |       1
 2026-07-23 |         16 |       1
 2026-07-24 |          6 |       0
 2026-07-25 |          3 |       1
```

都在个位数量级,不构成成本因素。

---

## C. 待办(需要写操作才能推进的,本次一律没动手)

1. **人工开一张新 civic poll 以拿到第一批 post-backfill 样本**(`backend/app/routers/polls.py:60` 的提案 API)。
   这是写操作,本次审计未执行。不做的话,下一批样本要等到 ~2026-08-21。
2. **修 `civic_service._npc_choice` 的 option-0 结构性偏置**(A.5 的三条根因):
   给 A2=M 一档真实信号;给平局引入个体化的确定性打散(如按 `resident.id` hash);
   为「所有选项都有 effect」的 poll(镇长选举)补一条能产生分化的评分维度。不修的话复核门大概率还是红。
3. **修 `backend/scripts/burnin_report.py` 的尾注**:「cost_usd 为 Anthropic 列表价折算的估计值(F-02 未验证)」
   与 `backend/app/llm/pricing.py` docstring / `COST_RESEARCH_LOG.md:292`「F-02 已解除」自相矛盾,会误导后续对账。
4. **抄录 DashScope 控制台 2026-07-15 起的逐日真实账单**,与 §B.4 的 `$2.87412` / 逐日 `tariff_usd` 比对,
   才能完成 C4。需要账单凭据,不在只读审计权限内。
5. **(可选)历史 `qwen3.7-plus` 250 行的口径标注**:要么重算要么在报表里显式分段,
   避免总额被虚高的 $0.67372 污染。任何重算都是写操作,不在本次范围。

## D. 观察到但未动手的异常项(仅记录)

- `burnin_report.py` P1 探针:**饥饿(satiety < 临界)= 25/26 人**,需求健康度
  energy 均 0.038 / satiety 均 0.019 / social 均 0.0 —— 脚本自己标注「持续高企 = 需求死锁信号」。
- `burnin_report.py` P0 探针:**计划到达率 = 0.0%**(目标 >70%),同时行为-记忆一致率 100.0%(样本 947 条移动记忆)。
- S2-1 探针:**mayor / town_clerk / postman / doctor 四个职位全部空缺**(0 天),
  三存储一致性为 `office=None config=None meta=[]`。
- S1-3 探针:`issue_stances` 表为空(可能是 `POLIS_OPINION_ENABLED` 关闭的对照组形态)。
- `votes` 表 0 行 —— 玩家侧从未在任何 poll 上投过票,「玩家票 vs NPC 票」目前没有玩家侧数据可比。
- `/opt/skills-world` 下堆积了 10 个 `backend.bak*` / `backend.partial*` 目录和 4 个 db-backup 压缩包
  (最大 61 MB)。**未做任何清理**(红线禁止)。

---

## E. 给 `docs/ROADMAP.md` 的状态更新建议(一句话)

> 阶段 1「下一轮投票分布复核」**不能判绿**:SBTI 回填脚本 2026-07-25 03:41 UTC 才落到现网,
> 晚于现有 3 张 poll 的 NPC 投票(2026-07-24 23:29 UTC)约 4 小时,且 `run_npc_voting` 幂等锁死不会重投,
> **回填后样本数为 0**(现有 3 张仍是 14/14 全票 option 0);静态推演显示即使等到新 poll 也只会降到
> 13/14(建设类)/ 14/14(选举),因此该门应改为「先修 `_npc_choice` 的 A2=M 零信号 + index-0 平局兜底,
> 再人工开一张新 poll 复核」,预期时间从「等下一轮」改为「修复后的下一次 nightly」;
> `llm_usage` 侧 07-15 起 deepseek 按量口径逐日与价目表 100% 自洽(累计 $2.87412,日预算占用峰值 8.8%),
> **但与供应商真实账单的对账仍未完成(缺账单侧数据),该子项维持「未完成」**。
