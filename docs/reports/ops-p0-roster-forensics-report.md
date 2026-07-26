# P0 取证与恢复方案 — 2026-07-25 阵容迁移误删事故

- 分支：`ops/p0-roster-forensics`（base `999e098`）
- 取证时间：2026-07-26 03:26 UTC（本机 11:26 +08）
- 对 vm212 **全程只读**：所有 psql 均为 `BEGIN; SET TRANSACTION READ ONLY; … COMMIT;`；唯一的批量读取是 `pg_dump`（只读）。**未执行任何 UPDATE/DELETE/INSERT/DDL、未跑 alembic、未改 .env、未 `docker compose` 任何子命令、未重启容器、未碰任何卷。**
- 隐私：报告内所有 user id / resident id 截断到前 8 位；邮箱、密码哈希、头像等字段一律未读取、未落盘。

---

## 0. 结论先行

| 问题 | 实测答案 |
|---|---|
| 事故后 10h40m 有真实用户产出吗？ | **几乎没有。** 45 个用户里只有 **1 个**（`176a210c…`）回来过，其全部产出 = **1 行 time_capsule（正文 3 个字符）+ 1 行新 location_visits + 4 行 location_visits 计数更新**；其余 11 行（成就 2 / 交易 2 / 通知 2 / 日常任务 1 / 摘要 1 / +35 灵魂币）全是系统自动发放。**玩家对话产出 = 0：conversations 0 行、messages 0 行、`llm_usage.user_id` 非空 0 行。** |
| 备份是 047 schema、live 是 049，老行插不进新表吗？ | **前提不成立。备份的 `alembic_version` 就是 `049_add_policies`，与 live 完全一致。** 864 个列定义逐字节 diff 零差异；253 条索引零差异；229 条约束仅 21 条 `= ANY (ARRAY[...])` 的 pg_dump 渲染差异（语义等价，且全在 `lab_*`/`coin_*` 表，与 residents/users 无关）。**方案 2 无 schema 风险。** |
| 已在本地真跑过方案 2 吗？ | **跑过，两个变体都绿。** 把 live 库 `pg_dump` 出来在本地重建成副本，然后真跑恢复 SQL：基础版 `COPY 12 / UPDATE 12 / COMMIT`，扩展版 `COPY 21 / COPY 19591 / … / UPDATE 12 / COMMIT`，退出码 0，FK 孤儿 0、slug 重复 0。 |
| 推荐 | **方案 2 扩展版（2+）**，理由见 §7。 |

**取证中发现的、任务书没提到的一件事**：事故不止毁了 12 个玩家角色，还毁了 **9 个用户用 forge 亲手捏的 NPC**（persona/soul/ability 各 1–3 KB 的原创设定）。这 9 个角色的用户投入远高于那 12 个默认名（"新居民"/"测试员小柯"）的玩家化身。详见 §4.2。

---

## 1. 本地临时库：搭建与清理证据

### 1.1 搭建

| 项 | 值 |
|---|---|
| 容器名 | `p0f-restore-pg16` |
| 镜像 | `pgvector/pgvector:pg16`（PostgreSQL 16.14，与 vm212 同版本同镜像） |
| 端口映射 | `0.0.0.0:55432 -> 5432`（**独立端口，未复用 deploy 那套 compose，未使用任何 compose project 名**） |
| 卷名 | `p0f-restore-pgdata`（新建独立命名卷） |
| Docker 上下文 | `DOCKER_HOST=unix:///Users/jimmy/.colima/default/docker.sock`（动手前 `docker info` 已确认走 colima） |
| 库 1 | `skills_world_backup` ← 事故前 16:46 备份 |
| 库 2 | `skills_world_livecopy` ← live 只读 `pg_dump` 副本（方案 2 基础版排练） |
| 库 3 | `skills_world_livecopy2` ← live 只读 `pg_dump` 副本（方案 2 扩展版排练） |

启动命令（原样）：

```
docker run -d --name p0f-restore-pg16 \
  -e POSTGRES_PASSWORD=p0forensics -e POSTGRES_DB=skills_world_backup -e POSTGRES_USER=postgres \
  -p 55432:5432 -v p0f-restore-pgdata:/var/lib/postgresql/data pgvector/pgvector:pg16
```

### 1.2 备份文件完整性

```
vm212 : ab517007deb897f8c1848d62ba33b1b9  /opt/skills-world/deploy/db-backup-roster-20260725-164629.sql.gz  (72559925 B)
本机  : ab517007deb897f8c1848d62ba33b1b9  /tmp/p0f/db-backup-roster-20260725-164629.sql.gz                  (72559925 B)
gzip -t : GZIP_OK
```

备份文件在 vm212 上**只被 `scp` 读取**，未覆盖 / 未移动 / 未删除（`ls -la` 时间戳仍为 `Jul 25 16:46`）。

restore 结果：exit 0，日志 48 行，其中 **48 行全部是 `ERROR: role "lab_financial_kernel_owner"/"lab_command_submitter_v2"/"lab_terminalizer_v2" does not exist`**（本地没建这三个 lab 角色，只影响 GRANT，不影响任何数据）。vm212 上这三个角色**存在**（`pg_roles` 已核），所以真恢复时连这些告警都不会有。

### 1.3 清理证据

见文末 §9「清理确认」（收工时补写的实际输出）。

---

## 2. 三向差异 —— 逐表数字

基准线：备份时刻 `2026-07-25 16:46:29+00`；清库时刻 `2026-07-25 16:53:47.565+00`（live `min(residents.created_at)`）；取证时刻 `2026-07-26 03:26:31+00`。事故窗口长度 = **10h40m**（任务书里的"9.5h"略偏小）。

> 注意：`pg_stat_user_tables.n_live_tup` 在 vm212 上严重过时（例如 users 显示 0、实际 45；world_events 显示 10、实际 86）。本报告所有数字均来自 `count(*)` 实测。

### 2.1 备份 vs live：逐表行数 + 主键集合差异

`live_only` = live 有、备份没有的行（= 方案 1 全量回滚会丢的行）
`bk_only` = 备份有、live 没有的行（= 事故毁掉的行）

