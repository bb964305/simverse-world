# Kickoff S2-5 — policies 表 + 四级分级审批

> **结论先行。** 本模块把今天散落在 `system_config`（`String(2000)` KV，无类型/无分级/无版本）里的治理状态**升格为一张带 `tier`/`procedure`/`version` 的 `policies` 表**，并在其上叠一层**四级分级审批矩阵**（行政 / 简单多数 / 绝对多数 / 宪法核心，对应方案 §3.3 line 90–99）。
>
> **最关键的现状认知（gotcha，务必先读）**：仓库里**根本不存在 `tier` 字段、也不存在任何审批分级概念**。今天只有两条**互不相交**的治理生命周期——(A) 管理员审批线（`WorldChangeProposal`，单一 `is_admin` 布尔、二值批准、无法定人数）与 (B) 投票线（`season.Poll`，NPC+玩家投票，**简单多数其实是相对多数 plurality，没有 >50% 阈值**）。方案的四个 tier **横跨这两条线**，不是改造某个已有的 tier 系统。`risk_level(low/medium/high)` 只做**标注**，从不改变审批路径。本模块新增的一切都必须**独立门控、默认 False**，关闭时字节级回落到上述现状。
>
> **零 LLM 边际成本**：政策审批为纯规则（阈值比较 + 路由查表 + 原子写），公告搭文书现有调用（不新增 LLM 调用）。规则做骨架，LLM 只在既有公告/日报里做血肉。
>
> **本模块对应方案**：§2 机制总表 `S2-5`（`SOCIETY_EXPANSION_PLAN.md:43`）、§3.3 分级审批矩阵（`SOCIETY_EXPANSION_PLAN.md:90`–`99`）、§6 接口面预告 `S2-5 政策`行（`SOCIETY_EXPANSION_PLAN.md:221`）、L0–L4 修改面（`SOCIETY_EXPANSION_PLAN.md:82`–`88`，policies = L2 层）、红线（`SOCIETY_EXPANSION_PLAN.md:320`–`328`）。

---

## 1. 现状锚点（逐文件逐行核实；只用已核实的 file:line）

### 1.1 存储现状——今天的"政策"是 `system_config` 里的无类型 JSON blob

- `backend/app/models/system_config.py:7-15` `class SystemConfig`：列为 `key`(PK `String(200)`)、`value`(**`String(2000)`——小上限，非 JSON/Text**)、`group`(`String(50)` indexed)、`updated_at`、`updated_by`。**唯一的通用配置存储**，无 `tier`、无 `procedure`、无 `version` 列。`value` 只有 2000 字符，大 policy 载荷放不下——**这就是新建 `policies` 表的正当性**。
- `backend/app/services/config_service.py:10-56` `ConfigService(get/set/get_group)`：`get()` 对 value 做 `json.loads`，`set()` 做 `json.dumps` + upsert + commit，`get_group()` 返回某 group 的 `{key:value}`。所有 civic/election 运行时状态都住这里（`current_mayor`、`election_last_season`、`election_last_opened`，`group='civic'`）。**今天的政策就是这里的无类型无分级 JSON blob**，本模块要把它们形式化/迁移到 `policies` 表。

### 1.2 管理员审批线（track A）——单人二值，无分级

- `backend/app/models/world_change_proposal.py:10-39` `class WorldChangeProposal`：字段 `id, origin(lab_run|resident|admin), origin_ref, author_slug, kind(add_location|add_mechanic|add_lore|edit_location|add_npc|edit_npc), title, rationale_md, patch_json(JSON), cost_sc, status(pending→approved→applied|rejected|reverted|failed, indexed), risk_level(low|medium|high `:30`), reviewer_id, review_note, approved_at(`:36`), applied_at, reverted_at, created_at`。**没有 `tier` 字段、没有审批矩阵概念**。`risk_level` 是最接近 tier 的既有物，但它**只标注、不改变审批路径**。
- `backend/app/routers/admin/world.py:51-58` `approve_proposal`（`POST /world/proposals/{proposal_id}/approve`）：**唯一**的世界提案审批端点，单审批人、二值批准，`Depends(require_admin)` 守卫，委托 `proposal_service.approve_proposal(db, id, admin.id, note)`，`psvc.ProposalError→409`。兄弟端点 reject(`:61-68`)/revert(`:71-78`)/list(`:23-30`)/get+preview(`:33-48`)。**无多签、无分级门、无法定人数。**
- `backend/app/routers/admin/middleware.py:10-31` `require_admin`：管理员鉴权 = JWT 提取 + 校验 + `if not user.is_admin: raise 403`。**单一布尔 `user.is_admin`**，无角色分层、无按-tier 授权等级。**任意 admin 可批任意提案，无视 `risk_level`。**
- `backend/app/services/proposal_service.py:125-213` `approve_proposal`：完整批准生命周期。经 `transitions.cas_proposal_status` 做 `pending→approved` 的 CAS（`rowcount==1` 胜出，挡住并发双批），同 commit 盖 `approved_at`（P0-5b）；随后 `apply_engine.apply_proposal`（revisioned kinds flush-only；否则 legacy commit-inside）；`ApplyError→rollback+_fail_apply`（退款）；置 `status=applied, applied_at`，单 commit，再 reload_world/publish/broadcast + `emit world_proposal_applied`。**无 tier 检查、无票数统计、无法定人数——一次 admin 调用即把 pending 翻到 applied。**
- `backend/app/services/proposal_service.py:26-38` `OPEN_KINDS + assess_risk`：`OPEN_KINDS=(add_lore,edit_location,add_location,add_mechanic)`；`add_npc/edit_npc` 推迟(P4)。`assess_risk` 映射 kind→risk：`add_npc/edit_npc=high`、`add_location/add_mechanic=medium`、否则 low。**这是既有的 kind→严重度映射**，四级矩阵会叠加于其上或替换它。

