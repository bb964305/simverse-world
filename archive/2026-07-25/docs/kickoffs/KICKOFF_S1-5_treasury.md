# Kickoff S1-5 — 镇财政闭环(税 / 薪 / 公共支出)

> **结论先行。** 本模块要建的是一个**全新的第三类账户**:`town_treasuries`(镇财政)。当前世界里**不存在任何镇级 / 集体 / 公共账户**——钱只有两处:`User.soul_coin_balance`(玩家)与 `resident_treasuries.balance_sc`(单个居民)。所谓"镇财政闭环"必须新建表、新建 `TreasuryService.tax/disburse`,两者是 `coin_service.treasury_credit/treasury_debit`(`backend/app/services/coin_service.py:166-207`)原子范式的**逐字薄封装**,而**不是** `User` 版的 `charge/reward`。闭环的三段是:税(购买 / 送礼 / 打赏 / 售货抽成 → 镇财政)→ 薪(镇财政 → 居民工资,拦截 `duty_service._pay_wage`)→ 公共支出(镇财政 → 建设 / 补贴 / 镇长工资)。独立门控 `town_treasury_enabled` 默认 **False**,关闭时字节级回落到现状(工资继续"凭空铸造",购买不抽税)。
>
> **对齐方案。** 见 `docs/SOCIETY_EXPANSION_PLAN.md:37`(§2 机制总表 S1-5 行:"小额税 → 镇财政 → 镇长工资 + M5 建设费 + 实验楼预算 + 医疗补贴;税率进政策表")、`docs/SOCIETY_EXPANSION_PLAN.md:217`(§6 接口面预告经济行:`town_treasury` 账目表 / `TreasuryService.tax / disburse` / `GET /town/treasury`(玩家只读)/ `treasury_changed` / `ECON_` 前缀)、`docs/SOCIETY_EXPANSION_PLAN.md:76`(§3.1 实验楼预算走镇财政、镇长财政排序权是缰绳)、`docs/SOCIETY_EXPANSION_PLAN.md:138`(§4.1 医疗补贴进政策表、遗产无亲充公镇财政的下游)。
>
> **全局纪律(全部写进本文档,后文逐条落实)。**
> - 规则做骨架、LLM 做血肉;本模块**零 LLM 边际成本**(纯规则:抽税是乘法取整,发薪是搬账,公告搭文书 / digest 现有调用,不新增任何 LLM 调用)。
> - 独立门控 `town_treasury_enabled` 默认 **False**;关闭时所有税 / 薪 / 支出 hook 字节级回落现状。
> - 迁移号只写占位符 **NNN**;落地时按当时链头定(**现链头 = `040_residents_creator_nullable`**,已 `alembic` 版本目录核实为唯一数字链头)。
> - Alembic 链尾单头校验;写路径原子(条件 UPDATE + upsert,禁读改写);WS 新事件带 revision/seq 锚;性能红线 tick 循环每居民查询 +1 以内;所有 hook fail-open,财政异常绝不打断 tick / 购买。

---

## 1. 现状锚点(逐文件逐行核实,只引用已核实 file:line)

> 本节所有 file:line 均来自 reader 逐行核实的 anchors;**未核实的行号一律不写**。凡 plan 假设但代码里不存在的,按"现状缺口"如实标注,不编接口。

### 1.1 现状缺口:镇级账户不存在(本模块的立足点)

**现状缺口(硬事实)。** 世界里没有任何镇 / 集体 / 公共账户。钱只有两个存储:
1. `User.soul_coin_balance`(玩家钱包,`coin_service.charge/reward/transfer` 的操作对象);
2. `resident_treasuries.balance_sc`(单个居民账户,slug 主键)。

因此 plan 里写的 "`town_treasury` 账目表 / 镇财政" 是**全新的第三类账户**,不能声称"扩展现有镇账户"。

### 1.2 原子化范式:coin_service 的四个模板(本模块要逐字抄)

