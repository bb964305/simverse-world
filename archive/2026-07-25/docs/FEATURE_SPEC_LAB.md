# FEATURE SPEC — 实验楼（The Lab / Experiment Building）

> 状态：设计草案 v0.2（2026-07-16 按代码逐条核对后修订：金库入表、前端动态地点链路、队列可靠性、审批超时、运行时 kill switch、注入防护、SC-USD 定价挂钩等）· 作者：Jimmy + Claude
> 关联文档：`docs/FEATURE_SPECS.md`、`README.md` Roadmap v1.5/v1.6/v2.0
> 决策基线：**真实 Agent 沙箱（computer-use / 浏览器，OpenClaw / Hermes 级）** · **世界自改走「提案 → 审核 → 应用」** · 产出为设计文档 + 文件级实施计划

---

## 0. 一句话定位

在小镇里新增一栋特殊建筑「实验楼」。少数被授权的**研究员居民（Researcher）**可以进入其中，借助实验楼提供的**接口与隔离沙箱**真实地访问外界（联网浏览、执行代码、调用 API、computer-use），去**完成玩家发布的委托任务**并赚取代币；同时实验楼是小镇的「元游戏入口」——研究员在冒险中产出**世界变更提案**（补全地图、新增建筑、新增机制、生成支线），提案经审核后被真正写入游戏世界。

它把 Simverse 从「AI 居民在封闭世界里自主生活」升级为「AI 居民能对真实世界产生副作用，并能反过来改写自己所处的世界」。

---

## 1. 与现有框架的关系（为什么这样设计）

设计前对现有代码做了完整勘探，几条硬约束直接决定了架构取舍：

**(1) 现有 Agent 没有工具层 / 沙箱层。** `backend/app/agent/` 的执行模型是：LLM 从 14 个枚举 `ActionType`（`agent/actions.py`）里挑一个，后端做一次确定性状态变更。唯一的外部 I/O 是**计量过的 LLM 调用**（`app/llm/client.py`）、Redis、Postgres，以及只读的 tilemap 文件。没有 MCP、没有 tool-calling、没有代码执行。

> 结论：实验楼的「真实沙箱」是一个**全新子系统**，不能复用现有 action 管线去跑真实任务。真实任务执行必须**与 resident tick 解耦**，放到独立的 **Lab Runner 工作进程**里。tick 里的居民行为只负责「叙事层」（走进实验楼、宣布在做研究），真实工作由 Runner 异步执行并回流。

**(2) 地图是 Python 字典，不是数据。** 20 个地点写死在 `agent/map_data.py` 的 `LOCATIONS` dict 里；前端还在 `districtZonesData.ts` 和 `decor.ts` 三处重复。想让「新增建筑 / 补全地图」在**不发版**的前提下生效，必须引入一个 **DB 世界覆盖层（World Overlay）**：静态 `LOCATIONS` + 动态 `dynamic_location` 表在加载时合并。提案审核通过后写覆盖层，而不是改代码。

**(3) 经济已就绪但缺三块拼图。** `services/coin_service.py` 的 `charge / reward` 是所有代币流动的唯一入口，`transactions` 是流水账。但：没有原子的 P2P 转账、没有托管（escrow）、`charge` 存在竞态（先 SELECT 再减，无行锁）。居民本身**不是 User**（`Resident.creator_id → users.id`），没有余额概念。

> 结论：需要给 `coin_service` 补 `transfer / hold / settle / refund`（并顺手加行级锁修复竞态）；居民「赚到的钱」落到**研究员金库（treasury）**，这笔金库又成为研究员发起世界变更提案的燃料——形成闭环经济。

**(4) 委托脚手架已存在，但方向反了、且无生产者。** `models/commission.py` + `services/commission_service.py` 已实现「居民 → 玩家」的差事委托（乐观占单、48h 过期、完成检测、`commission_completed` 领域事件），但 `create_commission` 只在测试里被调用，没有任何生产者。玩家想要的是**反方向**：玩家 → 居民 的委托，且带真实执行与托管。

> 结论：不硬改 Commission（它承载居民→玩家的差事语义），而是**新建 `LabTask` 域**承载「玩家→研究员」的真实任务，复用 Commission 已验证过的模式（乐观状态机、过期 cron、领域事件、完成检测）。

**(5) 入建筑交互已有先例。** 「走进地点 → 弹 UI」由后端 `services/location_tracker.py` 检测（tile→location），推 `encounter_prompt` WS 帧，前端 `EncounterCard` 渲染。实验楼的入口交互沿用这条链路即可，只是把「卡片」升级为「面板」。

现有可直接复用的扩展点：`coin_service`（钱）、`shop_effects` 的 `@register` 注册器（道具效果）、`Commission` 的状态机模式、`LocationVisit` + `location_tracker`（入场检测）、`GoalInvestment`（玩家→居民出资先例）、YAML 插件 registry（可选的新 phase）。

---

## 2. 核心概念模型（词汇表）