### 1.3 投票线（track B）——相对多数（plurality），不是真多数

- `backend/app/services/civic_service.py:32-80` `propose`（M3 advisory/civic propose）：**独立于 track A 的治理路径**。开一张 `season.Poll`（`question, options_json=[{label,effect,npc_votes}], closes_at, status=open`）——**不是 `WorldChangeProposal`**。文书经 bulletin 公告。受 `settings.civic_polls_enabled` 门控。选项带 `effect` dict，胜出后分派。这是**投票制轨道**（NPC+玩家），与管理员审批轨道不相交。
- `backend/app/services/civic_service.py:254-315` `_close_one + _execute_outcome`：统计 = 每选项 `npc_votes + player votes(Vote 表)`；winner = max（**简单 plurality，确定性 tie-break `-i`**）。胜者 `effect` 经 `_execute_outcome` 落地，仅走**既有渠道**：`system_config`(`ConfigService.set`)、`dynamic_location`(overlay+reload)、`narrative`(WorldEvent)、`mayor`(`election_service.install_mayor`)。**无 tier/法定人数/绝对多数逻辑——纯相对多数。** 这是"policies"效果类型或分级阈值逻辑要挂的地方。

### 1.4 "在任者/职权"现状——双存储，无专表

- `backend/app/services/election_service.py:127-172` `install_mayor`：镇长今天怎么存的——`meta_json['mayor']=True` 写在 winner `Resident`（其余全清，`flag_modified`），**且** `current_mayor` 键经 `ConfigService.set(group='civic')` 写进 `system_config`。**无专用 office/role/policy 表。** 任何"在任者/授权"tier 概念必须与这**双存储**（`Resident.meta_json` + `system_config`）对账。

### 1.5 并发原语——复用已有 CAS，不自造锁

- `backend/app/lab/transitions.py:61-74` `cas_proposal_status`：对 `WorldChangeProposal.status` 的原子 compare-and-swap（`UPDATE...WHERE status IN expected`，`rowcount==1` 胜出，不 commit）。**任何新的分级审批状态机都应复用此原语做多步门，不要自造锁**（并发审批竞态已在此处理妥当）。

### 1.6 迁移链与漂移

- `backend/alembic/versions/040_residents_creator_nullable.py:14-17`：**当前单一链头（已确认）**。`revision = "040_residents_creator_nullable"`，`down_revision = "039_add_resident_relations"`，`branch_labels = None`，`depends_on = None`。链：`037→038_add_realism_fields→039_add_resident_relations→040`。
- `backend/alembic/versions/040_residents_creator_nullable.py:20-32`：`upgrade()/downgrade()` 用 `with op.batch_alter_table("residents") as batch_op:` 做列变更（SQLite 兼容 batch 模式——dev DB 是 SQLite `skills_world_dev.db`）。**新迁移对既有表做 ALTER 一律用 `batch_alter_table`。**
- `backend/alembic/versions/033_add_world_governance.py:21-64`：建了 `world_change_proposals` + `dynamic_locations`(slug unique overlay) + `dynamic_mechanics`(code unique)。**注意漂移**：此迁移**没有**建模型 `:36` 现有的 `approved_at` 列——`approved_at` 是后加的（realism P0-5b，约 `038_add_realism_fields`）。**落地前先核实当前 DB schema 再加列。**

### 1.7 nightly seam 与共享基础设施

