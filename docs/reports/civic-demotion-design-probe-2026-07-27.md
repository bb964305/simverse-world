# F2 公民权撤销机制 · 多视角失效模式探查报告

> 2026-07-27。5 个视角并行探查 + 逐条对抗验证（默认怀疑，站不住的不进 spec）。
> 59 条发现存活，3 条被证伪（见文末）。本文是 `docs/PARALLEL_WORKSTREAMS_2026-07-27.md` §4 的推导过程与证据，
> spec 只保留结论；实现时以本文的 file:line 为准。

## 4. 线 F2 · 公民权晋升 + 撤销（降级／逐出的第一档）

> 本节替换原 §4 全文。原 §4「晋升是单向的，v1 不做降级（YAGNI）」与「零迁移」两条边界被本节显式改写，理由见 4.1 / 4.6。

### 4.0 结论先行

| # | 决策 | 一句话理由 |
|---|---|---|
| D1 | **状态模型 = 出身（provenance）× 档位（standing）二维**；v1 档位仍编码在 `resident_type` 单列，**不加列、不加取值**；对外只暴露 `civic_membership` 的派生函数与**两个写入口** | 今天 13 处 type 读已全部走 `Resident.is_autonomous` / `Resident.is_civic_voter`（`backend/app/models/resident.py:92-125`），扩档位时只改 hybrid 表达式，调用点零改动 |
| D2 | **撤销 v1 = 事件驱动**（显式调用 + 必填 reason），**不做「门槛反向触发的自动降级」**；自动档位下滑单列开关 `CIVIC_AUTO_DEMOTION_ENABLED`，默认关，开启必须带滞后三件套 | 晋升门槛读的是会被周衰减推着走的 `familiarity`，升降共用判据 = 制造抖动源；而用户要的「违规逐出」本来就是事件驱动 |
| D3 | 撤销是**有序复合事务**：先卸民选职务与三处镇长表示、再改档位；**劳动职务不受撤销影响** | `offices` 表把政治职务与劳动职务混在一张表（`backend/app/services/office_service.py:41-46`），一刀切会误伤 town_clerk / postman / doctor |
| D4 | 在途投票用「**开票时冻结分母**」解决，不实现撤票；幽灵票写成设计语义 | 撤票要改 `options_json` 形状且要兼容存量 poll；冻结分母对晋升与撤销同时免疫，改动局限在 `civic_service` |
| D5 | 撤销射程是**白名单**：只有携带 F2 晋升记录的归化公民可被撤销；`player` / 内置阵容 / `preset` 一律 **raise 拒绝**（对标 `PlayerPurgeRefused`） | 07-25 的教训是「拒绝，不是静默跳过」（`backend/seed/reset_builtin_residents.py:60-114`） |
| D6 | 「可回滚」硬门由**新表 `civic_standing_history`** 承载；§4 的「零迁移」边界改为「**零数据迁移**，允许一次纯建表 additive migration，且该迁移不得与开闸同批」 | `meta_json` 有 7 个 read-modify-write 写入方（见 4.6），滞后状态被静默覆盖 = 最短任期／冷却期静默失效，且只在并发窗口发生、测试抓不到 |
| D7 | 上线切成**四次独立变更**：建表迁移 → T2 回填 → F2 代码合入（flag off）→ shadow 观察 → 开闸 | 首夜爆炸半径不可预演；红线要求迁移／回填与开闸分离 |
| D8 | `resident_type` 收敛为**唯一写入口**；F2 独占文件**显式扩展**到 `backend/app/routers/admin/residents.py:117-118`（仅这一处赋值改为调用写入口） | 列上没有 CHECK，代码就是最后一道闸；不封这一处，「唯一写入口」条款是假的 |

---

### 4.1 状态模型：出身 × 档位

**维度 A · 出身（provenance）——冻结，业务代码不再改写它**

`resident_type` 的四个取值今天各有唯一创建路径，本轮之后除 admin 纠错外不再被业务改写：

| 取值 | 创建路径 | provenance 判定 |
|---|---|---|
| `npc` | `backend/seed/preset_characters.py:1259`（`creator_id=SYSTEM_USER_ID`，`:1253`；`meta_json={"origin":"preset","is_preset":True}`，`:1237`） | 内置阵容 |
| `resident` | forge / import 五处，全部写 `UGC_RESIDENT_TYPE`（`backend/app/forge/pipeline.py:161`、`legacy_pipeline.py:151`/`:304`、`backend/app/routers/residents.py:185`/`:291`），`meta_json.origin ∈ {forge, import, quick_forge}` | 玩家创作（UGC） |
| `player` | `backend/app/services/onboarding_service.py:81`，同时写 `users.player_resident_id` | 玩家化身 |
| `preset` | `backend/app/routers/admin/residents.py:163`（默认值来自 `backend/app/schemas/admin.py:129`） | admin 创建 |

**维度 B · 档位（civic standing）——有序三档**

```
citizen  有票 · 在镇 · 被 loop 驱动          ← 晋升终点
denizen  无票 · 在镇 · 被 loop 驱动          ← 降级落点（本轮实现）
exiled   无票 · 不在镇 · 不被驱动 · 不在地图  ← 逐出落点（本轮不实现，见 4.8）
```

正好对应「降级与逐出是同一套机制的不同强度档」。

**v1 的落地形态（零列变更）**：档位仍由 `resident_type` 的 `npc` / `resident` 编码，但**任何业务代码不得再直接读写 `resident_type`**，一律走 `backend/app/services/civic_membership.py` 新增的三个 API：

```
civic_standing(resident) -> Literal["citizen", "denizen", "exiled"]
    # "exiled" 分支现在就写进枚举并 raise NotImplementedError —— 逐出上线时是填空不是改签名

grant_citizenship(db, resident, *, reason, actor, evidence)  -> bool
revoke_citizenship(db, resident, *, reason, actor, tier="demote") -> bool
    # tier: "demote"（本轮）| "exile"（占位，v1 raise NotImplementedError）
```

同时新增常量 `CIVIC_MEMBER_TYPE = "npc"`，禁止任务里出现裸字面量——`resident_type` 是裸 `String(20)`、无 enum 无 CHECK（`backend/app/models/resident.py:55`），写错一个字符（`"npc "`）就同时掉出两个集合，居民从 agent loop、市政厅名册、职务查找、mayor 清扫里一起消失（`backend/app/services/civic_membership.py:22-23`）。