| 表 | 备份 | live | live_only | bk_only | 性质 |
|---|---:|---:|---:|---:|---|
| memories | 27514 | 2957 | 2957 | 27514 | 全量换血（system agent tick 产物） |
| llm_usage | 25354 | 2449 | 2106 | 25011 | 遥测；343 行（resident_id 为空的）幸存 |
| residents | 26 | 11 | 11 | 26 | 26 全删，11 新阵容 |
| resident_relations | 127 | 44 | 44 | 127 | system |
| personality_history | 155 | 26 | 26 | 155 | system |
| feed_events | 26 | 23 | 23 | 26 | system |
| bulletin_posts | 34 | 18 | 3 | 19 | 15 行幸存；被删的 19 行是 NPC 撰写 |
| conversations | 20 | 0 | 0 | 20 | **全灭** |
| messages | 106 | 0 | 0 | 106 | **全灭**（其中 53 行是玩家真实发言） |
| resident_goals | 6 | 16 | 16 | 6 | system |
| resident_treasuries | 0 | 7 | 7 | 0 | system（本轮新表数据） |
| world_events | 81 | 86 | 5 | 0 | system（天气/集市日） |
| time_capsules | 2 | 1 | 1 | 2 | 2 个旧胶囊被删、1 个新胶囊 |
| transactions | 222 | 224 | 2 | 0 | 系统自动发放 |
| user_achievements | 40 | 42 | 2 | 0 | 系统自动解锁 |
| notifications | 31 | 33 | 2 | 0 | 系统推送 |
| items | 10 | 12 | 2 | 0 | seed 数据（幂等可重播） |
| location_visits | 15 | 16 | 1 | 0 | +4 行仅更新计数 |
| daily_quests | 18 | 19 | 1 | 0 | 系统发放 |
| digests | 19 | 20 | 1 | 0 | 系统生成 |
| commissions | 0 | 1 | 1 | 0 | NPC 发布、无人接单 |
| debates | 1 | 0 | 0 | 1 | system |
| follows | 2 | 0 | 0 | 2 | 关注老 NPC |
| users | 45 | 45 | 0 | 0 | **45 个 id 完全一致**，见 §2.3 |
| achievements / offices / policies / polls / purchases / forge_sessions / system_config / town_treasuries / debate_stakes | — | — | 0 | 0 | 未受影响 |

**live_only 合计 5211 行**（其中 5063 行 = memories 2957 + llm_usage 2106，纯系统遥测）。

### 2.2 事故窗口内的新增：系统 vs 真实用户

| 分类 | 表 | 行数 | 判定依据 |
|---|---|---:|---|
| 系统自动 | memories | 2957 | agent tick；`min(created_at)`=16:53:47.86 |
| 系统自动 | llm_usage | 2106 | **`owner` 全为 `system`，`user_id` 非空 0 行**；scenario: decide 1144 / chat_turn 526 / evolution_drift 169 / chat_wrapup 110 / reflect 73 / evolution_sync 26 / plan 23 / gossip 18 / evolution_shift 14 / dream 3；合计 $0.2644 |
| 系统自动 | resident_relations | 44 | NPC 互动 |
| 系统自动 | personality_history | 26 | 人格漂移 |
| 系统自动 | feed_events | 23 | 世界流 |
| 系统自动 | resident_goals | 16 | 每日目标生成 |
| 系统自动 | residents | 11 | 新阵容 seed（`seed_presets` 幂等，可无损重播） |
| 系统自动 | resident_treasuries | 7 | 财政初始化 |
| 系统自动 | world_events | 5 | 天气 ×3 / 集市日 / 公开课，`created_by` 全为 NULL |
| NPC 产出 | bulletin_posts | 3 | 作者是 `1552e012`(何巧云) / `0500922d`(沈静书) / `54bd7b03`(赵启文)，`author_user_id` 全 NULL |
| NPC 产出 | commissions | 1 | `acceptor_user_id` NULL（没人接） |
| seed | items | 2 | 幂等 |
| **真实用户** | **time_capsules** | **1** | 用户 `176a210c…`，`carrier_resident_slug=shen-jingshu`，`deliver_on=2026-07-29`，`status=sealed`，**正文长度 3 字符** |
| **真实用户** | **location_visits** | **1 新 + 4 更新** | 用户 `176a210c…` 00:18–00:32 走了 shop（新发现）/ apt_star / north_path / academy / central_plaza |
| 系统发放（因用户上线触发） | transactions | 2 | `daily_login_reward` +15、`achievement:remembered` +20 |
| 系统发放 | user_achievements | 2 | `remembered`、`memory_keeper_10` |
| 系统发放 | notifications | 2 | 「新发现：杂货铺」「被记住」 |
| 系统发放 | daily_quests / digests | 1 / 1 | 自动 |
| 用户字段更新 | users | 1 行 | `176a210c…`：`soul_coin_balance` 150→185、`last_daily_reward_at` 07-25 16:11 → 07-26 00:11 |

**真实用户不可再生的产出，全部就是那 1 行 3 字符的时间胶囊。**（其余全部可由系统重新发放，或只是位置计数。）

### 2.3 那 12 个用户事故后回来过吗？

跨 9 张带 `user_id` 的表（transactions / user_achievements / notifications / digests / daily_quests / time_capsules / location_visits / purchases / conversations）做 UNION，事故窗口内出现过的 **distinct user 只有 1 个：`176a210c…`**。

事故前后各用户的活跃度（`registered` / `last_activity` 取自备份库多表 max）：

| user8 | 绑定角色 | 注册 | 事故前最后活动 | 事故后是否回来 | 事故前对话数 | 购买 | 灵魂币 |
|---|---|---|---|---|---:|---:|---:|
| `176a210c` | `32689525` p-新居民-d7de95 | 07-23 05:41 | 07-25 16:41（事故前 12 分钟） | **是**（07-26 00:11–00:32） | 0 | 0 | 150→185 |
| `eb3d2091` | `7c57e2e0` p-测试员小柯-1a106a | 07-23 09:35 | 07-23 16:26 | 否 | 5 | 1 | 341 |
| `11769050` | `5633cfa5` p-新居民-adc21f | 07-17 03:21 | 07-23 15:22 | 否 | 1 | 2 | 28 |
| `088f49ab` | `0d077fd0` p-新居民-ef4836 | 07-23 14:55 | 07-23 15:01 | 否 | 0 | 5 | 85 |
| `e99dcd76` | `0ca8d29c` p-测试员小柯-0a8352 | 07-23 14:55 | 07-23 14:58 | 否 | 2 | 1 | 191 |
| `dc3a67ee` | `a4a33128` p-新居民-d97d9c | 07-23 09:35 | 07-23 09:51 | 否 | 0 | 5 | 85 |
| `75429466` | `e662aeaa` p-测试员小柯 | 07-23 09:31 | 07-23 09:36 | 否 | 2 | 0 | 246 |
| `504a550d` | `4f20c783` p-新居民-fb9b04 | 07-23 09:31 | 07-23 09:31 | 否 | 0 | 0 | 110 |
| `8c542f56` | `848d2608` p-新居民-a55197 | 07-23 03:35 | 07-23 03:35 | 否 | 0 | 0 | 100 |
| `790cb952` | `58881a2a` p-新居民-8b6aa3 | 07-21 14:21 | 07-21 14:21 | 否 | 0 | 0 | 100 |
| `77df9911` | `7065395e` p-新居民-466207 | 07-18 16:41 | 07-18 16:41 | 否 | 0 | 0 | 100 |
| `c96a2e74` | `1250e7bc` p-新居民 | 07-13 16:01 | 07-17 03:34 | 否 | 1 | 0 | 315 |

