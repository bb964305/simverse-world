# S2-5 policies 表 + 四级分级审批 — 工作线报告

- 分支:`feat/s2-5-policies`(worktree `/Volumes/data/dev/sv-s2-policies`)
- base:`master` @ `c54c606`(阶段 0 文档归档收口后)
- 规格:`archive/2026-07-25/docs/kickoffs/KICKOFF_S2-5_policies.md`
- 方案:`archive/2026-07-25/docs/SOCIETY_EXPANSION_PLAN.md` §2 `S2-5` / §3.2 L0–L4 / §3.3 分级审批矩阵 / §6 接口面 / §9 红线
- 日期:2026-07-25

---

## 1. 任务状态表

| # | 任务 | 状态 | commit | 主要产出 |
|---|---|---|---|---|
| 1 | `policies` 表 + ORM 模型 + 迁移 | ✅ 完成 | `4b6b914` | `backend/app/models/policy.py`、`backend/alembic/versions/048_add_policies.py`、`models/__init__.py` 注册 |
| 2 | `PolicyService` 四级矩阵 + 播种 + 原子 amend | ✅ 完成 | `5aca9f5` | `backend/app/services/policy_service.py`、`config.py` `POLIS_POLICY_` 块、`.env.example` 块 |
| 3 | track A 行政审批接线 | ✅ 完成 | `2a915d1` | `routers/admin/policies.py`、`routers/admin/__init__.py`、`routers/townhall.py`、`services/proposal_service.py` tier 门 |
| 4 | track B 阈值 + `policy` 效果类型 | ✅ 完成 | `b02320b` | `services/civic_service.py` `_close_one` / `_execute_outcome` |
| 5 | config flag 收口 + §6 探针 | ✅ 完成 | 本次提交 | `scripts/burnin_report.py` 两个探针、`tests/test_burnin_report_policies.py`、本报告 |

测试文件:`backend/tests/test_policy_service.py`(27)、`backend/tests/test_policy_approval_integration.py`(21)、`backend/tests/test_burnin_report_policies.py`(6)。

---

## 2. 落地形状(与规格逐条对照)

### 2.1 表与迁移

`policies(id, key UNIQUE, value Text, tier, procedure, group, version, updated_by, created_at, updated_at)`。
`value` 用 `Text` 而非 `String(2000)`——`system_config.value` 的 2000 字符上限正是本表存在的理由。
迁移只 `create_table` + 三个索引,**不 ALTER 任何既有表**(`world_change_proposals` 不加 `tier`/`procedure` 列,规格 §7),静态测试 `test_migration_creates_table_only_no_alter` 锁死。

### 2.2 四级矩阵

```
administrative      → admin_direct              threshold=None   authority=is_admin
simple_majority     → civic_poll                threshold=0.50   authority=vote
absolute_majority   → civic_poll_supermajority  threshold=0.667  authority=vote  quorum=True
constitutional_core → immutable                 threshold=None   authority=none
```

未知键回落 `simple_majority`(保守档)——**绝不回落 `admin_direct`**,因为"把公投事项悄悄划进行政直批"正是 §3.3 点名的最高级夺权手法。

seed catalog 共 17 条:

| tier | 条目 |
|---|---|
| administrative(3) | `civic_poll_days`、`market_day_weekday`、`market_day_discount` |
| simple_majority(5) | `tax_rate`*、`medical_subsidy_sc`*、`npc_default_wage_sc`*、`curfew_hours`、`business_hours` |
| absolute_majority(4) | `election_interval_days`、`recall_threshold`、`approval_routing`、`housing_development_scale`* |
| constitutional_core(5) | `election_exists`、`exile_right`、`lab_approval_gate`、`lab_envelope_definition`、`lab_self_governance_immunity` |

`*` = 财政类待接 S1-5(见 §5)。
自指保护:`approval_routing` 自身置 `absolute_majority`,其 seed 值就是 tier→path 映射本身;被非法降级的路径留给 S3-7 违宪控告,本模块只拒绝对 `constitutional_core` 的**直接修改**。

### 2.3 原子性

- `apply_amend`:`UPDATE policies SET value=…, version=version+1, updated_by=…, updated_at=… WHERE key=:k AND version=:expected`,`rowcount==1` 胜出。**无读-改-写**。`expected_version=None` 表示"先读当前版本再 CAS",竞态下落败(返回 `False`)而不是覆盖。
- `seed_defaults`:方言感知幂等 upsert——sqlite/postgresql 走 `INSERT … ON CONFLICT (key) DO NOTHING`,其它方言回落"select 已存在键 → 只插缺失"。第二次调用返回 0。
- 多步门复用 `app/lab/transitions.py:cas_proposal_status`,**不自造锁**;`app/lab/` 的 apply/preflight 内核未被修改(只复用该 helper)。

