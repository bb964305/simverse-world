# S1-5 镇财政闭环(税 / 薪 / 公共支出)— 工作线报告

- 分支:`feat/s1-5-treasury`,worktree `/Volumes/data/dev/sv-s1-treasury`
- base:`c54c606`(阶段 0 文档归档收口后的 master)
- 规格:`archive/2026-07-25/docs/kickoffs/KICKOFF_S1-5_treasury.md`
- 状态:**6 个任务全部完成**,未合并 / 未 push / 未部署

---

## 1. 任务状态表

| # | 任务 | 状态 | commit | 主要文件 |
|---|---|---|---|---|
| 1 | `town_treasuries` 表 + `TownTreasury` 模型 + 迁移 | ✅ | `da86920` | `backend/app/models/town_treasury.py`、`backend/alembic/versions/NNN_add_town_treasury.py`、`backend/app/models/__init__.py` |
| 2 | `TreasuryService.tax / disburse / balance` | ✅ | `7a5bcf7` | `backend/app/services/treasury_service.py` |
| 3 | 税收 hook 接线(销售税 + 送礼/打赏旋钮) | ✅ | `ebad026` | `backend/app/services/shop_effects.py`、`backend/app/config.py`、`backend/.env.example` |
| 4 | 发薪拦截:funded wage | ✅ | `81108f5` | `backend/app/services/duty_service.py` |
| 5 | nightly 公共支出 / 对账作业 | ✅ | `7a0ff67` | `backend/app/services/treasury_service.py`、`backend/app/tasks/nightly_cron.py` |
| 6 | REST 只读端点 + WS `treasury_changed` | ✅ | `a2fa954` | `backend/app/routers/townhall.py`、`backend/app/services/treasury_service.py` |
| 6b | §6 burn-in 探针 | ✅ | `f31a39c` | `backend/scripts/burnin_report.py`、`backend/tests/test_burnin_report_treasury.py` |
| 7 | 全量套件隔离修复 | ✅ | `f1bad6b` | `backend/tests/test_treasury_service.py` |

串行门:1 → 2 → 3 → 4 → 5 → 6,逐任务 TDD(先红后绿)、逐任务提交,未跳步未合并步骤。

## 2. 测试口径

- 新增用例:`backend/tests/test_treasury_service.py`(41)+ `backend/tests/test_burnin_report_treasury.py`(7)= **48 个**。
- 全量:`python -m pytest tests/ -q`
  - base(主会话在同 base 上跑,`/tmp/batch25B-base.txt`):`51 failed, 1737 passed, 25 skipped, 17 errors`
  - 本线终态(`/tmp/s1-5-final2.txt`):`51 failed, 1785 passed, 25 skipped, 11 deselected, 207 warnings, 17 errors in 282.77s`
  - 归一化差集 `comm -13 /tmp/batch25B-base-fails.txt /tmp/s1-5-final2-fails.txt` → **空**(零新增失败);`comm -23` 也为空(未修好也未破坏既有失败集)。
  - passed 增量 1737 → 1785 = +48,与新增用例数一致。

## 3. 偏差清单

### 3.1 规格 anchors 行号漂移(逐条校验,以代码为准)