- `backend/app/tasks/nightly_cron.py:86-126` 治理夜间块：治理夜间任务如何登记的——内联在 nightly cron 函数里，各裹自己的 try/except + `async_session()`：`close_due_polls`(`:88-94`)、`seed_civic_agenda`(`:99-105`)、`maybe_open_seasonal_election`(`:110-116`)、`run_npc_voting`(`:120-126`)；`reclaim_stuck_proposals`(`:190-192`)。**无调度器抽象——新的政策-tier 关闭/执行步骤作为又一个内联块加在这里。**
- `backend/app/tasks/nightly_cron.py:28-35` `run_nightly_jobs()`：每个 job 是一个隔离 try/except，开 `async with async_session() as db:` 调 service。新夜间聚合按此块形加。
- `backend/app/tasks/nightly_cron.py:343-347` `nightly_cron_loop()`：`while True: sleep(...); run_nightly_jobs()`，固定 00:30 UTC 每日一次；无 per-job 调度器，周级任务自行 `weekday()` 门控。
- `backend/app/main.py:85-93`：`nightly_cron_loop` 仅当 `settings.run_background_tasks` 为 True 时注册为 asyncio 任务；否则由 standalone agent-worker 拥有。**夜间钩子在恰好一个进程里跑。**
- `backend/app/config.py:7-19` `class Settings(BaseSettings)`：每字段是带默认字面量的类属性（如 `auto_create_tables: bool = False`）。新 flag 就加 `xxx_enabled: bool = False` 类属性，无需 `__init__`/`Field()`。
- `backend/app/config.py:375-378`：`model_config = {"env_file": ".env"}` + 模块级单例 `settings = Settings()`；`FOO_BAR` 环境变量自动覆盖 `foo_bar` 字段。`from app.config import settings` 导入。
- `backend/app/config.py:246-268` `realism_enabled + REALISM_*` 块：主开关 `realism_enabled: bool = False`(`:249`) 门控一组 `realism_*` 调参常量，注释约定"Default False → behavior identical to pre-realism"。**主开关 + 调参常量组**的参考范式。
- `backend/app/config.py:321-352` realism P2 独立门：三个独立 bool 门全默认 False（`realism_relations_enabled:325`、`realism_info_gradient_enabled:326`、`realism_crowd_enabled:327`），各自独立门控。**"每特性独立门"参考。**
- `backend/app/config.py:354-373` M1–M6 town flags：**这些默认 TRUE 不是 False**（`civic_polls_enabled:368=True`、`election_enabled:371=True` 等）。**本模块新 flag 必须默认 False**（回滚安全），与此不同。（`:373` 有个错位尾注释，别抄那个错。）
- `backend/app/routers/admin/economy.py:115-159` + `backend/app/routers/admin/__init__.py:18-31`：鉴权**逐端点**执行（每路由带 `admin: User = Depends(require_admin)` 参数，如 `:117,:128,:159`），**无 router 级 `dependencies=[...]`**。新 admin 子路由：建模块 `router = APIRouter(prefix="/xxx")`、每端点自带 `Depends(require_admin)`、在 `__init__.py:18-31` 处 `router.include_router(...)`。
- `backend/app/lab/apply.py:237-245` `broadcast_world_changed(payload=None)`：**world_changed WS emit helper**（在 `lab/` 下但被广泛复用）。`payload` 给则广播完整信封；`None` 则裸 ping。`manager.broadcast` 内部懒导入，try/except 包裹（广播失败仅 warn）。
- `backend/app/ws/manager.py:109-128` `manager.broadcast(data, exclude=None)`：经 Redis pub/sub 跨 worker 扇出所有客户端；`send_to_user(user_id, data)` 定向。`from app.ws.manager import manager`。
- `backend/app/services/world_revision_service.py:187-204` `world_changed_event(*, revision, action, seq, event_id, occurred_at)`：构建**冻结的 world_changed v1 信封**（美术规格）。kwargs-only。**新 WS 事件若需 revision/seq 锚，镜像此形状。**
- `backend/app/services/world_revision_service.py:72-84` `current_source_cursor(db)`：耐久单调 seq = `max(OutboxEvent.id)` where `topic=='world_changed'`（无事件前为 0）。**seq/revision 耐久性由 OutboxEvent 表背书，非内存计数器。**
- `backend/app/events/bus.py:28-42` `on(event)/emit(db, event, **kw)`：**进程内同步域事件总线（非 WS）**。`@on("name")` 注册 async handler；`await emit(db, "name", **kw)` 触发，失败隔离。反应 chat_completed 等域事件的接缝——与 WS broadcast 路径不同。

### 1.8 本模块要接线的确切位置（现状小结）

四级矩阵叠在**两条不相交生命周期**上，映射如下（依 interface_hints 逐条落位）：

| 方案 tier（§3.3） | 现状最近物 | 缺口 |
|---|---|---|
| 行政级 | track A：`world.py:51-58` approve + `require_admin`（单 `is_admin`） | 无按-tier 授权；`is_admin` 全有或全无 |
| 简单多数级 | track B：`civic_service.py:254-315` `_close_one` **plurality** | **无 >50% 阈值**（今天只取 max） |
| 绝对多数级 | **无任何对应物** | 需新增法定人数 + 超多数阈值逻辑 |
| 宪法核心 | **无任何对应物**；`is_admin` 无角色层级 | 需"不可修改"拒绝逻辑 + 自指保护（§3.3:97） |

**结论**：新增 (1) 一张 `policies` 表承载 `key/value/tier/procedure/version`；(2) 一个 `tier→{approval_path, threshold, authority}` 矩阵；(3) 在 `_close_one` 加阈值判定、在 `_execute_outcome` 加 `policy` 效果类型；(4) 复用 `cas_proposal_status` 做任何多步门；(5) 全部行为门控在默认-False 开关后。

---

## 2. 任务切分

> 串行门：任务 1（表 + 模型 + 迁移）全绿并提交后才开任务 2（PolicyService 矩阵）；任务 2 全绿才开任务 3/4（审批路由接线）。每任务独立提交、提交信息带任务号（`s2-5-1: policies table + migration`）。

### 任务 1 — `policies` 表 + ORM 模型 + 迁移

**新建 `backend/app/models/policy.py`**（ORM 模型）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `Integer` PK | 自增主键 |
| `key` | `String(200)`, unique, indexed | 政策键（如 `election_interval_days`、`tax_rate`、`curfew_hours`、`approval_routing`）；对齐 `SystemConfig.key` 语义 |
| `value` | `Text` | 政策载荷 JSON 字符串（`json.dumps`/`loads`，**不用 `String(2000)`**——正是本表存在理由，见 `system_config.py:12`） |
| `tier` | `String(32)`, indexed | 四级之一：`administrative` / `simple_majority` / `absolute_majority` / `constitutional_core`（对应 §3.3:93-97） |
| `procedure` | `String(64)` | 审批路由标签：`admin_direct` / `civic_poll` / `civic_poll_supermajority` / `immutable`（tier→procedure 见任务 2 矩阵） |
| `group` | `String(50)`, indexed | 沿用 `ConfigService` 的 group 语义（如 `civic`/`fiscal`/`routing`），迁移期与 `system_config.group` 对齐 |
| `version` | `Integer`, default 1 | 修订版本号，每次成功 amend +1（乐观并发字段，见 §4） |
| `updated_by` | `String(200)`, nullable | 最后修改来源（`admin:<id>` / `poll:<id>` / `seed`） |
| `created_at` | `DateTime(timezone=True)` | |
| `updated_at` | `DateTime(timezone=True)` | |

