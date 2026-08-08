# M-A 经济内生化 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Spec: `docs/superpowers/specs/2026-08-09-npc-economy-design.md`(先通读)。
> Worktree: `/Volumes/data/dev/simverse-world-npc-economy`(branch `feat/npc-economy-ma`,base `0be94d2`)。**只许写这个 worktree。**
> 纪律:每 step 先写失败测试→跑出失败→实现→跑出通过→单独 commit(message 末尾贴真实 `Verified-by:` 行)。禁 --no-verify/amend/squash。测试跑法:`cd /Volumes/data/dev/simverse-world-npc-economy/backend && python3 -m pytest tests/<file> -x -q`。基线口径:全套相对 54 失败(49 lab + 5 postpone)零新增。
> 风格:先读同目录邻近测试与被改文件全文再动手;注释密度/命名跟随现有代码;新中文文案与既有措辞一致。

## 全局事实(Explore 已核,直接信)

- `coin_service.treasury_credit_pending(db, slug, amount, reason="")`(coin_service.py:336):dialect-native upsert,校验 slug 非空≤100、amount 正 int(bool 拒);`treasury_debit(db, slug, amount, reason="") -> bool`(:494):原子 guarded UPDATE,**自带 commit**,行不存在返 False;`treasury_balance(db, slug) -> int`(:477)。无 resident↔resident 转账原语。
- `shop_effects._skim_town_tax(db, gross, rate, reason) -> int`(shop_effects.py:42-69):内部经 `fiscal_policy_service.tax_rate(db, fallback=rate)` 取有效税率,`cut=int(gross*rate)`,`treasury_service.tax(db, cut, reason)`;`town_treasury_enabled` 关→0。调用点 :245(gift)/:310(tip)/:338(resident_work)。
- `treasury_service.tax(db, amount, reason="")`(treasury_service.py:97)→`tax_pending`(:62,flush-owned upsert,caller owns transaction)。`system_config` 列:`updated_at,key,value,group,updated_by`。**`ConfigService.set` 内部 commit,禁止在交易事务中使用**。
- `_charge_meal(db, resident)`(app/agent/phases/execute/basic.py:34-69):`treasury_debit(…, settings.npc_meal_cost_sc=2, reason="meal")`(:45)→ balance→`set_wallet_cache`(:46-47);失败走赊账分支(:52-68):`find_duty_resident(db, "cafe_host" if loc_id=="cafe" else "tavern_hub")` + memory + `relation_service.bump(d_familiarity=0.02)`。EAT 分支在 :181-197(satiety 恢复在扣费**之前**,扣不到钱也吃饱)。
- dining:`map_data.py:254` `_DINING_LOCATIONS={"cafe","tavern"}`;cafe=咖啡馆(林晚秋 `cafe_host`,preset:111-116)、tavern=酒馆(周大河 `tavern_hub`,preset:183-188);两 duty **无 `_WORK_HANDLERS` 条目**(duty_service.py:375-383)→零工资,本里程碑给他们营收。
- 作品:`duty_service._maybe_list_resident_work`(:386-418)建 `Item(kind="resident_work", price_sc=15, payload={"creator_slug", "stock":3}, active=True)`;售出扣库存/清零下架在 `shop_effects._resident_work_effect`(:320-369,:347-348 下架,:351-366 作者售出 memory 措辞)。
- 委托:`commission_service.py` 状态机 `open|accepted|completed|expired`;`create_commission`(:50,cap `commission_global_cap` 默认 15,expiry 48h);玩家 `accept`(:66)乐观 UPDATE;`complete`(:94)`reward(db, user_id, …)` **铸币**、发单人 treasury 不动、写发单人 memory(:113)+notify+emit;`expire_commissions`(:128)由 nightly #3 调。唯一发单方:陈铁生 `_work_workshop_fixer`(duty_service.py:221-232,reward 恒 8)。
- nightly:`app/tasks/nightly_cron.py:129` `run_nightly_jobs`,22 段各自 try/except fail-open;新段追加到 #22 `run_public_spending`(:427-441)之后。
- event_cron:`app/tasks/event_cron.py`,`flip_active_events` 返回 `(event_dict, phase)`;`phase=="start"` 块 :36-42(唯一 start 钩子 `write_collective_memories`);:69-77 有共享 session 的 rollback 兜底先例;market_day 判据 `payload_json.market_day`。
- feed:`feed_service.push(resident_slug, kind, payload)`(feed_service.py:28,自开 session、fail-open);memory 单条 `MemoryService.add_memory`(app/memory/service.py:52-66)。
- config:npc_economy 块在 config.py:529-539(`npc_economy_enabled=True`、`npc_meal_cost_sc=2`、`npc_work_item_price_sc=15`…);S1-5 块 :654-657(`town_treasury_enabled=False`、`town_tax_rate_sales=0.1`)。
- 口味哈希先例:`civic_service._stable_unit`(civic_service.py:306,模块私有;**复制**5 行到新模块,不跨模块 import 私有函数)。
- 迁移 head:`backend/alembic` 当前 054(执行前 `alembic heads` 复核)。