**11/12 在事故发生前就已休眠 ≥2 天**（最近一次活动 07-18 ~ 07-23）。只有 `176a210c` 是活跃用户，且他在事故后回来过 —— 他会是唯一能察觉到"我的角色不见了"的人。

---

## 3. 12 个玩家角色画像（脱敏）

来源：本地 `skills_world_backup`。

| res8 | slug | name | creator8 | users 行 | 绑定 users 行 | memories | conversations | messages | created | 最后对话 | district | status | heat | ★ |
|---|---|---|---|---|---|---:|---:|---:|---|---|---|---|---:|---:|
| `1250e7bc` | p-新居民 | 新居民 | `c96a2e74` | ok | `c96a2e74` | 1567 | 1 | 4 | 07-13 16:17 | never | central_plaza | idle | 0 | 1 |
| `5633cfa5` | p-新居民-adc21f | 新居民 | `11769050` | ok | `11769050` | 1464 | 0 | 0 | 07-17 03:22 | never | central_plaza | idle | 0 | 1 |
| `7065395e` | p-新居民-466207 | 新居民 | `77df9911` | ok | `77df9911` | 1366 | 0 | 0 | 07-18 16:42 | never | central_plaza | idle | 0 | 1 |
| `58881a2a` | p-新居民-8b6aa3 | 新居民 | `790cb952` | ok | `790cb952` | 926 | 0 | 0 | 07-21 14:22 | never | central_plaza | idle | 0 | 1 |
| `848d2608` | p-新居民-a55197 | 新居民 | `8c542f56` | ok | `8c542f56` | 710 | 0 | 0 | 07-23 03:35 | never | central_plaza | idle | 0 | 1 |
| `32689525` | p-新居民-d7de95 | 新居民 | `176a210c` | ok | `176a210c` | 617 | 0 | 0 | 07-23 05:41 | never | central_plaza | idle | 0 | 1 |
| `e662aeaa` | p-测试员小柯 | 测试员小柯 | `75429466` | ok | `75429466` | 621 | 0 | 0 | 07-23 09:31 | never | central_plaza | idle | 0 | 1 |
| `4f20c783` | p-新居民-fb9b04 | 新居民 | `504a550d` | ok | `504a550d` | 665 | 0 | 0 | 07-23 09:31 | never | central_plaza | idle | 0 | 1 |
| `7c57e2e0` | p-测试员小柯-1a106a | 测试员小柯 | `eb3d2091` | ok | `eb3d2091` | 714 | 0 | 0 | 07-23 09:35 | never | central_plaza | idle | 0 | 1 |
| `a4a33128` | p-新居民-d97d9c | 新居民 | `dc3a67ee` | ok | `dc3a67ee` | 642 | 0 | 0 | 07-23 09:35 | never | central_plaza | idle | 0 | 1 |
| `0ca8d29c` | p-测试员小柯-0a8352 | 测试员小柯 | `e99dcd76` | ok | `e99dcd76` | 694 | 0 | 0 | 07-23 14:55 | never | central_plaza | idle | 0 | 1 |
| `0d077fd0` | p-新居民-ef4836 | 新居民 | `088f49ab` | ok | `088f49ab` | 624 | 0 | 0 | 07-23 14:55 | never | central_plaza | idle | 0 | 1 |

关键观察：

1. **12 个 creator_id 全部指向 live 里存在的 users 行**（45 个 user id 备份与 live 完全一致），且 `creator_id == users.player_resident_id` 反查 **0 处不一致**、`creator_id` 在 12 行内 **distinct = 12、NULL = 0**。→ 方案 2 的回填映射可以完全从 `residents.creator_id` 推导，**不需要在 SQL 里硬编码任何 user id**。
2. **没有人改过角色名**：9 个还叫默认「新居民」、3 个叫「测试员小柯」（onboarding 默认值）。`meta_json.origin` 全为 `onboarding`。
3. **12 个角色 11 个从未被对话过**（`last_conversation_at` 全 NULL，`heat` 全 0，`star_rating` 全 1）；唯一一条玩家角色对话是 `4a715fc8`（`11769050` × p-新居民，2 turns，07-17）。
4. 每个角色 600–1600 条 memories，`type` 分布 = event 10482 / relationship 125 / reflection 2 / dream 1 —— **全部是 agent tick 自动生成的观察记忆，不是用户写的内容**。玩家角色合计 10610 条 memories = 全库 27514 的 39%。

### 3.1 备份里那 20 条对话的归属（说明"恢复 12 行角色"能带回多少对话）

| resident_type | 对话数 | turns | 涉及用户数 |
|---|---:|---:|---:|
| npc | 19 | 51 | 13 |
| player | 1 | 2 | 1 |

全库 messages 106 行 = user 53 + assistant 53。19 条 NPC 对话中 **16 条挂在 klaus/isabella 上**（见 §6.1，这两个是会被 bootstrap 再删一次的 legacy demo NPC）。

---

## 4. 事故实际毁掉的用户内容 —— 比任务书描述更多

### 4.1 被删的 26 个 residents 的构成

| 分类 | 数量 | creator | 说明 |
|---|---:|---|---|
| 玩家化身（`resident_type='player'`） | 12 | 真实用户 | §3 |
| **用户 forge 捏的 NPC** | **9** | 真实用户 | §4.2 |
| legacy demo NPC | 5 | `SYSTEM_USER_ID`(`…0001`) | isabella / klaus / adam / mei / tamara |

### 4.2 9 个用户亲手捏的 NPC（新发现，任务书未提）

| res8 | slug | name | creator8 | memories | conv | msgs | persona/soul/ability 长度 | created | ★ |
|---|---|---|---|---:|---:|---:|---|---|---:|
| `fa61afe1` | 林晚秋 | 林晚秋 | `c96a2e74` | 1493 | 0 | 0 | 1312 / 1581 / 1741 | 07-13 18:18 | 2 |
| `7f8e50f0` | 陈默 | 陈默 | `c96a2e74` | 1704 | 0 | 0 | 1272 / 1740 / 1853 | 07-13 18:26 | 2 |
| `b34c6023` | 夏洛克-福尔摩斯 | 夏洛克·福尔摩斯 | `c96a2e74` | 1902 | 0 | 0 | 1372 / 3680 / 2457 | 07-13 18:41 | 2 |
| `d3ee6b1a` | 夜风侦探 | 夜风侦探 | `75429466` | 646 | 0 | 0 | 662 / 0 / 193 | 07-23 09:33 | 1 |
| `80a63c1d` | 阿达-洛芙莱斯 | 阿达·洛芙莱斯 | `75429466` | 704 | 0 | 0 | 1228 / 0 / 633 | 07-23 09:36 | 2 |
| `3a87fa24` | 夜风侦探-a23160 | 夜风侦探 | `eb3d2091` | 590 | 0 | 0 | 1305 / 1262 / 820 | 07-23 09:37 | 3 |
| `9c38b554` | 夜风侦探-46ff1f | 夜风侦探 | `e99dcd76` | 622 | 0 | 0 | 1318 / 858 / 782 | 07-23 14:56 | 3 |
| `72895370` | 部署回归图灵0724 | 部署回归图灵0724 | `eb3d2091` | 655 | 2 | 10 | 995 / 241 / 0 | 07-23 16:07 | 2 |
| `6373f312` | 格蕾丝-霍珀 | 格蕾丝·霍珀 | `eb3d2091` | 665 | 1 | 6 | 1472 / 2189 / 1838 | 07-23 16:11 | 2 |

