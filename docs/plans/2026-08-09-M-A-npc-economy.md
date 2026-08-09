# M-A 经济内生化 — 实施计划(v2,经三 lens 对抗批判修订)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Spec: `docs/superpowers/specs/2026-08-09-npc-economy-design.md`(先通读)。
> Worktree: `/Volumes/data/dev/simverse-world-npc-economy`(branch `feat/npc-economy-ma`,base `0be94d2`)。**只许写这个 worktree。**
> 纪律:每 step 先写失败测试→跑出失败→实现→跑出通过→单独 commit(message 末尾贴真实 `Verified-by:` 行)。禁 --no-verify/amend/squash。测试:`cd /Volumes/data/dev/simverse-world-npc-economy/backend && python3 -m pytest tests/<file> -x -q`。基线口径:全套相对 54 失败(49 lab + 5 postpone)零新增。
> 风格:先读同目录邻近测试与被改文件全文再动手;注释密度/命名跟随现有代码;新中文文案与既有措辞一致。

## v2 批判修订要点(执行时的高危注意面,来自 3 个 critic 的 28 条发现)

1. **id vs slug**:`commissions.issuer_resident_id`/新列存 **Resident.id**,钱包(`resident_treasuries`)按 **slug** 记账——所有委托资金操作先 select Resident 解析 id→slug,测试 fixture 里故意让 id≠slug。
2. **payload_json 无 mutable 跟踪**(app/models/shop.py:23):库存扣减必须整段镜像 shop_effects.py:330-348 的"`payload = dict(item.payload_json or {})` → 改 → `item.payload_json = payload`"重赋值模式,就地改会静默丢失。
3. **commit 语义是本里程碑的头号故障面**:`treasury_service.tax`(:97-108)与 `coin_service.treasury_credit`(:484-491)**自带 commit**;`MemoryService.add_memory`(app/memory/service.py:94-96)**自带 commit**。凡"单事务"步骤只许用 pending 原语(`tax_pending`/`treasury_credit_pending`/`treasury_debit_pending`/`skim_tax_pending`/`kv_upsert_pending`),memory/feed 一律放 commit 之后 fail-open。
4. **pending 原语的 rollback 纪律**:任何循环里单次迭代失败,必须先 `await db.rollback()` 再 continue,否则悬挂的半笔 debit 会被后续无关 commit 落库烧钱。treasury_service 模块头"never rollback"军规只适用于 zero-row guard,不适用于写了半截的事务。
5. **`tax_carry_enabled` 独立闸**(v2 新增,默认 False):vm212 `TOWN_TREASURY_ENABLED` 在产已开,carry 不设闸=迁移+在产行为变更同车(红线)。关闸时 skim 逐字节等价旧 `int()` 截断。
6. **守恒公式**:carry 不是钱,不进货币总量;不变量是 `Δ(Σ居民余额+镇库) == 商队注入+摊位费−进口sink`(整数精确),carry 单独断言。
7. **测试必须用新 session 重读断言**(防同 session 读到未 commit 的 pending 改动而假绿)。

## 全局事实(Explore+critic 已核,直接信)