| 规格写的 anchor | 实际位置 | 说明 |
|---|---|---|
| `coin_service.treasury_credit` @ `coin_service.py:166-192` | `coin_service.py:474-482` | 且实现已换成 `treasury_credit_pending`(`:335-369`)的**方言原生 upsert**(pg/sqlite `on_conflict_do_update`,其他方言才走守卫 UPDATE→insert),不是规格描述的 `IntegrityError` rollback+retry |
| `coin_service.treasury_debit` @ `:195-207` | `coin_service.py:484-496` | 语义一致(守卫扣减 + 返回 rowcount>0) |
| `coin_service.treasury_balance` | `coin_service.py:467-471` | 规格未给行号 |
| `coin_service.settle` treasury 路由 @ `:122-146` | `settle_pending` @ `:372-413`(`treasury:` 前缀路由在 `:396-398`) | settle 已拆成 pending/commit 两层 |
| `_pay_wage` @ `duty_service.py:125-146` | `duty_service.py:147-168`(提示词已给,核实无误) | 镇长加成在 `:157-158` |
| `set_wallet_cache` @ `:149-160` | `duty_service.py:171-182` | — |
| `_resident_work_effect` 入账 @ `shop_effects.py:283` | `shop_effects.py:315`(函数 `:294-342`) | — |
| `_gift_effect` 分成 @ `:203-205` | `shop_effects.py:200-205` 附近(函数 `:171-227`) | — |
| `_tip_effect` 分成 @ `:255-261` | `shop_effects.py:236-264` | — |
| `run_nightly_jobs` @ `nightly_cron.py:28-40`,`RUN_HOUR=0 RUN_MINUTE=30` | `nightly_cron.py:63+`,**`RUN_HOUR=7 RUN_MINUTE=0`**(北京晨锚) | 规格的 UTC 00:30 已过时 |
| M1–M6 settings 块 @ `config.py:354-373` | 该块仍在,但类尾已追加 `POLIS_OFFICE_` / `POLIS_OPINION_` 两块 | 本线的 `town_*` 块追加在**最后**,未改他人行 |
| alembic 链头 `040_residents_creator_nullable` | **`047_add_issue_stances`**(045 → 046_add_offices → 047) | 见 §4 |

### 3.2 实现偏差(与规格文字不同,逐条给理由)

1. **`tax` 的 upsert 实现**:规格要求"守卫 UPDATE → 零行 insert → `IntegrityError` rollback+retry"。实际按**当前 `coin_service.treasury_credit_pending` 的方言原生 `ON CONFLICT DO UPDATE`** 抄(pg/sqlite),非方言保留守卫 UPDATE→insert 兜底。理由:规格描述的是旧版 coin_service,现役代码路径已换成方言 upsert,"逐字抄已验证代码路径"优先。并发无丢更新有测试(`test_concurrent_tax_no_lost_update`)。
2. **REST 路径 `/town/treasury` → `/townhall/treasury`**:挂新 router 必须改 `backend/app/main.py`,而本批 `main.py` 归工程健康线独占(并行纪律)。handler 与路径无关,收口时一行 `include_router` 即可另出 `/town/treasury` 别名。鉴权按规格:普通登录(非 admin),该路径不挂任何写动词。
3. **不碰 `shop_service.purchase` 的 charge 边界税**:规格 §2 任务 3 标为"可选"、并建议 MVP 只做销售税。`shop_service.py` 因此**未修改**(与 §8 will_modify 清单有出入,已登记)。
4. **不碰 `app/agent/phases/execute/basic.py`(EAT 餐费)与 `'sink'` 分账**:规格 §7 明确 MVP 不重定向(会改变货币供给模型)。§8 will_modify 里列了 `basic.py`,实际未修改。
5. **不碰 `coin_service.py`**:规格 §8 建议"优先只读复制惯式到 treasury_service,尽量不改 coin_service"。已做到零修改(降低与 S1-1 的冲突面)。
6. **§6 探针形态改用可计算代理**:规格要"余额时间序列 + 收支流向分类累计"和"funded 发薪成功率"。二者都依赖流水账,而镇流水按 §7 明确不进 `transactions`、本模块也不新建流水表。落地为:
   - 一次运行 = 时间序列的一个采样点(`burnin_report.py` 每日跑,序列由多次运行拼);
   - 收支流向 → **货币分布**(镇 / 居民 / NPC 侧货币量 / 镇占比),直接判读"有界货币 vs 单调通胀";
   - 发薪覆盖率 → **财政续航天数** = 镇余额 ÷ 当日应发工资账单(含镇长加成),`<1` 天标欠薪风险。