**为什么不新增第 5 个 type（例如 `"exiled"`）**：它会同时掉出 `SIM_RESIDENT_TYPES`，而地图与感知**不读 type**——公开名录是全表（`backend/app/services/resident_service.py:6-18`，`backend/app/routers/residents.py:60-67` 直接透传）、tile 占用也是全表（`backend/app/services/resident_placement.py:104-111`、`:157-160`）。结果是「被逐出者仍在地图上、仍被别人搭话，只是自己不再 tick」的活体雕像，而不是离开小镇。逐出要收窄的是第四族谓词（4.8），不是这两个集合。

**遗漏收口（F2 开工第一步）**：`backend/app/services/reputation_service.py:74` 是裸的 `Resident.resident_type == "npc"`，既不走 `is_civic_voter` 也不走 `is_autonomous`——这是 `civic_membership` 收口时漏掉的第 11 处读。它必须归到**人口口径**（声誉是社会属性不是政治权利）改成 `Resident.is_autonomous`，否则被降级者退出夜间声誉重算、分数永久冻结在降级前那一刻，而 `backend/app/services/election_service.py:53-60` 的候选排序读的正是这个冻结值；更要命的是未来「违规扣声誉」若先改档位再扣分，扣分动作会因这行字面量永远不生效。F2 开工前先做一次全仓 `resident_type` 字面量分类，任何未归类的 `== "npc"` 都是半状态源。

---

### 4.2 晋升判据（门槛口径保持不变，但补两条硬语义）

两个条件同时满足：

- **①在镇世界日** ≥ `CIVIC_PROMOTION_MIN_WORLD_DAYS`
- **②与至少 `CIVIC_PROMOTION_MIN_PEERS` 位锚定公民建立 `familiarity ≥ CIVIC_PROMOTION_MIN_FAMILIARITY` 的关系**

**（a）在镇天数必须锚在「公民时钟」，不是 `created_at`**

`Resident.created_at`（`backend/app/models/resident.py:50`）是真实 UTC 时间。公式写死为：

```python
from app import world_clock
age_world_days = (world_clock.now_world() - world_clock.real_to_world(anchor)) / timedelta(days=1)
```

`real_to_world` 是仿射变换（`backend/app/world_clock.py:67-71`），epoch 相减时抵消，等价于 `k × 真实经过时间`，`k=4`（`backend/app/config.py:211`）。

`anchor` **不是** `created_at`，而是「本轮公民资格的起算点」：取 `civic_standing_history` 里该居民最近一条档位变更的世界时间；无任何历史行时回落 `real_to_world(created_at)`。这条是硬性的——若门槛①读 `created_at`，T2 把一个已在镇 200 世界日的 UGC 降权后，F2 开闸当晚它的条件①立刻重新满足，T2 的降权对这批人只是走了个过场。

（注：内置阵容的世界龄已 ≈450 世界日，UGC 新人从 0 开始；标定时不要把两类人放进同一分布看。）

**（b）条件②的同伴必须取自「锚定公民集」，不是活的 `is_civic_voter`**

若同伴集合就是 `is_civic_voter` 本身，判定的转移函数自指且同伴数是整数 → 非单调、多不动点、级联升降；完全脱锚的公民团（某人的 N 位同伴全部是已晋升 UGC、零条内置边）在第二代晋升后可达。

锚定公民集 = 下面两类的并集：

1. **内置阵容**：`creator_id == SYSTEM_USER_ID`（`backend/seed/preset_characters.py:20`/`:1253`）且 `is_civic_voter`
2. **已过考察期的归化公民**：有晋升记录且 `now_world − promoted_at ≥ CIVIC_PEER_SEASONING_WORLD_DAYS`

> ⚠️ 不要用 `meta_json.origin == "preset"` 判内置——admin 创建的 preset 也写 `{"origin":"preset"}`（`backend/app/routers/admin/residents.py:148`），两者同值。provenance 判定以 `creator_id == SYSTEM_USER_ID` 为主键，`origin` 只作辅助信号。

**（c）整个 pass 是 snapshot 语义**

pass 开始时一次性读出 `{resident_id: (type, tenure_anchor, qualified_peer_ids)}` 并冻结，所有判定基于快照，所有写入在 pass 末尾一次 commit，中途绝不重读选民集。否则结果依赖数据库行序，同一状态多次运行得到不同不动点。

判定必须做成**纯函数**（输入快照 → 输出待升／待降 id 集合），测试用 `random.shuffle` 打乱内存中的居民列表再跑，断言输出集合恒等——不要试图在 Postgres 上控制行序。

**（d）F2 第 0 步：只读标定**（保留原 §4 的纪律，补三项）

标定要产出的东西：

| 要测的分布 | 用途 |
|---|---|
| UGC 居民的在镇世界日分布 | `CIVIC_PROMOTION_MIN_WORLD_DAYS` |
| 每位 UGC 居民对锚定公民的 **top-N familiarity 分布**（不只是达标计数） | `CIVIC_PROMOTION_MIN_FAMILIARITY` / `MIN_PEERS` |
| 当前公民总数 | `CIVIC_MIN_ELECTORATE`、单次上限、熔断阈值 |
| **生产 `REALISM_RELATIONS_ENABLED` 实际取值** | 见下 |

三条硬约束：

- `θ` **不要取 0.3**——`realism_circle_threshold = 0.3`（`backend/app/config.py:512`）是圈子检测的强边阈值，撞上去会让两套语义纠缠。
- **familiarity 的主增长路径被 `realism_relations_enabled` 门控**（`backend/app/config.py:495` 默认 False；`backend/app/agent/chat.py:55-56` 提前 return）。未门控的只有赊饭 +0.02（`backend/app/agent/phases/execute/basic.py:68`）与 arc 完结 +0.05（`backend/app/services/arc_service.py:212-213`）两条噪声路径，衰减也在这个门后面（`backend/app/tasks/nightly_cron.py:365`）。⚠️ **待复验**：项目记忆记录 2026-07-23 部署 vm212 时 `REALISM_*` 四个开关置 true 并在容器内坐实，此后经多轮部署未复验——F2 第 0 步必须登录 vm212 读一次实际取值。若为 false，条件②要么改判据要么先开这个开关，且按红线开闸不得与回填同批。
- 本机 dev 库是空的，**标定不可在本机做**；若 T1 未落地只能先用 dev 库，必须显式标注「待生产数据复标」，不得直接开闸。