---

## Step 1 — 配置键与开关

- [ ] `backend/tests/test_npc_trade_config.py`:断言 `Settings()` 默认 `npc_trade_enabled is False`、`caravan_enabled is False`、`npc_trade_buy_prob==0.25`、`npc_trade_reserve_sc==5`、`npc_trade_max_buys_per_night==2`、`caravan_stall_fee_sc==5`、`caravan_budget_sc==30`(参考现有 config 断言测试写法)。跑出失败。
- [ ] `app/config.py`:npc_economy 块(:539 之后)追加上述 7 键,注释风格随文件(每键一句中文用途)。
- [ ] `deploy/backend/.env.example`:在 S1-5/经济相关段落追加 7 键示例(值=代码默认,`NPC_TRADE_ENABLED=false` 等),遵循该文件既有分组与注释风格。
- [ ] 跑过。Commit:`feat(economy): M-A 配置面——NPC 贸易与商队开关及参数(默认全关)`

## Step 2 — C0 转账原语

- [ ] `backend/tests/test_coin_transfer.py`(读 `tests/` 里现有 coin/treasury 测试的 fixture 用法):
  1. `treasury_debit_pending`:余额足→True 且余额减、**未 commit 前同 session 可见**;不足→False 零变化;行不存在→False;amount≤0/bool→raise CoinError。
  2. `treasury_transfer`:双方余额一增一减(单 commit);from 不足→False 且**双方零变化**;to 行不存在→upsert 建行;`from==to`→CoinError;reason 透传不崩。
- [ ] `app/services/coin_service.py` 实现(紧邻 :494 现有 debit,校验复用 credit_pending 的写法):

```python
async def treasury_debit_pending(db: AsyncSession, slug: str, amount: int) -> bool:
    """Flush-owned guarded debit — caller owns the transaction (mirror of
    treasury_credit_pending). Returns False when the row is missing or short."""
    # 校验与 treasury_credit_pending 逐字一致(slug 非空≤100;amount 必须正 int,bool 拒)
    result = await db.execute(
        update(ResidentTreasury)
        .where(ResidentTreasury.resident_slug == slug,
               ResidentTreasury.balance_sc >= amount)
        .values(balance_sc=ResidentTreasury.balance_sc - amount)
    )
    return (result.rowcount or 0) > 0


async def treasury_transfer(db: AsyncSession, from_slug: str, to_slug: str,
                            amount: int, reason: str = "") -> bool:
    """M-A C0: atomic resident→resident move. Debit-first; nothing is written
    when the payer is short. Single commit owns both legs."""
    if from_slug == to_slug:
        raise CoinError("transfer to self")
    if not await treasury_debit_pending(db, from_slug, amount):
        return False
    await treasury_credit_pending(db, to_slug, amount, reason)
    await db.commit()
    return True
```

- [ ] Commit:`feat(economy): 居民间原子转账原语 treasury_transfer/treasury_debit_pending`

## Step 3 — C5 skim_tax 抽取 + 分数税账