7. **新增 `town_ws_min_delta_sc` 旋钮(规格未列)**:规格 §2 任务 6 要求"高频微额抽税不要逐笔广播",需要一个阈值才能落地。默认 `0` = 完全不广播,nightly 是设计中的低频触发点。
8. **`.env.example` 已就地追加**(不只是列清单):沿用 S2-1 / S1-3 两条已合并线的先例,追加式、在文件末尾,合并冲突可平凡解。清单同时列在 §5 供收口核对。
9. **既有语义记录(未改)**:`_resident_work_effect` 的分账按 `item.price_sc * qty` 计,不按集市日折后实付价——折扣日居民+镇拿到的总额略高于买家付出。这是 pre-S1-5 行为,本线未改,仅在集成测试里把折扣钉死为 1.0 以隔离进程级 active-event 缓存污染(见 `f1bad6b`)。

### 3.3 冻结门与红线自查

- **S2-1 三道冻结门**:`_pay_wage` 镇长加成仍读 `meta_json['mayor'] × election_mayor_wage_bonus`(只改资金来源,不改加成语义,`test_mayor_bonus_funded_from_town`);未碰 `_execute_outcome`;未碰 `current_mayor` 任何消费者。`tests/test_office_service.py`、`tests/test_office_integration.py`、`tests/test_duty_service.py`、`tests/test_m1_economy.py` 零改动通过。
- **不碰区域**:`civic_service` / `election_service` / `proposal_service` / `app/lab` 内核、`transactions` 的 FK —— 全部零改动(`app/lab/apply.broadcast_world_changed` 只是被调用,未修改)。
- **`nightly_cron.py`**:只新增 1 个独立 `try/except` 块,追加在既有治理块之后;wiring guard 断言 `close_due_polls` / `term_check` 仍在其之前(既有块未被移动)。
- **`config.py`**:只在 `Settings` 类尾追加 `town_*` 块,`POLIS_OFFICE_` / `POLIS_OPINION_` 零行改动。
- **财政数字永不进 NPC prompt**:写成硬断言 `test_treasury_numbers_never_enter_npc_prompt`。
- **未提交 `backend/skills_world_dev.db`**:全程显式 `git add <path>`,每次提交前 `git status --porcelain` 核对暂存区。
- **未 push / 未合并 / 未部署 / 未碰 vm212**。

## 4. 迁移占位登记

| 项 | 值 |
|---|---|
| 文件 | `backend/alembic/versions/NNN_add_town_treasury.py` |
| `revision` | `NNN_add_town_treasury`(**占位符**) |
| `down_revision` | `047_add_issue_stances`(实测链头;规格写的 `040` 已过时) |
| 实测链 | `045_residents_creator_nullable` → `046_add_offices` → `047_add_issue_stances` → `NNN_add_town_treasury` |
| 本 worktree `alembic heads` | 单头 `NNN_add_town_treasury (head)` |
| 收口动作 | 并行的 S2-5 `NNN_add_policies` 同样接 047 → 合并时按落地顺序线性化为 048/049,重跑 `alembic heads` 单头硬门。**`tests/test_treasury_service.py::test_town_treasury_migration_single_head` 里的 `"NNN_add_town_treasury"` / `"047_add_issue_stances"` 两处字面量需随之改号。** |
| 迁移实测 | `alembic stamp 047_add_issue_stances && alembic upgrade head` → 建表成功,列 `key VARCHAR(100) PK / balance_sc INTEGER NOT NULL DEFAULT '0' / updated_at DATETIME NOT NULL`;`alembic downgrade -1` → 表已删。(整链 `upgrade head` 在 sqlite 上跑不通,卡在既有的 `003_foundation_upgrade`(SQLite 不支持 ALTER 约束)——**pre-existing,与本线无关**,生产走 Postgres。) |

## 5. 收口时要进 `.env.example` 的配置清单