**（e）已知的结构性偏置（写进风险项，不是阈值问题）**：`extravert` 档的 `SpontaneousDecidePlugin` 用加权采样挑聊天对象，权重与既有熟识度正相关，系统性歧视新人。若标定发现晋升面长期为空，根因可能在采样而不在阈值——所以观测面要输出 top-familiarity 分布而非只输出达标计数。

---

### 4.3 撤销的触发条件与防振荡

**v1 的触发是事件，不是门槛**

```
grant_citizenship   ← 夜间任务（门槛驱动，单调只升）
revoke_citizenship  ← 显式调用（必填 reason + actor），v1 的调用者只有 admin 路由与未来的司法/违规动作
```

夜间任务**只做升，永不自动降**。理由：门槛②读的 `familiarity` 有唯一下行力（周衰减 `backend/app/services/relation_service.py:206-224`），把它接成降级判据，等于让公民权跟着社交衰减飘；而用户要的「违规被逐出」本来就是显式事件，不需要门槛反推。

**若将来要开自动下滑降级（`CIVIC_AUTO_DEMOTION_ENABLED`，默认关），必须同时具备三件套，缺一不可：**

| 机制 | 参数 | 下限（形态，不拍数字） |
|---|---|---|
| **滞后区间** | `θ_down = θ_up − Δ`；同伴数 `P_down = P_up − 1` | **Δ 必须严格大于单次最大相关增量 0.05**（`backend/app/config.py:499` 聊天 +0.05；`arc_service.py:213` 硬编码 +0.05），建议 **Δ ≥ 0.10**（两次聊天）；同伴数是整数，必须整位留白 |
| **最短任期** | `CIVIC_MIN_TENURE_WORLD_DAYS`：晋升后此期内不得降级 | **≥ 12 世界日**——一张 poll 开 `civic_poll_days = 3` 真实天（`backend/app/config.py:539`）= 12 世界日，小于它则单张 poll 生命周期内仍可翻转 |
| **冷却期** | `CIVIC_PROMOTION_COOLDOWN_WORLD_DAYS`：降级后此期内不得复升 | **≥ 12 世界日**，同上 |

三个值一律走 `world_clock`，不得用真实日。注意衰减用的是真实日（`realism_rel_decay_idle_days = 30`，`backend/app/config.py:504`）而门槛用世界日——**这是有意的两套尺度**，spec 在此显式声明，实现不得擅自统一。

**关于抖动烈度的准确口径**（写进 spec 免得被当成夸大）：decay 由世界周闸门控制（`backend/app/tasks/nightly_cron.py:359-373`，一个世界周 ≈1.75 真实天放行一次），且只命中 `last_interact_at` 已闲置 30 真实日的关系；任何一次 `bump` 都会刷新 `last_interact_at`（`backend/app/services/relation_service.py:107-108`）。所以现实形态是「长期不互动 → 跨世界周缓慢掉档，一次互动即回弹」，不是每晚来回震荡。滞后设计的必要性不因此下降（法定人数分母会跟着漂），但不要在 spec 里写成高频抖动。

---

### 4.4 撤销时的同步清理

**分档清理表**（降级与逐出共用一张表，逐出档 v1 不实现）：

| 清理项 | demote（本轮） | exile（占位） |
|---|---|---|
| `resident_type` → `UGC_RESIDENT_TYPE` | ✅ | ✅ |
| `fill_strategy == "election"` 的 offices 行（今天只有 `mayor`，`backend/app/services/office_service.py:42`） | ✅ 卸任 | ✅ |
| `meta_json['mayor']`（工资倍率唯一读点，`backend/app/services/duty_service.py:172-173`，×1.2 见 `backend/app/config.py:547`） | ✅ 清 | ✅ |
| `system_config['current_mayor']` | ✅ 清（仅当指向此人） | ✅ |
| 劳动职务：`town_clerk` / `postman` / `doctor` 的 offices 行 + `meta_json['duty']` | ❌ **不动** | ✅ 全撤 |
| 住房 `home_location_id`、tile 占用 | ❌ 不动 | ✅ 释放（见 4.8） |
| 删行 | ❌ 永不 | ❌ **永不** |

**撤销必须是有序复合动作，顺序不可颠倒**：

```
0. 防呆（Guard first，第一条 UPDATE 之前全部做完）—— 见 4.5
1. 卸民选职务：guard UPDATE  offices SET holder_slug=NULL, term_ends_at=NULL
                WHERE office_key IN POLITICAL_OFFICE_KEYS AND holder_slug = :slug
2. 清 meta_json['mayor']：按 slug 直查该居民，pop + flag_modified
3. 清 system_config['current_mayor']：仅当当前值 == slug
4. 改档位：UPDATE residents SET resident_type=:new WHERE id=:id AND resident_type=:expected
5. 写 civic_standing_history 一行
6. 断言：三处镇长表示都不指向他；is_autonomous 仍 True；is_civic_voter 已 False
7. 广播 civic_standing_changed（见下）
```

四条实现约束，每条都对应一个会让实现落空的坑：

1. **不得调用 `OfficeService.vacate()`**。它自带 `await self.db.commit()`（`backend/app/services/office_service.py:138`），挂不进 F2 的事务；更关键的是 `polis_office_enabled` 默认 False（`backend/app/config.py:556`）时 offices 表根本没有 mayor 行，guard UPDATE 命中 0 行 → `vacated=False` → `_clear_mayor_legacy_stores()` **不会被调用**（`office_service.py:136-137`），两个 legacy store 一点没清。所以步骤 2、3 必须**无条件**执行，offices 侧只是 gate 开时的附加项。F2 自己写 guard UPDATE（只 `import app.models.office.Office`，**不改 `office_service.py`**，避免与 F3 的独占文件冲突）。
2. **清 `meta_json['mayor']` 不得复用 `_clear_mayor_legacy_stores`**（`office_service.py:220-222`）——它的 WHERE 是 `Resident.is_autonomous`，即用「人口集合」去清理「刚离开集合的人」。降级档他还在集合里所以侥幸命中，逐出档就天然自锁。通用约束写死：**凡是清理「已离开集合 S 的居民」的扫描，都不能用 S 本身做 WHERE**——`office_service.py:222` 与 `election_service.py:141` 两处都要按 slug 直查。
3. **guard 必须带 holder 校验**。`polis_office_enabled` 关时 `offices.holder_slug` 可能是迁移 046 遗留的陈旧值，无条件 `vacate("mayor")` 会罢免错的人。⚠️ 这是**实现陷阱不是现存 bug**（`app/` 下今天零个 `vacate` 调用点）；陈旧值是否存在取决于 046 执行那一刻 `system_config['current_mayor']` 有没有值，**T1/T2 现场读一次 offices 表确认，不要假设**。硬约束：**F2 撤销的正确性不得依赖 `polis_office_enabled` 的取值，gate 开与关两种状态都要有测试覆盖**（默认是关，最容易漏测的恰恰是生产以外的那一态）。
4. **顺序不可颠倒**。若先改档位再清理，`meta_json['mayor']` 在逐出档会永久卡死（清扫扫不到他），期间 `install_mayor()` 清他人标志时也会跳过他，可产生「两个 `meta_json['mayor']=True`」并双份工资倍率。这条要写成测试断言。