| 概念 | 说明 | 落地形态 |
|------|------|----------|
| **实验楼 Lab** | 小镇里的一栋公共建筑，元游戏入口 | `map_data.LOCATIONS["experiment_building"]` |
| **研究员 Researcher** | 被授权进入实验楼、可接真实任务的居民子集 | `Resident.meta_json["lab"]`（access / tier / skills；treasury **不在此**，见下） |
| **委托 LabTask** | 玩家发布、悬赏代币、指定或公开招募研究员执行的真实任务 | 新表 `lab_tasks` |
| **运行 LabRun** | 一次 LabTask 的实际沙箱执行会话（可重试→多 run） | 新表 `lab_runs` |
| **步骤 / 产物 RunStep / Artifact** | run 内的每一步动作、观察、产出物（文件/链接/文本） | 新表 `lab_run_steps` / `lab_artifacts` |
| **托管 Escrow** | 发布委托时冻结的悬赏 + 平台费，结算时分账/退款 | `coin_service` 扩展 + `coin_holds` 表 |
| **沙箱适配器 SandboxAdapter** | 封装真实 agent 运行时（OpenClaw / Hermes / computer-use）的统一接口 | `backend/app/lab/sandbox/` |
| **能力域 Capability Scope** | 一次 run 被授予的能力白名单（web_search / browse / code / http…） | `lab_tasks.scopes_json` + 运行时护栏 |
| **世界变更提案 WorldChangeProposal** | 研究员在冒险中产出的、对真实游戏世界的结构化改动 | 新表 `world_change_proposals` |
| **世界覆盖层 World Overlay** | 让提案在不发版下生效的动态数据层 | 新表 `dynamic_locations` / `dynamic_mechanics` |
| **研究员金库 Treasury** | 居民赚到的、可用于发起世界变更冒险的资金池 | 新表 `resident_treasuries`（原子 UPDATE + 每笔记 `transactions` 流水，见 §4.7） |

---

## 3. 分层架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│  A. 建筑与交互层（前端 Phaser + React 面板）                            │
│     进入实验楼 → 面板：发布委托 / 观看运行 / 领取产物 / 查看提案墙        │
└───────────────┬──────────────────────────────────────────────────────┘
                │ REST + WS
┌───────────────▼──────────────────────────────────────────────────────┐
│  B. 委托与托管经济层（FastAPI routers + services）                      │
│     LabTask 状态机 · Escrow 冻结/分账/退款 · 研究员金库 · 分账规则        │
└───────────────┬──────────────────────────────────────────────────────┘
                │ 任务队列（Redis）
┌───────────────▼──────────────────────────────────────────────────────┐
│  C. Agent 执行层 —— 真实沙箱（独立 Lab Runner 进程）                     │
│     SandboxAdapter(OpenClaw/Hermes/computer-use) · 能力护栏 · 预算/超时  │
│     · 步骤流式回传 · 产物落库 · 人审断点（sensitive action gate）        │
└───────────────┬──────────────────────────────────────────────────────┘
                │ 产出「世界变更提案」
┌───────────────▼──────────────────────────────────────────────────────┐
│  D. 世界自改治理层（提案 → 审核 → 应用）                                 │
│     WorldChangeProposal 队列 · Admin 审核台 · Apply 引擎（写覆盖层）      │
│     · 回滚 · 审计日志                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

关键解耦点：**B 层只管钱和状态机**，把「要执行一个真实任务」丢进 Redis 队列；**C 层（独立进程）**消费队列、在隔离环境里跑真实 agent、把步骤和产物流回来；任务成功后 C 层可选地产出 **D 层**的世界变更提案。resident tick（现有 AgentLoop）完全不阻塞在真实任务上，只做叙事同步（「XX 正在实验楼做研究」）。

---

## 4. 数据模型（新增表）

命名沿用现有约定：`String` 主键 `uuid4`、`*_json` 存 JSON、`created_at` timezone-aware、状态字段用短 `String` 枚举。每个新 model = `backend/app/models/<x>.py` + 登记到 `models/__init__.py` + `alembic/env.py` + 一个迁移文件。

### 4.1 `lab_tasks` — 玩家发布的委托
```python
class LabTask(Base):
    __tablename__ = "lab_tasks"
    id: str            # uuid4, PK
    issuer_user_id: str        # FK users.id, index —— 发布者（玩家）
    researcher_slug: str | None  # 指定研究员；None = 公开招募
    title: str                 # 简短标题
    brief_md: Text             # 任务说明（玩家自然语言）
    scopes_json: JSON          # 授予的能力域白名单，如 ["web_search","browse","code"]
    reward_sc: int             # 悬赏代币（进托管）
    platform_fee_sc: int       # 平台费（进托管，结算入 sink）
    deliverable_kind: str      # "report" | "file" | "link" | "dataset" | "world_change"
    status: str                # 状态机见下
    hold_id: str | None        # 关联 coin_holds.id
    accepted_run_id: str | None
    result_summary_md: Text | None
    deadline_at: datetime
    created_at / updated_at / completed_at
```
状态机（沿用 Commission 乐观占单思路）：
`draft → funded（托管已冻结）→ assigned（研究员接单/被指派）→ running → review（玩家验收）→ completed | rejected | failed | expired | cancelled`

> 验收模式二选一（管理台可配），**默认 manual**：玩家点「满意」放款，**72h 超时自动放款**（防跑路）。防白嫖：**产物在放款后才解锁领取**；`reject-result` 每任务限 1 次，拒收后进 admin 仲裁（仲裁放款或退款），不允许无限拒收重试。
>
> 公开招募（`researcher_slug=None`）的接单主体：研究员是 NPC，接单由**后端规则自动分派**（tier/skills 匹配 + 忙闲 + 轮转），不依赖 tick 内 LLM 决策——否则公开池无人消费。

### 4.2 `lab_runs` — 一次真实执行会话
```python
class LabRun(Base):
    __tablename__ = "lab_runs"
    id: str
    task_id: str               # FK lab_tasks.id, index
    researcher_slug: str
    adapter: str               # "openclaw" | "hermes" | "computer_use" | "mock"
    status: str                # queued → running → succeeded | failed | cancelled | needs_approval
    scopes_json: JSON          # 实际生效的能力域（≤ task.scopes）
    budget_usd_cents: int      # 本 run LLM/算力预算上限（整数分；不用 float 存钱）
    cost_usd_cents: int        # 实际花费（回填）
    heartbeat_at: datetime | None  # Runner 心跳，孤儿 run 检测用
    started_at / ended_at
    error: Text | None
    approvals_json: JSON | None  # 断点人审：待批准敏感动作**列表**（一次 run 可能多个）
```