- [ ] `backend/tests/test_tax_carry.py`:
  1. gate 关(`town_treasury_enabled=False`)→ `skim_tax` 返 0、`system_config` 无 `town_tax_carry` 行、镇库无行(逐字节现状)。
  2. gate 开、rate 0.05、gross 16 → 首次返 0,carry≈0.8;连续调 13 次 → 累计恰好落 10 SC 进镇库(0.8×13=10.4→镇库 10、carry≈0.4)。
  3. carry 持久化:重读 `system_config['town_tax_carry']` 数值正确;**过程不触发额外 commit**(caller 事务内可 rollback 干净)。
  4. 政策覆盖:policies 表 `tax_rate` 存在时覆盖 fallback(镜像现有 `_skim_town_tax` 的既有测试;找到并列出受影响的旧测试,若其断言依赖旧截断行为,更新断言并在 commit message 里点名)。
  5. `cut=min(cut,gross)` 上界保持。
- [ ] `app/services/treasury_service.py` 新增(放 `tax` 之后):`TAX_CARRY_KEY="town_tax_carry"`、`kv_read(db, key, default)`、`kv_upsert_pending(db, key, value, updated_by)`(dialect-native upsert,**镜像 :63-96 `tax_pending` 的写法**,pg/sqlite `ON CONFLICT DO UPDATE`,其余方言 guarded UPDATE+add;value 存 `str`)、
  `skim_tax(db, gross: int, fallback_rate: float, reason: str) -> int`:gate 关→0;`rate = await fiscal_policy_service.tax_rate(db, fallback=fallback_rate)`;`exact=gross*rate`;≤0→0;`carry = float(kv_read)+exact`;`cut=min(int(carry), gross)`;`carry-=cut`;`kv_upsert_pending(TAX_CARRY_KEY, round(carry,6), "skim_tax")`;`cut>0` 时 `await tax_pending(db, cut, reason)`;返回 cut。整体 try/except fail-open 返 0(镜像原函数)。
- [ ] `app/services/shop_effects.py` `_skim_town_tax` 改薄委托(签名不动、三调用点零改):

```python
async def _skim_town_tax(db, gross: int, rate: float, reason: str) -> int:
    from app.services import treasury_service
    return await treasury_service.skim_tax(db, gross, fallback_rate=rate, reason=reason)
```

- [ ] 跑本文件 + `pytest tests/ -q -k "tax or shop or treasury"` 确认无新失败。Commit:`feat(economy): 销售税抽取为 treasury_service.skim_tax 并加分数税累计账(int 截断修复)`

## Step 4 — C1 餐费入账

- [ ] `backend/tests/test_meal_revenue.py`(fixture:建 cafe_host 林晚秋/tavern_hub 周大河样式的 duty resident,参考 preset 字段):
  1. `npc_trade_enabled=True`、食客在 cafe:余额-2、林晚秋+2(转账非铸币)、食客 `meta_json['wallet']` 缓存更新、feed `meal_income` 尝试发出(monkeypatch `feed_service.push` 断言参数)。
  2. tavern → 周大河收款。
  3. 食客余额<2:转账 False → 走既有赊账分支(satiety 已恢复不回滚、赊账 memory 写入、familiarity bump)——断言与现网赊账测试一致(先找现有 `_charge_meal` 测试,若无则本文件补齐现状基线用例)。
  4. host 缺失(无该 duty resident)或 host==食客 → 回退旧 sink(`treasury_debit`)。
  5. `npc_trade_enabled=False` → 行为与现状逐字节一致(sink debit)。
- [ ] `app/agent/phases/execute/basic.py` `_charge_meal` 改造:通读 :34-69 现体后最小改——`npc_trade_enabled` 且 host 可用时 `charged = await coin_service.treasury_transfer(db, resident.slug, host.slug, cost, reason="meal")`,否则 `charged = await coin_service.treasury_debit(...)`(原样);`charged` 为 True 时给食客 set_wallet_cache(原 :46-47),转账路径另给 host set_wallet_cache(host ORM 在手,`treasury_balance(host.slug)` 后调用)并 `try: await feed_service.push(host.slug, "meal_income", {"from": resident.slug, "amount": cost, "location": loc_id}) except Exception: pass`;False 走原赊账分支不动。host 查找复用赊账分支同款 `find_duty_resident` 三元式(避免重复查询:提前查一次两用)。
- [ ] Commit:`feat(economy): 餐费从纯 sink 改为转账给经营者(cafe→林晚秋/tavern→周大河),赊账保障不回归`