`forge_sessions` 8 行（4 个用户，6 done / 1 building / 1 error）**在 live 里完好幸存**（没 FK 到 residents），但它们指向的角色已经不存在 —— 也就是说，用户在"我的创造"里会看到 session 记录却点不出角色。

**判断**：这 9 个角色的 persona/soul/ability 是用户经 forge 流程真实撰写/生成的原创设定（1–3 KB 一份），比那 12 个默认名玩家化身的用户投入高得多。恢复方案应把它们一起考虑。它们同样是 **bootstrap-safe**（见 §6.1）。

---

## 5. Schema 差异实测 —— 「047 → 049」这个前提不成立

任务书点名的技术风险是"备份是 047 schema、live 是 049，老行未必插得进新表"。**实测否证**：

```
备份库  : SELECT * FROM alembic_version;  ->  049_add_policies
live 库 : SELECT * FROM alembic_version;  ->  049_add_policies
```

（推测原因：16:46:29 那次备份是在 `alembic upgrade head` 之后、`purge_residents` 之前拍的，所以已经是 049 schema。）

进一步逐项 diff（备份库 restore 后 vs live 只读查询）：

| 维度 | 备份 | live | 差异 |
|---|---:|---:|---|
| `information_schema.columns`（table/column/type/长度/nullable/default 全字段） | 864 行 | 864 行 | **`diff` 零输出** |
| `pg_indexes.indexdef` | 253 | 253 | **`diff` 零输出** |
| `pg_constraint`（含 `pg_get_constraintdef`） | 229 | 229 | 21 行文本差异，**全部是 `= ANY (ARRAY[a,b])` vs `= ANY ((ARRAY[a,b])::text[])` 的渲染差**，语义等价；涉及表全部是 `coin_hold_entries` / `coin_holds` / `lab_artifact*` / `lab_control_targets` / `lab_global_kills` / `lab_queue_claims` / `lab_run_control_requests` / `lab_runtime_*` / `lab_terminalization_commands` / `lab_tool_executions` —— **residents、users、memories、conversations、messages 一条不涉及** |

**结论：新增 NOT NULL 列 0 个、类型变更 0 处、新增约束 0 条。方案 2 的 schema 兼容性风险为零，且已由 §8.3 的真实排练证实。**

### 5.1 id / slug 冲突实测

| 检查 | 结果 |
|---|---|
| 备份 26 个 resident id ∩ live 11 个 resident id | **空集** |
| 备份 26 个 slug ∩ live 11 个 slug | **空集** |
| 备份 45 个 user id vs live 45 个 user id | **逐行完全一致** |

唯一需要留意的不是约束冲突而是**显示名撞车**：备份的 forge NPC slug `林晚秋`（`fa61afe1`）与 live 预设 slug `lin-wanqiu` 的 `name` 都是「林晚秋」。slug 不同 → 唯一约束不冲突，但恢复后前端会出现两个同名居民。

### 5.2 users 表的实际损伤范围

45 行逐字段 diff（id / player_resident_id / soul_coin_balance / last_daily_reward_at / is_admin / is_banned / last_x,last_y）只有两类差异：

1. **12 行的 `player_resident_id` 由角色 id 变为 NULL**（`purge_residents` 里 `update(User).values(player_resident_id=None)` 干的，`backend/seed/reset_builtin_residents.py:102-107`）；
2. **1 行（`176a210c…`）的 `soul_coin_balance` 150→185、`last_daily_reward_at` 前移**。

其余字段（余额、坐标、权限、封禁位）**全部未损**。→ 恢复只需回填 12 个指针，不需要碰 users 的任何其他列。

---

## 6. 三个方案的代价对比

### 6.1 关键前置事实：bootstrap 会对每个方案做什么

`master 999e098` 起 compose 有一次性 `bootstrap` 服务（`alembic upgrade head && python -m seed.reset_builtin_residents`），api/agent-worker `depends_on: service_completed_successfully`。也就是说 **任何恢复动作之后的第一次 `docker compose up -d` 都会跑一遍 `find_targets()` + `purge_residents()`**。

`find_targets()`（`backend/seed/reset_builtin_residents.py:53-65`）匹配条件：`resident_type != 'player'` **且**（`slug ∈ LEGACY_BUILTIN_SLUGS` **或** `creator_id == SYSTEM_USER_ID`）**且** `slug ∉ NEW_ROSTER_SLUGS`。

我把这段逻辑翻译成 SQL，直接跑在两种恢复后状态上：

| 恢复后状态 | bootstrap 会删掉 | 证据 |
|---|---|---|
| 方案 2 / 2+ 之后（本地 `skills_world_livecopy2`） | **0 行** | 查询返回 `(0 rows)` |
| 方案 1 全量回滚之后（本地 `skills_world_backup`） | **5 行：klaus / tamara / mei / isabella / adam** | 查询返回 5 行 |

也就是说 **方案 1 的"完整回滚"是假的**：回滚完只要正常 `up` 一次，bootstrap（这次是走正确路径的）就会把 5 个 legacy demo NPC 再删一遍，并按 `purge_residents` 的级联规则连带删掉：

| 级联对象 | 行数 |
|---|---:|
| memories（demo NPC 自己的） | 7923 |
| llm_usage | 6440 |
| memories（**别人的**，因 `related_resident_id` 指向 demo NPC 而被删） | 315 |
| ↳ 其中属于那 12 个玩家角色的 | **55** |
| messages | 86 |
| personality_history | 33 |
| resident_relations | 22 |
| conversations | **16** |
| feed_events（按 slug） | 13 |
| bulletin_posts | 12 |
| follows（按 slug） | 1 |

→ 方案 1 号称能带回 20 条对话，实际稳态只剩 **4 条**（20 − 16）。

### 6.2 方案 1 · 全量回滚到 16:46 快照

