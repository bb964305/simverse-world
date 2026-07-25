# Kickoff S2-1 — offices 职位实体化(镇长 / 文书 / 邮差 / 医生)

> **结论先行。** 本模块把当前**四处分裂的"职位"表示**统一成一张 `offices` 表 + `OfficeService.appoint/vacate/term_check`:镇长现在活在 `system_config['current_mayor']`(读路径权威源)**加** `Resident.meta_json['mayor']`(仅供工资加成);文书 `town_clerk` / 邮差 `postman` 活在 `Resident.meta_json['duty']`(seed 静态预设);**医生 doctor 在代码里根本不存在(grep 空)——是绿地新建**。核心纪律:新机制**零 LLM 边际成本**(纯规则任免),**独立门控 `polis_office_enabled: bool = False`**,关闭时字节级回落到今天的 `install_mayor` / `current_mayor` / `find_duty_resident` 行为。**迁移不会 replace 掉 `meta_json['mayor']`——那不是身份源,只是工资乘子;迁移必须同时兼容两个存储,否则 `duty_service.py:135` 的工资加成静默失效**(reader gotcha #1)。

> **本文引用的所有 file:line 均来自 reader 已逐行核实的 anchors,未核实处一律写"现状缺口",不编接口。**

---

## 1. 现状锚点(逐文件逐行核实)

### 1.1 镇长:双存储,无单一事实源

- **写入 = 唯一写路径** `election_service.install_mayor(db, slug) -> bool`(`backend/app/services/election_service.py:127-172`,共享锚点 `127-181`):线性扫描全部 `resident_type=='npc'` 居民,给胜者 `meta_json['mayor']=True`(经 `flag_modified(r, 'meta_json')`),从其余所有人 pop 掉 `meta_json['mayor']`;commit;再把 slug 写进 `system_config` key `'current_mayor'`(经 `ConfigService(db).set(group='civic', updated_by='election')`);随后触发副作用(feed goal_achieved、镇长反思记忆)。**无任期、无到期、无 vacate——新一次 install 直接覆盖旧的**。
- **读取 = 唯一读路径** `election_service.current_mayor(db)`(`election_service.py:175-180`):返回 `ConfigService(db).get('current_mayor')`(一个 slug 字符串)或 `None`。**不读 `meta_json`**。所以镇长身份**双存储**:读权威源在 `system_config`,per-resident 布尔 `meta_json['mayor']` 只被工资加成路径消费。
- **工资加成 = `meta_json['mayor']` 的唯一消费者** `duty_service._pay_wage(db, resident)`(`backend/app/services/duty_service.py:125-146`,关键在 `duty_service.py:135`):`if settings.election_enabled and meta_json.get('mayor')` → `wage *= settings.election_mayor_wage_bonus`(1.2)。工资 base = `perk(resident,'wage_sc', npc_default_wage_sc=5)`,经 `coin_service.treasury_credit(reason='duty_wage')` 入镇财政。**`meta_json['mayor']` 纯粹是经济乘子,不是镇长身份的事实源**。
- **选举→镇长衔接**:`open_election` 的每个 Poll option 携带 effect `{"type":"mayor","slug": c.slug}`(`election_service.py:57-63`);候选人默认 SBTI Ac1=H 或 So1=H 的 npc(回落热度 top-3),cap 4 min 2;委托 `civic_service.propose`。won option → `civic_service._execute_outcome(db, effect)`(`backend/app/services/civic_service.py:284-315`)的 `etype=='mayor'` 分支(`civic_service.py:310-312`)`import election_service.install_mayor` 并以 `effect['slug']` 调用——**这是 install_mayor 除测试外的唯一调用者**。
- **节律门** `maybe_open_seasonal_election(db)`(`election_service.py:66-124`):跳过若已有 open Poll `question LIKE '镇长选举%'`;赛季内一季一次(`system_config 'election_last_season'` 守卫),off-season 每 `settings.election_interval_days` 一次(`system_config 'election_last_opened'`)。**全部节律状态在 system_config,无 schema**。受 `election_enabled AND civic_polls_enabled` 双门控。

### 1.2 文书 / 邮差:meta_json.duty 静态预设,无 runtime 任免

- **Duty 定义与定位** `duty_service.py:45-90`:Duty(职务)= `meta_json['duty'] = {key, title, prompt_hint, perks}`。`duty_key()` 读 `meta_json['duty']['key']`;`find_duty_resident(db, key)` 对全部 npc 居民做**镇级线性扫描**(meta_json JSON 操作符 sqlite/PG 不可移植),返回**第一个** `duty_key==key` 的居民。文书 `find_duty_resident(db,'town_clerk')`、邮差 `find_duty_resident(db,'postman')` 就靠它。**一居民一 duty(单 dict,非 list)**。
- **文书** `zhao-qiwen`(`backend/seed/preset_characters.py:613-620`):`meta_json.duty {key:'town_clerk', title:'公告与登记处', perks:{}}` + 自由文本 `meta_json['role']='市政厅文书'`。**无独立 office/appointment 记录,duty 只烘焙在 seed 预设里**。
- **邮差** `luo-xiaozhou`(`preset_characters.py:833-840`):`meta_json.duty {key:'postman', title:'邮差', perks:{wage_sc:6}}`,`meta_json['role']='邮差'`。同文书。
- **唯一 runtime"任命"路径** `sync_duty_meta(db)`(`preset_characters.py:1030-1056`,seed flow `1188` 调用):幂等 re-seed,按 slug 把每个预设的 duty 块 merge 进现有居民(`meta['duty']=duty`)。**没有通用 `appoint(resident,key)` / `vacate(resident)` API——duty 是静态预设**。`meta_json['role']` 是独立自由文本,从不被程序化设置。

### 1.3 医生:完全不存在(绿地)

- reader 核实 `new_storage_hints`:**医生 / doctor 完全不存在**——无 duty key、无 preset、无 role 文本(`grep 医生|doctor|physician|clinic` 空)。现有 duty keys:`cafe_host, tavern_hub, workshop_fixer, chronicle_editor, lecturer, explorer, shop_keeper, town_clerk, researcher, street_artist, postman`。**医生这个 office 是纯绿地**,不像镇长(system_config)/ 文书(town_clerk duty)/ 邮差(postman duty)有现存表示。

### 1.4 存储载体与门控现状

- `Resident.meta_json`(`backend/app/models/resident.py:30-39`,JSON nullable):松类型子命名空间 `sbti/lab/duty/wallet/role/mayor`。**模型上没有任何 office/role 专用列**。
- 门控现状(`backend/app/config.py:357-373`):`npc_default_wage_sc=5`(357)、`civic_polls_enabled=True`(368)、`civic_poll_days=3`(369)、`election_enabled=True`(371)、`election_mayor_wage_bonus=1.2`(372)、`election_interval_days=28`(373)。**注意:这些 M1–M6 旗标默认 True,不是 False**(commit 5172f0e 已启用发布)。
- nightly 挂点(`backend/app/tasks/nightly_cron.py:86-126`):每晚顺序(各自 async_session,全 fail-open)(1) close_due_polls、(2) seed_civic_agenda、(3) maybe_open_seasonal_election、(4) run_npc_voting。install_mayor 在 `close_due_polls→_close_one→_execute_outcome` 内触发。**无 term_check / 任期到期 job**——镇长无限期持续到下次选举覆盖。

### 1.5 本模块要接线的确切位置

| 目标 | 落点(file:line) | 接线动作 |
|---|---|---|
| 统一任免入口 | 新建 `OfficeService.appoint/vacate/get_holder/term_check` | 净新增(今天无 appoint API) |
| 镇长写路径改道 | `election_service.py:127-172` install_mayor | 门控开时 install_mayor 内**加**写 offices 表(dual-write) |
| 镇长读路径改道 | `election_service.py:175-180` current_mayor | 门控开时优先读 offices,回落 system_config |
| 选举 outcome 衔接 | `civic_service.py:310-312` _execute_outcome mayor 分支 | 保持调 install_mayor(经上面 dual-write 落表) |
| 工资加成消费者 | `duty_service.py:135` | **不动语义**;门控开时可改读 offices,回落 meta_json['mayor'] |
| 文书/邮差定位 | `duty_service.py:45-90` find_duty_resident | 门控开时可改查 offices,回落线性扫描 |
| 任期到期 nightly job | `nightly_cron.py:86-126` 块尾追加 | 净新增 term_check try/except 块 |
| 迁移链 | `backend/alembic/versions/040_residents_creator_nullable.py:14-17`(现链头) | 新建 `NNN_add_offices`,down_revision 落地时定 |

---

## 2. 任务切分

> 串行门:任务 1(迁移+表+service)全绿并提交后才开 2;任务 2(镇长改道)全绿才开 3(duty 改道);4/5(admin/nightly/probe)可在 2 后并行。每任务独立提交、commit 带任务号(`s2-1-offices-1: offices table + OfficeService`)。

### 任务 1 — offices 表 + OfficeService(迁移 + 原子 service)

**改哪些文件**:新建 `backend/alembic/versions/NNN_add_offices.py`、新建 `backend/app/services/office_service.py`、`backend/app/models/__init__.py`(注册新 ORM,若采用独立表)、新建 `backend/app/models/office.py`。

**新表结构 `offices`**(新 create_table,非 alter,不需 batch_alter_table):

| 列名 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK | 自增 |
| `office_key` | String NOT NULL | `mayor` / `town_clerk` / `postman` / `doctor`(可扩 `lab_director`);唯一索引(单持有者假设,匹配 find_duty_resident 首匹配语义) |
| `holder_slug` | String NULLABLE | 在任者 resident slug;NULL = 空缺 |
| `institution` | String NOT NULL | 机构:`town_hall` / `post_office` / `clinic` / `lab` |
| `perms_json` | JSON NULLABLE | 权限集(默认 `{}`;镇长裁量权 S2-2 消费) |
| `fill_strategy` | String NOT NULL | 填充策略:`election` / `appointment` / `sortition` / `seed` |
| `term_started_at` | DateTime NULLABLE | 任期起(UTC) |
| `term_ends_at` | DateTime NULLABLE | 任期止;**NULL = 无限期**(back-compat 镇长现"覆盖式"语义) |
| `created_at` / `updated_at` | DateTime | 审计 |

约束:`UniqueConstraint(office_key)`(一 key 一 active 行;空缺时保留行、`holder_slug=NULL`)。迁移里 seed 四行 office(`mayor`/`town_clerk`/`postman`/`doctor`),`holder_slug` 从现状回填(见 §4)。

**Service 签名**(`backend/app/services/office_service.py`):

```python
class OfficeService:
    def __init__(self, db: AsyncSession): ...
    async def appoint(self, office_key: str, slug: str, *,
                      fill_strategy: str, term_days: int | None = None) -> bool
    async def vacate(self, office_key: str) -> bool
    async def get_holder(self, office_key: str) -> str | None
    async def list_offices(self) -> list[dict]
    async def term_check(self) -> int          # nightly: 到期任期 → vacate,返回处理数
```

`appoint` / `vacate` 必须走条件 UPDATE + upsert(见 §4),禁读改写;涉及 `meta_json` 写(镇长 dual-write)时必须 `flag_modified(r, 'meta_json')`(否则 SQLAlchemy 不检测 in-place JSON 变更,复刻 install_mayor 现有做法)。

### 任务 2 — 镇长写/读路径改道(门控开时 dual-write,关时字节级回落)

**改哪些文件**:`backend/app/services/election_service.py`(install_mayor `127-172`、current_mayor `175-180`)、`backend/app/services/civic_service.py`(_execute_outcome mayor 分支 `310-312`——**保持调 install_mayor 即可,不改分支**)。

- `install_mayor`:门控开 → 原逻辑照旧(写 meta_json['mayor'] + system_config)**并**调 `OfficeService(db).appoint('mayor', slug, fill_strategy='election', term_days=settings.polis_office_mayor_term_days)`;门控关 → 一字不改。
- `current_mayor`:门控开 → 优先 `OfficeService(db).get_holder('mayor')`,None 时回落 `ConfigService.get('current_mayor')`;门控关 → 一字不改(只读 system_config)。
- **`_pay_wage`(`duty_service.py:135`)不改语义**:门控开时判据可改为 `OfficeService.get_holder('mayor')==slug`,门控关回落 `meta_json.get('mayor')`。**这是 gotcha #1 的硬约束——两存储必须同活,否则工资加成失效**。

### 任务 3 — 文书/邮差/医生统一走表(门控开时查表,关时线性扫描)

**改哪些文件**:`backend/app/services/duty_service.py`(find_duty_resident `45-90`)。

- 门控开 → `find_duty_resident(db, key)` 优先查 `offices.holder_slug where office_key==key`;门控关 → 回落现有线性扫描(首匹配)。
- **医生**:迁移 seed 一行 `office_key='doctor', holder_slug=NULL, institution='clinic', fill_strategy='appointment'`;运行时经 `OfficeService.appoint('doctor', slug, fill_strategy='appointment')` 任命(S5-8 健康模块消费)。**本模块只建 office 槽位,不建 duty/preset/诊所**(那是 S5-8)。
- 文书/邮差回填见 §4(从 meta_json.duty 一次性迁移进 offices）。

### 任务 4 — admin 只读端点(每端点 require_admin)

**改哪些文件**:新建 `backend/app/routers/admin/offices.py`、`backend/app/routers/admin/__init__.py`(`18-31`,`router.include_router(offices_router)`)。

REST(**鉴权**:每端点 `admin: User = Depends(require_admin)`,`from app.routers.admin.middleware import require_admin`;admin 无 router 级鉴权,漏加则裸奔):

```
GET  /admin/offices              -> list_offices()   admin only
```

对齐 §6 预告 `GET /town/offices`(玩家只读)可后续在 S2 前端线补;本模块只交付 admin 只读面,**避免与其他 KICKOFF 抢公共 /town 路由**。

**WS 事件** `office_changed`:任免/到期时经 `world_revision_service.world_changed_event(*, revision, action, seq, event_id, occurred_at)`(`backend/app/services/world_revision_service.py:187-204`)构造带 revision/seq 锚的 envelope,seq 复用 `current_source_cursor(db)`(`world_revision_service.py:72-84`,基于 `OutboxEvent.id`,非内存计数器),经 `broadcast_world_changed(payload)`(`backend/app/lab/apply.py:237-245`)广播。`action` 取 `office_appointed` / `office_vacated`。门控关时不发。

### 任务 5 — term_check nightly job(门控在 cron 内 guard)

**改哪些文件**:`backend/app/tasks/nightly_cron.py`(`run_nightly_jobs` 块尾追加,`86-126` 之后)。

追加一个 isolated try/except 块(照 realism 门控范式,**guard 在 cron 内**,区别于 M2/M3/M6 的 ungated):

```python
try:
    from app.config import settings
    if settings.polis_office_enabled:
        from app.services.office_service import OfficeService
        async with async_session() as db:
            n = await OfficeService(db).term_check()
        if n:
            logger.info("office term_check vacated %d", n)
except Exception:
    logger.error("office term_check failed", exc_info=True)
```

fail-open(broad try/except,log-and-continue),绝不打断 nightly tick。运行进程见 `backend/app/main.py:85-93`(`run_background_tasks` True 时主 API 跑,否则 agent-worker 跑,恰好一个进程)。

---

## 3. 门控开关与默认值

- 新增独立开关 **`polis_office_enabled: bool = False`**,加在 `Settings` 类(`backend/app/config.py:7-19` 的 class-attribute 范式,`375-378` 的 `settings = Settings()` 单例 + `.env` 自动 override,env 名 `POLIS_OFFICE_ENABLED`)。**默认 False(rollback-safe)——显式区别于 M1–M6 那批默认 True 的旗标**(`config.py:354-373`),对齐 realism 家族默认 False 的正确范式(`config.py:246-268` master + `321-352` 三个独立门 `realism_relations_enabled`/`info_gradient_enabled`/`crowd_enabled`)。
- 前缀 **`POLIS_OFFICE_`**(对齐 §6 接口面预告 S2-1 行 config 前缀)。配套调参常量(同样加进 Settings,默认沿用现状语义):
  - `polis_office_mayor_term_days: int = 0`(0 = 无限期,字节级等价今天的"覆盖式"镇长;>0 才启用任期到期)。
- **关闭时字节级回落**:`install_mayor`/`current_mayor`/`_pay_wage`/`find_duty_resident` 全部走今天的分支;offices 表可存在但不被读写;term_check 在 cron 内被 `if settings.polis_office_enabled` 跳过。既有测试零改动通过。

---

## 4. 原子性要求

写路径一律**条件 UPDATE + upsert,禁读改写**,复刻 `coin_service` 原子化范式:

- **upsert 范式**照 `coin_service.treasury_credit`(`backend/app/services/coin_service.py:166-192`):先 `update(...).where(office_key==key).values(...)`,`rowcount==0` → `db.add(Office(...))` + `try commit / except IntegrityError: rollback → 再 update`(处理并发插入竞态)。`appoint` 对空缺→在任的转移用此范式。
- **守卫 UPDATE 范式**照 `coin_service.charge` / `treasury_debit`(`coin_service.py:23-48` / `195-207`):`update(Office).where(office_key==key, <条件>).values(holder_slug=..., synchronize_session=False)`,靠 `rowcount` 判定是否命中,**不 SELECT-then-write**。`vacate` 用 `where(office_key==key).values(holder_slug=None, term_ends_at=None)`。
- `synchronize_session=False`(照 coin_service),写后不信任已加载 ORM 对象,需要新 SELECT 取新值。
- 镇长 dual-write 里对 `meta_json['mayor']` 的写:每次 in-place 变更后 `flag_modified(r, 'meta_json')`(复刻 install_mayor 现有做法,`election_service.py:127-172`)。
- **回填迁移**(migration data step)幂等:文书/邮差从 `find_duty_resident` 首匹配取 slug 写入对应 office 行;镇长从 `system_config['current_mayor']` 取 slug 写入 `office_key='mayor'` 行;医生行 `holder_slug=NULL`。回填在迁移的 `upgrade()` 里做,可空则跳过(容忍空世界)。

---

## 5. 测试口径

> seeded RNG:本模块任免无随机(appoint/vacate 确定性);term_check 的时间判定用 frozen clock / 注入 `now` 参数,不写"跑三次看大概"。门控回落断言为硬门。

**单测**(`backend/tests/test_office_service.py`):
- `test_appoint_upsert_atomic_no_lost_update`(并发 appoint 无丢更新,照 coin_service 测试范式)
- `test_appoint_transfers_holder_atomically`
- `test_appoint_upsert_race_integrityerror_falls_through`
- `test_vacate_clears_holder_and_term`
- `test_get_holder_returns_none_when_vacant`
- `test_term_check_expires_due_terms_only`(term_ends_at 过期才 vacate,frozen clock)
- `test_term_check_infinite_term_never_expires`(term_ends_at=NULL / mayor_term_days=0)
- `test_office_key_unique_constraint`
- `test_gate_off_appoint_is_noop_or_not_wired`(门控关时业务路径不触表)

**集成测**(`backend/tests/test_office_integration.py`):
- `test_install_mayor_dual_writes_office_when_gate_on`(install_mayor 落 offices 表 + 保留 meta_json['mayor'] + system_config)
- `test_current_mayor_reads_office_then_falls_back_to_config`
- `test_pay_wage_bonus_preserved_when_gate_on`(**gotcha #1 回归门:镇长工资仍 ×1.2**)
- `test_find_duty_resident_reads_offices_when_gate_on`(town_clerk/postman)
- `test_gate_off_byte_level_fallback`(门控关 → install_mayor/current_mayor/_pay_wage/find_duty_resident 行为与现状完全一致,既有测试零改动)
- `test_execute_outcome_mayor_branch_still_installs`(选举 outcome → install_mayor → 落表,`civic_service.py:310-312` 路径不破)
- `test_nightly_term_check_wired_and_gated`(cron 块存在且门控 guard 生效)
- `test_migration_backfills_existing_mayor_clerk_postman`(回填幂等)

---

## 6. 探针出数定义

`burnin_report.py` 新增 office 快照(纯读 offices 表 + system_config,零 LLM):
- **职位占用/空缺时序**:每 office_key 的 `holder_slug` 是否非空、空缺持续天数;目标形态——四职位常态在任,空缺是短暂过渡。
- **任期轮替计数**:统计周期内 `office_changed`(appoint/vacate)事件数(经 OutboxEvent / office 行 updated_at 聚合);目标——镇长随选举周期轮替(`election_interval_days=28`),文书/邮差稳定。
- **镇长身份一致性探针**:`OfficeService.get_holder('mayor')` vs `system_config['current_mayor']` vs 持有 `meta_json['mayor']==True` 的居民集,三者应一致(不一致 = dual-write bug 告警)。
- **对照组(开关关)形态**:offices 表不被读写,身份只在 system_config['current_mayor'] + meta_json['mayor'];一致性探针只比对后两者;轮替计数从 install_mayor 覆盖事件推导。开/关两组形态对比即验证"统一表未改变政治节律,只改变了可追溯性"。

---

## 7. 边界与"不碰区域"

- **串行门**:任务 1 提交后才开 2;2 提交后才开 3;dual-write(2)先于 duty 查表改道(3)。
- **性能红线 tick +1**:`find_duty_resident` 今天已是**镇级线性扫描 O(N)**(`duty_service.py:45-90`);改查 offices 应是**单次索引查询**,是净改善,**不得**把每居民查询次数抬升超过 +1。offices 查询批量取 / 进 TickContext 复用,不在热循环里逐居民查表。
- **Lab 工程安全不变量**:本模块只碰虚构层职位;**不碰 `app/lab/` 审批门、安全不变量、capability profile**(§3 L0 物理禁区)。`lab_director` 若纳入 offices 仅作叙事槽位,不改 Lab 审批权限。
- **prompt 隔离**:offices 表 / 占用率 / 任期等**全局指标永不进入任何 NPC prompt**(§3.5 红线);office 信息进 prompt 唯一合法通道是文书公告(复用现有 announcement 调用,零新增 LLM)。
- **fail-open 语义**:所有新路径(term_check、dual-write、查表)必须 broad try/except log-and-continue,保持 election/civic/duty 现有 fail-open,绝不因 offices 异常打断 nightly tick 或工资发放。
- **Alembic 链尾单头校验**:迁移落地后 `alembic heads` 必须单头。
- **WS 新事件带 revision/seq 锚**:`office_changed` 照 world_changed v1 envelope(`world_revision_service.py:187-204`),seq 复用 OutboxEvent 游标(`72-84`),不滚新计数器。
- **不碰区域**:`meta_json['role']`(自由文本显示,从不程序化用于逻辑);Forge / 铸造;模型计价 / prompt 文风。

---

## 8. 依赖与冲突声明

### 8.1 依赖(前置模块)

- **§2 机制总表 S2-1 依赖列 = "—"**:无硬前置(镇长/文书/邮差的现状表示已在,医生绿地)。**下游**依赖本模块:S2-2 镇长裁量点(依赖 offices 权限集 `perms_json`)、S2-4 问责(recall→vacate)、S3-1 抽签任官(offices.fill_strategy)、S3-2 议事会(确认实验楼主任 office)、S5-3 退休(卸任→office 空缺→接任命权)、S5-8 健康(医生 office)。本模块是这些的地基,应尽早落。

### 8.2 会碰哪些文件(本模块)

修改:`backend/app/config.py`、`backend/app/services/election_service.py`、`backend/app/services/civic_service.py`、`backend/app/services/duty_service.py`、`backend/app/tasks/nightly_cron.py`、`backend/app/routers/admin/__init__.py`、`backend/app/models/__init__.py`。
新建:`backend/alembic/versions/NNN_add_offices.py`、`backend/app/services/office_service.py`、`backend/app/models/office.py`、`backend/app/routers/admin/offices.py`、`backend/tests/test_office_service.py`、`backend/tests/test_office_integration.py`。

> 注:跨模块文件触碰表里 S2-1 行的"修改/新建"列为空(建表者未填);以本节为准。

### 8.3 与其他 4 份 KICKOFF 的文件交集(逐条点名 + 串行/协调建议)

| 交集文件 | 与谁交集 | 冲突性质 | 串行/协调建议 |
|---|---|---|---|
| `backend/app/config.py` | S2-5 / S1-1 / S1-3 / S1-5 **全部** | 各自 append 一组 flag,低冲突纯行级 | 各自只追加自己前缀的 flag(`POLIS_OFFICE_` / `POLIS_POLICY_` / `REP_` / `POLIS_OPINION_` / `ECON_`);合并时 rebase 即可,不重排既有块 |
| `backend/app/tasks/nightly_cron.py` | S2-5 / S1-1 / S1-3 / S1-5 **全部** | 各加一个 try/except 块,高接触点 | **追加新块到 `86-126` 之后,不重排现有 M2/M3/M6 块**;合并按块拼接;顺序无语义依赖(各自 async_session 隔离) |
| `backend/app/services/civic_service.py` | S2-5 / S1-1 | `_execute_outcome`(`284-315`)同一 dispatcher 语义重叠:S2-1 保持 mayor 分支调 install_mayor(经 dual-write 落表),**S2-5 给同 dispatcher 加 policy 分级审批路由** | **串行:S2-1 先落 offices-backed 任免路径,S2-5 再在其上叠 policy 审批**;两者改的是不同 etype 分支,协调后可并行,但 merge 需同文件手工合 |
| `backend/app/services/election_service.py` | S1-1 | S2-1 拥有 mayor→office 桥(install_mayor/current_mayor `127-181`);S1-1 声誉读 current_mayor 做选人权重 | **S2-1 先落 offices-backed `current_mayor`,S1-1 消费其返回**;S1-1 不应绕过 current_mayor 直读 system_config |
| `backend/app/services/duty_service.py` | S1-5 | `_pay_wage`(`125-146`,mayor 消费者在 `135`)同函数:S2-1 保留镇长工资加成语义;S1-5 把 duty 工资改走 town_treasury | **串行 + 同函数协调**:S2-1 先锁定 `135` 行工资加成回归门(gotcha #1),S1-5 再改工资资金流向;两者都动 `_pay_wage`,merge 需手工合 |
| `backend/app/routers/admin/__init__.py` | S2-5(world.py 已存在,不新注册);S1-1/S1-3 的可选 admin 端点(若启用)亦落此文件 | 仅 `include_router` 追加,低冲突 | 各自 append include,合并无碍 |
| `backend/app/models/__init__.py` | S2-5(`policy`)/ S1-3(`issue_stance`)/ S1-5(`town_treasury`)各加一行 import | S2-1 加 `import app.models.office  # noqa: F401` 注册新表 | 追加式,相邻 import 行,merge 无语义冲突 |
| **Alembic 迁移(多头风险)** | S2-5 `041_add_policies` / S1-3 `041_add_issue_stances` / S1-5 `0XX_add_town_treasury` **均 branch off `040`** | **最尖锐冲突:多份 spec 各自以 `040_residents_creator_nullable` 为 down_revision → merge 时多头** | **迁移号只写占位符 `NNN`,down_revision 落地时按当时链头定(现链头 `040_residents_creator_nullable`,`040_residents_creator_nullable.py:14-17`)**;merge 时线性化重指 down_revision,`alembic heads` 校验单头;注意 S2-5 与 S1-3 都想叫 `041_`,并行时必须重命名 |

---

## HARD 纪律(全部适用)

1. **规则做骨架、LLM 做血肉;零 LLM 边际成本**:appoint/vacate/term_check 纯规则,任免公告复用既有 announcement/文书调用输出,不新增任何 LLM 调用。
2. **独立门控默认 False**:`polis_office_enabled: bool = False`,关闭时字节级回落到 install_mayor/current_mayor/_pay_wage/find_duty_resident 现状。
3. **迁移号占位符 `NNN`**:落地时 down_revision 按当时链头定(现链头 `040_residents_creator_nullable`);新表用 `create_table`(非 alter,不需 `op.batch_alter_table`);dev DB 是 SQLite、prod 是 Postgres,注意方言差异(回填幂等、可空容忍)。
4. **Alembic 链尾单头校验**:落地后 `alembic heads` 单头;与 S2-5/S1-3/S1-5 的迁移多头在 merge 时线性化解决。
5. **写路径原子**:条件 UPDATE + upsert(照 `coin_service.py:166-192` / `23-48` / `195-207`),`synchronize_session=False`,`meta_json` 写用 `flag_modified`,禁读改写。
6. **WS 新事件带 revision/seq 锚**:`office_changed` 照 world_changed v1(`world_revision_service.py:187-204`),seq 复用 OutboxEvent 游标(`72-84`)。
7. **性能红线 tick 每居民查询 +1 以内**:offices 查询批量取 / 进 TickContext,不逐居民查表(现状 find_duty_resident 已是 O(N) 线性扫描,改查表是净改善)。
8. **跨进程状态**:offices 是 DB 表(天然跨进程);节律状态若需临时缓存进 Redis,不新滚内存计数器。
9. **gotcha 如实记为现状缺口,不编接口**:医生 = 绿地(无 duty/preset/诊所,本模块只建 office 槽位,诊所/健康在 S5-8);无 term/vacate 历史(净新增);文书/邮差是 meta_json.duty 而镇长非 duty(两种表示桥接);find_duty_resident 首匹配(单持有者假设);duty 静态 seed(runtime appoint 净新增);offices 是这条线首个 schema 变更(M1–M6 刻意零迁移)、首个 mayor/duty HTTP 面。
