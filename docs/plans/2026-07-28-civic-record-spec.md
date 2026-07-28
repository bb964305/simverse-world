# 政绩记录（civic record）设计规格 · 2026-07-28

> 07-27B 收口第 3 项「声誉影响接线（F3 卸任审计 × F1 声誉数据）」的展开。
> 基线 `master = 163e9b1`。本文是**规格**，step 级 TDD 计划另出。

## 0. 为什么这不是一次「接线」

批次文档里这一项只有 4 行字，没有方向、没有字段、没有正负号、没有验收标准；F3 计划把它放在收口清单最后一行，与同一清单里标了「**【收口硬门】**」并写了 4 条细则的第 3 项形成对照。开工前逐条查过两侧，实际情况是：

**审计侧 17 个字段里没有一个是评价。** 只有 4 个数值量，每个都有语义缺陷：

| 字段 | 缺陷 |
|---|---|
| `fiscal_polls_passed` | 计「任内全镇通过了几项财政公决」，**不看提案人**。镇长既非唯一提案人也无否决权 |
| `fiscal_policy_changes` | **不看 `updated_by`**（管理员改的也算）；`policies` 表一个 key 只有一行，同一政策改 3 次只记 1 条 |
| `town_balance_sc_end` | 只有终值**无起点基线**，算不出任内涨跌；第一任期永远无前驱可比 |
| `mayor_wage_multiplier` | 纯 `settings` 常量，每个镇长恒等，零个人信息 |

**声誉侧没有任何写入口。** `recompute` 是全仓唯一写者，全量覆写；`score = clamp((1-α)·previous + α·(0.2·mood_valence + gossip 均值))`。原 kickoff 计划的 `bump` 从未落地。EMA `α=0.3` 意味着任何注入值**三夜衰减到 34%**，而选举间隔 28 天。

**今天生产上频率是 0。** `polis_office_enabled=False` + `polis_office_mayor_term_days=0` → `term_check` 整段不跑 → 一条审计都不产生。

所以这是一次新增信号源的设计，不是把两根现成的线接起来。

## 1. 目标与非目标

**目标**：让「任期干得好/差」通过声誉通道影响后续选举得票。

**非目标**（明确不做，避免范围蔓延）：

- 不做玩家可见的政绩展示（无新端点、无前端、无市政厅面板）
- 不把审计从 `system_config` 升级成表（现有载体够用；升级会引入迁移，落进红线序列）
- 不做「审计公决」（居民投票裁决官员，那是 S3-4 的完整形态）
- 不改 `gossip_signal` 的均值语义（会让 F1 全部标定作废）
- 不动候选资格 —— 被动选举权不因政绩受损而剥夺（继承 F1 第 2 项的口径）

## 2. 三轴评价函数

### 轴的选取理由

「活动量」与「归因后的作为」不是两个维度，是同一维度的两种精度——活动量 = 去掉归因的作为，相加即重复计分。真正互相独立的是三条：

| 轴 | 语义 | 归因强度 |
|---|---|---|
| **A 作为** | 他自己推动了什么 | 强 |
| **B 破产** | 有没有把镇库搞垮 | 弱（镇库主要由税收/工资自动流转决定） |
| **C 善终** | 怎么下台的 | 强（下台方式是事实） |

B 归因最弱，所以**不参与加权，只作否决项**——吸收了财政维度，又不让一个噪声量去抬权重。

### A 轴 · 作为（连续）

归因口径：

- 公决：`options_json[0]["_proposer_slug"] == holder_slug`（锚点已存在，`civic_service.py:69` 写入，`:395`/`:441` 已在读）
- 政策：`policies.updated_by` 形如 `poll:<poll_id>` 时回溯该 poll 的提案人；形如 `admin:<id>` 或 `seed` 的**一律不计**

```
A = 归因命中的财政公决数 + 归因命中的政策改动数
f(A) = ln(1 + A) / ln(1 + A_REF)
```

**取对数不是审美，是防刷**：线性会让「疯狂提案」成为最优策略——提案在这个世界里成本极低（`seed_civic_agenda` 是幂等一次性，但玩家/NPC 提案无硬上限），线性计分会把镇长变成提案机器。对数使第 10 个提案的边际收益只有第 1 个的约 1/3。

`A_REF` 是「一个正常勤勉任期的作为量」，使 `f(A_REF) = 1.0`。**不在本规格里定值**，由 shadow 期实测分布反推（见 §6）。

### B 轴 · 破产（否决项）

```
B_triggered = (任内镇库最低水位 < 0) 或 (期末余额 < 期初余额 × B_RUIN_RATIO)
若 B_triggered：政绩分 clamp 到 ≤ 0
```

需要**任期起点基线**。写点：`OfficeService.appoint`（`office_service.py:73-100`）在写 `term_started_at` 的同一次 UPDATE 里追加 `term_start_balance_sc`。这是 offices 表的 additive 列，属独立一次迁移。