- **`charge()`** — `backend/app/services/coin_service.py:22-48`。对 **User** 的原子守卫扣款:`update(User).where(User.id==user_id, User.soul_coin_balance >= amount).values(soul_coin_balance=User.soul_coin_balance - amount).execution_options(synchronize_session=False)`(`coin_service.py:32-37`);`result.rowcount==0` → 余额不足,**不 rollback** 直接返回 False(rollback 会 expire caller 的 ORM 对象 → `MissingGreenlet`,见 `coin_service.py:38-45`)。成功后加 `Transaction(user_id, -amount, reason)` 再 `db.commit()`。**这是本模块原子性一节要引用的规范扣款惯式。**
- **`transfer()`** — `coin_service.py:51-80`。用户→用户原子 P2P:守卫扣款(`57-61`)→ 无守卫入账(`67-71`);若入账 `rowcount==0`(收款方缺失)→ `db.rollback()`(`75`)。两条腿 + 两条 Transaction 一起 commit(`77-79`)。**本模块 funded 发薪(镇→居民)要镜像这个 debit→credit 两段式。**
- **`treasury_credit(db, slug, amount, reason)`** — `coin_service.py:166-192`。**slug 键账户入账(upsert)的模板**:`update(ResidentTreasury).where(resident_slug==slug).values(balance_sc=ResidentTreasury.balance_sc + amount)...`(`172-177`);`rowcount==0` → insert 新行 commit,`IntegrityError` → rollback + retry update(`178-191`)。守卫 `amount<=0`(`170`)。**无 transactions ledger 行**(合成账户不能 FK `users.id`,`167-169`)。镇财政入账逐字抄这个,换成镇键表。
- **`treasury_debit(db, slug, amount, reason) -> bool`** — `coin_service.py:195-207`。**slug 键账户支出的模板**:`update(ResidentTreasury).where(resident_slug==slug, balance_sc >= amount).values(balance_sc=balance_sc - amount)...`(`200-205`);commit;返回 `rowcount>0`。`amount<=0` no-op。余额不足返回 False(**不抛异常**)。镇财政支出逐字抄这个守卫扣减。
- **`settle(db, hold_id, splits)`** — `coin_service.py:122-146`。按 splits 分账:收款方前缀 `'treasury:<slug>'` 路由到 `treasury_credit`(`139-140`),`'sink'` 被消费,否则 `reward()` 给 user(`141-142`)。守恒:`sum(splits)==hold.amount` 否则 `CoinError`(`131-133`)。**现有的 recipient 路由约定** —— 镇财政收款 token(如 `'treasury:town'`)可自然扩展进这里。

### 1.3 现有 slug 键账户模型(新表要照抄形状)

- **`class ResidentTreasury(Base)`** — `backend/app/models/resident_treasury.py:9-32`。表 `resident_treasuries`,列:`resident_slug: Mapped[str]` `String(100)` primary_key(`27`);`balance_sc: Mapped[int]` `Integer` default=0(`28`);`updated_at: Mapped[datetime]` `DateTime(timezone=True)` default/onupdate `now(UTC)`(`29-31`)。Docstring(`17-22`)记录了故意的偏差:**treasury 流水不进 transactions ledger**(`transactions.user_id` 硬 FK → `users.id`)。**新 `TownTreasury` 模型照抄这个形状**(slug PK → 镇键,`balance_sc`,`updated_at`)。
- **迁移模板** — `backend/alembic/versions/032_add_lab_core.py:35-40`。`create_table('resident_treasuries')`:`resident_slug String(100) primary_key`(`37`)、`balance_sc Integer nullable=False server_default='0'`(`38`)、`updated_at DateTime(timezone=True) nullable=False`(`39`);downgrade `drop_table`(`130`)。**新迁移镜像这段。**

### 1.4 税收入口(要接线的确切位置)

- **购买 charge 边界** — `backend/app/services/shop_service.py:64-98`。唯一购买入口:precheck(`79`)→ `total = price_sc*qty`(`81`)→ M1 市场日折扣(`84-86`)→ `charge(user_id, total)`(`87`)→ `Purchase` 行(`91-94`)→ `commit`(`95`)→ `apply_effect`(`97`)。**purchase-tax hook 必须活在这个 charge+commit 事务边界内**(当前 total 全额从 user 扣走,`resident_work` 情形全额入某居民账户,无镇抽成)。
- **居民售货入账** — `backend/app/services/shop_effects.py:267-311`(`kind='resident_work'`,M1 F1.4)。`earned = item.price_sc*qty`;`coin_service.treasury_credit(db, creator_slug, earned, reason=f'work_sold:{code}')`(`283`);扣库存,售罄下架。**这是"购买时钱移动"的 hook:玩家的 charge 资助了居民账户。销售税从这里 skim 一刀进镇财政。**
- **送礼 / 打赏** — `backend/app/services/shop_effects.py:171-227`(gift)与 `236-264`(tip)。gift 给 `resident.creator_id` 20% 分成走 `reward()`(`203-205`),含 `relationship_boost` 消费端(P2-2,`214-225`);tip:`post.tips_sc += amount`,creator 80% 分成(`255-261`)。**都是 user→user 的 `reward()` 流,无 treasury 参与——额外的抽税候选点。**

### 1.5 发薪路径(闭环的"薪"段,要决定是否拦截)

- **`_pay_wage(db, resident)`** — `backend/app/services/duty_service.py:125-146`。**今天的薪资路径**。门控于 `settings.npc_economy_enabled`(`130-131`);`wage = int(perk(resident,'wage_sc', settings.npc_default_wage_sc))`(`132`);M6 镇长加成:`if settings.election_enabled and resident.meta_json['mayor']` → `wage *= settings.election_mayor_wage_bonus`(`134-136`);然后 `coin_service.treasury_credit(db, resident.slug, wage, reason='duty_wage')`(`141`)+ 写穿 `set_wallet_cache`。**关键现状缺口:工资现在是凭空 MINT 的(从无到有入账居民账户),没有任何镇账户资助来源。** 真正的税→薪闭环要求 `_pay_wage` 先从镇财政 `treasury_debit` 再入账居民——这是对 `_pay_wage` 的**行为改动**,不是纯加法。
- **`on_work(db, resident, market_day)`** — `duty_service.py:95-122`。WORK 派发器;成功产出后设 redis 冷却(`sv:duty_work:<id>`,`DUTY_WORK_COOLDOWN_HOURS=20`,市场日减半)再调 `_pay_wage`(`117-119`)。fail-open。**每日发薪节律的闸门。**
- **`set_wallet_cache(db, resident, balance)`** — `duty_service.py:149-160`。写穿:`meta_json['wallet']=int(balance)` + `flag_modified`。decision prompt 读钱包压力无需额外查询。**任何 funded 发薪改动必须同步刷新这个 cache。**