**在任镇长被撤销后的补选**：F2 只保证出缺并广播事件，补选由 F3 的钩子接手（收口时接线）。允许的空缺上限 = **1 个夜间周期**，超出由探针报红旗。注意 `polis_office_mayor_term_days = 0`（`backend/app/config.py:560`）意味着现任永不到期，`term_check` 又被 gate 整段跳过（`backend/app/tasks/nightly_cron.py:258-260`）——**gate 开与关都没有自动收回路径**，撤销是唯一的下台方式。另：候选池读 `is_civic_voter`（`backend/app/services/election_service.py:40`），被降级者下届不再进候选但当届仍在任，正好印证「不卸任就下不了台」。

**`install_mayor` 的两处收口（归属真空，spec 必须点名归属线）**

`backend/app/services/election_service.py:135-193` 落在 F1 声明的独占区 `:53-60` 之外，F1/F3 都不覆盖它，容易两边都不动。本节把它划给 **F2**：

- **结票时复核资格**：用 `Resident.is_civic_voter` 而非 `is_autonomous`（`:141`）解析 winner；不合格直接 `return False` 且**不做任何写**，由 `_close_one` 走「当选人已失去资格，本案流会」的公告分支（复用 `_VERDICT_NOTE`，`backend/app/services/civic_service.py:521-526`）。通用约束：**候选资格开票时快照、结票时复核，快照不构成信任**。
- **事务化**：现状是先 `await db.commit()`（`:158`）再判 `winner is None`（`:160-161`），winner 解析失败时旧镇长的 `meta_json` 已被清掉、`system_config` 与 offices 却仍指向他，留下三向分歧。改成「先解析 winner，失败立即 return False 且零写入；旧镇长清理与新镇长安装同一事务同一次 commit」。触发条件今天就可达（目标 slug 查不到即可），不必等逐出。

**在途投票：冻结分母（方案 A），不撤票**

- `propose()` / `open_election()` 开票时把当时的合格选民数快照进 `options_json[0]['_eligible_at_open']`；`_policy_threshold_verdict`（`backend/app/services/civic_service.py:541-564`）读快照而非实时 `_eligible_voter_count()`（`:529-538`）。
- 幽灵票**保留**，写成设计语义：「**投票时具备资格即计票**」。`_npc_voters` 是 `options_json[0]` 上的扁平 slug 列表（`civic_service.py:165`/`:173`），物理上没存票的归属，撤票要改结构 + 兼容存量 poll，不值当。
- 顺带修掉既有口径错配：tally 把 `votes` 表的玩家票并入 total（`civic_service.py:472-479`），而分母只数 residents（`:535-538`），分子分母本来就不同源。

**适用面必须写清楚**（否则会被以「默认 False」驳回）：threshold / quorum 整段只在 `polis_policy_approval_enabled`（`backend/app/config.py:612`，默认 False，**vm212 为 true**）为真、且 `options_json[0]` 带 `META_THRESHOLD` 时才计算（`civic_service.py:491-492`、`:552-554`），quorum 还要额外带 `META_QUORUM`（`:558`）。普通 civic poll 与镇长选举 poll 走纯 plurality（`:476-484`），分母不参与判决——撤销对它们的影响是票差而非流会。

**真正会翻转的算例**（写进测试）：10 位选民 / 4 票 → `4 < 10×0.5` 判 `quorum_not_met`；降掉 4 位已投票者后 eligible=6、total=4 → `4 < 3` 为假 → 通过。

**顺带修掉的一处冗余**：`civic_service.py:560` 的 `eligible > 0` guard 在 `total <= 0` 已提前返回（`:556-557`）的前提下恒无影响，去掉结果完全一样。它不是 bug 成因，但建议改成显式 fail-closed 或告警——安全阀在分母为 0 时自己关掉，语义上说不通。

**接线位置是语义决策，spec 在此写死**

cron 内顺序固定：`close_due_polls`(`backend/app/tasks/nightly_cron.py:215`) → `seed_civic_agenda`(`:226`) → `maybe_open_seasonal_election`(`:237`) → `run_npc_voting`(`:247`) → office `term_check`(`:263`)。

> **`civic_promotion` 接在 `close_due_polls` 之后、`run_npc_voting` 之前（≈`nightly_cron.py:245`）。**

理由：当晚晋升、当晚补投，新公民参与的第一次关票分子分母同源。接在末尾并不能消除危害，只把它推迟一晚——因为每晚 close(215) 先于 vote(247)，夜 N 末尾晋升的人在夜 N+1 关票时仍然是「进了分母、一票未投」。收口接线时用与 `nightly_cron.py:142-145`（opinion drift 顺序硬约束）同样的注释形式锚住位置，防止后续批次随手挪。

对应的回归测试要按 **N+1 晚**断言，不能只断言当晚 verdict 不变（那条会绿而 bug 在下一晚复现）。

撤销任务因为是无人值守夜跑、没有人工挑窗口的机会，**由方案 A（冻结分母）覆盖**；在方案 A 落地前，撤销任务必须在有 `status == "open"` 的 poll 时整批推迟，并对「连续推迟 N 晚」告警（`seed_civic_agenda` 按 topic 一次性去重不会持续开票，但 `maybe_open_seasonal_election` 与政策提案会，存在饿死风险）。

**WS 事件名**：用 `civic_standing_changed`，payload 为 `old_standing / new_standing / reason_code`（**只发不敏感的枚举码，不发 reason 文本**）。⚠️ **不得叫 `resident_type_changed`**——该名字已被 SBTI 人格类型漂移占用（`backend/app/ws/handlers/chat.py:474-482`），复用会让前端把政治事件渲染成人格变化。挂 world_revision / seq 与开关的写法参照 `backend/app/services/office_service.py:244-271`；注意那是易失的 WS 扇出、**不落任何表**，不能拿它当「可回滚」硬门的载体。

---

