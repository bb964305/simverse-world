# S2-1 offices 职位实体化 — 交付报告

> 分支 `feat/s2-1-offices`,base = master(d27fce7)。规格:`docs/kickoffs/KICKOFF_S2-1_offices.md`。
> **结论:5 个任务全部 done,全量 pytest 相对基线零新增失败(68→68,+36 全为本线新测试),`alembic heads` 单头。未合并、未 push、未部署。**

## 1. 任务状态表

| 任务 | 状态 | commit | 说明 |
|---|---|---|---|
| 1. offices 表 + 迁移 + OfficeService | done | `ecb052f` | UNIQUE(office_key) 单持有者;条件 UPDATE + upsert(coin_service 范式);term_days=世界日经 world_clock 换算;迁移 seed 四行 + 幂等回填 |
| 2. 镇长写/读路径改道 | done | `c12ee4f` | install_mayor 门控 dual-write;current_mayor offices→config 读序;civic `_execute_outcome` mayor 分支零改动(测试锁定);`_pay_wage` 零改动(gotcha #1 回归门绿) |
| 3. 文书/邮差/医生查表 | done | `d1edec8` | find_duty_resident 门控开先查 offices(单次索引查询,净改善),缺行/空缺/异常回落线性扫描;医生仅槽位(S5-8 消费) |
| 4. admin 只读端点 + WS | done | `5ce2d73` | GET /admin/offices,端点级 require_admin;office_changed WS 带 seq(OutboxEvent 游标)+world_revision_id 锚,门控关不发 |
| 5. nightly term_check | done | `fb6cfc5` | 治理块尾(M3 run_npc_voting 后)独立 try/except,门控在 cron 内 guard,fail-open;未改未挪任何他人块 |
| §6 探针 | done | `3f3af8b` | burnin_report 三组 office 探针 + seeded fixture 出数(见 §5) |

三道回归门(规格 gotchas)全部锁死并有测试:
- `duty_service._pay_wage` 镇长工资 ×1.2 语义不变 → `test_pay_wage_bonus_preserved_when_gate_on` / `test_gate_off_byte_level_fallback`(且 `_pay_wage` 本线**一行未改**);
- `civic._execute_outcome` mayor 分支经 install_mayor 双写落表 → `test_execute_outcome_mayor_branch_still_installs`;
- `current_mayor` offices-backed 后既有消费者(`routers/townhall.py:51` 唯一业务消费者)行为不变 → 返回语义不变(slug|None),`test_current_mayor_reads_office_then_falls_back_to_config` + townhall 套件回归绿。

## 2. 全量 pytest:基线 vs 收尾

| 轮次 | commit | failed | errors | passed | skipped | 证据 |
|---|---|---|---|---|---|---|
| 基线(开工) | d27fce7(=ecb052f 前工作树) | 51 | 17 | 1642 | 25 | `/tmp/s2_offices_baseline_pytest.log`(副本 `.keep.log`) |
| 收尾 | 3f3af8b | 51 | 17 | 1678 | 25 | `/tmp/s2_offices_final_pytest.log` |

- 失败集逐条 diff(`/tmp/s2_offices_baseline_failures.txt` vs `/tmp/s2_offices_final_failures.txt`):**唯一差异行是同一个参数化用例**(`test_lab_runtime_v2_store_auth.py::test_service_auth_rejects_untrusted_or_invalid_tokens[…token_not_yet_valid]`)的参数 ID 内嵌了运行时新签的 JWT(nbf/exp 时间戳不同),用例与失败原因两轮完全相同 → **实质零新增失败**。
- 预存 68 项失败均为 lab-v2 需真 redis/testcontainers/postgres 的既知集合(与记忆中"~55 预存"同族,基线快照为准)。
- +36 passed = 本线新增 36 个测试(test_office_service 12 + test_office_integration 17 + test_burnin_report_offices 7)。

## 3. 偏差清单(规格 vs 实际)

| # | 偏差 | 处置 |
|---|---|---|
| 1 | **链头漂移**:规格 §1.5/§8.3 写现链头 `040_residents_creator_nullable`;实际 master 链头 = `045_residents_creator_nullable`(port 重编号后,045 从 realism 线 040 重挂到 044 merge head) | 迁移 down_revision 按环境事实接 045;`alembic heads` 单头=`NNN_add_offices` 已核验 |
| 2 | **file:line 锚点漂移**:config.py `357-373`→实际 `496-514`;civic mayor 分支 `310-312`→实际 `309-311`;world_changed_event `187-204`→实际 `201-218`;coin_service 各锚亦漂移(treasury upsert 实际 `335-370`) | 逐条以代码为准核对,语义均与规格一致后才动手 |
| 3 | **vacate 守卫条件**:规格 §4 写 `where(office_key==key)`;实现为 `where(office_key==key, holder_slug IS NOT NULL)` | 使返回值语义="确实清掉了在任者"且幂等——term_check 计数与 office_changed 事件依赖该语义;仍是纯条件 UPDATE |
| 4 | **appoint upsert 重试上限 3 次**:规格只写单次 IntegrityError fall-through | 并发测试(文件级 sqlite 多连接)暴露单次重试在竞态下可静默丢更新;加 bounded retry + 每轮 rowcount 复核,0 行不报成功 |
| 5 | **office_changed envelope 手工构造**:`world_changed_event()` 签名硬依赖 `WorldRevision` 实例(实核 `world_revision_service.py:201`),office 变更无 revision | 在 `office_service._emit_office_changed` 按 world_changed v1 形制构造(type/schema_version/event_id/seq/world_revision_id/action/office_key/holder_slug/occurred_at);seq 复用 `current_source_cursor`,revision 锚取 `current_revision_id`,不滚新计数器;**不写 OutboxEvent 行**,轮替探针取规格 §6 允许的 updated_at 聚合径 |
| 6 | **`_pay_wage` 未改**(规格任务 2 的"判据**可**改为 get_holder"为可选项) | 选择零改动:dual-write + term_check 清 meta 保两存储同活,回归门测试锁定 ×1.2;与 S1-5 的 `_pay_wage` 同函数 merge 冲突降为零 |
| 7 | **term_check 增加注入参数** `term_check(*, now=None)` | 规格 §5 明示 frozen clock/注入 now;默认值走 world_clock |
| 8 | **mayor vacate/到期附带清两个旧存储**(meta_json['mayor'] + system_config['current_mayor'],规格未明写) | 否则到期后工资加成经 meta 走风、current_mayor 经 config 回落复活;§6 三存储一致性探针要求同步 |
| 9 | **迁移测试径**:全链 alembic 在 sqlite 从零不可跑(**预存**:003 非 batch ALTER;dev 走 create_all,prod 是 PG) | `test_migration_backfills_*` 直接驱动迁移模块的 `seed_offices`/`backfill_holders`(与 upgrade() 同一代码);另以 `stamp 045 → upgrade head` 在 scratch sqlite 实测单迁移端到端(commit 1 Verified-by) |
| 10 | **.env.example 本线已追加 POLIS_OFFICE_ 块**(任务书原计划收口时由主会话统一进) | `test_every_settings_field_is_documented_or_allowlisted` 是基线绿的硬门,新 Settings 字段不进 .env.example 即新增失败;按 append-only 前缀纪律追加,收口只需核对 |
| 11 | **预存顺序依赖**(与本线无关,记录备查):`pytest tests/test_m1_economy.py tests/test_townhall.py` 组合下 `test_market_day_inactive_by_default` 失败,master d27fce7 原样复现;全量套件字母序不触发 |留待独立修复,不在本线 scope |

## 4. 迁移占位登记(收口线性化用)

- 文件:`backend/alembic/versions/NNN_add_offices.py`
- `revision = "NNN_add_offices"`,`down_revision = "045_residents_creator_nullable"`
- 内容:`create_table offices`(纯新表,无 ALTER,无需 batch)+ seed 四行(mayor/town_clerk/postman/doctor)+ 幂等回填(mayor←system_config['current_mayor'],clerk/postman←meta_json.duty 首匹配,doctor 留 NULL);**不触碰 meta_json['mayor']**
- 收口动作:与 S2-5/S1-3/S1-5 的占位迁移一起重命名编号 + 重指 down_revision 线性化;`alembic heads` 必须单头
- 当前状态:本 worktree `alembic heads` = `NNN_add_offices`(单头,已核验)

## 5. 探针出数(seeded fixture 演示,gate ON/OFF 双形态)

命令:seeded fixture(3 居民 + 4 office,doctor 任后即免)驱动 `fetch_office_snapshot` + `render_probes_offices`(完整输出存 `/tmp/s2_probe_demo.txt`,断言版在 `tests/test_burnin_report_offices.py::test_office_probe_numbers_from_seeded_fixture`):

```
== 社会探针（S2-1 验收：offices 职位实体化）==     [gate ON]
  职位占用/空缺：
    mayor        在任 he-qiaoyun
    town_clerk   在任 zhao-qiwen
    postman      在任 luo-xiaozhou
    doctor       空缺（0 天）
  任期轮替（7 天窗口，按 office 行 updated_at 聚合）：4 个职位有变更
  镇长身份一致性（offices/system_config/meta_json 三存储）：一致
      （office=he-qiaoyun config=he-qiaoyun meta=['he-qiaoyun']）

[gate OFF 对照组] 一致性只比对 system_config/meta_json 两存储:一致;
offices 表不被业务路径读写(test_gate_off_* 断言 rows==[])。
```

- 占用/空缺:4 职位 3 在任 1 空缺(doctor 绿地),空缺天数按 updated_at 推算;
- 轮替计数:updated_at 聚合径(office_changed WS 不落 Outbox,见偏差 #5);
- 一致性:三存储一致=True;测试另覆盖 meta divergence / config divergence → False(dual-write bug 告警形态)。

## 6. 收口时需核对的配置清单(.env.example 已进,见偏差 #10)

| env 键 | 默认 | 说明 |
|---|---|---|
| `POLIS_OFFICE_ENABLED` | `false` | S2-1 总门。false=字节级回落现状,offices 表零读写 |
| `POLIS_OFFICE_MAYOR_TERM_DAYS` | `0` | 镇长任期(世界日,world_clock 换算);0=无限期=现状覆盖式语义 |

## 7. 接口冻结声明(给 S1-5 / S2-5 / S1-1)

> 本线冻结的是 `install_mayor` / `current_mayor` / `_pay_wage` 语义(tax/disburse 与本线无关)。

- **`election_service.install_mayor(db, slug) -> bool`**:签名、返回值、既有副作用(meta_json['mayor'] 独占置位、system_config['current_mayor']、feed+反思记忆)全部不变;`polis_office_enabled=True` 时**追加** `OfficeService.appoint('mayor', slug, fill_strategy='election', term_days=settings.polis_office_mayor_term_days)`(fail-open)。**S2-5**:`_execute_outcome` mayor 分支保持照旧调 install_mayor 即自动获得落表,叠 policy 审批请勿绕开该分支。
- **`election_service.current_mayor(db) -> str | None`**:返回语义不变;门控开时读序 = offices → system_config 回落。**S1-1**:声誉选人权重请消费 `current_mayor()`,不要绕过直读 system_config(门控开时 config 可能滞后于 offices)。
- **`duty_service._pay_wage`**:本线**一行未改**(`duty_service.py:125-146` 与 master 逐字节一致),镇长加成判据仍 `meta_json['mayor']`。**S1-5** 改工资资金流向时与本线无文本冲突;唯一行为耦合:门控开 + `POLIS_OFFICE_MAYOR_TERM_DAYS>0` 时,任期到期的 term_check 会清 `meta_json['mayor']`(加成随任期终止,预期语义)。
- **`duty_service.find_duty_resident(db, key) -> Resident | None`**:返回类型与"单持有者首匹配"语义不变;门控开时 town_clerk/postman 以 offices 行为准,非 office duty key 与空缺行为回落线性扫描。
- **`OfficeService`(新,下游可依赖)**:`appoint(office_key, slug, *, fill_strategy, term_days=None) -> bool`、`vacate(office_key) -> bool`、`get_holder(office_key) -> str | None`、`list_offices() -> list[dict]`、`term_check(*, now=None) -> int`。`perms_json`(S2-2 裁量点)与 `fill_strategy`(S3-1 抽签)字段就位、本线不消费。

## 8. 红线自查

- 未合并、未 push、未部署;纯本地,未连任何远程库。
- 未碰 `app/lab/`(仅 import 调用既有 `broadcast_world_changed`,与 proposal_service 同径)、未碰 agent 主链路文件、未碰 `meta_json['role']`、未碰 Forge/计价/prompt(offices 全局指标未进任何 NPC prompt)。
- config.py 仅 Settings 尾部追加 POLIS_OFFICE_ 前缀块;nightly_cron 仅新增自己的独立块(settings 引用取 `_office_settings` 别名避免与他块互扰);admin/__init__、models/__init__ 仅追加行。
- 未碰 docs/PROGRESS.md。