**新建 `backend/alembic/versions/NNN_add_policies.py`**（迁移号占位符 **NNN**，落地时按当时链头定——**现链头 `040_residents_creator_nullable`**）：
- `revision = "NNN_add_policies"`，`down_revision` 落地时须 = 当时链头（现为 `"040_residents_creator_nullable"`；若并行 workstream 已插入迁移导致多头，merge 时重排，见 §8）。
- `upgrade()` 用 `op.create_table("policies", ...)`（新表，无需 batch）；对既有表**不加列**（本模块**不改** `world_change_proposal` 的列——tier 挂在 `policies` 侧，见 gotcha 1）。
- `downgrade()` 用 `op.drop_table("policies")`。
- SQLite dev DB 兼容：新表 `create_table` 无 batch 需求；`server_default` 谨慎（SQLite 限制），default 交给 ORM。

**改 `backend/app/models/__init__.py`**：注册 `Policy` 模型（对齐既有模型导出方式；落地时按该文件现有 import 风格追加，不臆造）。

### 任务 2 — `backend/app/services/policy_service.py`（新建，tier→阈值矩阵 + 审批路由）

**tier→审批矩阵（模块级常量，纯规则、零 LLM）**：

```
TIER_MATRIX = {
    "administrative":      {"path": "admin_direct",              "threshold": None,  "authority": "is_admin"},
    "simple_majority":     {"path": "civic_poll",                "threshold": 0.50,  "authority": "vote"},
    "absolute_majority":   {"path": "civic_poll_supermajority",  "threshold": 0.667, "authority": "vote", "quorum": True},
    "constitutional_core": {"path": "immutable",                 "threshold": None,  "authority": "none"},
}
```

Service 方法签名（`PolicyService(db: AsyncSession)`，对齐 `ConfigService` 构造范式 `config_service.py:11`）：

```
async def get(self, key: str, *, default=None) -> Any
    # json.loads(policies.value)；miss → 回落 ConfigService(db).get(key)（迁移期共存，见门控 §3）
async def get_group(self, group: str) -> dict[str, Any]
async def seed_defaults(self) -> int
    # 幂等 upsert：把 config.py 现值 + system_config 现有键播种成 typed rows；返回新增行数
async def classify(self, key: str) -> str
    # 返回该 key 的 tier（查表；未知键回落 'simple_majority' 保守档）
async def propose_amend(self, key: str, new_value: Any, *, origin: str, author: str, rng=None) -> AmendResult
    # 纯路由：查 tier→path。administrative→建 admin 审批任务；simple/absolute_majority→开 civic Poll（带 threshold 元数据）；constitutional_core→拒绝（raise PolicyImmutableError）
async def apply_amend(self, key: str, new_value: Any, *, expected_version: int, updated_by: str) -> bool
    # 原子条件 UPDATE（见 §4），version 匹配才写，version+1；rowcount==1 → True
```

- `propose_amend` 对 `constitutional_core` 抛 `PolicyImmutableError`（对应 §3.3:97 宪法核心不可修改 + 自指保护：`approval_routing` 本身置 `absolute_majority`，其被降级的尝试留给 S3-7 违宪控告管辖，本模块只**拒绝直接修改核心条款**）。
- **零新增 LLM**：全部为查表 + 阈值比较 + 原子写；公告复用 track B 既有 bulletin 调用。

### 任务 3 — 管理端审批路由接线（track A，行政级）

**改 `backend/app/routers/admin/world.py`**（复用现有 `approve_proposal` 端点结构 `:51-58`，**不新建端点除非必要**）+ 新建 `backend/app/routers/admin/policies.py`（admin 子路由，逐端点 `Depends(require_admin)`）：

- `GET /admin/policies`（`admin: User = Depends(require_admin)`）→ `PolicyService.get_group` 全量，带 tier/version 标注。
- `POST /admin/policies/{key}/amend`（`admin: User = Depends(require_admin)`，body `{value, expected_version}`）→ 仅当 `classify(key)=='administrative'` 才直批（`apply_amend`）；否则返回 409 + 指示走 civic poll（`simple/absolute_majority`）或拒绝（`constitutional_core`）。
- `GET /town/policies`（**玩家只读**，对应 §6:221"玩家只读"）→ 只读投影，**鉴权依赖为标准登录用户依赖**（现状缺口：本模块 anchors 未核实 `/town` 路由的用户级鉴权依赖具体符号，落地时按项目既有玩家端鉴权依赖接线，不臆造符号名）。
- 在 `backend/app/routers/admin/__init__.py:18-31` 处 `router.include_router(policies_router)`。

**改 `backend/app/services/proposal_service.py`**（`approve_proposal :125-213`）：在 CAS 之前插入 tier 门——当提案 kind 对应政策条目时，查 `PolicyService.classify`；`administrative` 才走现有单-admin CAS 路径；非 administrative 的走 track B（不允许 admin 直批），复用 `cas_proposal_status`（`transitions.py:61-74`）做多步门，**不自造锁**。门控关闭时此分支完全跳过，回落现状单-admin 批准。

### 任务 4 — 投票线阈值 + policy 效果类型（track B，简单/绝对多数级）

**改 `backend/app/services/civic_service.py`**（`_close_one + _execute_outcome :254-315`）：

- `_close_one`：在现有 plurality（取 max）之后加**阈值判定**——从 poll 元数据读 `threshold`（simple_majority=0.50 / absolute_majority=0.667）；胜者得票占比 `< threshold` 或（absolute 档）未达法定人数 → **不执行、标记流会**（对应现状缺口：今天 `_close_one` 无阈值，纯 max）。门控关闭时回落纯 plurality（无阈值），与现状字节级一致。
- `_execute_outcome`：新增 `effect` 类型 `"policy"` → 调 `PolicyService.apply_amend(key, value, expected_version=..., updated_by="poll:<id>")`（原子写，见 §4）。既有四类型（`system_config`/`dynamic_location`/`narrative`/`mayor`）不动。