### 2.4 门控回落(两个开关独立)

| 开关 | 关闭时行为 |
|---|---|
| `polis_policy_enabled` | `get`/`get_group` 回落 `ConfigService`(`system_config`);`seed_defaults` 返回 0 不写库;`apply_amend` 返回 `False` 不写库;`GET /admin/policies`、`GET /townhall/policies` 返回空投影;`POST /admin/policies/{key}/amend` 与 `/seed` 409 |
| `polis_policy_approval_enabled` | `proposal_service.approve_proposal` 的 tier 门首行 return(回落单-admin CAS→apply);`civic_service._close_one` 不计算 verdict(回落纯 plurality);`_execute_outcome` 的 `policy` 分支不参与匹配 → 落到函数尾 `return False`,与 S2-5 之前遇到未知类型完全一致 |

对照断言用同一份 4/3/3 票型跑两次:门开=流会(`threshold_not_met`),门关=plurality 执行且 `policy` 效果无人识别。

---

## 3. 偏差清单(规格 vs 落地)

| # | 规格写法 | 实际落地 | 原因 |
|---|---|---|---|
| D1 | 现链头 `040_residents_creator_nullable`,新迁移接 040 | 迁移 `048_add_policies`,`down_revision = "047_add_issue_stances"` | 实测链头 = `047`(045→046_add_offices→047)。规格自己也提示过撞号,**未硬编码 041** |
| D2 | `_close_one/_execute_outcome` 在 `civic_service.py:254-315` | 实测 `_close_one` `:254-282`、`_execute_outcome` `:284-315`(改动前)。`mayor` 分支已被 S2-1 改道 offices | 行号漂移;**mayor 分支字节未动**,只在 dispatcher 上新增 `policy` 类型 |
| D3 | `config.py:7-19` Settings、`:246-268` realism 块、`:354-373` town flags | 实测 `Settings` 类尾已有 `POLIS_OFFICE_` 与 `POLIS_OPINION_` 两块;`POLIS_POLICY_` 块追加在最后,未改他人行 | 并行线纪律 |
| D4 | 玩家只读端点 `GET /town/policies` | 落为 `GET /townhall/policies` | 仓库里不存在 `/town` 前缀 router;`/townhall` 正是"政治层只读聚合、fail-open"面(该模块自述 docstring),S2-1 也因同样原因把 `/town/offices` 推迟。追加在文件尾,与并行线冲突面最小 |
| D5 | 任务 5 = config flag | config flag 前置到任务 2 提交 | 服务层与其门控回落测试对 `settings.polis_policy_*` 是硬依赖;`.env.example` 同步追加以保持 `test_env_example_consistency` 绿。任务 5 收口 = 探针 + 报告 |
| D6 | 任务 4 "改 `nightly_cron.py` 新增块" | **未新增任何 nightly 块** | 规格自带条件"若 track B 的 `close_due_polls` 已覆盖则复用,不重复关闭"。实测既有 M3 块 `nightly_cron.py:86-126` 的 `close_due_polls` 已覆盖到期政策 poll,`test_close_due_polls_covers_policy_polls` 锁死。与并行线 3(改调度骨架)零冲突 |
| D7 | `apply_amend(..., expected_version: int)` | `expected_version: int \| None = None` | `_execute_outcome` 侧没有版本快照可带;`None` = 先读当前版本再 CAS,仍是条件 UPDATE,竞态下落败不覆盖。签名向后兼容 |
| D8 | 探针"核心条款触碰计数"未指定存储 | 计数器落 `system_config`(group=`policy_probe`,key=`policy_core_touch_attempts`),读-改-写 | 纯遥测,非政策状态,§4 的条件-UPDATE 红线针对政策写路径;并发下少计可接受,fail-open 不掩盖拒绝。**成功数**不读该计数器,而由 core 行的 `Σ(version-1)` 算出,天然不可伪造 |
| D9 | `propose_amend(..., rng=None)` | 接受但不使用 | 该路径是查表路由,无随机分支;保留形参以符合 seeded-RNG 纪律 |

---

## 4. 迁移占位登记(收口必读)

| 项 | 值 |
|---|---|
| 文件 | `backend/alembic/versions/048_add_policies.py` |
| `revision` | `"048_add_policies"`(**本 worktree 占位号**) |
| `down_revision` | `"047_add_issue_stances"`(实测链头) |
| 内容 | 仅 `create_table("policies")` + 3 个索引;`downgrade` 仅 `drop_table` |
| 播种 | **不在迁移里**——`PolicyService.seed_defaults()` 承担,受 `POLIS_POLICY_ENABLED` 门控(迁移播种会在关闭特性的机器上凭空建行) |
| 冲突面 | 并行线 S1-5 的 `NNN_add_town_treasury` 同样接 `047`。收口时按合并顺序线性化重排 `down_revision`,并复验 `alembic heads` 单头 |
| 已有单头测试 | `tests/test_policy_service.py::test_integration_migration_single_head`(重排后需同步改 `048_add_policies` / `down_revision` 两处字面量);同类断言另见 `tests/test_opinion_service.py::test_integration_migration_single_head` |