- `coin_service.treasury_credit_pending(db, slug, amount, reason="")`(coin_service.py:336):dialect-native upsert,flush-owned,校验 slug 非空≤100、amount 正 int(bool 拒),非法时 **raise CoinError**;`treasury_debit(db, slug, amount, reason="") -> bool`(:494):guarded UPDATE,**自带 commit**;`treasury_balance(db, slug) -> int`(:477)。三者全按 `resident_slug` 键。
- `shop_effects._skim_town_tax(db, gross, rate, reason) -> int`(:42-69):内部 `fiscal_policy_service.tax_rate(db, fallback=rate)` 取有效率、`treasury_service.tax`(**自 commit**);调用点 :245(gift)/:310(tip)/:338(resident_work)。tip 路径在哨兵 creator 分支后**没有保证到达的 commit**(shop_service.purchase :97 后不 commit、emit 不 commit)——薄委托必须保住自 commit 语义。
- `treasury_service.tax_pending`(:62-94):flush-owned dialect-native upsert 的标准写法(**显式带 updated_at**);`SystemConfig` 模型(app/models/system_config.py:9-15):`group: Mapped[str]` **非 Optional 无默认**——kv 写入必须带 `group`,否则 create_all 建表下 NOT NULL 直接炸。`ConfigService.set` 内部 commit,**禁用**于交易事务。
- `_charge_meal`(app/agent/phases/execute/basic.py:34-69):`treasury_debit(2 SC, "meal")`(:45)后**无条件** balance→`set_wallet_cache`(:46-47,赊账路径也刷),paid 才 commit-return;赊账分支 :52-68(`find_duty_resident("cafe_host"|"tavern_hub")` + memory + `relation_service.bump(d_familiarity=0.02)`);except 分支 :70-71 目前无 rollback(改转账后必须补)。EAT 分支 :181-197(satiety 恢复在扣费前)。
- dining:map_data.py:254 `{"cafe","tavern"}`;cafe=林晚秋 `cafe_host`(preset:111-116)、tavern=周大河 `tavern_hub`(:183-188),两 duty 无 `_WORK_HANDLERS` 条目(零工资)。
- 作品:`duty_service._maybe_list_resident_work`(:386-418),`Item(kind="resident_work", price_sc=15, payload={"creator_slug","stock":3})`;售出全模式 shop_effects.py:330-366(:330 dict 拷贝、:344-346 重赋值、:347-348 下架、:351-354 **creator None 守卫**、:356-357 作者 wallet 缓存、:358-366 售出 memory 措辞)。item 生命周期与居民解耦——creator 可能已被 purge(vm212 有存量孤儿)。
- 委托:commission_service.py 状态机 `open|accepted|completed|expired`;玩家 `accept`(:66)是 **guarded UPDATE**(`WHERE status='open' AND expires_at>now`)防并发;`complete`(:94)玩家路径铸币(不动);`expire_commissions`(:128)nightly #3 扫。发单方唯一:陈铁生(duty_service.py:221-232,reward 恒 8,`issuer_resident_id=resident.id`)。
- `relation_service.get_pair(db, id1, id2, type1="resident", type2="resident") -> ResidentRelation | None`(relation_service.py:136-148):canonical pair,入参 **Resident.id**;relation bump 的 IntegrityError 分支会 `db.rollback()`(:125-131)。
- `shop_service.get_catalog`(:53-57):`select(Item).where(active)` **无 kind 过滤**;`purchase`(:58-98)对无 handler 的 kind 照样扣款(apply_effect 返 None 不补偿)——import_good 必须挡玩家。
- nightly:nightly_cron.py:129,22 段各自 try/except;新段追加到 #22(:427-441)后,段内 `async with async_session()`。
- event_cron:phase=="start" 块 :36-42;:69-77 rollback 兜底先例;market_day 判据 `payload_json.market_day`;`flip_active_events` 先 commit(world_event_service.py:110-113),同 event 不会二次 start。
- feed:`feed_service.push(slug, kind, payload)`(自开 session,fail-open);memory:`MemoryService.add_memory`(**自 commit**)。
- config:npc_economy 块 :529-539;S1-5 块 :654-657。
- 口味哈希:civic_service.py:306 `_stable_unit`(模块私有,**复制** 5 行,注明出处)。
- alembic:054 真实 revision 串 = `"054_freeze_lab_model_cost_rate"`(revision id 是文件名 stem,**不是** "054");commissions 既有列全 `sa.String` 无 FK(alembic/versions/022_add_commissions.py:23-30)。

---

## Step 1 — 配置键与开关

- [ ] `backend/tests/test_npc_trade_config.py`:断言 `Settings()` 默认 `npc_trade_enabled is False`、`caravan_enabled is False`、`tax_carry_enabled is False`、`npc_trade_buy_prob==0.25`、`npc_trade_reserve_sc==5`、`npc_trade_max_buys_per_night==2`、`caravan_stall_fee_sc==5`、`caravan_budget_sc==30`。跑出失败。
- [ ] `app/config.py` npc_economy 块(:539 后)追加 8 键,注释风格随文件。
- [ ] `deploy/backend/.env.example` 追加 8 键示例(全关/默认值),随既有分组注释风格。
- [ ] Commit:`feat(economy): M-A 配置面——NPC 贸易/商队/分数税账开关及参数(默认全关)`