**改 `backend/app/tasks/nightly_cron.py`**（`:86-126` 治理块）：新增一个隔离 try/except 块调 `PolicyService` 相关的到期政策 poll 关闭/执行步骤（若 track B 的 `close_due_polls` 已覆盖则复用，不重复关闭；仅在需要独立 tier-close 节律时新增块）。块内 `from app.config import settings` + `if settings.<flag>:` 门控（见 §3），fail-open：try/except 包裹，异常仅 `logger.error(exc_info=True)`，不打断其他 job（对齐 `:86-126` 现有块形）。

### 任务 5 — config flag（见 §3 独立成节，此处仅列改动文件）

**改 `backend/app/config.py`**：`Settings` 类加 `polis_policy_enabled: bool = False` 等（详见 §3）。

---

## 3. 门控开关与默认值

沿用 `config.py:7-19` 的类属性 flag 模式（`from app.config import settings`），前缀 **`POLIS_POLICY_`**（对应 §6:221 config 前缀列 `POLIS_POLICY_`）。**全部默认 `False`**——**注意**：M1–M6 town flags（`config.py:354-373`，如 `civic_polls_enabled=True`）默认 TRUE 是历史例外（commit `5172f0e` 已发布启用）；本模块**必须遵循默认-False 项目规则**（回滚安全），参考物是 realism 家族（`config.py:246-268`/`321-352` 全默认 False）。

在 `backend/app/config.py` 的 `Settings` 类（`:7`）追加：

```
# --- S2-5 policies + 四级分级审批（默认 False → 行为与现状字节级一致）---
polis_policy_enabled: bool = False           # 主开关：policies 表读写路径总门
polis_policy_approval_enabled: bool = False  # 独立门：四级审批路由（叠在 track A/B 上）
polis_policy_simple_majority_threshold: float = 0.50    # 简单多数阈值
polis_policy_absolute_majority_threshold: float = 0.667 # 绝对多数阈值（超多数）
polis_policy_quorum_fraction: float = 0.50   # 绝对多数档的法定出席/投票人数占比
```

**关闭时的字节级回落语义**（逐开关）：

- `polis_policy_enabled=False`：所有 `PolicyService.get/get_group` 路径**不建表读**，回落 `ConfigService`（`system_config`）现状读；`seed_defaults` 不执行；`GET /town/policies`/`GET /admin/policies` 返回空或 404（按项目既有空态约定）。政策仍是 `system_config` 里的无类型 JSON blob——**与现状一致**。
- `polis_policy_approval_enabled=False`：`proposal_service.approve_proposal` 不插 tier 门（回落 `:125-213` 现状单-admin CAS→apply）；`_close_one` 不做阈值判定（回落 `:254-315` 现状纯 plurality）；`_execute_outcome` 不识别 `policy` 效果类型（未知类型按现状忽略/no-op）。**两条治理生命周期字节级回到现状。**

两个开关独立：可只开 `polis_policy_enabled`（先落表、影子读写，审批仍走现状）验证存储层，再开 `polis_policy_approval_enabled` 接四级路由。数值阈值走 `POLIS_POLICY_` 前缀进 config，env 自动覆盖（`config.py:375-378`）。

---

## 4. 原子性要求

**红线**：写路径一律条件 UPDATE + upsert，**禁读-改-写**。

- **政策修订（`apply_amend`）用乐观并发条件 UPDATE**：
  ```
  UPDATE policies
     SET value = :new_value, version = version + 1, updated_by = :by, updated_at = now()
   WHERE key = :key AND version = :expected_version
  ```
  `rowcount == 1` 胜出（版本不匹配 = 有并发修订，本次落败，调用方需重取重试或标记冲突）。**绝不**先 `SELECT value` 再 Python 里改再写回。
- **播种（`seed_defaults`）用 upsert**：`INSERT ... ON CONFLICT(key) DO NOTHING`（Postgres）/ SQLite `INSERT OR IGNORE`——幂等，多进程/多次调用不重复插。（注意 DB 方言差异：dev=SQLite、prod=Postgres asyncpg，见 §7。）
- **审批状态机复用既有 CAS 原语**：任何"提案/poll 从待批→已批/已执行"的多步门，一律经 `transitions.cas_proposal_status`（`backend/app/lab/transitions.py:61-74`，`UPDATE...WHERE status IN expected`，`rowcount==1` 胜出，不 commit）——**并发审批竞态已在此处理妥当，不自造锁**。
- **原子化范式参照**：项目既有的原子化标杆是 `coin_service` 的条件 UPDATE 范式（realism P0 任务 5 的并发无丢更新标准；`backend/app/services/coin_service.py`——**现状缺口**：本模块 anchors 未核实 coin_service 的具体 file:line，落地时以该服务的条件-UPDATE/upsert 实现为准对齐，勿臆造行号）。`apply_amend`/`bump` 类写入按此同标准对待：多 worker 并发下丢更新不可接受。

---

## 5. 测试口径

全部随机路径注入 RNG，seeded 断言可复现；每类都含**门控回落断言**（开关 False 时既有行为字节级不变）。

**单测（`backend/tests/test_policy_service.py`，具体 `test_` 函数名）**：