---

## 5. 待接 S1-5 的财政类条目清单

S1-5(镇财政闭环)同期开发未合并。本线**只落存储 + 审批骨架**:这些键可以被正常 amend(值落库、版本 +1、探针计数),但**没有下游 treasury 接线,effect 为 no-op 占位**。代码侧登记在 `app/services/policy_service.py::FISCAL_PENDING_KEYS`,`apply_amend` 成功后打一条 INFO 日志说明"仅存储、未接线",`list_all()` 与 `GET /townhall/policies` 的每行带 `fiscal_pending: true` 标记。`tests/test_policy_service.py::test_fiscal_pending_keys_registered` 保证清单与 catalog 同步。

| 键名 | tier | group | seed 默认值 | 期望 effect 语义(等 S1-5 冻结签名后接线) |
|---|---|---|---|---|
| `tax_rate` | `simple_majority` | `fiscal` | `0.0` | 镇税率。`TreasuryService.tax(...)` 的税基比率;amend 生效后下一个征税节律按新值抽成入 `town_treasury` |
| `medical_subsidy_sc` | `simple_majority` | `fiscal` | `0` | 每次就医的公共补贴额(SC)。`TreasuryService.disburse(...)` 从镇库支付,余额不足时降级/停发(需与 S1-5 的余额语义对齐) |
| `npc_default_wage_sc` | `simple_majority` | `fiscal` | `settings.npc_default_wage_sc`(5) | 公职日薪基准。现由 `duty_service._pay_wage` 读 `settings`;接线后改读政策值(**S1-5 独占 `duty_service`,本线不碰**),并须保住 S2-1 的镇长加成回归门 |
| `housing_development_scale` | `absolute_majority` | `fiscal` | `0` | 住房开发规模(本期新增住房单位数)。走公共支出:`TreasuryService.disburse` 扣款 + 触发住房容量提升(§5.3),绝对多数档 |

**接线前置(S1-5 合并后)**:① `TreasuryService` 的 `tax` / `disburse` / `balance` 签名冻结;② 确认 `town_treasury` 的余额不足语义(拒付 vs 部分支付);③ 在 `PolicyService` 侧只加读取(`get`),写路径仍走 amend,**不得**由财政侧反向写 `policies`;④ `duty_service._pay_wage` 的改动归 S1-5 一线,本线只提供 `PolicyService.get("npc_default_wage_sc")` 读口。

---

## 6. 收口 config / `.env.example` 清单

`backend/app/config.py` `Settings` 类**尾部追加**(`POLIS_OPINION_` 块之后,未改他人行):

| 字段 | 类型 | 默认 | env |
|---|---|---|---|
| `polis_policy_enabled` | bool | `False` | `POLIS_POLICY_ENABLED` |
| `polis_policy_approval_enabled` | bool | `False` | `POLIS_POLICY_APPROVAL_ENABLED` |
| `polis_policy_simple_majority_threshold` | float | `0.50` | `POLIS_POLICY_SIMPLE_MAJORITY_THRESHOLD` |
| `polis_policy_absolute_majority_threshold` | float | `0.667` | `POLIS_POLICY_ABSOLUTE_MAJORITY_THRESHOLD` |
| `polis_policy_quorum_fraction` | float | `0.50` | `POLIS_POLICY_QUORUM_FRACTION` |

`backend/.env.example` 已同步追加同名 5 行(注释含回落语义)。`tests/test_env_example_consistency.py::test_every_settings_field_is_documented_or_allowlisted` 已绿。
**merge 注意**:`config.py` 与 `.env.example` 均为**尾部追加式**改动,与 S1-5 的 `TOWN_`/`town_` 块只会产生相邻行冲突,按前缀分块手工拼即可。

**上线顺序建议**:先只开 `POLIS_POLICY_ENABLED` + 调一次 `POST /admin/policies/seed` 做影子读写验证(审批仍走现状),观察一轮夜间后再开 `POLIS_POLICY_APPROVAL_ENABLED`。

---

## 7. 探针出数(§6,seeded fixture)

Seeded 治理史:行政级 2 次 amend、简单多数 3 次、绝对多数 1 次、核心条款 3 次被拒尝试。
出数命令等价于 `scripts/burnin_report.py` 的 `render_probes_s25`(纯读,零 LLM)。