## Step 2 — C0 转账原语(含 rollback 纪律)

- [ ] `backend/tests/test_coin_transfer.py`(**断言一律新开 session 重读**):
  1. `treasury_debit_pending`:足额→True 余额减(commit 后重读);不足→False 零变化;行不存在→False;amount≤0/bool→CoinError;**flush-owned**:调用后不 commit 直接丢弃 session→余额不变。
  2. `treasury_transfer`:成功→一增一减单 commit;from 不足→False 双方零变化;to 无行→upsert 建行;`from==to`→CoinError;**credit 段抛异常(monkeypatch credit_pending raise)→ 内部 rollback,悬挂 debit 不落库**(随后无关 commit 也看不到扣款)。
- [ ] `app/services/coin_service.py`(紧邻 :494,校验逐字复用 credit_pending 写法):

```python
async def treasury_debit_pending(db: AsyncSession, slug: str, amount: int) -> bool:
    """Flush-owned guarded debit — caller owns the transaction (mirror of
    treasury_credit_pending). False when the row is missing or short."""
    # 校验与 treasury_credit_pending 逐字一致(slug 非空≤100;amount 正 int,bool 拒)
    result = await db.execute(
        update(ResidentTreasury)
        .where(ResidentTreasury.resident_slug == slug,
               ResidentTreasury.balance_sc >= amount)
        .values(balance_sc=ResidentTreasury.balance_sc - amount)
    )
    return (result.rowcount or 0) > 0


async def treasury_transfer(db: AsyncSession, from_slug: str, to_slug: str,
                            amount: int, reason: str = "") -> bool:
    """M-A C0: atomic resident→resident move. Debit-first; nothing survives a
    mid-flight failure — the pending debit is rolled back, never left to ride
    a later unrelated commit."""
    if from_slug == to_slug:
        raise CoinError("transfer to self")
    if not await treasury_debit_pending(db, from_slug, amount):
        return False
    try:
        await treasury_credit_pending(db, to_slug, amount, reason)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return True
```

- [ ] Commit:`feat(economy): 居民间原子转账原语——debit-first、中途失败回滚不烧钱`

## Step 3 — C5 skim_tax 两态 API + 分数税账(独立闸)

- [ ] `backend/tests/test_tax_carry.py`:
  1. `town_treasury_enabled=False` → 两版均返 0 零写入。
  2. `town_treasury_enabled=True, tax_carry_enabled=False` → **逐字节等价旧 int 截断**(16×0.05→0,不写 carry 行;100×0.05→5 入镇库)——这是 vm212 暗上安全线。
  3. 双闸开、rate 0.05、gross 16:首次 0/carry 0.8;13 次累计 → 镇库 10、carry≈0.4;carry 行带 `group="town"`、`updated_at` 非空(create_all 建表下不炸 = group 已带)。
  4. `skim_tax_pending` flush-owned:调用后 rollback → 镇库与 carry 均无痕。
  5. `skim_tax`(自提交版):调用即落库;`_skim_town_tax` 薄委托后,tip 路径哨兵 creator 分支(share 发放被跳过)税仍入库(回归 :310-316 场景)。
  6. `cut=min(int, gross)` 上界;政策 `tax_rate` 覆盖 fallback(镜像既有测试;受影响旧断言列在 commit message)。
- [ ] `app/services/treasury_service.py`:`TAX_CARRY_KEY="town_tax_carry"`;`kv_read(db, key, default) -> str|None`;`kv_upsert_pending(db, key, value, *, group="town", updated_by)`(**完整镜像 tax_pending :62-94:显式 updated_at、带 group、pg/sqlite on_conflict、其余方言 guarded UPDATE+add**);`skim_tax_pending(db, gross, fallback_rate, reason) -> int`(gate 语义见 spec C5;`tax_carry_enabled` 关→`cut=min(int(gross*rate), gross)` 直接 `tax_pending`,不碰 carry);`skim_tax(...)` = pending 版 + `await db.commit()`。
- [ ] `shop_effects._skim_town_tax` 薄委托到**自提交版** `skim_tax`(签名不动,三调用点零改)。
- [ ] `pytest tests/ -q -k "tax or shop or treasury"` 无新失败。Commit:`feat(economy): skim_tax 两态 API+分数税累计账(独立闸 TAX_CARRY_ENABLED,关闸逐字节旧截断)`