- `test_seed_defaults_idempotent`：连调两次 `seed_defaults`，第二次返回 0 新增，行数不变（upsert 幂等）。
- `test_apply_amend_optimistic_version_wins`：`expected_version` 匹配 → `rowcount==1`、version+1、value 更新。
- `test_apply_amend_stale_version_loses`：并发下 `expected_version` 过期 → 返回 False，value 不变（无丢更新）。
- `test_apply_amend_concurrent_no_lost_update`：两协程同 `expected_version` 竞争，恰一个成功、一个失败（复现 coin_service 并发标准）。
- `test_classify_returns_tier`：已知 key→tier 映射正确；未知 key 回落保守 `simple_majority`。
- `test_propose_amend_administrative_routes_admin`：`administrative` tier → 走 admin 直批路径。
- `test_propose_amend_simple_majority_opens_poll`：`simple_majority` → 开 civic Poll 且带 `threshold=0.50` 元数据。
- `test_propose_amend_absolute_majority_sets_supermajority`：`absolute_majority` → poll 元数据 `threshold=0.667` + `quorum` 标记。
- `test_propose_amend_constitutional_core_rejected`：`constitutional_core` → `raise PolicyImmutableError`，无 poll、无写入（对应 §3.3:97 不可修改 + 自指保护）。
- `test_gate_off_falls_back_to_config_service`：`polis_policy_enabled=False` 时 `get` 回落 `ConfigService`、写路径不触表——现状字节级一致。

**集成测试（`backend/tests/test_policy_approval_integration.py`，具体 `test_` 函数名）**：

- `test_admin_amend_administrative_applies`：`POST /admin/policies/{key}/amend`（`administrative`）→ 200、policies.version+1。
- `test_admin_amend_non_administrative_409`：admin 试图直批 `simple_majority` 条目 → 409 + 指示走 poll。
- `test_admin_amend_requires_admin`：无 `Depends(require_admin)` 凭证 → 401/403（逐端点鉴权断言）。
- `test_close_one_simple_majority_below_threshold_no_apply`：seeded 票——胜者占比 <50% → 政策不变（流会），对照现状 plurality 会执行。
- `test_close_one_simple_majority_above_threshold_applies`：胜者占比 ≥50% → `_execute_outcome` 走 `policy` 类型、`apply_amend` 落地。
- `test_close_one_absolute_majority_quorum_and_supermajority`：seeded——达法定人数且 ≥66.7% 才执行；欠一即流会。
- `test_execute_outcome_policy_effect_atomic`：`policy` 效果经条件 UPDATE 写入，version 单调 +1。
- `test_approval_gate_off_track_a_unchanged`：`polis_policy_approval_enabled=False` → `approve_proposal` 走现状单-admin CAS→apply，既有提案测试零改动通过。
- `test_approval_gate_off_track_b_plurality`：门控关时 `_close_one` 回落纯 plurality（无阈值），既有 civic 测试零改动通过。
- `test_migration_single_head_after_add_policies`：`alembic heads` 单头校验（新迁移接链头后仍单头）。

---

## 6. 探针出数定义

对应方案 §2 S2-5 验收列"政策漂移距离探针；核心条款不可触碰"（`SOCIETY_EXPANSION_PLAN.md:43`）。在 `burnin_report.py` 新增：

- **政策漂移距离（policy drift distance）**：对每个非 `constitutional_core` 政策条目，累计其 `version` 变化次数与归一化数值漂移量（对数值型 value 取 `Σ|Δnormalized|`，对枚举/布尔型取翻转计数），随模拟时间输出曲线。
  - **目标形态**：漂移随治理活动累积、呈**阶梯状**（每次成功 amend 一跳），`simple_majority` 条目漂移多于 `administrative`，`absolute_majority` 条目漂移最少（高门槛=稳定）；`constitutional_core` 漂移**恒为 0**（探针硬断言：核心条款不可触碰，对应红线 §9.2 `:321`）。
  - **对照组（`polis_policy_approval_enabled=False`）**：政策仍是 `system_config` 无类型 blob，无 tier 约束 → 漂移**无分级差异**（任意 admin 可改任意键，`constitutional_core` 无保护），曲线呈无差别随机游走。**开关开/关的对照 = 有分级纪律 vs 无分级纪律。**
- **核心条款触碰计数（constitutional-core touch count）**：统计对 `constitutional_core` 条目的**修改尝试数**与**成功数**。
  - **目标形态**：尝试数可 >0（有人想改），**成功数恒 = 0**（`PolicyImmutableError` 全挡）。
  - **对照组（开关关）**：无 `constitutional_core` 概念，成功数 = 尝试数（无保护）。

seeded fixture 演示出数，首轮数值记入 `PROGRESS.md`。

---

## 7. 边界与"不碰区域"