### 1.6 现有支出 / sink 消费端(公共支出的资金来源候选)

- **EAT 餐费** — `backend/app/agent/phases/execute/basic.py:44-46`。`cost = settings.npc_meal_cost_sc`;`paid = await coin_service.treasury_debit(db, resident.slug, cost, reason='meal')`;`balance = await coin_service.treasury_balance(db, resident.slug)`。**现有的 treasury-DEBIT 消费端——餐费币从居民账户离开后消失(不进任何镇账户)。** 税模型可把这个 sink 重定向进镇财政。
- **`'sink'` settle 分账** — `coin_service.py:137-138`(见 1.2 settle)。币离开流通。**若要税资助公共支出,这也是重定向候选,但改它会改变货币供给模型(见 §7)。**

### 1.7 镇长身份(若支出需"谁授权")

- **`install_mayor / current_mayor`** — `backend/app/services/election_service.py:127-179`。镇长**双存**:(1) `Resident.meta_json['mayor']=True`(其余清零),`flag_modified` + commit(`137-149`);(2) `ConfigService(db).set('current_mayor', slug, group='civic', updated_by='election')` 进 `system_config`(`154-158`)。`current_mayor(db)` 经 `ConfigService.get` 读回(`175-179`)。**镇财政若需"谁授权拨款",身份用 `current_mayor()` 解析——没有专门的 mayor FK 列。**

### 1.8 nightly 挂点(税收结算 / 发薪的批处理位置)

- **`run_nightly_jobs()`** — `backend/app/tasks/nightly_cron.py:28-40`。nightly 宿主(~00:30 UTC,`RUN_HOUR=0 RUN_MINUTE=30`,`17-18`)。每责任一个独立 `try/except` + `async with async_session() as db:`(digest `30-35`、commission 过期 `37-40`)。M2/M3/M6 hook 已挂在同文件(arc `77-84`、civic `87-94`、seed `98-105`、election `109-116`、npc_voting `119-126`)。**nightly 税收结算 / 公共支出作业照这个块形状新增一个独立 try/except。**

### 1.9 门控与配置层(新 flag 的落点)

- **M1-M6 settings 块** — `backend/app/config.py:354-373`。`npc_economy_enabled=True`(`356`,经济总闸)、`npc_default_wage_sc=5`(`357`)、`npc_meal_cost_sc=2`(`358`)、`npc_wallet_pressure_threshold=3`(`359`)、`civic_polls_enabled=True`(`368`)、`election_enabled=True`(`371`)、`election_mayor_wage_bonus=1.2`(`372`)。**每里程碑一个独立布尔闸的约定** —— 新镇财政 feature 在这块加自己的闸 + 税率常量。
- **`ConfigService.get/set`** — `backend/app/services/config_service.py:14-49`(`get(key, *, default=None)` `14`;`set(key, value, *, group, updated_by)` `27`;`get_group(group)` `50`)。`system_config` KV 存,`current_mayor` 就存这里。**镇财政可把标量策略(`tax_rate`、`last_collected_at`)存这里,而非新列,和 mayor provenance 同套。**

---

## 2. 任务切分

> 串行门:任务 1(新表 + 迁移 + 模型)全绿并提交后才开 2(TreasuryService);2 全绿才开 3(税收 hook)与 4(发薪拦截)。5(nightly 结算)与 6(REST/WS)可在 3/4 之后并行。每任务独立提交,commit 信息带任务号(如 `s1-5-2: TreasuryService.tax/disburse wrapping coin_service atomics`)。

### 任务 1 — `town_treasuries` 表 + `TownTreasury` 模型 + 迁移

**改哪些文件:**
- 新建 `backend/app/models/town_treasury.py`(照抄 `resident_treasury.py:9-32` 的形状)。
- 新建 `backend/alembic/versions/NNN_add_town_treasury.py`(镜像 `032_add_lab_core.py:35-40` 的 `create_table` + downgrade `drop_table`;`down_revision` **落地时按当时链头定,现链头 `040_residents_creator_nullable`**)。
- 修改 `backend/app/models/__init__.py`(导出 `TownTreasury`,让 Base metadata 收录)。

**新表结构 `town_treasuries`(列名 + 类型,逐列):**