### 4.3 `lab_run_steps` / `lab_artifacts` — 步骤流 & 产物
```python
class LabRunStep(Base):        # 每一步：思考/动作/观察，用于前端「直播」+ 审计
    id / run_id(index) / seq:int
    phase: str                 # "think" | "tool_call" | "observation" | "message"
    tool: str | None           # 具体工具名，如 "browser.navigate"
    summary: str               # 人类可读摘要（脱敏后）
    payload_json: JSON         # 结构化细节（脱敏后）
    created_at

class LabArtifact(Base):       # 产物
    id / run_id(index) / task_id
    kind: str                  # "file" | "link" | "text" | "image" | "dataset"
    title: str
    uri: str | None            # 存储路径或外链
    text_md: Text | None
    meta_json: JSON
    created_at
```

### 4.4 `coin_holds` — 托管账
```python
class CoinHold(Base):
    __tablename__ = "coin_holds"
    id / user_id(index)        # 被冻结方
    amount: int                # 冻结总额（reward + fee）
    reason: str                # "lab_task:<id>"
    status: str                # held → settled | refunded
    created_at / settled_at
```
> 冻结时：`charge(user, amount, "lab_task_hold:<id>")` 已把钱扣走并进流水（钱其实离开了玩家余额）；`coin_holds` 记录「这笔钱名义上属于某任务、尚未分配」。结算 = 把 hold 拆成若干 `reward(...)` 到各收款方；退款 = `reward(issuer, amount, "lab_refund:<id>")`。这样复用现有流水账语义，避免引入独立余额系统。**不变量：settle 的 splits 总和必须恰等于 hold.amount**（platform fee 入 sink 也要记一条 transactions），service 层断言 + 回归测试保证，否则会凭空造币/销币。

### 4.5 `world_change_proposals` — 世界变更提案（治理核心）
```python
class WorldChangeProposal(Base):
    __tablename__ = "world_change_proposals"
    id: str
    origin: str                # "lab_run" | "resident" | "admin"
    origin_ref: str | None     # run_id / resident_slug
    author_slug: str | None    # 提出的研究员
    kind: str                  # 见下「提案类型」
    title: str
    rationale_md: Text         # 冒险叙事 + 为什么要改
    patch_json: JSON           # 结构化改动（Apply 引擎消费）
    cost_sc: int               # 从研究员金库扣的燃料（提案时冻结）
    status: str                # pending → approved → applied | rejected | reverted | failed
    risk_level: str            # "low" | "medium" | "high"
    reviewer_id: str | None    # admin user
    review_note: Text | None
    applied_at / reverted_at
    created_at
```

### 4.6 世界覆盖层 `dynamic_locations` / `dynamic_mechanics`
```python
class DynamicLocation(Base):   # 提案「新增建筑/补全地图」应用后的落点
    id / slug(unique)
    data_json: JSON            # 与 LOCATIONS 条目同构：name/type/bounds/center/entrance/...
    active: bool
    proposal_id: str | None
    created_at

class DynamicMechanic(Base):   # 提案「新增机制/支线」应用后的落点
    id / code(unique)
    kind: str                  # "quest_template" | "event" | "boosted_rule" | "lore" | ...
    spec_json: JSON
    active: bool
    proposal_id: str | None
    created_at
```
> `agent/map_data.py` 增加一个 `load_dynamic_locations()`，在进程启动 / 收到失效信号时把 active 的 `dynamic_locations` 合并进内存 `LOCATIONS`。这样「审核通过 → 应用」即可让新建筑在下一个加载周期出现在寻路 / 规划 / codex 里，**无需发版**（视觉 tilemap 仍需美术资源，见 §9 取舍）。

**提案类型（`kind`）与 `patch_json` 契约：**

- `add_location`：补全地图 / 加建筑 → 写 `dynamic_locations`（含 bounds/center/entrance；风险点：碰撞与出生点，见护栏）。
- `add_mechanic`：新增支线任务模板 / 世界事件 / 地点增益规则 → 写 `dynamic_mechanics`。
- `add_lore`：给地点加传说 / 隐藏点 → 合并进 `location_lore`。
- `edit_location`：改名 / 改描述 / 改 boosted_actions（不改 bounds，低风险）。
- `add_npc` / `edit_npc`：引入或微调居民（高风险，复用 Forge 管线产出角色）。

### 4.7 `resident_treasuries` — 研究员金库（v0.2 修订：不放 meta_json）
```python
class ResidentTreasury(Base):
    __tablename__ = "resident_treasuries"
    resident_slug: str         # PK
    balance_sc: int            # 扣减一律 UPDATE ... WHERE balance_sc >= amount（原子）
    updated_at
```
> 理由：meta_json 的 read-modify-write 有 TOCTOU 竞态（与 `charge` 同病），且绕开 `transactions` 流水导致不可审计、economy 统计失真。金库每笔收支都写 `transactions`（合成账户 `treasury:<slug>`），保持全局代币守恒可查。

---

## 5. Agent 执行层 —— 真实沙箱（C 层详设）

### 5.1 为什么是独立进程
现有后端进程（API / agent-worker）都不适合承载真实 computer-use / 浏览器 agent：它会长时间阻塞、需要强隔离、可能崩溃。新增 **Lab Runner**：`python -m app.lab.runner`，形态对齐现有 `app/agent/main.py` 的独立 worker 模式。它消费 Redis 任务队列 `sv:lab:queue`，每个任务在一个**一次性隔离环境**里执行。

**队列必须 at-least-once**：用 `BRPOPLPUSH` 进 processing list（或 Redis Stream + consumer group）+ 显式 ack——裸 LPUSH/BRPOP 在 Runner 崩溃时会丢任务。Runner 定期回写 `lab_runs.heartbeat_at`；watchdog（挂 `nightly_cron` 或独立循环）清扫心跳超时的孤儿 run → 置 `failed` → 走托管退款，防止 run 永久卡 `running`、托管款无人退。