### 4.5 安全约束与防呆（对标 `PlayerPurgeRefused`）

**绝对不可被 F2 任务碰的居民**（命中即 **raise，不是跳过**）：

| 类别 | 判定 | 理由 |
|---|---|---|
| 玩家化身 | `resident_type == "player"` **且**查库复核 `users.player_resident_id`（`backend/app/models/user.py:30`） | 07-25 事故对象 |
| 内置阵容 | `creator_id == SYSTEM_USER_ID` | 内置被降 = 选举与法定人数熄火；`polis_office_mayor_term_days=0` 下真实稳态是「现任镇长被永久冻结、再也选不出新人」 |
| admin preset | `resident_type == "preset"` | 不在两个集合内，本来就不该被政治层动 |
| 无晋升记录者 | `civic_standing_history` 里查不到 `to=citizen` 的行 | 撤销是晋升的**严格逆操作**，白名单而非泛谓词 |

**异常照抄 07-25 的两条设计选择**（`backend/seed/reset_builtin_residents.py:93-101`）：

- **Raise，不 skip**——静默跳过会让调用方以为动作完成了；
- **读数据库，不信传入对象**——offending 调用点自己建的目标列表，`target.resident_type` 恰恰是不能信的字段。

新增 `CivicStandingRefused(RuntimeError)`，在**第一条 UPDATE 之前**抛出（照抄 `reset_builtin_residents.py:125-127` 的 "Guard first: no DELETE has run yet" 姿势），使拒绝是真正的 no-op。

**批量写形态**（今天仓库里对 `resident_type` 零个批量 UPDATE，F2 是第一个；唯一并发对手是 admin 手改 `backend/app/routers/admin/residents.py:117-118` 的读-改-写）：

```sql
UPDATE residents SET resident_type = :new
WHERE id IN (:ids) AND resident_type = :expected
```

`rowcount != len(ids)` → **整批回滚 + 告警**（有人在窗口内改过）。正面样板：`backend/app/services/relation_service.py:214-223`、`backend/app/services/office_service.py:128-135`；反面样板：`backend/app/routers/admin/residents.py:103-127`。

**四道数值闸门**：

1. `CIVIC_PROMOTION_MAX_PER_RUN` / `CIVIC_DEMOTION_MAX_PER_RUN`（建议 5）——单夜移动分母有上限；
2. **熔断**：候选集 > 当前公民数 × 20%（或 > 绝对值 N）→ **整批拒绝并告警，不截断执行**。截断会掩盖「阈值写反」这类全量误判；
3. **选民下限不变式**：撤销后 `is_civic_voter` 计数必须 ≥ `max(CIVIC_PROMOTION_MIN_PEERS + 1, CIVIC_MIN_ELECTORATE)`，`CIVIC_MIN_ELECTORATE ≥ 3`（`open_election` 需要 ≥2 候选，`backend/app/services/election_service.py:62-63`）；不满足整批拒绝并 WARN。这条不变式在未来做逐出时同样成立——**逐出内置成员必须撞同一道墙**；
4. **取值白名单断言**：`new_type` 与 `expected_type` 都必须 `in SIM_RESIDENT_TYPES` 且取自 `civic_membership` 导出的常量，不满足直接抛异常。

**结构性守卫（测试）**：仿 `backend/tests/test_ugc_resident_no_political_rights.py:69-88` 的 AST 扫描，把覆盖面从「`Resident(...)` 构造」扩展到「`*.resident_type = ...` 赋值」，断言除 `civic_membership` 的两个转移函数与既有创建路径外，`app/` 下无人给 `resident_type` 赋值。

**admin 路由收口（D8）**：`backend/app/routers/admin/residents.py:117-118` 的裸赋值改为调用 `grant_citizenship` / `revoke_citizenship`（或直接拒绝该字段）。这**扩展了原 §4 的独占文件清单**——不扩展就意味着有一条完全不受 F2 管辖、零校验零清理的变更入口，「唯一写入口」条款失效。同时明确写进文档：admin 手工把某人改回 `npc` 会在探针上显示为「无晋升记录的 UGC-origin 公民」——这正好是一条有用的红旗，不是噪声。

⚠️ **不采纳的一条论断**（防止被写进实现）：「把误改成 npc 的玩家化身降级为 resident 会让 agent loop 开始驱动它 / 让装修权反转」是误读——`npc` 与 `resident` 同属 `SIM_RESIDENT_TYPES`，也同时满足 `!= "player"`（`backend/app/routers/home_decor.py:56`、`backend/app/agent/map_data.py:475`），危害在 admin 手滑那一刻就已发生。但 `player` 仍然必须被 raise 拒绝——理由是**射程纪律**，不是这条危害链。

---

### 4.6 与 T2 存量回填的顺序

**四次独立变更，顺序不可合并：**

```
① 建表迁移（纯 DDL，零数据行为）   ← F2 交付的第一步，必须先于 T2
② T2 存量回填（一次性脚本，数据变更）
③ F2 代码合入（双开关默认 off，零数据写）
④ shadow 观察 ≥ 3 个夜间周期 → 开闸（单独一次变更，只翻开关）
```

**① 建表：`civic_standing_history`（additive，只建表不动存量行）**

形状照抄仓内先例 `backend/app/models/personality_history.py`：

```
civic_standing_history(
  id PK, resident_id FK→residents.id (index),
  old_standing, new_standing,          # citizen / denizen / exiled
  reason Text NULL, reason_code,       # code 可外发，text 不可
  actor,                               # civic_promotion | civic_demotion | admin:<id> | ops_backfill_t2
  evidence_json,                       # {world_days, peers, min_familiarity}
  created_at,
  Index(resident_id, created_at)
)
```

这**修改了原 §4 的「零迁移」边界**，改为「零数据迁移；允许一次纯建表 additive migration，且不得与开闸同批」。理由：

- 硬门「可回滚（晋升动作留下可反向的记录）」需要一个载体，而三个候选里只有它成立——`meta_json` 有 7 个 read-modify-write 写入方（`reputation_service.py:124`、`circle_service.py:117`、`office_service.py:231`、`duty_service.py:213`、`social_status_recovery.py:115`/`:125`、`election_service.py:155`），agent loop 也在同一批居民上写，滞后状态被覆盖 = 最短任期与冷却期**静默失效**、只在并发窗口发生、测试抓不到；`_emit_office_changed` 是易失 WS 广播、不落表，不是记录。
- `meta_json` 还是 `sa.JSON` 而非 jsonb，无法建索引；且它由多个无鉴权前台接口原样公开（`frontend/src/components/NpcTooltip.tsx`、`BulletinBoard.tsx`、`SearchDropdown.tsx`），**撤销原因文本绝不能放进去**。
- 它同时是 4.2(a) 的「公民时钟锚点」载体：tenure anchor = 最近一条历史行的世界时间，无行则回落 `created_at`。T2 写一行 = 双字段写入等价物，不需要在 `residents` 上加列。