| 列 | 类型 | 约束 / 默认 | 说明 |
|---|---|---|---|
| `key` | `String(100)` | primary_key | 镇键;单镇 MVP 固定 `'town'`(多镇时 = town_slug) |
| `balance_sc` | `Integer` | `nullable=False`, `server_default='0'`, default=0 | 镇财政余额(soul coin) |
| `updated_at` | `DateTime(timezone=True)` | `nullable=False`, default/onupdate `now(UTC)` | 最近变动时间 |

> **审计说明(硬事实,写进模型 docstring)。** 镇财政流水**不进** `transactions` ledger(`transactions.user_id` 硬 FK → `users.id`,合成账户被拒;偏差已在 `resident_treasury.py:17-22` 与 `coin_service.py:167-169` 记录)。可审计性只依赖 `balance_sc` + `updated_at`,与 `resident_treasuries` 同待遇。标量策略(`tax_rate`、`last_collected_at`)存 `ConfigService`(`config_service.py:27`),不建新列。

**验收:** `alembic upgrade head` 后单头(见 §7 链尾校验);`TownTreasury` 可 import;`select` 空表返回 0 行(账户按需 upsert 创建)。

### 任务 2 — `TreasuryService.tax / disburse`(逐字封装 coin_service 原子惯式)

**改哪些文件:** 新建 `backend/app/services/treasury_service.py`。底层可复用 `coin_service` 的表无关惯式,或在 `coin_service.py` 增加镇键版 `town_credit/town_debit`(与 `treasury_credit/treasury_debit` 同构,只换 Model 与 PK 列)——二选一,推荐后者以复用已验证代码路径。

**接口签名(service method signatures):**

```python
# treasury_service.py — 全部 async def，db: AsyncSession 显式传入（不持有 session）
TOWN_KEY = "town"  # 单镇 MVP

async def tax(db: AsyncSession, amount: int, reason: str = "") -> None:
    """镇财政入账（抽税 / 售货抽成 / 罚款 / 遗产充公）。
    amount<=0 静默 no-op（保持 coin_service 守卫）。
    逐字复制 coin_service.treasury_credit 的 guarded-UPDATE→insert-on-zero-row→
    IntegrityError rollback+retry（coin_service.py:172-192），PK 换 key==TOWN_KEY。"""

async def disburse(db: AsyncSession, amount: int, reason: str = "") -> bool:
    """镇财政支出（发薪资金 / 建设费 / 补贴）。返回 rowcount>0。
    amount<=0 no-op 返回 False；余额不足返回 False（不抛异常，不 rollback）。
    逐字复制 coin_service.treasury_debit 的守卫扣减（coin_service.py:200-207），
    where(key==TOWN_KEY, balance_sc >= amount)。"""

async def balance(db: AsyncSession) -> int:
    """读镇财政余额，账户不存在返回 0（镜像 coin_service.treasury_balance）。"""
```

**REST 鉴权:** 本任务不出 REST(见任务 6)。
**WS 事件:** 本任务不出 WS(见任务 6)。
**验收:** 见 §5 单测(原子性 / 守卫 / no-op / 余额不足回 False)。

### 任务 3 — 税收 hook 接线(三处入口,全部门控 + fail-open)

**改哪些文件:** `backend/app/services/shop_service.py`、`backend/app/services/shop_effects.py`。

**接线点与语义(税 = 纯规则乘法取整,零 LLM):**
- **销售税** — `shop_effects._resident_work_effect`(`shop_effects.py:283`):居民售货 `earned` 入账前 skim `cut = int(earned * settings.town_tax_rate_sales)` 进 `TreasuryService.tax(db, cut, reason=f'sales_tax:{code}')`,居民实收 `earned - cut`。
- **送礼 / 打赏税(可选,同一 flag)** — `shop_effects._gift_effect`(`203-205`)/ `_tip_effect`(`255-261`):对 creator 分成再 skim 一刀(建议默认 `town_tax_rate_gift=0.0`,即默认不抽,留旋钮)。
- **购买边界税(可选)** — `shop_service.purchase()` 的 charge+commit 边界内(`87-95`):若要对**所有**购买抽税而非仅售货,在 total 里加镇税分量。**MVP 建议只做销售税(`_resident_work_effect`),其余留 0 默认旋钮**,避免一次改动改变整体货币模型(见 §7)。

**门控 + fail-open:** 每个 hook `if not settings.town_treasury_enabled: <原路径不变>`;`TreasuryService.tax` 包 `try/except` + `logger.warning`,税失败绝不打断购买(匹配现有 economy hook 的 fail-open 纪律)。
**验收:** 见 §5(税额断言 + flag=False 零抽成)。

### 任务 4 — 发薪拦截:funded wage(闭环的"薪"段,行为改动)

**改哪些文件:** `backend/app/services/duty_service.py`。

**改动 `_pay_wage`(`duty_service.py:125-146`):** 在 `treasury_credit(resident.slug, wage)`(`141`)之前插入镇财政扣款,镜像 `transfer()` 的 debit→credit 两段式(`coin_service.py:51-80`):