## Step 5 — C2 消费 pass

- [ ] `backend/tests/test_npc_consumption.py`(rng 注入 `random.Random(42)` 确定性):
  1. gate 关(`npc_trade_enabled` 或 `npc_economy_enabled` 任一 False)→ 返回摘要 `{"bought": 0}` 且零 DB 写入。
  2. 上架一件他人 `resident_work`(price 15)、买方余额 30:pass 后买方 15+reserve 内、作者 +15−税、镇库/carry 收到税、库存 3→2;买方 memory(`source="npc_trade"`, importance 0.5)与作者售出 memory、双方 feed `npc_purchase`(monkeypatch push 收集)。
  3. 买方余额 18(< price+reserve=20)→ 不买(保留金地板)。
  4. 全镇上限:3 个买方 3 件货、`npc_trade_max_buys_per_night=2` → 恰 2 笔。
  5. 不买自己的作品。
  6. 库存 1 售罄 → `active=False`(镜像 :347-348)。
  7. `import_good`(payload `{"caravan": true}`):买方 debit 全价、**无人 credit**(sink)、不抽税、买方 memory;本地作品优先(同夜同买方两类可选时先 resident_work)。
- [ ] `app/services/npc_trade_service.py` 新建,模块头注释仿 civic_service(一段中文说明"零 LLM 规则引擎")。内容:
  - `_stable_unit(*parts) -> float`:从 civic_service.py:306 **复制**(注明出处)。
  - `run_consumption_pass(db, rng=None) -> dict`:gate 双查;`rng = rng or random`;买方=`Resident.is_autonomous`(select 全列,需 meta/slug);逐买方 `rng.random() < settings.npc_trade_buy_prob` 掷骰;标的=active items `kind IN ("resident_work","import_good")`;排除 `payload_json.creator_slug == buyer.slug`;资格=`treasury_balance(buyer) >= price + npc_trade_reserve_sc`;打分 `1.0 + max(affinity(buyer→creator),0) + 0.5*_stable_unit(buyer.slug, item.code)`(affinity 经 `relation_service` 现有读接口取,creator 缺省 0;import_good 无 creator 记 0)+ resident_work 类目 +0.5 优先偏置;每买方至多 1 笔、全镇 `npc_trade_max_buys_per_night` 止;
  - 成交 resident_work:`cut = await treasury_service.skim_tax(db, price, fallback_rate=settings.town_tax_rate_sales, reason=f"npc_sales_tax:{item.code}")`→`ok = await coin_service.treasury_debit_pending(db, buyer.slug, price)`,不 ok 跳过并回补 carry?——**不回补**:skim 先算会脏 carry,因此**顺序必须反过来**:先 `debit_pending(buyer, price)`,ok 后再 `skim_tax`、`treasury_credit_pending(creator, price-cut)`、库存/active 更新、`db.commit()`;memory×2(`MemoryService.add_memory`,作者措辞对齐 shop_effects.py:351-366)+ feed×2;
  - 成交 import_good:`debit_pending(buyer, price)` ok→ 库存/active 更新、`db.commit()`、买方 memory("从商队的摊位上买了「X」",0.4)+ feed;
  - 返回 `{"bought": n, "spent": total, "tax": tax_total}`。
- [ ] Commit:`feat(economy): NPC 夜间消费 pass——零 LLM 规则购买同伴作品与进口货,真实转账+销售税`

## Step 6 — C3 迁移 055(暗上)