⚠️ 若运维时序不允许建表先于 T2：F2 首次读取时把「无任何历史行的 UGC 居民」的 anchor 取 `max(created_at 对应世界时间, T2 完成标记的世界时间)`，标记见下。这是**降级方案**，spec 里要写清哪个路径生效。

**② T2 的三条硬约束**

1. **目标谓词排除已晋升者**：`meta_json.origin ∈ {forge, import, quick_forge}` **OR** `creator_id` 为真实用户 id，**AND** 无 `to=citizen` 的历史行。⚠️ 不要用 `creator_id` 单条判定——迁移 045 让账号注销后它变 NULL（`backend/alembic/versions/045_residents_creator_nullable.py`），内置阵容是 `SYSTEM_USER_ID`，admin preset 写字面量 `"system"`，三值混合；也不要用 `origin` 单条一刀切——极老的 UGC 行不保证带 origin。残差人工点名复核。
2. **不可重放**：执行后在 `system_config` 写 `civic_backfill_done=<world_date>`（`ConfigService(db).set(key, value, group=, updated_by=)`，用法见 `backend/app/services/election_service.py:163-166`）；脚本启动时若标记已存在且未带 `--force-rerun` 则**拒绝退出**。F2 上线后重放一次 T2 = 大规模剥夺公民权且零告警。
3. **必须是进仓库、被评审、`--dry-run` 为默认值的 `backend/scripts/` 脚本**，禁止在 vm212 上手写一次性 SQL——07-25 的根因正是「hand-written roster migration 绕过 `find_targets` 自带 id 列表」。

**③ T2 与 F2 共用同一份「谁是 UGC」判定，但触发路径分离**

判定函数落在 `backend/app/services/civic_membership.py`（新增 `UGC_ORIGINS = frozenset({"forge","import","quick_forge"})` + `is_ugc_resident()` / `ugc_filter()`），T2 脚本与 F2 任务都从这里 import。两边各写一份必然漂移：T2 降了一批、F2 认为其中一部分不是 UGC 因而永不晋升 → 永久二等公民，正是本线要修的问题复发。但**二者不得互相调用、不得由同一次部署同时首跑**：T2 是一次性脚本，F2 是 cron。

**④ shadow 模式（第三态）**

`CIVIC_PROMOTION_MODE ∈ {off, shadow, on}`：shadow 执行完整候选计算与**全部防呆检查**，把「本晚会晋升/撤销的名单 + 每人的 tenure / peers / familiarity 证据」写进日志与探针，**不执行任何 UPDATE**。生产至少观察 3 个夜间周期，名单规模与标定预期一致才进开闸。

理由准确写法：**首夜爆炸半径未知且不可预演，shadow 是带全部防呆的实跑演练 + 名单落盘**。不要写成「规模在开闸前无人知晓」——4.2(d) 的只读标定本来就可测出候选规模；也不要把「功能开闸后由功能自身写自己的数据」说成撞红线，红线针对的是把数据迁移与行为变更打包进同一次变更。

---

### 4.7 可观测性（硬门「晋升可观测」的具体落法）

现有探针（`backend/scripts/burnin_report.py:1176-1282`）只输出按 `resident_type` 分组的静态计数，对 F2 的核心失败模式全盲：误升只会让 npc 计数变大，`leaked_voter_types` 判的是**常量集合被拓宽**（`civic_boundary_breakdown`，`:1234-1236`），F2 只改行值不改集合，永远不触发。07-25 是靠人眼看出「npc 该是 10 人却有 13 人」发现的——晋升让 npc 计数合法增长后，这条人工嗅觉也失效。

**F2 与晋升逻辑同批**升级探针，四项新增 + 一项升级：

1. **交叉表 `resident_type × provenance`**：provenance 由 `creator_id == SYSTEM_USER_ID` 判定，输出三行口径——内置 npc / 已晋升 UGC / 未晋升 UGC。判泄漏的条件改为「provenance=UGC 且 `is_civic_voter` 为真、但 `civic_standing_history` 里查不到晋升记录」。
2. **晋升队列**：满足门槛但仍是 `resident` 的人数（= shadow 模式的候选名单大小）。
3. **翻转统计**（静态计数发现不了振荡——11 内置 + 3 归化的读数在 X 升 / Y 降的同一夜看起来完全正常）：每位居民累计晋升/撤销次数、**最近 7 世界日内发生翻转的居民数**、当前处于最短任期或冷却期内的人数。**「最近 7 世界日翻转数 > 0」定为告警条件而非信息项**——滞后设计生效后稳态下这个数应恒为 0。
4. **交叉一致性探针**（只读、零 LLM、复用现有渲染形状）：
   - ①每个 `fill_strategy='election'` 的 `offices.holder_slug` 对应居民 `is_civic_voter` 为真（**只对民选职位断言**——`town_clerk` / `postman` / `doctor` 是劳动职务，UGC 居民担任它们是既定边界，不是红旗）；
   - ②带 `meta_json['mayor']` 的居民集合 == `{offices.mayor.holder_slug}` == `{system_config['current_mayor']}`（三者同一或同为空）——⚠️ 这条**必须按 `polis_office_enabled` 分档**，gate 关时 offices 是 046 遗留值，不分档会在 T2 前直接报红并被当噪声关掉；
   - ③每个 open poll 的 `_npc_voters` 全员当前仍是 `is_civic_voter`，否则输出幽灵票数；
   - ④所有 `offices.holder_slug` 都能在 residents 表查到（`purge_residents` 不清 offices 与 `current_mayor`，见 4.8）。
5. **`unknown_types` 非空从 ⚠️ 升为红旗**（`burnin_report.py:1278-1281`）——这是未来引入新 type 时唯一的自动发现口，也是写错一个字符的唯一兜底。

第 3 项需要载体：F2 任务把每次运行的结果（晋升数 / 撤销数 / reason 分布）写进 `system_config` 或直接由探针聚合 `civic_standing_history`——探针今天只吃一张 by_type 快照，加个 SQL 维度是不够的。

**这套探针在 T2 回填前后各跑一次，读数同时充当 T2 的验收证据。**