```
== 社会探针（S2-5 验收：政策漂移距离 / 核心条款不可触碰）==
  政策漂移距离（按 tier;目标:门槛越高漂移越少，阶梯状累积）：
    administrative       条目  3 | amend 累计   2 | 漂移合计 0.1111 (均值 0.037)
    simple_majority      条目  5 | amend 累计   3 | 漂移合计 1.2    (均值 0.24)
    absolute_majority    条目  4 | amend 累计   1 | 漂移合计 0.25   (均值 0.0625)
    constitutional_core  条目  5 | amend 累计   0 | 漂移合计 0.0    (均值 0.0)
    漂移最大的条目：tax_rate(v3, Δ0.2), market_day_discount(v3, Δ0.1111), curfew_hours(v2, Δ1.0)
  核心条款触碰计数:尝试 3 次 / 成功数 = 0 ✅ 核心条款不可触碰（核心条目 5 条,漂移合计 0.0）
    被盯上的核心条款：election_exists×1, exile_right×1, lab_approval_gate×1
```

**探针 1 — 政策漂移距离**:每条 = `amend 次数(version-1)` + 归一化数值漂移(数值型 `|Δ|/|seed|`;布尔/枚举/结构型取翻转计数 0/1),按 tier 聚合。目标形态"阶梯状累积、门槛越高漂移越少"在 seeded 样本上成立:`simple_majority` amend 累计 3 > `absolute_majority` 1,核心档 0。

**探针 2 — 核心条款触碰计数**:尝试 **3** / 成功 **0**。成功数由 core 行的 `Σ(version-1)` 独立算出,不读计数器。

**硬断言**(`tests/test_burnin_report_policies.py`):
- `test_constitutional_core_drift_is_always_zero` —— `core_drift == 0.0`、`core_amends == 0`、core 行 `version` 集合恒 `{1}`;
- `test_core_touch_counts_attempts_gt_zero_successes_zero` —— `attempts == 3` 且 `successes == 0`;
- `test_render_probes_s25_flags_a_breach` —— 人为构造 core 行 `version=2` 时探针必须渲染 `🔴 红线破防`,不得静默。

**对照组语义**:`POLIS_POLICY_APPROVAL_ENABLED=false` 时政策仍是 `system_config` 无类型 blob,无 tier 约束 → 漂移无分级差异、核心条款无保护(成功数 = 尝试数)。探针在门关时会打印对照组说明行。

---

## 8. 红线自检

| 红线 | 落地 |
|---|---|
| 不合并 / 不 push / 不部署 / 不碰 vm212 | ✅ 仅本地 worktree 提交 |
| 不给 `world_change_proposals` 加列 | ✅ 静态测试锁死;tier 挂在 `policies` 侧,提案侧只读 `patch_json["policy_key"]` |
| 不碰 duty/coin/shop/election 写路径 | ✅ 未修改这四个 service |
| 不碰 `app/lab/` apply/preflight 内核 | ✅ 只复用 `transitions.cas_proposal_status`;`broadcast_world_changed` 本线未新增调用(无新增 WS 事件,见下) |
| 政策指标永不进 NPC prompt | ✅ `test_policy_probe_data_never_enters_npc_prompt`:`app/agent/` 全树静态扫描不得出现 `policy_service` / `PolicyService` / `policy_core_touch` / `policy_drift` |
| 零新增 LLM 边际成本 | ✅ 审批为纯规则;公告复用 `civic_service.propose` / `_clerk_announce` 里既有的文书 bulletin 调用 |
| 每模块独立开关默认 False | ✅ 两个开关都默认 `False` 且互相独立 |
| 性能红线 tick +1 | ✅ 读取面是批量 `list_all` / `get_group`(百级行一次载入),无 per-resident 逐条查库;tick 循环未新增任何查询 |
| 不提交 `backend/skills_world_dev.db` | ✅ 全部提交用显式 `git add <path>`,提交前逐次核 `git status --porcelain` |

---

## 9. 未完成 / 后续项

| 项 | 说明 |
|---|---|
| `policy_changed` WS 事件 | 方案 §6 预告了该事件,本线**未实现**。规格 §7 要求"若发则镜像 `world_revision_service.world_changed_event` 信封 + `OutboxEvent.id` 做 seq 锚",属独立接口面工作;S2-1 的 `office_changed` 同样未落 Outbox(其探针改走 `updated_at` 聚合),本线探针同样不依赖 WS,故推迟不阻塞验收 |
| 财政类 4 条 effect 接线 | 见 §5,等 S1-5 合并 |
| `system_config` → `policies` 存量迁移 | 本线只做 catalog 播种,不搬运既有 `system_config` 行(`current_mayor` 等运行时状态不属政策)。迁移期两存储共存,`get` miss 回落 `ConfigService` |
| `approval_routing` 的实际消费 | 该键目前是"可被公投修改的路由声明",`PolicyService` 仍以 `TIER_MATRIX` 常量为准执行。让运行时真正读该键属 S3-7(违宪控告)配套工作 |