### 5.2 SandboxAdapter 抽象（可插拔真实运行时）
```python
# backend/app/lab/sandbox/base.py
class SandboxAdapter(Protocol):
    name: str
    async def start(self, run: RunSpec) -> SandboxHandle: ...
    async def step_stream(self, handle) -> AsyncIterator[StepEvent]: ...   # 流式回传步骤
    async def submit_goal(self, handle, brief: str, scopes: list[str]) -> None: ...
    async def approve(self, handle, approval_id: str, decision: bool) -> None: ...
    async def collect_artifacts(self, handle) -> list[ArtifactSpec]: ...
    async def stop(self, handle) -> None: ...
```
适配器实现：
- `OpenClawAdapter` / `HermesAdapter`：包装对应 agent 运行时（HTTP / 子进程 / 容器 exec）。
- `ComputerUseAdapter`：直连 computer-use 端点。
- `MockAdapter`：本地无外部依赖的假执行（CI / 开发 / 演示用，产出脚本化步骤与假产物）——**第一版默认**，保证功能闭环可测，再切真适配器。

`RunSpec` 携带：`brief`、`scopes`、`budget_usd`、`deadline`、`egress_allowlist`、`secrets=∅`。

### 5.3 隔离与护栏（真实外部访问的前提）
- **进程/容器隔离**：每 run 一个短生命周期容器（无状态、跑完销毁），非 root，只读根文件系统 + 可写临时盘（有配额）。
- **网络出口白名单**：默认拒绝，仅放行 `egress_allowlist`（如 `*.wikipedia.org`、搜索 API 域）。禁止访问内网 / 元数据地址（169.254.169.254 等 SSRF 目标）。
- **能力域 scope 强制**：`scopes` 是白名单；适配器只暴露被授权工具。玩家发任务时选择 scope，值越大费用越高（见经济）。
- **预算 & 超时**：`budget_usd` + `deadline` 双闸；LLM 调用继续走 `Meter` 计量（复用 E-18），超预算即熔断。
- **敏感动作人审断点**：涉及「登录账户 / 提交表单 / 发布内容 / 花真实钱」等动作，适配器暂停并置 run 为 `needs_approval`，把动作摘要写 `approvals_json` 并 WS 通知玩家/管理员；**默认拒绝一切金融交易**（与全局财务红线一致，绝不代替用户下单/转账/汇款）。**审批超时**（`lab_approval_timeout_s`，默认 30min）：超时默认拒绝该动作，run 降级完成或失败退款——不让一次性容器无限期挂起烧钱（人审可能等数小时，与容器生命周期天然矛盾，必须有超时兜底）。
- **脱敏**：写入 `lab_run_steps` 前对 payload 做 secret/PII 脱敏。
- **间接提示注入防护**（browse+code agent 的头号现实风险）：一切网页/外部内容视为不可信输入；产物 markdown 前端渲染必须 sanitize；产物外链默认不可直接点击（展示完整 URL + 警示）；提案只接受结构化 `patch_json` + 人审——防止被网页内容操纵产出恶意变更或钓鱼产物。
- **全局 kill switch**：**运行时标志**（Redis key `sv:lab:enabled`，管理台即时切换，无需重启——`settings.lab_enabled` 是启动时加载的，仅作部署级总开关）；单 run 可 cancel。
- **速率与配额**：每玩家每日委托数、每研究员并发 run 数、全局并发上限（对齐 `agent_max_concurrent` 思路）。
- **审计**：每一步 + 每次分账 + 每次提案应用都留痕（steps 表 + transactions + 提案表 + `agent.events` 结构化日志）。

### 5.4 与叙事层（现有 tick）的衔接
- 新增 `ActionType.RESEARCH`（第 15 个 action），**仅当**居民是研究员且身处实验楼 `bounds` 内时可用（在 `get_available_actions` 加门控）。它不跑真实任务，只把居民 `status` 设为 `researching`、发状态广播、写一条记忆——纯叙事，零外部 I/O，保持 tick 轻量。
- 真实执行由 Runner 独立驱动；run 开始/结束时向对应研究员写记忆（复用 `MemoryService`），让「做过的研究」进入居民的三层记忆，人格演化也能吃到（一次成功的黑客松式研究 → 关键事件跳变）。

---

## 6. 经济与托管（B 层详设）

### 6.1 `coin_service` 扩展
```python
# backend/app/services/coin_service.py（新增）
async def transfer(db, from_user, to_user, amount, reason) -> bool     # 原子：一扣一加
async def hold(db, user_id, amount, reason) -> str                     # 冻结→返回 hold_id
async def settle(db, hold_id, splits: list[tuple[user_id, amount, reason]]) -> None  # 断言 sum(splits) == hold.amount
async def refund(db, hold_id, reason) -> None
# 同时修复 charge 竞态：改为 UPDATE ... WHERE balance >= amount（行级原子）
```

### 6.2 分账规则（可在 admin economy config 配）
发布委托冻结 `reward_sc + platform_fee_sc`。成功结算：
- 研究员**创作者**分成（沿用 gift 20% / tip 80% 的创作者分成先例，默认可配 `lab_creator_share`）。
- 研究员**金库 treasury** 得一份（`resident_treasuries` 原子入账 + transactions 流水，§4.7）——这是驱动世界冒险的燃料。
- **平台 sink**：`platform_fee_sc` 不回流（通缩汇），进 admin economy 的 consumed 统计。

失败 / 超时 / 拒收：`refund` 全额退玩家（或按管理台策略扣少量取消费）。