**做法**：停 api/agent-worker → 拍一份当前库的保命备份 → `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` → `gzcat 备份 | psql` → `docker compose up -d`（bootstrap 重播 11 人阵容）。备份是 plain SQL（75 个 `CREATE TABLE` + 75 个 `COPY`），**无 `DROP`、无 `CREATE DATABASE`**，所以必须先清空目标库。

**丢失的具体行数**（= §2.1 的 `live_only` 列）：

| 表 | 丢失行数 | 性质 |
|---|---:|---|
| memories | 2957 | 系统 |
| llm_usage | 2106 | 系统遥测（含 $0.2644 成本记录） |
| resident_relations | 44 | 系统 |
| personality_history | 26 | 系统 |
| feed_events | 23 | 系统 |
| resident_goals | 16 | 系统 |
| residents（11 人新阵容） | 11 | **bootstrap 会用新 id 重播，功能无损** |
| resident_treasuries | 7 | 系统 |
| world_events | 5 | 系统（天气/节庆） |
| bulletin_posts | 3 | NPC 撰写 |
| transactions | 2 | 系统发放（`176a210c` +35 币） |
| user_achievements | 2 | 系统解锁（`176a210c`） |
| notifications | 2 | 系统推送 |
| items | 2 | seed 幂等可重播 |
| commissions | 1 | NPC 发布 |
| daily_quests | 1 | 系统 |
| digests | 1 | 系统 |
| location_visits | 1 新 + 4 更新 | `176a210c` 的探索足迹 |
| **time_capsules** | **1** | **唯一真正不可再生的用户产出（3 字符）** |
| users | 0 新增，**1 行字段回退**（`176a210c`：余额 185→150、`last_daily_reward_at` 回到 07-25 16:11） | — |
| **合计** | **5211 行 + 5 行更新回退** | 其中 5063 行（97.2%）是纯系统遥测 |

**额外副作用**：`last_daily_reward_at` 回退意味着 `176a210c` 可以再领一次日常奖励（无害）。11 人新阵容的 11 个 id 会变（bootstrap 生成新 UUID），任何外部写死 resident id 的东西会失效（未发现有）。

**再叠加 §6.1 的 bootstrap 二次清理**，方案 1 的稳态 = 备份数据 − 5 个 demo NPC 及其级联（含 16 条对话、86 条 messages、8238 条 memories）+ 11 人新阵容。

### 6.3 方案 2 · 只抽 12 行 resident + 回填 `users.player_resident_id`

**技术可行性逐项验证**

| 风险项 | 实测结论 |
|---|---|
| schema 兼容（047→049） | **前提不成立，备份就是 049；864 列零差异**（§5） |
| 新增 NOT NULL 列 / 类型变更 / 新增约束 | **0 / 0 / 0** |
| `residents.id` 主键冲突 | **无**（备份 26 id ∩ live 11 id = 空） |
| `residents.slug` 唯一约束冲突（`ix_residents_slug` + `residents_slug_key`） | **无**（slug 集合交集为空） |
| `residents.creator_id → users(id)` 外键 | **12/12 满足**（45 个 user id 备份与 live 逐行一致） |
| `users.player_resident_id` 回填映射 | **可从 `residents.creator_id` 推导**，12 个 creator distinct=12、NULL=0、与备份里的 `player_resident_id` 反查零不一致 → SQL 里不需要硬编码 id |
| 恢复后被 bootstrap 二次清理 | **不会**：`resident_type='player'` 被 `find_targets` 第一个条件排除；实跑模拟返回 0 行（§6.1） |
| 实跑验证 | **在 live 库的本地副本上真跑过**：`COPY 12 / UPDATE 12 / COMMIT`，exit 0；孤儿 FK 0、slug 重复 0、`bad_creator` 0（§8.3） |

**代价 / 缺口**

| 项 | 行数 | 影响 |
|---|---:|---|
| 恢复 residents | 12 | 角色本体（含 persona / meta_json / sbti / 坐标 / 家 / mood / versions_json）**完整回来** |
| 回填 users 指针 | 12 | 玩家重新"绑定"到自己的角色 |
| **不恢复** memories | 10610 | 角色回来但"失忆"——见下 |
| **不恢复** conversations / messages | 1 / 4 | 只影响 `11769050` 一人的一次 2 轮对话 |
| **不恢复** personality_history | 122（21 角色合计） | 人格演化曲线断档 |
| **不恢复** resident_relations | 98（21 角色互相） | 熟悉度/好感度归零，会被 agent 重新积累 |

**「角色回来但记忆丢失」对玩家意味着什么（基于实测，不是猜）**：

- 这些 memories **不是玩家写的内容**，是 agent tick 的观察日志（event 10482 / relationship 125 / reflection 2 / dream 1）。玩家从未通过对话产生过它们（11/12 角色 `total_conversations=0`、`last_conversation_at` NULL）。
- 玩家能察觉的差异集中在：角色档案页的"记忆"列表变空、`resident_relations` 归零（社交图谱重置）、`personality_history` 演化曲线断档。**角色名字、人格设定（SBTI/persona）、坐标、家、灵魂币余额、成就全部不受影响。**
- 而且 memories **是可以一起恢复的**：10610 条里 8426 条 `related_resident_id` 为 NULL、1603 条指向另一个（同样会被恢复的）玩家角色，只有 **581 条**指向已不存在的老 NPC —— 把这 581 行的 `related_resident_id` 置 NULL 即可全量插入。这就是下面的扩展版。

**方案 2+（扩展版，实测已跑通）**：12 玩家角色 + 9 forge NPC（共 21 residents）+ 它们的 memories 19591（315 行 `related_resident_id` 指向 21 人以外者已置 NULL）+ conversations 4 + messages 20 + personality_history 122 + resident_relations 98 + bulletin_posts 7 + users 指针 12。

排练输出：`COPY 21 / COPY 19591 / COPY 4 / COPY 20 / COPY 122 / COPY 98 / COPY 7 / UPDATE 12 / COMMIT`，exit 0。恢复后 residents 32（npc 20 + player 12）、memories 22548、users linked 12，FK 孤儿全 0、slug 重复 0，bootstrap 模拟清理 0 行。

### 6.4 方案 3 · 不恢复

**代价**：12 个玩家化身 + 9 个用户 forge 角色永久消失；`forge_sessions` 留下 8 条指向空角色的记录（用户可见的"幽灵"）；被删角色相关的 20 条对话历史永久消失。

**要通知这 12 个用户吗？** 基于 §2.3 的数据：

- **11/12 在事故前就已休眠 ≥2 天，事故后零活动** → 通知他们的边际价值接近零，反而是主动把一次他们没察觉的事故摆到台面上。
- **1/12（`176a210c`）事故前 12 分钟还在线、事故后回来过** → 他必然已经看到自己的角色不见了（角色页空、`player_resident_id` 为 NULL 会走 onboarding 重建流程）。**只有这个人需要通知。**
- 另外 **4 个用户（`c96a2e74` / `75429466` / `eb3d2091` / `e99dcd76`）丢了 forge 亲手捏的角色**（共 9 个）。他们目前也都休眠中（最后活动 07-17 ~ 07-23），但他们的投入最大，一旦回来最容易发现。