「最低水位」无法从两个端点算出，v1 只用期末 vs 期初两点判定；`< 0` 这一支保留是因为镇库允许透支时它才有意义，当前 `treasury_service` 不允许，故 v1 实际只有比例判据生效。**这一点必须写进实现注释**，免得后人以为最低水位真的被追踪了。

`B_RUIN_RATIO` 同样由 shadow 定值。

### C 轴 · 善终（三档离散）

**从历史反推，不碰 F2 的事务。** F2 的 `revoke_citizenship` 明文禁止三件事，其中包括「不得用 `ConfigService.set()`（自带 commit，会把复合事务劈成两半）」——而 `record_term_audit` 正是走它。所以撤销路径**不能**在事务内写审计。

改为夜间派生时反推：

| 档 | 判据 |
|---|---|
| `natural` 自然到期 | 有审计行，且 `term_ended_at ≈ term_ends_at`（容差一个夜间周期） |
| `revoked` 被撤公民权 | `civic_standing_history` 存在 `new_standing == DENIZEN` 且 `created_at` 落在任期窗内的行 |
| `vacated` 中途出缺 | 有审计行但两条都不满足 |

`civic_standing_history` 的 `(resident_id, created_at)` 复合索引（`models/civic_standing_history.py:66`）正好支持这个查询。**零新增写路径、零事务耦合。**

### 合成

```
政绩分 = clamp( W_A · f(A) + W_C · g(C) , RECORD_MIN, RECORD_MAX )
若 B_triggered： 政绩分 = min(政绩分, 0.0)
```

`g(C)`：`natural → +1`、`vacated → 0`、`revoked → -1`。

`W_A` / `W_C` 由 shadow 定值。

`RECORD_MIN` / `RECORD_MAX` **不独立标定，由 W 派生**：`RECORD_MAX = W_A + W_C`（A、C 双满）、`RECORD_MIN = -W_C`（A 为 0 且被撤）。独立标定它们会与 W 打架——clamp 收得比可达上界还紧，等于悄悄改了权重；收得更宽则永不生效。所以需要 shadow 反推的**只有四个**：`W_A` / `W_C` / `A_REF` / `B_RUIN_RATIO`。

## 3. 合流点：`vote_trust_delta`，不进 `_score_all`

```python
def vote_trust_delta(meta_json: dict | None) -> float:
    if not settings.rep_enabled:
        return 0.0
    delta = settings.rep_vote_trust_weight * score_from_meta(meta_json)
    delta += civic_record_delta(meta_json)   # 新增，独立开关
    return delta
```

**为什么不做成 `_score_all` 的第三个加项**（最直觉但是错的）：

1. `score` 的语义是**八卦证据的均值**——往「平均口碑」里加「生涯政绩」是语义混淆
2. EMA 三夜衰减到 34%，选举间隔 28 天，等投票时政绩已经没了
3. 会让 F1 刚做完的全部标定作废（稳态 ±0.02 是在现公式下测的）

在 `vote_trust_delta` 合流则三条全避开，且 F1 声明的「声誉进入一张选票的**唯一**通道」这个不变式保持成立——它仍是唯一那个函数。

**量级前提**：F1 计划书 `:1746` 实测稳态 ±0.02，而 `_npc_choice` 的口味噪声 `_TASTE_MAG = 0.25`（`civic_service.py:199`）。政绩分若要可见，其量级必须与 `rep_vote_trust_weight · score` 同阶。`rep_vote_trust_weight` 待反推（≈5.79）已在 F1 的收口清单里，本线**不重复排**，但定 `W_A`/`W_C` 时必须以它的定值为参照。

## 4. 存储：耐久的在审计行，派生的进 meta_json

- **耐久**：三轴原始读数（`A_polls` / `A_policies` / `B_start_balance` / `C_tier`）追加进 `office_audit` 的 payload。载体已存在，无新表。
  ⚠️ `system_config.value` 是 `String(2000)`，`ConfigService.set` 用 `ensure_ascii=True`（一个汉字 6 字符），`_fit` 会静默丢字段并置 `truncated=True`。新增的四个字段都是标量，且 `_fit` 的裁剪顺序是「先丢议案标题、再丢政策行」——身份与标量字段永不被裁。实现时须加断言确认新字段不会触发裁剪。
- **派生**：夜间把该居民全部审计行合成一个标量，写 `meta_json["civic_record"] = {"score": float, "terms": int, "updated_at": ISO}`。

`meta_json` 是 7 个 read-modify-write 方争抢的列。这里可以用，因为**覆盖是自愈的**——派生值每夜从耐久源重算。这与 F2 那个「滞后状态被覆盖就永久丢失」的场景性质不同，必须在代码注释里写清这条区别，免得被当成先例误用。

## 5. 三态开关

`CIVIC_RECORD_MODE ∈ {off, shadow, on}`，默认 `off`。语义逐字对齐 F2 的 `CIVIC_PROMOTION_MODE`：