> **SC 定价必须与真实成本挂钩**：玩家付游戏币，平台烧真实 USD（LLM + 容器）。scope 费率按「预期 `budget_usd` × 换算系数 `lab_sc_per_usd`」定价并随预算上限联动；每玩家/全局配额只是兜底，不能替代定价约束——否则委托量越大平台越亏。

### 6.3 闭环
玩家出钱 → 研究员完成真实任务赚钱 → 研究员金库积累 → 研究员用金库**发起世界变更提案**（提案 `cost_sc` 从金库冻结，应用成功才真正消耗，被拒则退回金库）。于是「玩家需求」驱动「AI 打真实工」，「AI 攒下的钱」驱动「世界演化」，经济与玩法自洽。

> 注意：admin economy config 里若干键当前**定义了但没被运行时读取**（signup/daily/chat 等）。本特性新增的 `lab_*` 配置键要确保在 service 里**真正读取**，不要重蹈覆辙。

---

## 7. 世界自改治理（D 层详设，propose → review → apply）

1. **产出**：一次成功的 LabRun 可（由研究员在 brief 语义或专门的「探索型任务」中）产出 `WorldChangeProposal`，`patch_json` 结构化、`rationale_md` 讲述冒险故事，`risk_level` 由规则+LLM 评估。
2. **审核台**：Admin 新增 `/admin/world/proposals` 面板。注意：现有 `EventsPanel` 实为「创建表单 + 列表 + 删除」，**没有**批准/驳回交互——只能参照其列表布局与 AdminTab 接入方式，审批交互需新设计。展示 diff 预览（这个提案会新增哪个建筑/机制、影响哪些 tile）。
3. **应用引擎** `backend/app/lab/apply.py`：按 `kind` 分派，把 `patch_json` 写入覆盖层（`dynamic_locations` / `dynamic_mechanics` / `location_lore`）。写前做**结构校验 + 冲突检测**（bounds 是否与既有地点/碰撞层重叠、slug 是否冲突、出生点是否可达）。应用后触发 `map_data` 重载信号（Redis pub/sub；API / agent-worker / lab-runner **各进程都要订阅**，并重建 `location_tracker` 的 tile→location 索引）。**前端能刷新的前提是数据可拉**：`districtZonesData.ts` / `LocationKey` 是编译期静态的，`world_changed` 帧到了也没有东西可刷——动态地点必须经新的 `GET /world/locations` 运行时接口下发，minimap/codex 改为「静态数据 + 服务端动态数据」合并渲染（P3 顺势把地点数据源统一到后端，根治三处重复）。
4. **回滚**：`active=false` 即软下线，`status=reverted`，保留审计。
5. **权限**：仅 `require_admin`（复用 `routers/admin/middleware.py`）可批准/应用/回滚。**默认不开启白名单自动应用**（本次决策为「提案→审核→应用」），Apply 永远经人手。

---

## 8. API 设计

玩家侧（`backend/app/routers/lab.py`，`prefix="/lab"`，Bearer 鉴权用 commissions 同款 `_require_user`）：

| 方法 & 路径 | 作用 |
|---|---|
| `GET /lab/researchers` | 列出可接单研究员（tier / skills / 忙闲 / 评分） |
| `POST /lab/tasks` | 发布委托（校验余额→`hold` 冻结→入队 or 待接单） |
| `GET /lab/tasks?scope=mine\|open` | 我的委托 / 公开招募池 |
| `GET /lab/tasks/{id}` | 委托详情（含最新 run 摘要） |
| `POST /lab/tasks/{id}/cancel` | 取消（未开始全退，运行中按策略） |
| `POST /lab/tasks/{id}/accept-result` | 手动验收放款 |
| `POST /lab/tasks/{id}/reject-result` | 拒收（触发重试或退款） |
| `GET /lab/runs/{id}` | run 状态 |
| `GET /lab/runs/{id}/steps` | 步骤流（前端「直播」轮询兜底，主推 WS） |
| `POST /lab/runs/{id}/approval` | 玩家/管理员回应敏感动作断点 |
| `GET /lab/artifacts/{id}` | 领取产物（放款后解锁） |
| `GET /world/locations` | 静态+动态地点合并快照（minimap/codex 动态数据源，P3） |

管理侧（`backend/app/routers/admin/world.py`，`prefix="/world"`，`Depends(require_admin)`）：

| 方法 & 路径 | 作用 |
|---|---|
| `GET /admin/world/proposals?status=` | 提案队列 |
| `GET /admin/world/proposals/{id}` | 提案详情 + diff 预览 |
| `POST /admin/world/proposals/{id}/approve` | 批准并应用（走 apply 引擎） |
| `POST /admin/world/proposals/{id}/reject` | 驳回（退研究员金库燃料） |
| `POST /admin/world/proposals/{id}/revert` | 回滚已应用提案 |
| `GET /admin/lab/runs` / `POST /admin/lab/runs/{id}/cancel` | run 监控 / 熔断 |
| `PUT /admin/economy/config`（扩展） | 新增 `lab_*` 分账/费率键 |

---

## 9. 前端

**新建筑与入场**：`map_data.LOCATIONS` 加 `experiment_building`（自动获得寻路/规划/placement/codex）；`districtZonesData.ts` 加 minimap 条目并扩 `LocationKey`；tilemap 画楼（美术资源）。入场沿用 `location_tracker.process_one` 分支推新 WS 帧 `experiment_prompt`，前端 `ws.ts` 捕获后 `bridge.emit('experiment:open')`。