→ 若走方案 3，建议**只对 `176a210c` 单点告知**，其余 4 个 forge 用户等他们下次登录时再按需解释；不做全站公告（45 人里 44 人无感）。

### 6.5 成本侧：恢复角色 = 后台 LLM 花销翻倍（三方案通用的隐性代价）

`backend/app/agent/loop.py:132-136` 的 tick 选取是 `select(Resident.id, …).where(Resident.status.not_in(['sleeping']))` —— **没有 `resident_type` 过滤**。所以恢复的每个角色都会立刻进入 agent tick 轮次。实测数据：

| 时期 | 居民数 | LLM 花销 |
|---|---:|---|
| 备份库全历史，按 `resident_id` 归属 | — | npc 14624 次 / $2.1192；**player 10387 次 / $1.3599**；无归属 343 次 / $0.4684 |
| 07-25（事故前，26 居民，约 16.8h） | 26 | $0.8782 / 6404 次 → ≈ **$1.26/天** |
| 事故后（11 居民，10.67h） | 11 | $0.2644 / 2106 次 → ≈ **$0.60/天** |

`BUDGET_GLOBAL_DAILY_USD=1.5`。→ 方案 1 或方案 2+ 之后居民数 11→23（或 32），后台日花销预计回到 **$1.2–1.4/天，即预算的 80–93%**，熔断（`PLAYER_ONLY` ≥100% / `RULE_ONLY` ≥95%）会开始频繁触发。**这是恢复决策里最容易被忽略的代价，需要一起拍板。**

### 6.6 产品可见副作用：公共居民名录被"默认名"污染

`backend/app/services/resident_service.py:6-18` 的 `list_residents()` **不按 `resident_type` 过滤**，`GET /residents`（`backend/app/routers/residents.py:58-65`）直接返回全表。→ 恢复 12 个玩家角色后，公共名录从 11 人变 23 人，其中 9 个叫「新居民」、3 个叫「测试员小柯」。这本来就是事故前的状态（26 人名录里 12 个默认名），但如果 Jimmy 是把"11 人干净中文原创阵容"当成对外门面，恢复会把它污染回去。

**可选缓解**（不在本线执行）：给 `list_residents` 加 `resident_type != 'player'` 过滤，或按 `heat>0` 过滤。

---

## 7. 推荐

### 推荐：方案 2+（扩展版精准恢复），并在恢复前后各加一道门

**推荐理由**

1. **方案 1 的唯一优势（"完整"）是假的**。§6.1 实证：全量回滚后第一次 `docker compose up` 会被 bootstrap 再删掉 5 个 demo NPC，级联带走 16 条对话 / 86 条 messages / 8238 条 memories。方案 1 稳态并不比方案 2+ 多带回什么有价值的东西，却要多丢 5211 行 live 数据、多承担一次 `DROP SCHEMA` 的操作风险。
2. **回滚代价虽小但完全没必要付**。事故窗口内唯一不可再生的用户产出是 1 行 3 字符的时间胶囊（`176a210c`）。方案 2+ 一行都不用丢；方案 1 要丢它 + 那个用户的 35 灵魂币 + 2 个成就 + 5 行探索足迹。差距不大，但方案 2+ 是严格更优，不存在 trade-off。
3. **方案 2+ 的技术风险已经被实测清零**。schema 零差异、id/slug 零冲突、外键 12/12 可满足、bootstrap 模拟不会二次删除、并且**已经在 live 库的本地副本上真跑通**（`COPY 21 / COPY 19591 / … / COMMIT`，exit 0，FK 孤儿 0）。这不是纸面推演。
4. **它把 9 个用户 forge 角色一起救回来**，这批内容（每个 1–3 KB 原创 persona/soul/ability）的用户投入远高于 12 个默认名化身。方案 1 也能救回它们，但要付上面的代价；方案 3 则永久放弃。
5. **它不用停机、不用 DROP SCHEMA、单事务可回滚**。整套操作是一个 `BEGIN … COMMIT`，验证不通过直接 `ROLLBACK`，对在线世界零影响。

**推荐的完整落地顺序**（本线不执行）

1. 先在 vm212 拍一份**当前状态**的保命备份（现在只有事故前 07-25 16:46 和 07-23 22:45 两份，**没有任何事故后的备份**）。
2. 决定是否先接受 §6.5 的成本影响：要么同步把 `BUDGET_GLOBAL_DAILY_USD` 上调，要么接受熔断更频繁，要么分两批恢复（先 12 玩家角色，观察一天再上 9 个 forge NPC）。
3. 跑 §8 的 SQL（单事务，先 `ROLLBACK` 试一遍再 `COMMIT`）。
4. 恢复后**不需要重启容器**（纯数据变更）；若要重启，§6.1 已证明 bootstrap 不会删任何恢复的行。
5. 单点告知 `176a210c`；对另外 4 个 forge 用户不主动公告。
6. 独立于恢复：修 `purge_residents` 的防呆（它应自己拒绝 `resident_type == 'player'` 的目标，而不是信任调用方）。

### 我最不确定的地方（按不确定程度排序）

1. **那 1 行 3 字符的时间胶囊到底是不是真人操作**。`176a210c` 在 00:20:14 的 0.1 秒内连续产生了 time_capsule → achievement `remembered` → transaction → notification → achievement `memory_keeper_10`，正文只有 3 个字符。这既可能是真人随手填了个"测试"，也可能是某个自动化脚本/冒烟测试在跑（这个用户绑定的角色叫「新居民」但历史行为很像测试账号）。**我无法从数据区分**。如果它是脚本产物，方案 1 和方案 2+ 在"用户产出损失"这一维度就完全等价了，方案 1 的相对劣势只剩 bootstrap 二次删除和操作风险。要确认得看 api 容器的访问日志（本线未读，属于写入侧之外的取证）。
2. **恢复后 agent 行为的真实成本**，我只能按"居民数 × 历史单居民花销"线性外推到 $1.2–1.4/天。真实值受 SBTI 作息、熔断降级、社交半径影响，可能显著偏离。**建议恢复后头 24h 盯 `llm_usage` 实测，而不是信我这个外推。**
3. **`resident_relations` 有 `uq_resident_relation_pair (party_a, party_b)` 唯一约束**。我只恢复"双方都在恢复集合内"的 98 行，和 live 现有 44 行（全在新 NPC 之间）id 与 pair 都不重叠，排练也没报冲突。但如果恢复后 agent 在同一 tick 里正好要为同一对居民建关系，理论上可能撞唯一约束。**这个竞态我没法在离线副本上复现**，只能靠恢复时选一个 agent tick 的间隙、或临时停 agent-worker 来规避。
4. **`176a210c` 的 `player_resident_id` 现在是 NULL，他事故后回来过** —— 我没有查前端/onboarding 是否已经给他重新走过一遍 onboarding（`residents` 里没有他的新玩家角色，`forge_sessions` 也没新增，所以**大概率没有**）。但如果在恢复窗口之前他又登录一次并触发了 onboarding 重建，就会出现"新角色已建 + 老角色要回填"的双角色冲突，`UPDATE … WHERE u.player_resident_id IS NULL` 的守卫会让回填静默跳过他。**§8.4 的检查清单里已经把这一项列为硬门。**
5. **`forge_sessions` 与恢复角色的关联我没有验证**。8 行 session 幸存，但我没确认 session 里存的角色引用（`payload` 之类）能不能在角色 id 回来后重新接上。属于恢复后需要点一眼 UI 的项。