```python
if settings.town_treasury_enabled:
    funded = await treasury_service.disburse(db, wage, reason=f'wage:{resident.slug}')
    if not funded:
        # 镇财政见底：按策略回落——MVP 建议“减发/欠薪”而非凭空补
        wage = 0  # 或按 settings.town_wage_unfunded_policy 分档；见 §7
    # funded 时才继续既有 treasury_credit + set_wallet_cache
```

> **这是行为改动,不是加法(硬事实)。** 现状工资是凭空 MINT 的(`_pay_wage:141` 无资金来源)。闭环要求先 `disburse` 镇财政。flag=False 时**完全走现状**(继续 MINT,不 disburse)。镇长加成路径(`134-136`,门控 `election_enabled`)在 flag=True 时对**扣款额**同样生效——即镇财政为加成买单;若 `election_enabled=False` 则无加成,保持一致。funded 成功后必须刷新 `set_wallet_cache`(`149-160`),因 `synchronize_session=False` 会留 stale ORM 行。
**验收:** 见 §5(镇财政足额 → 居民入账 + 镇余额减;镇财政见底 → 按策略;flag=False → 现状 MINT)。

### 任务 5 — nightly 税收结算 / 公共支出作业(可选,批处理)

**改哪些文件:** `backend/app/tasks/nightly_cron.py`。

**新增一个独立 try/except 块**(照 `nightly_cron.py:107-116` `maybe_open_seasonal_election` 形状):

```python
try:
    from app.config import settings
    if settings.town_treasury_enabled:
        from app.services.treasury_service import run_public_spending
        async with async_session() as db:
            spent = await run_public_spending(db)   # 镇长工资 / 建设费 / 补贴按策略拨款
        logger.info("town public spending disbursed=%s", spent)
except Exception:
    logger.error("town treasury nightly failed", exc_info=True)
```

> MVP 里"镇长工资"若已在 `_pay_wage` 走 duty 路径,则本作业只处理**周期性公共支出**(如按 `ConfigService` 的 `last_collected_at` 做税收对账 / 建设费拨款占位)。**gating 跟 realism 夜间作业一致:闸在 cron 块里判**(`if settings.town_treasury_enabled`),不像 M2/M3/M6 那样 ungated。
**验收:** 见 §5(seeded 拨款守恒;flag=False → 作业整体跳过)。

### 任务 6 — REST 只读端点 + WS `treasury_changed` 事件(可选)

**改哪些文件:** 新建 / 修改 `backend/app/routers/...`(玩家只读 `GET /town/treasury`)。

- **REST 鉴权:** `docs/SOCIETY_EXPANSION_PLAN.md:217` 指定 `GET /town/treasury`(**玩家只读**)。返回 `{"balance_sc": int, "updated_at": iso, "tax_rate": float}`。玩家只读端点用普通登录鉴权(非 admin);若走 admin 视图(`GET /admin/treasury`)则**每端点**加 `admin: User = Depends(require_admin)`(auth 是 per-endpoint,非 router 级)。
- **WS 事件:** `treasury_changed`(plan §6 指定)。**带 revision/seq 锚**:镇余额显著变动时经 `world_revision_service.world_changed_event(*, revision, action, seq, event_id, occurred_at)` 组 envelope,`broadcast_world_changed(payload)` 广播;seq 复用 `OutboxEvent.id`(topic 语义),不新造计数器。**高频微额抽税不要逐笔广播**(会刷屏)——建议 nightly 或阈值触发一次。
**验收:** 见 §5(端点鉴权;envelope 带 seq/revision)。

---

## 3. 门控开关与默认值

**在 `backend/app/config.py:354-373` 的 M1-M6 块内新增(沿用该块 per-milestone 独立布尔闸约定):**

```python
# S1-5 镇财政闭环（独立门控，默认 False → 字节级回落现状）
town_treasury_enabled: bool = False        # 主闸；关闭时税/薪/支出全部走现状
town_tax_rate_sales: float = 0.1           # 居民售货销售税率（skim 进镇财政）
town_tax_rate_gift: float = 0.0            # 送礼/打赏税率，默认 0（留旋钮，默认不抽）
town_wage_unfunded_policy: str = "skip"    # 镇财政见底时发薪策略：skip=欠薪/减发；mint=回落凭空铸造
```

**默认值纪律(硬事实):**
- 主闸 `town_treasury_enabled` 默认 **False**。这是本模块的**关键区别**:M1-M6 既有闸(`npc_economy_enabled`/`arc_engine_enabled`/`civic_polls_enabled`/`election_enabled`)在 commit `5172f0e` 里默认 **True**,但**新增 flag 一律默认 False**(rollback-safe,对齐 realism 家族 `realism_enabled` + 三个 P2 独立闸的正确范式)。
- **关闭时字节级回落现状**:税 hook `if not settings.town_treasury_enabled: return`(原路径不变);`_pay_wage` 不 `disburse`(继续现状 MINT);nightly 作业整块跳过;REST/WS 不发 `treasury_changed`。
- 命名前缀 `town_`(对应 plan §6 的 `ECON_` 前缀族;env 变量 `TOWN_TREASURY_ENABLED` 等自动解析,BaseSettings 惯例)。运行时可变的标量策略(`tax_rate`/`last_collected_at`)若需热改,走 `ConfigService`(`config_service.py:27`),否则静态 config 即可。