- **串行门**：任务 1（表+迁移）全绿并提交后才开任务 2；任务 2（矩阵）全绿才开任务 3/4（路由接线）。跨任务独立提交、提交信息带任务号。
- **性能红线 tick +1**：读取面（`classify`/`get` 在 track B 关闭 poll 或 track A 审批时）**不得把 tick 循环每居民查询次数抬升超过 +1**。政策读应批量取/进上下文复用（policies 表百级行，一次 `get_group` 全量载入缓存），**不在 per-resident 循环里逐条查库**（诊断报告点名过 perceive O(N²) 前科）。
- **Alembic 链尾单头**：新迁移落地后 `alembic heads` 必须单头；`down_revision` 落地时按当时链头定（现 `040_residents_creator_nullable`）。并行 workstream 可能各插一条迁移造成多头——**merge 时重排 down_revision 链**（见 §8）。
- **迁移号占位符 NNN**：文档内一律写 **NNN**，落地时按当时链头定。
- **DB 方言**：dev=SQLite（`skills_world_dev.db`），prod=Postgres（asyncpg）。新表 `create_table` 两端兼容；`ON CONFLICT` 语法差异用 SQLAlchemy dialect-agnostic upsert 或分支处理；对既有表 ALTER（本模块**不做**）才需 `batch_alter_table`。
- **WS 新事件带 revision/seq 锚**：若发 `policy_changed` WS 事件（对应 §6:221 WS 列 `policy_changed`），镜像 `world_revision_service.world_changed_event(*, revision, action, seq, ...)`（`:187-204`）的信封形状，seq 复用 `OutboxEvent.id`（`current_source_cursor :72-84`），**不自造内存计数器**。跨进程状态（如审批中间态）若需共享，进 Redis，不放内存。
- **Lab 工程安全不变量（L0，绝对禁区，红线 §9.1 `:320`）**：policies 表属 **L2 政策层**（`SOCIETY_EXPANSION_PLAN.md:84`）——实验楼对 L2 **只有起草权与背书权，无直接修改权**；Lab 审批门/安全不变量/LLM 预算/信封定义（L0，`:82`）**物理不可达**，本模块不触碰、不为其提供绕过路径。审批路由规则本身（`approval_routing`）置 `absolute_majority`，其被非法降级由 S3-7 违宪控告管辖（本模块只拒绝对 `constitutional_core` 的直接修改）。
- **prompt 隔离（红线 §9.3 `:322`）**：政策全局指标/漂移探针数据**永不进入任何 NPC prompt**，唯一例外是公报机制（世界内信息物）；写成测试断言。
- **零新增 LLM（红线 §2 表头 `:25`）**：审批为纯规则；公告搭文书/日报现有调用，不新增 LLM 调用。
- **不改 `world_change_proposal` 的列**：tier 挂在 `policies` 侧（gotcha 1）——不给 `world_change_proposals` 加 `tier`/`procedure` 列，避免与 track B 双写 tier 语义。
- **不碰 `app/lab/` 的 apply/preflight 内核逻辑**（除复用 `transitions.cas_proposal_status` 与 `broadcast_world_changed` 两个既有 helper 外，不改其实现）。

---

## 8. 依赖与冲突声明

**前置依赖**：

- **S1-5 镇财政闭环**（方案 §2 S2-5 依赖列标 `S1-5`，`:43`）：`tax_rate`、`医疗补贴`、`住房开发规模`等财政类政策条目的 value 语义依赖 S1-5 的 `town_treasury`/`TreasuryService` 落地。**协调**：本模块先落 `policies` 表的**存储与审批骨架**（tier/version/路由），财政类条目的**具体 effect 落库**待 S1-5 的 `treasury_service` 就位后接线；未就位时该类条目可 amend 但 effect 为 no-op 占位（记 PROGRESS）。
- **既有 M3 civic（track B）与 M6 election**：`civic_polls_enabled`/`election_enabled`（`config.py:368/371`，默认 True）——本模块 track B 阈值改造挂在 `_close_one`（`civic_service.py:254-315`）内，须与这两个既有开关协同（civic 关时无 poll 可关，阈值逻辑自然不触发）。

**本模块会碰的文件**（`will_modify` + `will_create`）：

- 改：`backend/app/models/world_change_proposal.py`（仅读 tier 判定接线，**不加列**）、`backend/app/services/proposal_service.py`、`backend/app/routers/admin/world.py`、`backend/app/services/civic_service.py`、`backend/app/tasks/nightly_cron.py`、`backend/app/config.py`、`backend/app/models/__init__.py`（注册 Policy）。
- 建：`backend/app/models/policy.py`、`backend/alembic/versions/NNN_add_policies.py`、`backend/app/services/policy_service.py`、`backend/app/routers/admin/policies.py`、`backend/tests/test_policy_service.py`、`backend/tests/test_policy_approval_integration.py`。

**与其他 4 份 KICKOFF 的文件交集（逐条点名 + 串行/协调建议）**：