- [ ] 测试:`backend/tests/test_commission_npc_accept.py` 先只写模型往返用例:建 Commission 后 `acceptor_resident_id` 可写可读、默认 None;`alembic heads` 单头 055。
- [ ] `alembic revision` 新建 `055_add_commission_acceptor_resident`(down_revision=054):`op.add_column("commissions", sa.Column("acceptor_resident_id", <UUID 类型与 residents.id 同款——先读 054 与 commissions 既有迁移抄类型写法>, sa.ForeignKey("residents.id"), nullable=True))`;downgrade 对称 drop。`app/models/commission.py` 加同名字段(风格随现有列)。
- [ ] 本地对 sqlite fixture + (若 colima 可用)pgvector 容器各跑一次 `alembic upgrade head`(参考既有迁移验证做法)。Commit:`feat(economy): 迁移 055——commissions.acceptor_resident_id(NPC 接单,暗上)`

## Step 7 — C3 接单/结算 pass

- [ ] `test_commission_npc_accept.py` 续:
  1. accept pass:open 未过期、发单人余额≥reward → 恰一人被选(rng 确定)、状态 `accepted`+`acceptor_resident_id`、双方 memory+feed;发单人余额<reward 的委托不被接;`is_autonomous=False` 或发单人自己不入选;已有 `acceptor_user_id`(玩家已接)的不动。
  2. settle pass(**在 accept 之前跑**):accepted+resident 的委托 → `treasury_transfer(发单→承接, reward)` 成功→`completed`+`completed_at`+双方 memory+feed `npc_commission_done`;发单人钱不够→状态回 `open`、`acceptor_resident_id` 清空、无 memory;玩家 accepted(`acceptor_user_id` 非空、resident 空)零影响。
  3. 同晚新 accept 的不会被同晚 settle(调用顺序保证,集成在 Step 8 断言)。
  4. gate 关 → 两 pass no-op。
- [ ] `npc_trade_service.py` 增 `run_commission_settle_pass(db) -> int`、`run_commission_accept_pass(db, rng=None) -> int`,按上述语义;候选打分 `_stable_unit(slug, commission.id)` 加 rng 掷骰(`npc_trade_buy_prob` 复用,不新增键);发单人余额查询用 `treasury_balance`。
- [ ] Commit:`feat(economy): NPC 接单/结算 pass——赏金改为发单人真实出资,付不起流单回开放`

## Step 8 — nightly 段落 #23

- [ ] `backend/tests/test_nightly_npc_trade.py`(参考既有 nightly 段落测试的 monkeypatch 手法):gate 开→三 pass 以 settle→accept→consume 顺序各调一次(mock 记录调用序);gate 关→零调用;任一 pass 抛异常→吞掉且后续段不受影响(fail-open)。
- [ ] `app/tasks/nightly_cron.py` #22(:427-441)之后追加段落,风格与 #22 齐平(独立 `try/except`、`from app.config import settings` 用法随文件、`async with async_session()`、结束 `logger.info` 摘要)。
- [ ] Commit:`feat(economy): nightly #23 NPC 贸易段——结算→接单→消费,fail-open`

## Step 9 — C4 商队服务

- [ ] `backend/tests/test_caravan.py`:
  1. gate 关 → no-op 零写入。
  2. 首访(event id X):摊位费 5 入镇库(`town_treasury_enabled` 关则跳过费但其余照跑);预算 30、在售两件 15 SC 作品 → 各买 1(按 code 稳定序),作者各 +15−税、税/carry 入账、库存扣、memory+feed `caravan_purchase`;`import_tea/import_trinket/import_cloth` 三件 upsert 上架(active、库存补满)。
  3. 同 event id 二访 → 幂等零写入(`system_config['caravan_last_event_id']`)。
  4. 预算不足单价 → 不买不崩;无在售作品 → 只收费+上进口货。