## Step 4 — C1 餐费入账

- [ ] `backend/tests/test_meal_revenue.py`(fixture id≠slug):
  1. gate 开、食客在 cafe:食客-2、林晚秋+2(新 session 重读)、**双方** wallet 缓存刷新、feed `meal_income`(monkeypatch push 断言)。
  2. tavern → 周大河收款。
  3. 食客余额<2:transfer False → 赊账分支(satiety 已恢复、赊账 memory、familiarity bump)、**食客 wallet 缓存仍被刷新**(现状 :46-47 无条件刷,不许回归)。
  4. host 缺失或 host==食客 → 回退旧 sink debit。
  5. gate 关 → 与现状逐字节一致(sink debit + 无条件缓存刷新 + 赊账分支)。
  6. 转账路径中途抛异常 → except 分支 **rollback** 后照旧 fail-open(不留悬挂 debit)。
- [ ] `_charge_meal` 改造:通读现体;保持"扣费尝试(transfer 或 debit)→ **无条件** balance+set_wallet_cache(食客)→ 按 charged 分流"的原结构;host 提前查一次两用(转账目标+赊账分支);转账成功另刷 host 缓存 + `try: await feed_service.push(host.slug, "meal_income", {...}) except: pass`;except 分支补 `await db.rollback()`。
- [ ] Commit:`feat(economy): 餐费从 sink 改为转账给经营者(cafe→林晚秋/tavern→周大河),赊账与缓存语义不回归`

## Step 5 — C2 消费 pass

- [ ] `backend/tests/test_npc_consumption.py`(rng=`random.Random(42)`,断言新 session 重读):
  1. gate 关(任一)→ `{"bought":0}` 零写入。
  2. 正常成交:买方-15、作者+(15−税)、税入镇库/carry、库存 3→2(**重赋值模式生效的回归点**)、**双方 wallet 缓存刷新**、双方 memory+feed;**造一条正 affinity 关系断言其影响选择**(两候选卖家,高好感者被选)。
  3. 保留金地板:余额 18 < 15+5 → 不买。
  4. 全镇每晚上限 2 笔;每人 1 笔。
  5. 不买自己的作品。
  6. 库存 1 售罄 → `active=False`。
  7. `import_good`:debit 全价、无人 credit(sink)、不抽税、买方 memory;resident_work 优先。
  8. **creator 已删**(item 在、居民无)→ 跳过且 item 被顺手 `active=False`。
  9. 单笔失败(monkeypatch 抛)→ rollback 后继续下一买方,已成交的不受污染。
- [ ] `app/services/npc_trade_service.py` 新建(模块头中文说明"零 LLM 规则引擎",`_stable_unit` 复制自 civic_service.py:306 注明出处)。`run_consumption_pass(db, rng=None) -> dict`:
  - 标的查询后**批量解析 creator_slug→Resident**(一次 select 建 map);creator 查无 → 下架该 item(重赋值模式)continue。
  - affinity = `relation_service.get_pair(buyer.id, creator.id)` 的 `.affinity`(None→0,负值截 0);打分 `1.0 + affinity + 0.5*_stable_unit(buyer.slug, item.code)` + resident_work 优先偏置 0.5。
  - 成交事务(resident_work):`treasury_debit_pending(buyer.slug, price)` → False 则 continue;`cut = skim_tax_pending(db, price, settings.town_tax_rate_sales, f"npc_sales_tax:{item.code}")`;`treasury_credit_pending(creator.slug, price-cut)`;库存扣减(:330-348 全模式);买方+作者 `set_wallet_cache`(ORM 在手,balance 用本地算术即可);`await db.commit()`;然后 memory×2(作者措辞对齐 :358-366)+ feed×2 fail-open。
  - import_good:debit_pending 全价 → 库存扣减 → 双缓存中只刷买方 → commit → 买方 memory("从商队的摊位上买了「X」",0.4)+feed。
  - 每次迭代 try/except → `await db.rollback()` → continue。
  - 返回 `{"bought": n, "spent": total, "tax": tax_total}`。