| 交集文件 | 与哪些模块交集 | 冲突面 | 建议 |
|---|---|---|---|
| `backend/app/config.py` | **全部 5 个模块**（S1-1/S1-3/S1-5/S2-1 均改） | 都在 `Settings` 类尾**追加** flag 块 | **追加式改动，低冲突**；各用独立前缀（本模块 `POLIS_POLICY_`，S1-1 `REP_`，S1-3 `POLIS_OPINION_`，S1-5 `ECON_`）；merge 时按前缀分块，git 文本冲突手动合并即可。**协调**：约定各模块只在类尾追加、不改他人行。 |
| `backend/app/tasks/nightly_cron.py` | **S2-1 / S1-1 / S1-3 / S1-5** 均改 | 各加一个**隔离 try/except 夜间块**（`:28-35` 块形；S2-1 加 `term_check` 块） | **块级隔离，低冲突**；各自新增独立块、不改他人块。**协调**：都插在 `:86-126` 治理块之后、`reclaim :190-192` 之前的同一区域，merge 时可能相邻行冲突，手工按块拼接。 |
| `backend/app/services/civic_service.py` | **S1-1 公共声誉轴、S2-1 offices** 也改 | **同文件、可能同函数**：本模块改 `_close_one/_execute_outcome`（`:254-315`）加阈值+policy effect；S1-1 在 civic 消费声誉（投票信任/八卦可信度）；S2-1 改 `_execute_outcome` mayor 分支走 offices 任免 | **中冲突——需协调**。三方可能同触 `_execute_outcome` dispatcher：**建议串行 S2-1 先落 offices-backed 任免路径 → 本模块叠 policy 分级审批路由 → S1-1 叠声誉着色**；merge 前对齐 `_close_one`/`_execute_outcome` 的最终形状。 |
| `backend/app/services/proposal_service.py` | 仅本模块（其他 4 份未列） | 本模块 `approve_proposal :125-213` 插 tier 门 | 低冲突（独占）。 |
| `backend/alembic/versions/NNN_*.py` | **S1-3（`041_add_issue_stances.py`）、S1-5（`0XX_add_town_treasury.py`）、S2-1（`NNN_add_offices.py`）** 各新增迁移 | **迁移链多头风险**：四份都拟从链头 `040` 分叉 | **高冲突——必须协调迁移号**。多个模块并行各接链头 `040` 会产生**多头**。**建议**：迁移号一律写占位符 **NNN**，落地/merge 时**串行重排 down_revision 链**（如 `040→policies→issue_stances→town_treasury→offices`），确保 `alembic heads` 单头。**不要**硬编码 `041`——S1-3 也宣告过 `041_add_issue_stances`，撞号。 |
| `backend/app/models/__init__.py` | **S1-3（加 `issue_stance`）、S1-5（加 `town_treasury`）、S2-1（加 `office`）** 也改 | 都在模型注册表追加 import | 低冲突（追加式）；merge 手工合并 import 行。 |
| `backend/app/services/election_service.py` | **S1-1** 改（本模块**不改**） | 本模块只**读** `install_mayor`/`current_mayor`（`:127-181`）语义做 authority 对账，不写该文件 | 无写冲突；仅需 mayor 双存储语义对齐（gotcha 7）。 |

**串行/协调总结**：迁移号（与 S1-3/S1-5）与 `civic_service.py`（与 S1-1）是**两个真冲突点**，需 merge 时串行重排 + 对齐函数最终形状；`config.py`/`nightly_cron.py`/`models/__init__.py` 是追加式低冲突，按前缀/块隔离即可。

---

## 附：本文档引用的全部 file:line anchors（供校验）

- `backend/app/models/world_change_proposal.py:10-39`（含 `:30` risk_level、`:36` approved_at）
- `backend/app/routers/admin/world.py:51-58`（+ 兄弟端点 `:23-30`,`:33-48`,`:61-68`,`:71-78`）
- `backend/app/routers/admin/middleware.py:10-31`（require_admin，`:30` is_admin 检查）
- `backend/app/routers/admin/middleware.py:10-33`（共享 infra 视角）
- `backend/app/services/proposal_service.py:125-213`（approve_proposal）
- `backend/app/services/proposal_service.py:26-38`（OPEN_KINDS + assess_risk）
- `backend/app/services/civic_service.py:32-80`（propose）
- `backend/app/services/civic_service.py:254-315`（_close_one + _execute_outcome）
- `backend/app/services/election_service.py:127-172`（install_mayor）
- `backend/app/services/election_service.py:127-181`（install_mayor + current_mayor，共享 infra 视角）
- `backend/app/services/config_service.py:10-56`（ConfigService get/set/get_group）
- `backend/app/services/config_service.py:11-27`（ConfigService get/set，共享 infra 视角）
- `backend/app/models/system_config.py:7-15`（SystemConfig，`:12` value String(2000)）
- `backend/app/lab/transitions.py:61-74`（cas_proposal_status）
- `backend/app/tasks/nightly_cron.py:86-126`（治理夜间块，含 `:88-94`,`:99-105`,`:110-116`,`:120-126`,`:190-192`）
- `backend/app/tasks/nightly_cron.py:28-35`（run_nightly_jobs）
- `backend/app/tasks/nightly_cron.py:343-347`（nightly_cron_loop，`:17-18` RUN_HOUR/MINUTE）
- `backend/alembic/versions/033_add_world_governance.py:21-64`（033 upgrade）
- `backend/alembic/versions/040_residents_creator_nullable.py:14-17`（链头 revision/down_revision）
- `backend/alembic/versions/040_residents_creator_nullable.py:20-32`（upgrade/downgrade batch_alter_table）
- `backend/app/config.py:7-19`（Settings 类）
- `backend/app/config.py:375-378`（model_config + settings 单例）
- `backend/app/config.py:246-268`（realism_enabled + REALISM_* 块，`:249` 主开关）
- `backend/app/config.py:321-352`（realism P2 独立门 `:325`,`:326`,`:327`）
- `backend/app/config.py:354-373`（M1–M6 town flags，`:356`,`:366`,`:368`,`:371`,`:373`）
- `backend/app/main.py:85-93`（background_tasks 注册，`:85` run_background_tasks 门，`:21` import）
- `backend/app/routers/admin/economy.py:115-159`（逐端点 Depends(require_admin)，`:117`,`:128`,`:159`）
- `backend/app/routers/admin/__init__.py:18-31`（admin APIRouter + include_router）
- `backend/app/lab/apply.py:237-245`（broadcast_world_changed，call sites `:136`,`:209`）
- `backend/app/ws/manager.py:109-128`（manager.broadcast）
- `backend/app/services/world_revision_service.py:187-204`（world_changed_event）
- `backend/app/services/world_revision_service.py:72-84`（current_source_cursor，`:207` build_world_changed_envelope）
- `backend/app/events/bus.py:28-42`（on/emit 域事件总线）
- 方案：`SOCIETY_EXPANSION_PLAN.md:43`（S2-5 行）、`:82-88`（L0–L4）、`:90-99`（分级审批矩阵，`:93-97` 四级）、`:221`（§6 S2-5 政策行）、`:242`（通用要求）、`:320-328`（红线）