**实验楼面板** `frontend/src/components/ExperimentPanel.tsx`（对齐 `BulletinBoard.tsx` 的 bridge-open 自挂载模式；注意 BulletinBoard 实际挂在 `TopNav.tsx:434`，**不在** GamePage——ExperimentPanel 同样挂 `TopNav.tsx`）。三个 tab：
- **发布委托**：研究员 `<select>` + scope 多选 + 悬赏输入 + brief（表单形态对齐 `ShopModal` 的 target-picker），提交后 `updateBalance(getMe())` 刷新余额。
- **运行直播**：订阅 `lab_run_step` WS 帧，逐条渲染研究员的思考/动作/观察，敏感动作弹「批准/拒绝」。
- **产物 & 提案墙**：领取产物（markdown 渲染 sanitize；外链展示完整 URL + 警示、不自动跳转——防注入产物钓鱼）；展示该研究员产出、已通过的世界变更（叙事化「小镇因你而改变」）。

**Admin 提案审核** `frontend/src/components/admin/ProposalsPanel.tsx`（对齐 `EventsPanel.tsx`）；`AdminSidebar.tsx` 加 `AdminTab`，`AdminPage.tsx` 加 `case`。

**API/WS client**：`services/api/lab.ts` 新增（`apiFetch` 封装，类型化）；admin 调用放 `services/api/adminWorld.ts`（token 走 header，对齐 `getAdminEvents`）；`ws.ts` 增 `lab_run_step` / `lab_task_update` / `world_changed` / `experiment_prompt` 分支；`phaserBridge.ts` 头注释登记 `experiment:open/close`。

**三处地点数据要同步改**（backend `map_data.py`、`districtZonesData.ts`、`decor.ts` 的 HOUSING_BOUNDS 无共享源）——已知坑，实施时逐一处理。

---

## 10. WebSocket 事件

新增 server→client 帧（`manager.broadcast` / `manager.send`，flat dict + `"type"`）：
- `experiment_prompt`：走进实验楼（send 给该玩家）。
- `lab_task_update`：委托状态流转（funded/assigned/running/review/completed…）。
- `lab_run_step`：run 步骤直播（send 给发布者 + 观战者；高频步骤合批节流，如 ≥1s 聚合一帧，DB 写入同样可合批）。
- `lab_run_approval`：敏感动作待批。
- `world_changed`：提案应用/回滚后，通知全体刷新 minimap/codex。

领域事件（`events/bus.py` 的 `@on/emit`）：`lab_task_completed`、`world_proposal_applied`（用于给研究员写记忆、发成就、通知创作者分成）。

---

## 11. 安全与治理清单（真实外部访问的红线）

- 出口白名单 + 拒绝内网/元数据地址（反 SSRF）；每 run 一次性隔离容器，非 root，配额受限。
- scope 能力白名单强制；预算 + 超时双闸熔断；LLM 计量复用 Meter。
- 敏感动作（登录/提交/发布/支付）→ 人审断点；**一切金融交易默认拒绝**，绝不代替用户下单/转账/汇款。
- 内容审核：brief 入口与产物出口都过审核（拒绝违法/恶意/攻击性任务，如写恶意代码、攻击站点——与产品红线一致）。
- 脱敏后再落库/直播；不注入任何宿主机密钥到沙箱。
- 全局 kill switch（运行时 Redis 标志 `sv:lab:enabled`，管理台即时切换）+ 单 run cancel + 管理台 run 监控。
- 间接提示注入：外部内容一律不可信；产物渲染 sanitize、外链警示；提案仅结构化 patch + 人审。
- 队列 at-least-once（processing list + ack）+ Runner 心跳 + 孤儿 run 清扫退款。
- 分账守恒：sum(splits) == hold.amount，金库收支全部进 transactions 流水。
- 世界写入永远经「提案→人审→应用」，Apply 前结构校验+冲突检测，支持回滚，全程审计。
- 速率/配额：每玩家日委托数、每研究员并发 run、全局并发上限。
- 竞态修复：`coin_service.charge/hold` 改行级原子，防超卖。

---

## 12. 分阶段落地计划

按「先闭环、后接真」的顺序，每阶段可独立验收、可上线。

**P0 — 建筑与骨架（1 阶段，无外部风险）**
把实验楼作为一栋可进入的建筑落地，打通入场→面板→占位内容。不含真实执行。验收：走进实验楼弹出面板、minimap 显示、codex 收录、后端测试绿。

**P1 — 委托与托管经济闭环（用 MockAdapter）**
LabTask 全状态机 + Escrow 冻结/分账/退款 + 研究员金库 + 运行直播（假步骤）。真实执行用 `MockAdapter` 产出脚本化步骤与假产物。验收：发布委托→扣款冻结→run 跑完→产物→验收放款→创作者分成+金库到账；取消/超时退款；`coin_service` 竞态修复有回归测试；settle 分账守恒（sum==hold）与金库表原子入账有回归测试；队列 ack + 孤儿 run 清扫可验证（kill 掉 runner 不丢单、不吞托管款）。

**P2 — 真实沙箱接入（OpenClaw / Hermes / computer-use）**
实现真实 `SandboxAdapter` + 隔离容器 + 出口白名单 + scope 强制 + 预算/超时 + 敏感动作人审断点 + 脱敏 + kill switch。灰度：先只放行 `web_search` + 只读 `browse` 两个低风险 scope，跑通再逐步放开 `code` / `http`。验收：真实联网研究任务端到端成功、护栏各项可触发、审计留痕完整。

**P3 — 世界自改治理（提案→审核→应用）**
WorldChangeProposal + 覆盖层（`dynamic_locations` / `dynamic_mechanics`）+ Apply 引擎（校验/冲突检测/重载/回滚）+ Admin 审核台。先只开 `add_lore` / `edit_location`（低风险）；`add_location` 待 `GET /world/locations` 前端动态数据链路就绪后再开（否则出现「后端可寻路、minimap 上不存在」的幽灵建筑）；`add_npc` 延后。验收：一次探索型任务产出提案→管理员批准→新地点在下个加载周期出现在寻路/codex→可回滚。