---

### 4.8 前向兼容：为「违规逐出」预留什么，刻意不做什么

**预留（现在就写进代码/spec，零行为变化）**

| 预留项 | 形态 | 落点 |
|---|---|---|
| `exiled` 档位 | `civic_standing()` 的返回枚举里现在就写上该分支并 `raise NotImplementedError` | 逐出上线时是填空不是改签名 |
| `revoke_citizenship(tier=...)` | 参数现在就有，`tier="exile"` 走 NotImplementedError | 分档清理表（4.4）已按两档写好 |
| **第四族谓词 `Resident.is_in_town`（D 类·世界可见性）** | **v1 不实现**（此时恒真、零行为变化），但**现在就命名并写死语义** | 语义 = 「出现在公开名录/地图 + 占用住房 + 占用 tile」，三处口径统一开关 |
| `civic_ban`（sticky 剥夺位） | v1 **只留状态位不实现**：晋升任务的候选面从第一天起就写成「先排除 ban 命中者」 | 见下 |

**`is_in_town` 的三处落点**（都要在 docstring/模块头记入「今天是全表，逐出时在此收窄」）：

- 公开名录：`backend/app/services/resident_service.py:6-18`（`list_residents` 全表无过滤，`backend/app/routers/residents.py:60-67` 直接透传）——今天玩家化身也被返回并当 NPC 精灵渲染，可佐证「地图 = 全表」是字面真实；
- tile 占用：`backend/app/services/resident_placement.py:104-111`（`_is_tile_occupied`）、`:157-160`（`_find_available_tile`），两处全表且完全不读 `resident_type`；
- 住房：`allocate_home` 的统计口径。⚠️ 注意 `allocate_home` 与 `_is_tile_occupied` **今天口径就不一致**（前者排除 player、后者不排除），逐出实现时必须显式选定一种并写进测试，否则引入新的不一致。

**`civic_ban` 的理由**：自然下滑降级**应当**可逆（重新融入就该复籍），而违规逐出**必须**不可逆于同一判据——否则被逐者只要在冷却期内和几个 npc 聊够 familiarity 就自动升回。若只用一个 `resident_type` 表达两种状态，晋升任务无法区分「因疏远而降」和「因违规而逐」，下一遍必然把后者纳入候选面。**这条即使 v1 不实现逐出也要先把状态位留出来**，否则 F2 落地后做逐出只能靠改 F2 的判据。

**账号封禁与角色逐出是两层，互不自动传导**：`users.is_banned`（`backend/app/models/user.py:27`，走 admin 路由与 middleware）是 OOC 层「这个人类不能操作」，与「这个角色被小镇驱逐」不同层。今天一个被封号用户名下的 UGC 居民照常被 loop 驱动、照常可被晋升。spec 显式声明两层不传导，并把「封号用户名下居民是否自动降级/冻结」列为**待决项而非默认行为**——否则某次封号操作会意外触发一次批量政治层变更，正是红线窗口。

**刻意不做（YAGNI，写清楚免得被当遗漏）**

- ❌ 不新增第 5 个 `resident_type` 取值（理由见 4.1）；
- ❌ 不做自动下滑降级（开关留着，默认关）；
- ❌ 不实现撤票（用冻结分母代替）；
- ❌ 不实现 `is_in_town` 收窄、不释放住房/tile（这是逐出档的副作用，也正是两档强度的可观测差别之一）；
- ❌ 不给 `civic_standing_history` 加任何读接口（避免撤销原因被公开）；
- ❌ **永不 DELETE**：逐出必须是软状态 + 副作用清单，绝不复用 `purge_residents`（`backend/seed/reset_builtin_residents.py:117-165`）或其中任何一段级联——那正是 07-25 毁掉 12 个玩家角色的代码。它今天跨 13 张表删除并 null 掉 `users.player_resident_id`（`:158-163`），但**全文件 `office` / `mayor` 零命中**：删掉在任镇长会留下悬空 `offices.holder_slug`，`current_mayor()` 照常返回它，`backend/app/routers/townhall.py:61` 找不到人就把 slug 当名字显示给玩家。既然分档清理表已经存在，**顺带让 `purge_residents` 复用最高档的清理函数**，把这条既有悬空路径一起收口。
- 未来任何新 `resident_type` 取值，必须与「加不加进 `SIM_RESIDENT_TYPES`」的决定在**同一个 commit** 里落地，并同步更新 `burnin_report.py:1181` 的 `_BOUNDARY_KNOWN_OUTSIDE`。

---

### 4.9 独占文件与硬门

**独占文件**：`backend/app/services/civic_membership.py`、新建 `backend/app/tasks/civic_promotion.py`、新建 `backend/app/models/civic_standing_history.py` + 建表迁移、`backend/app/services/reputation_service.py:74`（一处字面量归类，⚠️ 与 F1 的独占文件重叠 → **必须与 F1 协商归属，建议由 F1 顺手改并在 F2 的测试里断言**）、`backend/app/services/election_service.py:135-193`（`install_mayor` 收口，F1 独占区 `:53-60` 之外）、`backend/app/routers/admin/residents.py:117-118`（一行）、`backend/scripts/burnin_report.py` 探针区、对应测试。

**不改**：`backend/app/tasks/nightly_cron.py`、`backend/app/config.py`（接线与开关延到收口 §8）、`backend/app/services/office_service.py`（避免与 F3 冲突，F2 只 `import app.models.office.Office` 自写 guard UPDATE）。

**硬门**（全部要有测试，不是文档承诺）：

1. **可观测**：4.7 的五项探针改造全部落地，T2 前后各跑一次并留读数；
2. **可回滚**：每次档位变更在 `civic_standing_history` 留一行（含 `old_standing`，恢复时按最近一条回到哪一档，而不是一律回 citizen）；T2 也必须写（`actor="ops_backfill_t2"`），否则回填批次事后不可追溯；
3. **撤销 ⇒ 卸任**：撤销在任镇长后，`offices.holder_slug IS NULL`、全表无 `meta_json['mayor']`、`system_config['current_mayor']` 为空——**`polis_office_enabled` 开与关两种状态都要覆盖**；
4. **顺序无关性**：判定纯函数打乱输入顺序输出集合恒等；
5. **射程**：player / 内置 / preset / 无晋升记录者被撤销时 `raise` 且数据库零变化；
6. **不变式**：撤销后 `is_autonomous is True` / `is_civic_voter is False`（复用 `backend/tests/test_ugc_resident_no_political_rights.py:329-339` 的口径写反向断言，防止有人顺手把撤销实现成「移出世界人口」）；
7. **法定人数不漂移**：一张当晚到期的 poll + 一位当晚达标的 UGC 居民，断言该 poll 在**夜 N 与夜 N+1** 的 verdict 都不因晋升而改变。