---

## 8. 若选方案 2：精确的 SQL 与执行前检查清单

> **以下 SQL 本线一行都没有在 vm212 上执行。** 所有排练都在本地 `skills_world_livecopy` / `skills_world_livecopy2`（live 只读 `pg_dump` 的副本）上完成。

### 8.1 第一步：从备份提取载荷（在本地临时库上做，不碰 vm212）

设 `KEEP` 为恢复集合定义：

```sql
-- 只恢复 12 个玩家化身：
--   SELECT id FROM residents WHERE resident_type='player'
-- 恢复 12 玩家化身 + 9 个用户 forge NPC（推荐，方案 2+）：
--   SELECT id FROM residents
--    WHERE resident_type='player'
--       OR creator_id <> '00000000-0000-0000-0000-000000000001'   -- SYSTEM_USER_ID
```

`SYSTEM_USER_ID` 见 `backend/seed/preset_characters.py:20`。第二个分支刻意排除了 5 个 legacy demo NPC —— 它们会被 bootstrap 立刻删掉，恢复它们是白费（§6.1）。

提取（`$KEEP` 代入上面的子查询；列清单从 `information_schema.columns` 按 `ordinal_position` 生成，避免写死列序）：

```bash
# 在本地临时库上执行；产物是 COPY text 格式的 TSV
pq() { docker exec -i p0f-restore-pg16 psql -U postgres -d skills_world_backup -c "$1"; }

for t in residents memories conversations messages personality_history resident_relations bulletin_posts; do
  docker exec -i p0f-restore-pg16 psql -U postgres -d skills_world_backup -At -c \
    "SELECT string_agg(quote_ident(column_name),', ' ORDER BY ordinal_position)
       FROM information_schema.columns
      WHERE table_schema='public' AND table_name='$t';" > cols_$t.txt
done

pq "\copy (SELECT $(cat cols_residents.txt) FROM residents WHERE id IN ($KEEP)) TO STDOUT" > ext_residents.tsv

# memories：把指向恢复集合之外的 related_resident_id 置 NULL（否则违反 FK）
MEMSEL=$(sed "s/related_resident_id/CASE WHEN related_resident_id IN ($KEEP) THEN related_resident_id ELSE NULL END/" cols_memories.txt)
pq "\copy (SELECT $MEMSEL FROM memories WHERE resident_id IN ($KEEP)) TO STDOUT" > ext_memories.tsv

pq "\copy (SELECT $(cat cols_conversations.txt) FROM conversations WHERE resident_id IN ($KEEP)) TO STDOUT" > ext_conversations.tsv
pq "\copy (SELECT $(cat cols_messages.txt) FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE resident_id IN ($KEEP))) TO STDOUT" > ext_messages.tsv
pq "\copy (SELECT $(cat cols_personality_history.txt) FROM personality_history WHERE resident_id IN ($KEEP)) TO STDOUT" > ext_personality_history.tsv
pq "\copy (SELECT $(cat cols_resident_relations.txt) FROM resident_relations WHERE party_a IN ($KEEP) AND party_b IN ($KEEP)) TO STDOUT" > ext_resident_relations.tsv
pq "\copy (SELECT $(cat cols_bulletin_posts.txt) FROM bulletin_posts WHERE author_resident_id IN ($KEEP)) TO STDOUT" > ext_bulletin_posts.tsv
```

实测行数（方案 2+ 的 `KEEP`）：residents 21、memories 19591、conversations 4、messages 20、personality_history 122、resident_relations 98、bulletin_posts 7。
只要 12 玩家化身（基础版）时：residents 12、memories 10610（其中 581 行需置 NULL）。

组装成一个事务文件：

```bash
{
  echo "BEGIN;"
  for t in residents memories conversations messages personality_history resident_relations bulletin_posts; do
    echo "COPY public.$t ($(cat cols_$t.txt)) FROM STDIN;"
    cat ext_$t.tsv
    echo '\.'
  done
  echo "UPDATE users u SET player_resident_id = r.id"
  echo "  FROM residents r"
  echo " WHERE r.resident_type = 'player' AND r.creator_id = u.id AND u.player_resident_id IS NULL;"
  echo "COMMIT;"
} > option2plus_apply.sql
```

**注意 `UPDATE` 里没有任何硬编码 user id / resident id** —— 映射从 `residents.creator_id` 推导（§3 已验证 12/12 一致、distinct=12、NULL=0）。`AND u.player_resident_id IS NULL` 是防覆盖守卫：如果某个用户在恢复窗口前已经重新 onboarding 出了新角色，这一行会被静默跳过而不是覆盖他的新角色（§7 不确定点 4）。

### 8.2 第二步：在 vm212 上执行（先 ROLLBACK 演一遍）

```bash
# 试跑：把最后一行 COMMIT 换成 ROLLBACK，确认 COPY/UPDATE 行数符合预期
sed '$s/^COMMIT;$/ROLLBACK;/' option2plus_apply.sql \
  | ssh vm212 'docker exec -i deploy-db-1 psql -U postgres -d skills_world -v ON_ERROR_STOP=1 -f -'

# 正式执行
ssh vm212 'docker exec -i deploy-db-1 psql -U postgres -d skills_world -v ON_ERROR_STOP=1 -f -' \
  < option2plus_apply.sql
```

期望输出（与本地排练逐行一致）：

```
BEGIN
COPY 21
COPY 19591
COPY 4
COPY 20
COPY 122
COPY 98
COPY 7
UPDATE 12
COMMIT
```

### 8.3 排练证据（已在本地 live 副本上真跑）

基础版（`skills_world_livecopy`，只 12 玩家化身）：

```
BEGIN
COPY 12
UPDATE 12
COMMIT
APPLY_EXIT=0
```