**P4 — 打磨与生态**
研究员评分/排行、提案叙事墙、成就与季度联动、i18n、admin 经济配置真正读取 `lab_*` 键、真实 tilemap 美术管线（把 `add_location` 从「逻辑可达但无贴图」升级为有贴图）。

---

## 13. 文件级实施清单

> 约定回顾：新表 = `models/<x>.py` + `models/__init__.py` 加一行 `import` + `alembic/env.py` 加 class import + 一个 `alembic/versions/NNN_*.py` 迁移（`down_revision` 指向当前 head，当前为 `031_add_home_decor`）。新 router = `routers/` 建模块 + `main.py` 的 import 行与 `include_router`。新后台 loop 要**同时**加到 `main.py` lifespan 和 `agent/main.py`（或塞进已注册的 `nightly_cron`）。地点数据改**三处**。

### P0 建筑与骨架

后端
- `backend/app/agent/map_data.py` — `LOCATIONS` 增 `experiment_building` 条目（type=public, role, bounds/center/entrance, boosted_actions=["RESEARCH"]）；新增 `load_dynamic_locations()` 合并钩子（先留空实现，占位）。
- `backend/app/agent/actions.py` — `ActionType` 增 `RESEARCH`；在 `get_available_actions` 加门控（研究员且在实验楼 bounds 内）。
- `backend/app/agent/phases/execute/basic.py` — 处理 `RESEARCH`（设 `status="researching"`，纯叙事）。
- `backend/app/agent/phases/memorize/basic.py` — `format_action_memory` 加 `RESEARCH` 文案。
- `backend/app/agent/prompts.py` / `phases/plan/basic.py` — 决策/规划 prompt 纳入 `RESEARCH`（`boosted_actions` 已自动透传）。
- `backend/app/services/location_tracker.py` — `process_one` 加分支：进入 `experiment_building` 时 `manager.send(user_id, {"type":"experiment_prompt", ...})`。
- `backend/app/models/resident.py` — 约定 `meta_json["lab"]` 结构（无需 schema 改动，文档化：`{access, tier, skills, treasury}`）。
- `backend/tests/test_lab_building.py` — 入场帧、action 门控、可达性回归。

前端
- `frontend/src/components/minimap/districtZonesData.ts` — 加实验楼条目，扩 `LocationKey`。
- `frontend/src/services/ws.ts` — 加 `experiment_prompt` 分支 → `bridge.emit('experiment:open')`。
- `frontend/src/game/phaserBridge.ts` — 头注释登记 `experiment:open` / `experiment:close`。
- `frontend/src/components/ExperimentPanel.tsx` — 新面板骨架（bridge-open 自挂载，对齐 `BulletinBoard.tsx`）。
- `frontend/src/components/TopNav.tsx` — 挂载 `<ExperimentPanel/>`（与 BulletinBoard 同位，`TopNav.tsx:434` 附近）。
- `frontend/public/assets/village/tilemap/tilemap.json` — 画建筑 tile 与碰撞（美术）。

### P1 委托 + 托管（MockAdapter）

后端
- `backend/app/models/lab_task.py`、`lab_run.py`（含 `LabRunStep`）、`lab_artifact.py`、`coin_hold.py`、`resident_treasury.py` — 新 models（§4）。
- `backend/app/models/__init__.py` + `backend/alembic/env.py` — 登记以上 model。
- `backend/alembic/versions/032_add_lab_core.py` — 建表迁移（`down_revision="031_add_home_decor"`）。
- `backend/app/services/coin_service.py` — 加 `transfer / hold / settle / refund`；`charge` 改 `UPDATE ... WHERE balance>=amount` 原子。
- `backend/app/services/lab_task_service.py` — LabTask 状态机（create→fund→assign→run→review→complete/refund）、验收模式、乐观占单（对齐 `commission_service`）。
- `backend/app/lab/__init__.py`、`backend/app/lab/queue.py` — Redis 队列 `sv:lab:queue` 生产/消费封装（BRPOPLPUSH + processing list + ack；配合心跳与 nightly_cron 孤儿清扫）。
- `backend/app/lab/sandbox/base.py`、`backend/app/lab/sandbox/mock.py` — `SandboxAdapter` 协议 + Mock 实现。
- `backend/app/lab/runner.py` + `backend/app/lab/main.py` — Lab Runner 进程（`python -m app.lab.main`），消费队列、跑 adapter、流式写 `lab_run_steps` + 广播 `lab_run_step`、落 `lab_artifacts`、结算触发。
- `backend/app/routers/lab.py` — 玩家侧 API（§8），`main.py` 注册。
- `backend/app/main.py` — import 并 `include_router(lab_router.router)`；side-effect import lab 领域事件 handler。
- `backend/app/tasks/nightly_cron.py` — 加 `expire_lab_tasks` / escrow 超时清扫块（复用已注册的 nightly loop，避免改两处 worker 列表）。
- `backend/app/config.py` — 加 `lab_enabled: bool`、`lab_creator_share: float`、`lab_platform_fee_rate`、`lab_max_concurrent_runs`、`lab_daily_tasks_per_user`、`lab_default_budget_usd`、`lab_adapter: str = "mock"`、`lab_sc_per_usd`、`lab_approval_timeout_s`、`lab_run_heartbeat_ttl_s`（kill switch 用 Redis 运行时标志，config 键仅部署级开关）。
- `backend/app/events/bus` 使用点 — service 内 `emit("lab_task_completed", ...)`；handler 写记忆/分成/通知。
- `backend/tests/test_lab_economy.py`、`test_lab_task_flow.py` — 冻结/分账/退款、状态机、竞态回归。

前端
- `frontend/src/services/api/lab.ts` — 类型化 API（listResearchers/createTask/getTasks/getRun/steps/approval/artifact）。
- `frontend/src/components/ExperimentPanel.tsx` — 三 tab 实装（发布委托表单 / 运行直播 / 产物墙）。
- `frontend/src/services/ws.ts` — 加 `lab_task_update` / `lab_run_step` / `lab_run_approval` 分支。
- `frontend/src/stores/gameStore.ts` — 视需要加轻量 `labTasks` / `activeRun` slice（或面板内 local state，对齐 ShopModal）。