已在 `backend/.env.example` 末尾追加(如收口选择集中管理,以此表为准):

| env | Settings 字段 | 默认 | 语义 |
|---|---|---|---|
| `TOWN_TREASURY_ENABLED` | `town_treasury_enabled: bool` | `false` | 主闸。关闭 = 字节级回落现状(工资继续 MINT、售货不抽税、nightly 整块跳过、不发 WS) |
| `TOWN_TAX_RATE_SALES` | `town_tax_rate_sales: float` | `0.1` | 居民售货销售税率(镇财政主入口) |
| `TOWN_TAX_RATE_GIFT` | `town_tax_rate_gift: float` | `0.0` | 送礼 / 打赏 creator 分成税率;0 = 旋钮在但不咬 |
| `TOWN_WAGE_UNFUNDED_POLICY` | `town_wage_unfunded_policy: str` | `skip` | 镇财政见底:`skip`=欠薪(不入账不 MINT) / `mint`=回落 pre-S1-5 铸造 |
| `TOWN_PUBLIC_WORKS_DAILY_SC` | `town_public_works_daily_sc: int` | `0` | nightly 公共支出预算;0 = 只写对账时间戳不拨款 |
| `TOWN_WS_MIN_DELTA_SC` | `town_ws_min_delta_sc: int` | `0` | `treasury_changed` 广播阈值;0 = 不广播(防高频微额刷屏) |

生产默认值(是否开主闸、税率取多少)按规格 §7 **等拍板**,本线不改生产默认。

## 6. 探针出数(seeded,`test_seeded_treasury_probe_numbers` 可复现)

`scripts/burnin_report.py` 新增 `fetch_treasury_snapshot / treasury_money_split / treasury_wage_runway / render_probes_s15`,已接入 `_run()` 的报告拼装。

实验组(`town_treasury_enabled=True`,镇财政 120 SC,居民 mayor 30 + clerk 10,镇长加成 1.2):

```
== 拟真探针（S1-5 验收：镇财政闭环）==
  镇财政余额 = 120 SC（最近变动 2026-07-25T12:10:55+00:00；本表无流水账，一次运行 = 时间序列一个采样点）
  货币分布：镇 120 / 居民 40（2 个账户），NPC 侧货币量 160 SC，镇占比 0.75
  发薪覆盖代理：日工资账单 17 SC（2 名在职），财政续航 7.06 天
  nightly 公共支出最近一次 = 2026-07-25T12:10:55+00:00
    （目标形态：余额在税入与薪出之间波动、可为负压力，续航偶尔跌破 1 天 = 叙事张力来源）
```

对照组(`town_treasury_enabled=False`):

```
  镇财政余额 = 0 SC
  货币分布：镇 0 / 居民 40（2 个账户），NPC 侧货币量 40 SC，镇占比 0.0
  发薪覆盖代理：日工资账单 17 SC（2 名在职），财政续航 0.0 天 ⚠️ 欠薪风险
  nightly 公共支出最近一次 = -
    （对照组，开关关：镇余额恒 0、镇占比恒 0、续航恒 0，发薪 100% 靠 MINT——货币供给单调增）
```

关键数字:日工资账单 `17 SC` = 镇长 `10 × 1.2 = 12` + 文书 `5`(无 duty 的居民不计入);镇占比 `0.75`;续航 `120 / 17 = 7.06` 天。真实 burn-in 出数须等主闸在 vm212 打开后跑,本轮只给 seeded 基准。

## 7. 给 S2-5 的接口冻结声明

`backend/app/services/treasury_service.py` 顶部 docstring 已写入同一份声明。**以下签名冻结,变更须先在本报告显式声明。**