## 4. 原子性要求

**统一纪律:写路径一律条件 UPDATE + upsert,禁止读-改-写。** 镇财政的入账 / 支出逐字复制 `coin_service` 已验证的两个惯式:

**A) 原子守卫扣减(镇支出 `disburse`,抄 `coin_service.py:200-207`):**
```python
result = await db.execute(
    update(TownTreasury)
    .where(TownTreasury.key == TOWN_KEY, TownTreasury.balance_sc >= amount)
    .values(balance_sc=TownTreasury.balance_sc - amount)
    .execution_options(synchronize_session=False)
)
await db.commit()
return result.rowcount > 0   # 余额不足 → rowcount==0 → False，不抛异常
```

**B) 原子入账 upsert(镇抽税 `tax`,抄 `coin_service.py:172-192`):**
守卫 UPDATE →`rowcount==0` 则 insert 新行 commit →`IntegrityError`(并发插入撞车)则 `rollback` + retry update。

**三条 API 硬约束(违反即出 bug,全部写进注释):**
1. **`amount<=0` 静默 no-op / False** —— `coin_service` 每个函数都有此守卫(`coin_service.py:170`),必须保留。
2. **零行守卫命中时绝不 `db.rollback()`**(当 caller 持有 ORM 对象):见 `charge()` 注释 `coin_service.py:38-45`——rollback 会 expire identity-map 里所有对象 → asyncio 下 `MissingGreenlet`。**只在真正写入发生后才 rollback**(如 `transfer()` 入账失败分支 `coin_service.py:73-76`,upsert 的 `IntegrityError` retry 分支)。
3. **`synchronize_session=False` 留 stale ORM 行** —— caller 必须 re-SELECT 余额(见 `set_wallet_cache` 写穿范式,`duty_service.py:149-160`);funded 发薪后刷新 `meta_json['wallet']` cache。

**funded 发薪的两段式(镇→居民,镜像 `transfer()` `coin_service.py:51-80`):** 先 `disburse(town)` 守卫扣减,**成功后**才 `treasury_credit(resident)` 入账。若 `disburse` 返回 False(镇财政见底),按 `town_wage_unfunded_policy` 处理,**不要在此 rollback**(尚未写入居民账户)。

**守恒断言:** 一次 funded 发薪前后,`town.balance + Σresident.balance` 守恒(币在账户间搬,不新增不销毁);flag=False 的现状 MINT 路径**不守恒**(这是现状,测试要区分两种模式)。

---

## 5. 测试口径

> 新建 `backend/tests/test_treasury_service.py`,copy `coin_service` 的测试范式。所有随机路径注入 seeded RNG;每个 flag-gated 路径带"关闭回落"断言。下列为**具体 `test_` 函数名清单**。

**单测(atomicity / conservation / guard):**
- `test_tax_credits_town_balance` —— `tax(100)` 后 `balance()==100`;账户按需 upsert 创建。
- `test_tax_amount_zero_is_noop` —— `tax(0)` / `tax(-5)` 不改余额、不建行。
- `test_disburse_guarded_decrement` —— 足额 `disburse(30)` → True 且余额 -30。
- `test_disburse_insufficient_returns_false_no_exception` —— 余额 10 时 `disburse(50)` → False,余额不变,**无异常无 rollback**。
- `test_disburse_amount_zero_is_noop_false` —— `disburse(0)` → False,余额不变。
- `test_concurrent_tax_no_lost_update` —— 并发多次 `tax` 经守卫 UPDATE / IntegrityError-retry,总额无丢更新(照 `coin_service` 并发 upsert 测试)。
- `test_concurrent_disburse_no_overspend` —— 并发 `disburse` 不会把余额扣成负数(守卫 `balance_sc >= amount`)。
- `test_funded_wage_conserves_total` —— funded 发薪前后 `town + resident` 守恒。
- `test_no_rollback_on_zero_row_guard` —— 断言零行守卫命中路径不调用 `db.rollback()`(防 `MissingGreenlet` 回归)。
- `test_town_treasury_not_in_transactions_ledger` —— 镇流水不产生 `Transaction` 行(FK 约束偏差核实)。

**单测(gating 回落,seeded):**
- `test_tax_hook_disabled_no_skim` —— `town_treasury_enabled=False` 时 `_resident_work_effect` 居民实收全额,镇余额为 0。
- `test_pay_wage_disabled_mints_as_before` —— flag=False 时 `_pay_wage` 走现状 MINT(镇财政不动,居民入账 = 现状)。
- `test_pay_wage_enabled_draws_from_town` —— flag=True 且镇财政足额 → 镇余额减 wage、居民入账 wage。
- `test_pay_wage_unfunded_policy_skip` —— flag=True 且镇财政见底 → 按 `skip` 策略欠薪(居民不入账 / 减发),不 MINT。
- `test_mayor_bonus_funded_from_town` —— `election_enabled=True` 时加成额同样由镇财政扣款(seeded mayor via `meta_json['mayor']`)。