### P2 真实沙箱

后端
- `backend/app/lab/sandbox/openclaw.py`、`hermes.py`、`computer_use.py` — 真实适配器。
- `backend/app/lab/sandbox/isolation.py` — 一次性容器/网络白名单/文件配额封装。
- `backend/app/lab/guard.py` — scope 强制、预算/超时闸、敏感动作分类、脱敏。
- `backend/app/routers/lab.py` — `POST /lab/runs/{id}/approval` 断点回应；`backend/app/lab/runner.py` — `needs_approval` 暂停/恢复。
- `backend/app/config.py` — 加 `lab_egress_allowlist`、`lab_sandbox_image`、`lab_openclaw_base_url` / `lab_hermes_base_url` / key（空串=未配置，对齐 portrait/tts 分组约定）。
- `backend/app/routers/admin/*` — run 监控 + kill switch 端点（新 `admin/lab.py` 子路由，`admin/__init__.py` 登记）。
- `backend/tests/test_lab_sandbox_guard.py` — 白名单拦截、预算熔断、敏感动作断点、脱敏。

前端
- `frontend/src/components/ExperimentPanel.tsx` — 敏感动作「批准/拒绝」交互。
- `frontend/src/components/admin/LabRunsPanel.tsx` + `AdminSidebar.tsx` + `AdminPage.tsx` — run 监控台 + kill switch。
- `frontend/src/services/api/adminWorld.ts` — admin lab 调用（token 走 header）。

### P3 世界自改治理

后端
- `backend/app/models/world_change_proposal.py`、`dynamic_location.py`、`dynamic_mechanic.py` — 新 models（§4）。
- `backend/app/models/__init__.py` + `alembic/env.py` + `alembic/versions/033_add_world_governance.py`。
- `backend/app/agent/map_data.py` — `load_dynamic_locations()` 实装（启动加载 + Redis 失效信号重载）。
- `backend/app/lab/apply.py` — Apply 引擎（按 kind 分派、结构校验、bounds/碰撞/出生点冲突检测、写覆盖层、发 `world_changed`、回滚）。
- `backend/app/services/proposal_service.py` — 提案 CRUD + 状态机 + 金库燃料冻结/退回；risk 评估（规则 + LLM）。
- `backend/app/lab/runner.py` — 成功 run 可产出 proposal（探索型任务）。
- `backend/app/routers/admin/world.py` — 审核/应用/驳回/回滚 API；`admin/__init__.py` 登记。
- `backend/app/agent/location_lore.py` — 支持合并动态 lore（注意实际路径在 `agent/` 下）。
- `backend/app/routers/world.py`（或并入 `lab.py`）— `GET /world/locations` 静态+动态合并快照，`main.py` 注册。
- `backend/tests/test_world_governance.py` — 提案→应用→覆盖层生效→回滚；冲突检测拒绝重叠 bounds。

前端
- `frontend/src/components/admin/ProposalsPanel.tsx`（对齐 `EventsPanel.tsx`）+ `AdminSidebar.tsx` `AdminTab` + `AdminPage.tsx` `case`。
- `frontend/src/services/api/adminWorld.ts` — proposals 审核 API。
- `frontend/src/services/ws.ts` — `world_changed` 分支 → 重拉 `GET /world/locations` 刷新 minimap/codex。
- `frontend/src/components/minimap/*` — minimap 改「静态 districtZonesData + 运行时动态地点」合并渲染（`LocationKey` 收窄为静态 key，动态地点走运行时数据，不进联合类型）。
- `frontend/src/components/ExperimentPanel.tsx` — 提案墙叙事化展示。

### P4 打磨
- 研究员评分/排行、成就/季度联动、i18n、`lab_*` 配置在 service 内**真正读取**、tilemap 美术管线补贴图、前端测试底座覆盖关键面板。

---

## 14. 风险与取舍 / 开放问题

**取舍**
- **真实执行 vs 安全**：选择独立进程 + 一次性容器 + 出口白名单 + scope 分级 + 人审断点，是能力与风险的平衡点。第一版用 MockAdapter 把经济与 UI 闭环全部跑通，再切真适配器——降低「一上来就接真实 computer-use」的爆炸半径。
- **不发版改世界 vs 视觉一致**：覆盖层让新建筑逻辑上立刻可达（寻路/规划/codex），但**贴图仍需美术**。P3 的 `add_location` 会出现「有逻辑、无精美贴图」的中间态（可用占位贴图兜底），P4 补齐美术管线。
- **不硬改 Commission**：新建 LabTask 域承载「玩家→研究员」真实任务，只借鉴 Commission 的状态机模式，避免污染既有「居民→玩家差事」语义。
- **金融红线**：沙箱默认拒绝一切支付/转账/下单；世界写入永远经人审。宁可少一点自主性，不冒代替用户动钱或未审改世界的风险。

**已拍板（v0.2 默认，可后改）**
- 研究员资格：先手动授权（admin 白名单写 `meta_json["lab"]["access"]`），条件自动解锁放 P4 再议。
- 验收默认：manual + 72h 超时自动放款；产物放款后解锁；拒收限 1 次进 admin 仲裁。

**开放问题（需你拍板）**
- scope 分级定价：`web_search` < `browse` < `code` < `http` 的费率梯度具体数值。
- 真实运行时优先级：OpenClaw / Hermes / computer-use 哪个先接？取决于你手上已有的运行时。
- 提案可开放的 kind 范围与各自 `risk_level` 阈值（`add_npc` 是否延后到有 Forge 复用后再开）。