事后校验：residents 23（player 12）、users linked 12、`orphan users.player_resident_id` 0、`bad_creator` 0、`dup_slug` 0。

扩展版（`skills_world_livecopy2`，方案 2+）：

```
BEGIN
COPY 21
COPY 19591
COPY 4
COPY 20
COPY 122
COPY 98
COPY 7
UPDATE 12
COMMIT
APPLY_EXIT=0
```

事后校验：residents 32（npc 20 / player 12）、memories 22548、conversations 4、messages 20、users linked 12；FK 孤儿全 0（`users.player_resident_id` / `memories.resident_id` / `memories.related_resident_id` / `conversations.resident_id` / `messages.conversation_id`）、`dup residents.slug` 0；bootstrap `find_targets` 模拟返回 0 行。

### 8.4 执行前检查清单（硬门，任一不过就停）

| # | 检查 | 通过标准 | 命令（vm212 只读） |
|---|---|---|---|
| 1 | **先拍当前状态的保命备份** | 新 `.sql.gz` 存在且 `gzip -t` 通过；`md5sum` 记录在案 | `docker exec deploy-db-1 pg_dump -U postgres -d skills_world \| gzip > /opt/skills-world/deploy/db-backup-prerestore-$(date +%Y%m%d-%H%M%S).sql.gz`（**目前只有事故前的两份，没有任何事故后备份 —— 这是当前最大的裸奔项**） |
| 2 | live `alembic_version` 仍是 `049_add_policies` | 完全相等 | `SELECT * FROM alembic_version;` |
| 3 | live `residents` 里没有恢复集合的 id | 0 行 | `SELECT count(*) FROM residents WHERE id IN (<21 个 id>);` |
| 4 | live `residents` 里没有恢复集合的 slug | 0 行 | `SELECT count(*) FROM residents WHERE slug IN (<21 个 slug>);` |
| 5 | 12 个 `creator_id` 在 live `users` 里都存在 | 12 | `SELECT count(*) FROM users WHERE id IN (<12 个 creator_id>);` |
| 6 | **这 12 个用户的 `player_resident_id` 仍全为 NULL**（没人在此期间重新 onboarding 出新角色） | 12 行全 NULL | `SELECT count(*) FROM users WHERE id IN (<12>) AND player_resident_id IS NOT NULL;` → 必须为 0 |
| 7 | 恢复集合与 live 的 `resident_relations` pair 无重叠 | 0 行 | `SELECT count(*) FROM resident_relations WHERE (party_a,party_b) IN (<98 对>);` |
| 8 | 恢复集合的 `conversations.id` / `messages.id` 在 live 不存在 | 0 / 0 | 按 id 集合 count |
| 9 | 磁盘余量 | ≥1 GB（载荷 ~20 MB，含 WAL 余量充足；当前 `/` 146G available） | `df -h /var/lib/docker` |
| 10 | agent tick 间隙 / 或临时停 agent-worker | 规避 §7 不确定点 3 的 `uq_resident_relation_pair` 竞态 | 可选；若停则恢复后 `docker compose up -d agent-worker` |
| 11 | 试跑用 `ROLLBACK` 版确认行数 | 输出与 §8.3 逐行一致 | §8.2 第一条命令 |
| 12 | 成本决策已拍板 | §6.5 的日花销翻倍已被接受、或已上调 `BUDGET_GLOBAL_DAILY_USD`、或已决定分批 | — |
| 13 | 名录污染决策已拍板 | §6.6 —— 接受公共名录 11→23（含 12 个默认名），或先给 `list_residents` 加过滤 | — |

**恢复后验收（跑一遍再宣布完成）**

```sql
BEGIN; SET TRANSACTION READ ONLY;
SELECT count(*) FILTER (WHERE resident_type='player') players, count(*) total FROM residents;      -- 期望 12 / 32
SELECT count(*) FROM users WHERE player_resident_id IS NOT NULL;                                    -- 期望 12
SELECT count(*) FROM users u LEFT JOIN residents r ON r.id=u.player_resident_id
 WHERE u.player_resident_id IS NOT NULL AND r.id IS NULL;                                           -- 期望 0
SELECT count(*) FROM memories m LEFT JOIN residents r ON r.id=m.related_resident_id
 WHERE m.related_resident_id IS NOT NULL AND r.id IS NULL;                                          -- 期望 0
SELECT count(*) FROM (SELECT slug FROM residents GROUP BY 1 HAVING count(*)>1) x;                   -- 期望 0
COMMIT;
```

外加**真实用户路径验证**（不能只看 SQL 绿）：用 `176a210c` 的角色 slug 打一次公开接口，确认角色页能出来：

```
curl -s https://simverse-api.proxypool.eu.org/residents | jq -r '.[].slug' | sort   # 期望看到 p-新居民-d7de95 等
```

---

## 9. 清理确认

### 9.1 本地临时库已删除

```
=== BEFORE cleanup ===
p0f-restore-pg16 pgvector/pgvector:pg16 Up 30 minutes 0.0.0.0:55432->5432/tcp, [::]:55432->5432/tcp
p0f-restore-pgdata local
=== docker rm -f ===
p0f-restore-pg16
=== docker volume rm ===
p0f-restore-pgdata
=== AFTER cleanup (both must be empty) ===
container:[]
volume:[]
port 55432 listener:[]
```

容器 `p0f-restore-pg16`、命名卷 `p0f-restore-pgdata`（连同其中的 `skills_world_backup` / `skills_world_livecopy` / `skills_world_livecopy2` 三个库）均已删除，端口 55432 已释放。本机其他 docker 栈（`simverse-lab-*` / `simverse-world-*` / `pgrescue-test` / `simverse-redis` 等）**未被触碰**。

本机 `/tmp/p0f/` 下的中间产物（备份副本、live 只读 dump、提取出的 TSV 载荷）含用户撰写内容，取证结束后一并删除；需要时按 §8.1 的命令重新生成即可。

### 9.2 vm212 未被改动（收工复核）

```
=== containers still up, uptimes unchanged ===
deploy-api-1          Up 11 hours
deploy-agent-worker-1 Up 11 hours
deploy-db-1           Up 26 hours (healthy)
deploy-redis-1        Up 26 hours (healthy)
（sub2api / ollama / cloudflared 同样未受影响）

=== backup file untouched ===
-rw-r--r-- 1 root root 72559925 Jul 25 16:46 /opt/skills-world/deploy/db-backup-roster-20260725-164629.sql.gz
ab517007deb897f8c1848d62ba33b1b9   (与取证开始时一致)

=== live db state unchanged ===
 residents | users | linked |     alembic
-----------+-------+--------+------------------
        11 |    45 |      0 | 049_add_policies
```

`residents 11 / users 45 / linked 0 / alembic 049_add_policies` 与取证开始时完全一致 —— **本线对 vm212 零写入。**