- [ ] `app/services/caravan_service.py` 新建:`IMPORT_DEFS`(3 件:import_tea 茶叶 6/import_trinket 小玩意 4/import_cloth 花布 8,stock 2,payload `{"caravan": true}`,icon/描述中文一句)、`run_caravan_visit(db, event) -> dict`:gate 双查(`caravan_enabled`+`npc_economy_enabled`);幂等读写走 `treasury_service.kv_read/kv_upsert_pending`(key `caravan_last_event_id`,updated_by `caravan`);摊位费 `treasury_service.tax(db, settings.caravan_stall_fee_sc, reason="caravan_stall_fee")`(gate 内已判 town_treasury_enabled?——`tax`→`tax_pending` 自身不判 gate,**调用前判 `settings.town_treasury_enabled`**);收购循环(稳定序 `order_by(Item.code)`,每件买 1、`budget-=price`,`skim_tax`+`treasury_credit(creator, price-cut)`【注意此处 credit 即铸币=贸易顺差,spec §6】+库存/active+作者 memory+feed);进口货 upsert(存在且 inactive→复活重置库存,镜像 `_maybe_list_resident_work` :412-415 的复活写法);末尾单次 `db.commit()`;返回 `{"bought": n, "fee": fee, "imports": 3}`。
- [ ] Commit:`feat(economy): 外来商队服务——摊位费+按预算收购居民作品+上架进口货,按事件幂等`

## Step 10 — event_cron 集市日钩子

- [ ] `backend/tests/test_caravan_hook.py`:flip 一个 `payload_json.market_day=True` 的事件到 active,event_cron 单轮:gate 开→`run_caravan_visit` 恰被调一次(monkeypatch);gate 关→零调用;`run_caravan_visit` 抛异常→被吞、`db.rollback()` 被调、同轮后续(C3 脚本/E3 辩论 drive)仍执行(mock 断言)。
- [ ] `app/tasks/event_cron.py` `phase=="start"` 块(:36-42)`write_collective_memories` 之后插入(独立 try/except + rollback,判据 `payload.get("market_day")`+`settings.caravan_enabled`,风格对齐 :43-50 的 end 钩子)。
- [ ] Commit:`feat(economy): 集市日事件 start 触发商队进镇(独立兜底,不伤同轮脚本/辩论段)`

## Step 11 — 守恒契约 + gates-off 逐字节 + 集成

- [ ] `backend/tests/test_economy_conservation.py`:
  1. **守恒**:gate 全开造一个小世界(3 买方、2 作品、1 委托、1 进口货、cafe 一餐),顺序跑 settle→accept→consume + `_charge_meal` + `run_caravan_visit`;断言 `Δ(Σ居民余额+镇库+carry) == 商队收购注入 + 摊位费 − 进口货 sink`(其余全为内部转移,spec §6 表逐流核)。
  2. **gates-off 逐字节**:两开关全关跑同批入口 → `residents/resident_treasuries/town_treasuries/system_config/items/feed_events/memories/commissions` 全部零变化(快照对比)。
  3. nightly 全链 smoke:`run_nightly_jobs` gate 开跑一轮不炸、#23 摘要日志出现。
- [ ] 全套:`python3 -m pytest tests/ -q` 记录失败集,相对基线 54 零新增(逐个核对新失败为 0;lab/postpone 既有失败原样)。
- [ ] Commit:`test(economy): 货币守恒契约+双闸关逐字节回归+nightly 集成`

## Step 12 — 终验与交接材料(不写新功能代码)

- [ ] `frontend` 不动(本里程碑纯后端;feed 新 kind 前端按未知 kind 兜底展示——通读 `frontend/src` feed 渲染确认不炸,若 switch-case 会炸则最小补默认分支,单独 commit)。
- [ ] 汇总:steps 提交清单、测试证据、部署/开闸 runbook(spec §7 三段:暗上→开 NPC_TRADE→开 CARAVAN,含回滚=关开关)。写入 `docs/superpowers/2026-08-09-M-A-handoff.md`。
- [ ] Commit:`docs(economy): M-A handoff——部署开闸三段 runbook 与验收证据`

## 汇总口径

迁移:仅 055(Step 6)。新文件:`npc_trade_service.py`、`caravan_service.py`、7 个测试文件、handoff。改动文件:`config.py`、`coin_service.py`、`treasury_service.py`、`shop_effects.py`(薄委托)、`execute/basic.py`(_charge_meal)、`nightly_cron.py`(+段)、`event_cron.py`(+钩)、`commission.py`(模型)、`deploy/backend/.env.example`。玩家路径资金流零改动(shop_effects 三调用点签名不变)。