**集成用例(端到端路径):**
- `test_purchase_resident_work_skims_sales_tax` —— 走 `shop_service.purchase()` 全链,resident_work 购买后镇财政 = `int(earned*rate)`,居民 = 余额。
- `test_nightly_public_spending_seeded` —— seeded fixture 下 `run_public_spending` 拨款守恒;`town_treasury_enabled=False` 时整块跳过零改动。
- `test_treasury_changed_ws_envelope_has_seq_revision` —— 触发显著变动,断言 WS envelope 带 `seq` / `world_revision_id`(复用 `world_changed_event`)。
- `test_get_town_treasury_readonly_auth` —— `GET /town/treasury` 普通登录可读;`GET /admin/treasury`(若实现)缺 `require_admin` → 401/403。
- `test_baseline_unchanged_when_disabled` —— flag 全 False 时,既有 economy 测试零改动通过(字节级回落断言)。

---

## 6. 探针出数定义

**`burnin_report.py` 新增镇财政探针(纯读 `town_treasuries.balance_sc` + `ConfigService`,零 LLM):**

- **a) 镇财政余额时间序列 + 收支流向。** 出数形态:随模拟时间的 `balance_sc` 曲线,叠加 tax 入账 / disburse 支出的分类累计(销售税 / 送礼税 / 发薪 / 建设费 / 补贴)。**目标形态:余额在税入与薪出之间波动,可为负并触发"加税议案"素材**(对齐 plan §2 S1-5 验收:`docs/SOCIETY_EXPANSION_PLAN.md:37`)。
- **b) 税负 / 发薪覆盖率。** 出数形态:funded 发薪成功率(`disburse` 返回 True 占比)随时间;镇财政见底导致欠薪的天数。**目标:成功率高但非恒 100%(财政有紧张期,叙事张力来源)。**
- **对照组(开关关时的形态):** `town_treasury_enabled=False` 时,镇财政余额**恒为 0**(无账户流动),发薪 100% 靠 MINT(货币供给单调增),税负 = 0。对照组曲线是"平线 + 单调通胀",实验组是"波动余额 + 有界货币"。首轮数值记入 `PROGRESS.md`。

---

## 7. 边界与"不碰区域"

- **串行门:** 任务 1(表 / 迁移 / 模型)全绿并提交后才开 2;2 全绿才开 3/4;每任务独立提交、commit 带任务号。方案与代码漂移以代码为准,偏差记 `PROGRESS.md` 后继续,不停等。
- **性能红线:** 读取面每居民查询 +1 以内。发薪已在 `_pay_wage` 走 duty 路径,`disburse` 是**同一居民事务内**多一条 UPDATE(不新增 per-resident SELECT);镇余额读取批量 / 缓存,不进 tick 的 per-resident 循环(镇账户是单行,一次 SELECT 复用)。**不要在 perceive/decide 的 per-resident 循环里查镇财政**(诊断报告点名过 perceive O(N²) 前科)。
- **货币供给模型不轻改(硬事实 + 决策点):** ① 现状工资是 MINT(凭空),闭环把它改成"从镇财政搬"——**这是行为改动**,必须由 `town_treasury_enabled` 门控,默认 False 保持 MINT。② EAT 餐费(`basic.py:44-46`)与 `'sink'` 分账(`coin_service.py:137-138`)是币 sink(币离开流通);**MVP 不重定向它们进镇财政**(重定向会改变货币供给模型),仅把居民售货抽成作为镇财政主入口。若后续要用 sink 资助公共支出,单独立项并在 `PROGRESS.md` 记模型变更。
- **transactions ledger 不碰:** 镇流水**不进** `transactions`(FK 约束,`coin_service.py:167-169`);不要为镇账户改 `transactions.user_id` 的 FK(会波及全钱包系统)。可审计性靠 `balance_sc + updated_at`。
- **镇长身份不发明列:** 若支出需 mayor 授权,用 `current_mayor(db)`(`election_service.py:175-179`)/ `meta_json['mayor']`(`142`),**不新增 `Resident.is_mayor` 列**;写 `meta_json` 必须 `flag_modified`(SQLAlchemy 不检测原地 JSON 变更)。
- **Lab 工程安全不变量 / prompt 隔离:** 不碰 `app/lab/` 审批门与安全不变量;镇财政余额等全局指标**永不进入 NPC prompt**(唯一例外是未来的公报机制)——本模块不向 prompt 注入财政数字。
- **fail-open 纪律:** 所有 economy hook `try/except` + `logger.warning`,财政失败绝不打断 tick / 购买(匹配 `_pay_wage`/`on_work`/discount 的 fail-open 现状)。
- **不合并、不部署:** 交付到"分支 CI 绿 + 探针出数 + PROGRESS 更新"为止;生产默认值(是否开 `town_treasury_enabled`、税率)等拍板。

---

## 8. 依赖与冲突声明