---

### 4.10 待复验 / 待拍板（不确定项，实现前必须消解）

| # | 事项 | 状态 |
|---|---|---|
| U1 | vm212 的 `REALISM_RELATIONS_ENABLED` 实际取值 | 记忆记录 07-23 部署时置 true 并在容器内坐实，此后经多轮部署**未复验**；F2 第 0 步登录确认 |
| U2 | vm212 `offices` 表 `holder_slug` 现值（046 迁移遗留） | **未实测**，T1/T2 现场读一次 |
| U3 | alembic 链头 | 本机最新是 `backend/alembic/versions/051_add_lab_codex_model_tier.py`，与 §6 T1 写的「049 → 050」不一致 → 部署前复核，新建表迁移编号相应顺延 |
| U4 | `reputation_service.py:74` 归属 F1 还是 F2 | 建议 F1（该文件是 F1 独占），F2 只写断言 |
| U5 | 封号用户名下居民是否自动降级 | **待决**，v1 默认不传导 |
| U6 | `preset`（admin 创建）是否并入任一集合 | 沿用现状（两列之外），本轮不动 |
| U7 | 三个阈值 + Δ + 最短任期 + 冷却 + 三道数值闸门的具体数字 | 由 4.2(d) 的生产分布标定，本 spec 不拍 |

---

### 4.11 对本文档其它章节的连带修改

- **§4 边界**：「晋升是单向的，v1 不做降级（YAGNI）」→ 删除，替换为本节 D2/D3；「零迁移：只改 `resident_type` 列值」→ 「零**数据**迁移；允许一次纯建表 additive migration，且不得与开闸同批」。
- **§7 顺序约束**：在「T2 必须先于 F2 上线」之上，补 F2 交付的建表迁移**必须先于 T2**（T2 要写历史行）；并补 T2 与 F2 共用 UGC 判定实现、但触发路径分离（一次性脚本 vs cron，不得互相调用、不得同一次部署同时首跑）。
- **§8 收口**：第 2 项由「接入 F2 的 `civic_promotion`」细化为「接在 `close_due_polls` 之后、`run_npc_voting` 之前（≈`nightly_cron.py:245`），并用 `nightly_cron.py:142-145` 同款注释锚住顺序」；新增一项「`install_mayor` 事务化 + 结票复核资格」归 F2。
- **§6 线 T**：T2 行补「回填是 `resident_type` + 历史行的双写，只写 type 不写历史即视为回填未完成」与「完成标记 + 拒绝重放」。

---

## 附：被对抗验证证伪、未采纳的 3 条

**F2 的可行性完全悬在 realism_relations_enabled 上，而该开关默认 False；打开它又会在同一批次里同时启动全图衰减**

两个分支的前提都被仓库内的事实推翻。(1) 「关着 → 晋升面恒空」：代码默认确为 False（config.py:495，findings 写的 484 是旧行号），但**运行取值不是代码默认** —— backend/.env.example:445-450 明确写着「Production decision (ROADMAP §0): all three ENABLED」并设 `REALISM_RELATIONS_ENABLED=true`，docs/ROADMAP.md:13 记录「拟真 P0-P2 已实现并在 vm212 开启」。所以生产上关系写入与衰减都是活的，「晋升面恒空 / 标定得出错误结论」的情景没有依据。(2) 「本轮才打开 → 开闸+行为变更同窗口撞红线」：该开关是 2026-07-23 随 P2 部署开的（ROADMAP.md:13 + 07-23 部署记录），不是本批次动作；种子边的衰减早已在跑了几十天，不存在「F2 批次内同时启动全图衰减」这回事。红线本身也被误引——红线是「数据迁移/回填 与 开闸/行为变更 不得同一次变更」，单独开一个既有开关不触发它。整条发现的 severity=major 建立在一个已经不成立的运行前提上。

**降级任务需要的 07-25 等价防呆不是「跳过 player」，而是「查 users.player_resident_id 后拒绝」——admin 编辑口是真实可达的污染路径**

污染路径属实（backend/app/routers/admin/residents.py:117-118 直接赋值，backend/app/schemas/admin.py:114 是无约束的 `str | None`；users.player_resident_id 在 backend/app/models/user.py:30），但发现给的**危害链是误读**："npc" 与 "resident" 同时属于 SIM_RESIDENT_TYPES，也同时满足 `!= "player"`。所以把被误改成 npc 的玩家化身降级为 resident，既不会「开始让 agent loop 驱动它」（backend/app/agent/loop.py:60/138/314 读 is_autonomous，改成 npc 那一刻就已经为真），也不会让 backend/app/routers/home_decor.py:56 与 backend/app/agent/map_data.py:476 的 `!= "player"` 判定「反转」（两个取值都 != player，装修权在 admin 手滑那一刻就已丢失）。降级在这个场景里反而是**收回了被误发的选票**，是净修复。以 blocker 记的那条危害不成立。

**等阈值降级 + familiarity 衰减 = 公民权抖动，且实际语义会退化成「对不活跃玩家角色的政治清洗」**

第二个论断建立在误读上。UGC 居民 type="resident" ∈ SIM_RESIDENT_TYPES，是被 agent loop 正常驱动的自治居民——backend/app/agent/loop.py:130-142 的选取条件只有 `is_autonomous` + 非 sleeping，**没有任何 reply_mode / creator 活跃度过滤**；familiarity 由 NPC-NPC 对话（backend/app/agent/chat.py:64，+realism_rel_familiarity_chat=0.05）与串门（backend/app/agent/phases/execute/basic.py:68，+0.02）驱动。创作者不登录不会让角色停止社交，因此「关系必然闲置衰减 → 降级 = 惩罚不活跃玩家」不成立。衰减算例也偏：decay 只作用于 last_interact_at 早于 30 **真实**日的关系（relation_service.py:210-211），且夜间 cron 一真实日只跑一次，世界周闸门（nightly_cron.py:54-67/365）实际约每 2 真实日才放行一次 → 再闲置 30 真实日约 15 步（0.95^15≈0.46），不是「30 真实日内 0.95^17」。另外一次对话 +0.05 会同时把 last_interact_at 重置，所以任何抖动周期 ≥30 真实日，不是「反复抖动」。