- [ ] Commit:`feat(economy): NPC 夜间消费 pass——零 LLM 规则购买,单事务成交+孤儿作品下架`

## Step 6 — C3 迁移 055(暗上)

- [ ] `backend/tests/test_commission_npc_accept.py` 先写模型往返:`acceptor_resident_id` 可写可读默认 None;`alembic heads` 单头。
- [ ] 迁移文件 `055_add_commission_acceptor_resident.py`:`revision="055_add_commission_acceptor_resident"`、`down_revision="054_freeze_lab_model_cost_rate"`;`op.add_column("commissions", sa.Column("acceptor_resident_id", sa.String(), nullable=True))` + `op.create_index`(命名随既有迁移惯例);downgrade 对称。**不加 FK**(同表 issuer_resident_id 风格)。模型:`Mapped[str | None] = mapped_column(String, nullable=True, index=True)`。
- [ ] sqlite fixture + (colima 可用则)pgvector 容器各跑 `alembic upgrade head`。Commit:`feat(economy): 迁移 055——commissions.acceptor_resident_id(String 同 residents.id 形态,暗上)`

## Step 7 — C3 接单/结算 pass(单事务结算)

- [ ] `test_commission_npc_accept.py` 续(fixture id≠slug,断言新 session 重读):
  1. accept:open 未过期、发单人(按 slug 对账)余额≥reward → 恰一人;**guarded UPDATE**(`WHERE status='open' AND acceptor_user_id IS NULL AND expires_at>now`)rowcount=0 放弃;发单人穷/候选是发单人本人/`is_autonomous=False` 不入选;玩家已 accept 的零改写(并发语义测试:先玩家 accept 再跑 pass)。
  2. settle(先于 accept 跑):**单事务**——guarded `UPDATE commissions SET status='completed', completed_at=now WHERE id=? AND status='accepted' AND acceptor_user_id IS NULL`(占坑)+ `treasury_debit_pending(发单 slug)` + `treasury_credit_pending(承接 slug)` + 单 commit;memory/feed 在 commit 后;**crash 语义**:monkeypatch 让 commit 前抛 → rollback 后钱与状态都未动(重跑不重复付款)。
  3. 发单人付不起 → rollback、另起事务回 open+清 acceptor+显式 commit;任一方 Resident 已删 → 同回 open 不转账。
  4. 同晚 accept 不被同晚 settle(顺序);gate 关两 pass no-op。
- [ ] `npc_trade_service.py` 增两 pass,严格按上述事务边界;id→slug 解析先行;打分 `_stable_unit(slug, commission.id)` + `npc_trade_buy_prob` 掷骰。
- [ ] Commit:`feat(economy): NPC 接单/结算——guarded 占坑单事务付款,穷发单人流单回 open`

## Step 8 — nightly 段落 #23

- [ ] `backend/tests/test_nightly_npc_trade.py`:gate 开→settle→accept→consume 顺序各一次(mock 记录);gate 关→零调用;任一 pass 抛→吞掉不伤后续段。
- [ ] nightly_cron.py #22 后追加段,风格齐平(独立 try/except、async with async_session、logger.info 摘要)。
- [ ] Commit:`feat(economy): nightly #23 NPC 贸易段——结算→接单→消费,fail-open`

## Step 9 — C4 商队服务(at-most-once)

- [ ] `backend/tests/test_caravan.py`:
  1. gate 关 → no-op 零写入。
  2. 首访:**幂等标记先写先 commit**;摊位费(`tax_pending`,town gate 关则跳过)+收购(预算 30、两件 15 → 各买 1,每件单事务:skim_pending+credit_pending+库存重赋值+作者缓存→commit→memory/feed)+进口货三件 upsert(复活模式镜像 :412-415)。
  3. 同 event id 二访 → 零写入。
  4. **玩家不可见**:`get_catalog` 结果不含 `import_good`;玩家 `purchase` 该 kind 被拒(ShopError)——两断言。
  5. 预算不足单价/无在售作品 → 不崩,费与进口货照常。
  6. 中途单件失败 → rollback 该件 continue,已成交件不受污染;标记已落 → 不会重复收费。