**依赖的前置模块:**
- **S0-1(REALISM/M 合并部署 + 开关生产默认值):** 本模块新增 flag 默认 False,不阻塞;但真实 burn-in 出数依赖 S0-1 世界跑起来。
- **无硬前置代码依赖**:plan §2 S1-5 依赖列为 "—"(只依赖既有已交付机制:`coin_service` 原子层、`duty_service` 发薪、`shop_effects` 售货)。可独立开工。
- **下游(本模块是其弹药):** S2-2 镇长财政排序权、S5-8 医疗补贴、S5-9 遗产充公镇财政、S2-5 税率进政策表——它们**读 / 写** `town_treasuries` 与 `TreasuryService`,本模块须把接口留稳(`tax/disburse/balance` 签名冻结)。

**会碰哪些文件(本模块 will_modify / will_create):**
- 修改:`backend/app/config.py`、`backend/app/services/coin_service.py`、`backend/app/services/duty_service.py`、`backend/app/services/shop_service.py`、`backend/app/services/shop_effects.py`、`backend/app/tasks/nightly_cron.py`、`backend/app/agent/phases/execute/basic.py`、`backend/app/models/__init__.py`。
- 新建:`backend/app/models/town_treasury.py`、`backend/app/services/treasury_service.py`、`backend/alembic/versions/NNN_add_town_treasury.py`、`backend/tests/test_treasury_service.py`。

**与其他 4 份 KICKOFF 的文件交集(逐条点名 + 串行/协调建议):**

| 交集文件 | 与哪些模块共碰 | 冲突性质 | 协调建议 |
|---|---|---|---|
| `backend/app/config.py` | **S2-1 offices、S2-5 policies、S1-1 声誉、S1-3 舆论**(全部要加 flag / 常量) | 高频文本冲突(都往 Settings 类加块);语义无冲突(各加各的独立 flag 块) | **串行编辑或约定行区间**:各模块把自己的 flag 追加到 M1-M6 块**之后**,按模块号顺序(S1-1 → S1-3 → S1-5 → S2-1 → S2-5)追加,merge 时几乎无重叠;注意 `config.py:373` 有 dangling 注释,别复制那个错误 |
| `backend/app/tasks/nightly_cron.py` | **S2-1 offices、S2-5 policies、S1-1 声誉、S1-3 舆论**(全部要挂 nightly 作业) | 高频文本冲突(都往 `run_nightly_jobs` 加 try/except 块;S2-1 加 `term_check` 块);语义隔离(各块独立 try/except) | **各加独立块、追加在现有 M2/M3/M6 块之后**;本模块的块 gating 跟 realism 一致(闸在 cron 里判)。merge 时按块顺序拼接,冲突易解 |
| `backend/app/services/coin_service.py` | **S1-1 声誉**(reader 交集表列出 S1-1 修改 `coin_service.py`) | 中:S1-1 可能在赊账 / 选人权重处碰 charge/reward;本模块加镇键 `town_credit/town_debit` 或纯在 `treasury_service` 复用 | **本模块优先只读复制惯式到 `treasury_service.py`,尽量不改 `coin_service.py`**;若必须加镇键版函数,追加在 `treasury_debit`(`207`)之后,与 S1-1 的改动点(charge/reward 区)物理隔离 |
| `backend/app/services/duty_service.py` | **S2-1 offices**(同改 `_pay_wage:125-146`,mayor 加成在 `135`) | **中:同函数 `_pay_wage`**——S2-1 保留镇长工资加成语义(锁 `135` 行加成回归门);S1-5 把 duty 工资资金流向改走 `town_treasury`(发薪 = 财政支出) | **串行 + 同函数协调**:S2-1 先锁定 `135` 行加成语义,S1-5 再改资金来源;两者都动 `_pay_wage`,merge 需手工合同一函数 |
| `backend/app/services/civic_service.py` | **S1-1、S2-5、S2-1**(均修改);S1-3 **只读不碰**——**本模块不碰** | 无(本模块 will_modify 不含 civic_service) | 无需协调 |
| `backend/alembic/versions/NNN_*.py` | **S2-5(`041_add_policies`)、S1-3(`041_add_issue_stances`)、S2-1(`NNN_add_offices`)** 均从 `040` 分叉 | **迁移多头冲突**:四份都从 `040` 分叉 → 产生多个 alembic head | **迁移号写占位符 NNN,down_revision 落地时按当时链头定**;merge 时按落地顺序重编号(如 041/042/043/044)串成单链,**每份 PR 合并前跑 `alembic heads` 校验单头**(见 §7 链尾校验) |
| `backend/app/models/__init__.py` | **S1-3(`issue_stance`)、S2-5(`policy`)、S2-1(`office`)** 也导出新模型 | 低文本冲突(都在 __init__ 加 import/export 行) | 追加导出行,merge 易解 |

**Alembic 链尾单头校验(硬门槛):** 本模块 PR 合并前必须 `alembic heads` 返回单头;因 S1-3/S2-5 各自也加迁移,**并行工作流会产生多头,须在 merge 时按落地顺序重编号 down_revision 串成单链**。占位符 `NNN`,现链头 `040_residents_creator_nullable`。