- `off`：`civic_record_delta` 恒返回 `0.0`，夜间派生不跑，行为与本线开工前逐字节一致
- `shadow`：完整计算三轴与政绩分，写进探针与日志，**但 `civic_record_delta` 仍返回 `0.0`**——分数不进选票
- `on`：政绩分进入 `vote_trust_delta`

放在 `RUNTIME_ENV_KEYS`（运行时读 `os.environ`，同 F2 的理由：`Settings` 是 import 期单例，测试要 per-case 改档位），并同步登记进 `tests/test_env_example_consistency.py` 的注册表。

**开闸闸门**：仿 F2 的 `assert_thresholds_calibrated`——`W_A`/`W_C`/`A_REF`/`B_RUIN_RATIO` 仍是占位值时，`mode=on` 直接拒绝。这条是为了不重蹈 `rep_credit_min_score = -0.3` 的覆辙：那个值是拍出来的，结果拒绝面为空、闸门变成装饰。

## 6. 四次独立变更（红线）

```
① 前置开闸：POLIS_OFFICE_ENABLED=true + POLIS_OFFICE_MAYOR_TERM_DAYS>0
   —— 不开这一步，term_check 整段不跑，后面全是死代码
② 建列迁移：offices.term_start_balance_sc（纯 DDL，零数据行为）
③ 代码合入：归因改造 + 三轴函数 + 派生 + 探针，CIVIC_RECORD_MODE=off
④ shadow 观察 ≥3 次真实卸任 → 用实测分布定四个参数 → 开闸（单独一次，只翻开关）
```

① 与 ② 之间无依赖可并行；③ 必须后于 ②（派生要读新列）；④ 必须后于 ③ 且中间有真实观察期。

**②③ 不得同批**（迁移与行为变更不同批，07-25 事故窗口）；**③④ 不得同批**（代码合入与开闸不同批）。

**前置的真实代价**：① 会让世界第一次出现「镇长任期到期→自动补选」的循环。这本身是 F3 已交付并测过的能力，但**从未在生产上跑过**。①应当独立观察至少一个补选周期，确认补选真的发生、没有出现「无限期无镇长」，再进 ②。

## 7. 硬门

| # | 门 | 判据 |
|---|---|---|
| 1 | `off` 态逐字节无影响 | `civic_record_delta` 返回 `0.0`；`_npc_choice` 的打分与本线开工前逐位相同（用固定 seed 的确定性 harness 断言） |
| 2 | `shadow` 不进选票 | `mode=shadow` 时 `civic_record_delta` 仍返回 `0.0`，但探针有读数 |
| 3 | 归因真的在归因 | 构造「管理员改的政策」与「他人提的公决」，断言 A 轴计数**不**增加 |
| 4 | 对数真的抑制刷量 | `f(10) / f(1) < 3.5`（线性下该比值为 10） |
| 5 | B 是否决不是加权 | 构造高 A 高 C 但触发 B 的任期，断言总分 ≤ 0 |
| 6 | C 从历史反推、零事务耦合 | 撤销路径跑完后，`revoke_citizenship` 的调用栈里**不出现** `ConfigService.set` / `record_term_audit`；C 轴读数由夜间派生产出 |
| 7 | 占位值挡住开闸 | 四个参数任一为占位值时 `mode=on` 抛拒绝 |
| 8 | 派生是自愈的 | 手工把 `meta_json["civic_record"]` 改脏，跑一次夜间派生后恢复正确值 |

## 8. 独占文件

- `backend/app/tasks/office_audit.py`（`_fiscal_polls` / `_fiscal_policy_changes` 加归因；payload 加四个标量）
- `backend/app/services/office_service.py`（`appoint` 写 `term_start_balance_sc`）
- 新建 `backend/app/services/civic_record.py`（三轴函数 + 派生 + `civic_record_delta`）
- `backend/app/services/reputation_service.py`（**仅** `vote_trust_delta` 一个函数体）
- `backend/app/tasks/nightly_cron.py`（接派生 pass）
- 新建迁移 + `backend/.env.example` + `tests/test_env_example_consistency.py`（注册表）
- 对应测试

**与在飞线的冲突面**：`reputation_service.py` 与 F1 的收口项（`rep_vote_trust_weight` 反推）同文件不同区；`nightly_cron.py` 与 07-27B 已落地的 F2 接线同文件不同区。两处都需 rebase 而非并行写。

## 9. 明确的未决

以下**不在本规格内**，需要时另开：

- 政绩分要不要给玩家看（今天连 admin 面板都只是把 `meta_json` 原样吐出）
- 劳动职务（`town_clerk` / `postman` / `doctor`）要不要也算政绩——v1 只做 `fill_strategy == "election"` 的民选职务
- 多任期怎么合成：v1 取**全部任期的政绩分之和**再 clamp，不做时间衰减。若将来要「陈年政绩淡去」，那是另一次设计