- [ ] `app/services/caravan_service.py` 新建:`IMPORT_DEFS` 3 件(import_tea 茶叶 6/import_trinket 小玩意 4/import_cloth 花布 8,stock 2,payload `{"caravan": true}`);`run_caravan_visit(db, event) -> dict`:gate 双查;读 `kv_read('caravan_last_event_id')` == event id → return;**先 `kv_upsert_pending` 写标记 + `await db.commit()`**(at-most-once);摊位费 `settings.town_treasury_enabled` 时 `tax_pending` + commit;收购循环(`order_by(Item.code)`,creator 解析与孤儿下架同 C2);进口货 upsert;返回摘要。
- [ ] `app/services/shop_service.py` `get_catalog` 加 `Item.kind != "import_good"` 过滤;`purchase` 对该 kind 提前拒(风格随既有 ShopError)。
- [ ] Commit:`feat(economy): 外来商队——at-most-once 到访,收购/摊位费/进口货,玩家目录隔离`

## Step 10 — event_cron 集市日钩子

- [ ] `backend/tests/test_caravan_hook.py`:market_day 事件 start:gate 开→`run_caravan_visit` 恰一次;gate 关→零调用;visit 抛→吞+`db.rollback()` 被调+同轮 C3/E3 仍执行。
- [ ] event_cron.py `phase=="start"` 块(:36-42)`write_collective_memories` 后插入(独立 try/except+rollback,对齐 :43-50 风格)。
- [ ] Commit:`feat(economy): 集市日 start 触发商队进镇(独立兜底不伤同轮脚本/辩论段)`

## Step 11 — 守恒契约 + 关闸口径 + 集成

- [ ] `backend/tests/test_economy_conservation.py`:
  1. **守恒**(gate 全开,小世界:3 买方、2 作品、1 委托、1 进口货、cafe 一餐,顺序 settle→accept→consume+一餐+caravan):`Δ(Σ居民余额+镇库) == 商队收购注入+摊位费−进口sink`(整数精确);carry 单独断言 `0≤carry<1` 且与逐笔 exact−cut 累计一致。
  2. **关闸口径分双轨**:三新闸全关 → 三 pass+caravan 钩子+carry **零 DB 写入**;`_charge_meal` 单独断言与现状基线一致(Step 4 用例 5 口径,不进零变化快照)。补:`town_treasury_enabled=True` 且三新闸关 → 玩家 gift/tip/resident_work 税路径行为逐字节不变(vm212 在产前提)。
  3. nightly 全链 smoke:gate 开 `run_nightly_jobs` 一轮不炸、#23 摘要出现。
- [ ] 全套 `python3 -m pytest tests/ -q`:相对基线 54 零新增(逐一核对)。
- [ ] Commit:`test(economy): 货币守恒契约(carry 不计入货币)+三闸关双轨口径+nightly 集成`

## Step 12 — 前端兜底核查 + handoff

- [ ] 通读 `frontend/src` 的 feed 渲染:新 kind(`meal_income`/`npc_purchase`/`npc_commission_done`/`caravan_purchase`)是否安全兜底;若 switch 会炸则最小补默认分支(单独小 commit),否则零改动并在 handoff 记录证据。
- [ ] `docs/superpowers/2026-08-09-M-A-handoff.md`(本地,不入库):提交清单、测试证据、**部署开闸 runbook**(暗上→开 NPC_TRADE+TAX_CARRY→开 CARAVAN 三段、每段验证读数与回滚=关开关)、**通胀监控项**(每周 `Σ余额+镇库` 趋势,超阈值校准 caravan_budget_sc/进口货定价)。spec §7-3 由此 runbook 在合并部署后承接。
- [ ] Commit(若前端有改动才有代码 commit;handoff 本地不 commit):`fix(frontend): feed 未知 kind 兜底`(条件性)

## 汇总口径

迁移:仅 055。新文件:`npc_trade_service.py`、`caravan_service.py`、7 个测试文件。改动:`config.py`、`coin_service.py`、`treasury_service.py`、`shop_effects.py`(薄委托)、`shop_service.py`(catalog 过滤+purchase 拒)、`execute/basic.py`、`nightly_cron.py`、`event_cron.py`、`commission.py`(模型)、`deploy/backend/.env.example`。玩家路径资金流零改动(税写入的 commit 语义显式保住)。