```python
# backend/app/services/treasury_service.py
TOWN_KEY = "town"                  # 单镇 MVP 的镇键
LAST_SPEND_KEY = "town_last_spend_at"   # ConfigService/system_config 的时间戳 key

async def tax(db: AsyncSession, amount: int, reason: str = "") -> None
async def disburse(db: AsyncSession, amount: int, reason: str = "") -> bool
async def balance(db: AsyncSession) -> int

# 附属(非冻结,但已稳定)
async def tax_pending(db, amount: int, reason: str = "") -> None      # flush-owned，caller 持事务
async def notify_changed(db, *, delta: int, reason: str = "") -> bool # treasury_changed 广播
async def run_public_spending(db) -> int                              # nightly 作业
```

**语义契约(调用方必须遵守):**

- `tax`:`amount <= 0` **静默 no-op 且不建行**;账户按需 upsert;`reason` 只为可读性,**不落库**(镇无流水表)。内部 `commit`。
- `disburse`:`amount <= 0` → `False`;余额不足 / 账户不存在 → **返回 `False`,不抛异常、不 `rollback`**(零行守卫命中绝不 rollback 是 `MissingGreenlet` 回归门)。内部 `commit`。
- `balance`:账户不存在返回 `0`,**纯读、不 upsert**。
- 三者都 `synchronize_session=False`:调用方不能读缓存的 ORM 行,必须经 `balance()` 重新 SELECT。

**财政类政策条目怎么对接(S2-5 的 `_execute_outcome` / policy effect):**

1. **税率类条目**(`tax_rate` / 销售税 / 送礼税):`settings.town_tax_rate_sales` / `town_tax_rate_gift` 是**静态 config**。S2-5 若要让政策热改税率,推荐路径是 policy 的 value 落 `system_config`,再由本模块在 `_skim_town_tax` 里改读 `ConfigService.get("town_tax_rate_sales", default=settings.town_tax_rate_sales)`——**这一步本线未做**(S2-5 存储层尚未落地),接线时改 `backend/app/services/shop_effects.py::_skim_town_tax` 一处即可,`tax/disburse/balance` 签名不变。
2. **支出类条目**(医疗补贴 S5-8 / 建设费 / 实验楼预算):调 `await treasury_service.disburse(db, amount, reason="<policy_key>")`,**必须检查返回值**——`False` = 镇财政见底,按各自策略降级(本模块自己的先例:`town_wage_unfunded_policy`)。禁止在 `False` 分支 `rollback`。
3. **收入类条目**(罚款 / 遗产充公 S5-9):调 `await treasury_service.tax(db, amount, reason="...")`,无返回值,失败靠调用方 `try/except` fail-open。
4. **余额读取**:`await treasury_service.balance(db)`;**不要**在 per-resident 循环里调(镇账户是单行,批量场景读一次复用,性能红线)。
5. **门控**:所有对接点须自带 `if settings.town_treasury_enabled` 或等价门,关闸时字节级回落——本模块的服务层**不**内建主闸(除 `run_public_spending` / `notify_changed` 外),门在调用点。
6. **WS**:如需政策落地后播报余额变化,调 `notify_changed(db, delta=..., reason=...)`,不要自造 `treasury_changed` envelope。

## 8. 未完成 / 待收口项

| 项 | 原因 / 需要谁做 |
|---|---|
| 迁移重编号 `NNN` → `048`/`049` + 测试字面量同步 | 与 S2-5 的落地顺序有关,收口时主会话统一线性化 |
| `/town/treasury` 别名(改 `main.py` 挂 router) | `main.py` 本批归工程健康线独占,收口后一行 include |
| 税率热改走 `ConfigService`(S2-5 政策接线) | 依赖 S2-5 policies 存储层落地,见 §7.1 |
| 生产默认值(是否开 `town_treasury_enabled`、税率取值) | 规格 §7 明确"等拍板",非本线决定 |
| 真实 burn-in 出数写回 `PROGRESS.md` | 需主闸在 vm212 打开后跑;本轮只给 seeded 基准,且本批禁碰 vm212 |
| `docs/ROADMAP.md` 更新 | 按并行纪律不碰,收口时主会话统一更新 |
