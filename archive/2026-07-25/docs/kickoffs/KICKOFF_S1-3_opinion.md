# KICKOFF S1-3 — 议题立场与舆论动力学(有界信任模型;衔接 辩论 + 日报)

> 对应 `docs/SOCIETY_EXPANSION_PLAN.md` §2 行 S1-3(SOCIETY_EXPANSION_PLAN.md:35)、§6 接口面预告行 S1-3/4 舆论(SOCIETY_EXPANSION_PLAN.md:216)、被引用专章 §3.4 实验楼报告为高权重输入(SOCIETY_EXPANSION_PLAN.md:76 一节)与 §3.5 prompt 隔离纪律(SOCIETY_EXPANSION_PLAN.md:110-116)。
> 格式与纪律照 `docs/KICKOFF_PROMPT_REALISM_P2.md`。

**结论先行**:本模块新建一张 `issue_stances(issue_key, resident_slug, stance)` 表 + `OpinionService`,用**有界信任(bounded-confidence)** 规则模型让每位居民对"活跃议题"持一个 `stance ∈ [-1,1]` 的标量,并在三条**零新增 LLM**的信号上小步移动:①居民互聊 wrapup 的 `mood`(复用既有输出);②辩论 `create_debate`/`settle`(纯 SQL);③nightly 规则漂移(仿 `_npc_choice` 形状)。出面只有两处:村日报多一行 `opinion_line`(搭现有单次 digest 调用,零新调用),以及一个可选的 admin 只读端点。独立门控 `polis_opinion_enabled: bool = False`,关闭时字节级回落到现状。

**关键现状缺口(务必先读,决定了接线方式)**:
- **辩论生命周期只接了一半**。`run_live`(debate_service.py:132-181)与 `settle`(debate_service.py:202-246)**在 app 代码里从未被调用**——仓库里只有 tests 引用它们;router 只暴露 stake+vote,没有 live/settle 端点,也没有 cron 驱动。因此 `maybe_spawn_lecture_debate`(civic_service.py:346-385)产出的辩论**永远停在 `status='announced'`**,不会 live、不会开投票、不会 settle。**不要假设辩论会走到 `settled`**——本模块把**可靠的一手信号放在 `create_debate`(announced)**上,`settle` 钩子标记为"机会性、settle 真被驱动时才触发"。
- **全仓没有任何 `issue`/`议题` 一等实体**。topic 是自由字符串(`debate.topic` / `Poll.question`),`stance`/`opinion`/`issue` 在 `backend/app` 无领域命中。所以 `issue_key` 是**去规范化的自由字符串**(不建 issues 注册表、不建 FK),键取自既有 topic 字符串。

---

## 1. 现状锚点(逐文件逐行核实;仅用已核实的 file:line)

### 1.1 辩论域(要接线的一手信号源,但生命周期半残)
- **`class Debate`**(backend/app/models/debate.py:10-32):字段 `topic:str(300)`、`resident_a_slug`/`resident_b_slug`、`status(announced|live|voting|settled)`、`transcript_json`、`winner(a|b|draw)`、`pool_a/pool_b`、`votes_a/votes_b`、`starts_at`、`settled_at`。**没有 per-resident 的 stance/position 字段,没有 issue 关联**;`topic` 是自由串。是最接近"议题"的东西,但它是辩论作用域的一次性对象,不是持久议题实体。
- **`class DebateStake`**(backend/app/models/debate.py:34-43):`debate_id, user_id, side(a|b), amount, payout`,`uq_debate_stake`。今天唯一"类立场"行,但它是**玩家下注**,不是居民意见——与本模块 `issue_stances` 无对应。
- **`create_debate`**(backend/app/services/debate_service.py:51-56):建 `status='announced'`、自由 topic + 两个居民 slug。**本模块一手信号的可靠钩点**——announced 一定发生。
- **`run_live`**(backend/app/services/debate_service.py:132-181):announced→live→voting,跑 `ROUNDS(6)` 次 LLM turn(用 `background_model`)。**app 代码从不调用**(见现状缺口)。与本模块无关,不改。
- **`settle`**(backend/app/services/debate_service.py:202-246):voting→settled,按多数票结算、赔付(5% burn)、置 winner、调 `_resident_aftermath`。幂等、零 LLM。**若 settle 真被驱动**,这里 winner/loser slug + topic 已知,是天然 `update_from_debate` 缝——但今天不会触发。
- **`_resident_aftermath`**(backend/app/services/debate_service.py:262-278):settle 时 winner mood+,两居民各写一条 `source='debate'` 事件记忆(best-effort try/except)。`OpinionService.update_from_debate` 的理想机会性钩点,但今天不持久任何 stance 值。

### 1.2 civic 域(规则化立场逻辑的复用模板)
- **`propose`**(backend/app/services/civic_service.py:32-80):建 `Poll`(from `app.models.season`),`options_json` 项 `{label, effect, npc_votes}`;`question:str` 即议题。NPC 投票纯规则(`run_npc_voting`/`_npc_choice`),零 LLM。是现存最接近本模块的规则化"立场"逻辑。
- **`_npc_choice`**(backend/app/services/civic_service.py:180-227):逐居民选项打分——SBTI A2(守序 H→维持现状 / L→求变)、duty 兴趣、对提案人关系亲和微调、确定性 tie-break。**零 LLM 的有界/规则立场计算模板,`OpinionService.drift` 直接复用这个形状。**
- **`maybe_spawn_lecture_debate`**(backend/app/services/civic_service.py:346-385):**app 代码里唯一创建辩论的路径**。lecturer 公共讲座 WorldEvent 结束时(event_cron.py:45 调),挑两个 SBTI 反差的社交活跃 NPC(A1 H vs L)调 `create_debate(topic='关于「{lecture}」的争论')`。受 `settings.civic_polls_enabled` 门控。辩论留在 announced,不再前进。

### 1.3 日报域(零新增 LLM 的素材增强先例)
- **`gather_material`**(backend/app/services/digest_service.py:39-114):纯 SQL 组装(无 LLM),产出 chats/shifts/events/arc_lines/heat_top/circle_line/stats/has_material 的 dict。`circle_line`(digest_service.py:96-102)是**精确的零-LLM 增强先例**:门控(`realism_relations_enabled`)、best-effort try/except、只追加一个字符串。`opinion_line` 原样照此插入。
- **`_build_prompt`**(backend/app/services/digest_service.py:117-134):把 material dict 拼成 prompt 文本,每段 `if material[...]: parts.append(...)`;circle_line 在 132-133 追加。opinion 段就是**再一个 `parts.append`**,喂**同一次** `compose_digest` 调用。
- **`compose_digest`**(backend/app/services/digest_service.py:137-151):**单次**村日报 LLM 调用(`effective_model`, `max_tokens=800`)。opinion 素材加进 gather/_build_prompt **零额外 LLM 调用**。

### 1.4 聊天 wrapup 域(from_chat 的免费 mood 信号)
- **`process_chat_wrapup`**(backend/app/memory/service.py:521-582):居民-居民互聊收尾,**一次** LLM 调用抽取双方记忆 + 关系,`_persist_wrapup_side` 落库。返回 `{summary, mood in positive|neutral|negative}`。**from_chat 缝**:mood + 两居民 id/slug 是零额外 LLM 的免费信号。
- **`_persist_wrapup_side`**(backend/app/memory/service.py:584-618):写每侧 `source='chat_resident'` 事件记忆(601-605)+ 关系更新。这些正是 digest gather 读取的信号(digest_service.py:43-48)。
- **`resident_chat`**(backend/app/agent/chat.py:155-247):自主居民-居民互聊驱动;247 调 `svc.process_chat_wrapup(...)`。所有 `chat_resident` 记忆的入口 = from_chat 管线顶端。
- **`add_memory`**(backend/app/memory/service.py:52-66):签名 `add_memory(resident_id, type, content, importance, source, *, related_resident_id=None, ..., metadata_json)`。**没有 stance/opinion 字段**——stance 值必须进新表,`add_memory` 只在你要一条叙事记忆时用。

### 1.5 nightly seam
- **`run_nightly_jobs`**(backend/app/tasks/nightly_cron.py:28-126):每晚 00:30 UTC,每个 job 独立 try/except。顺序关键:**digest 先跑(nightly_cron.py:32)**,随后 civic `close_due_polls`(88)、`seed_civic_agenda`(99)、`maybe_open_seasonal_election`(110)、`run_npc_voting`(120)。→ **本模块 drift 必须排在第 32 行 digest 之前**,才能让当晚日报反映漂移(见任务 3 与边界)。

### 1.6 本模块要接线的确切位置汇总
| 信号 | 钩点(file:line) | 可靠性 |
|---|---|---|
| 一手 · 辩论开场 | `create_debate` debate_service.py:51-56 | 可靠(announced 必发生) |
| 一手 · 辩论结算 | `settle` debate_service.py:202-246 / `_resident_aftermath` :262-278 | 机会性(今天不触发) |
| 一手 · 互聊情绪 | `process_chat_wrapup` 返回处 memory/service.py:521-582(入口 agent/chat.py:247) | 可靠(需自主互聊在跑,见 §7) |
| 漂移 · nightly | `run_nightly_jobs` 第 32 行 digest 之前 nightly_cron.py:28-126 | 可靠 |
| 出面 · 日报 | `gather_material`:96-102 + `_build_prompt`:132-133 + `compose_digest`:137-151 | 可靠 |

---
## 2. 任务切分

> 串行门:任务 1(表+迁移)全绿并提交后才开 2(Service 逻辑),2 全绿才开 3(接线)/4(出面)。每任务独立提交,commit message 带任务号(如 `s1-3-1: issue_stances table + migration`)。

### 任务 1 — `issue_stances` 表 + 迁移(新建;链尾单头校验)
**新建文件**:
- `backend/app/models/issue_stance.py`(镜像 debate.py 形状:uuid str PK via `default=lambda`,`Mapped`/`mapped_column`,`UniqueConstraint` 仿 `uq_debate_stake`)。
- `backend/alembic/versions/NNN_add_issue_stances.py`(占位符 **NNN**;`down_revision` 落地时按当时链头定——**现链头 `040_residents_creator_nullable`**,单头已核实;仿 `029_add_debates.py` 的 create_table/create_index/drop 形状;列 ALTER 走 `op.batch_alter_table`(SQLite dev DB),本表是新建 create_table 无需 batch)。

**修改文件**:
- `backend/app/models/__init__.py`:加 `import app.models.issue_stance  # noqa: F401`(debate 在 __init__.py:32 注册),使 `Base.metadata.create_all`(main.py:45,测试用)能看到新表。

**新表结构 `issue_stances`**:

| 列名 | 类型 | 说明 |
|---|---|---|
| `id` | `str` PK (`default=lambda: uuid...`) | 仿 debate.py PK 范式 |
| `issue_key` | `str(300)` NOT NULL | 去规范化议题键(取自 `debate.topic` / `Poll.question`,规范化后;**非 FK**,无 issues 表) |
| `resident_slug` | `str` NOT NULL | 居民 slug(与 debate.resident_a_slug 同口径) |
| `stance` | `Float` NOT NULL default `0.0` | 有界信任标量 `∈ [-1.0, 1.0]` |
| `confidence` | `Float` NOT NULL default `0.5` | 立场强度/信心 `∈ [0,1]`,drift 步长调制(可选,默认 0.5) |
| `updated_from` | `str(16)` | 最近更新来源 `chat|debate|drift|seed`(可观测/调试用) |
| `interact_count` | `Integer` NOT NULL default `0` | 该 (issue,resident) 被更新次数 |
| `last_update_at` | `DateTime` | 最近更新时间(UTC) |
| `created_at` | `DateTime` default now | |

**约束/索引**:
- `UniqueConstraint("issue_key", "resident_slug", name="uq_issue_stance")`(仿 `uq_debate_stake`)——保证 upsert 的冲突目标,防重复行。
- `Index("ix_issue_stance_issue", "issue_key")`(按议题聚合出 variance/drift)。
- `Index("ix_issue_stance_resident", "resident_slug")`(按人查其所有立场)。

> **`issue_key` 决策(现状缺口的落地选择)**:不建 issues 注册表、不建 FK。`issue_key` = 规范化字符串(去首尾空格 + 折叠内部空白;**不做大小写折叠**,中文为主),由 `OpinionService._normalize_issue_key(topic)` 统一产生。键来源:`debate.topic`(辩论)与 `Poll.question`(civic,civic_service.py:32-80)。同一 topic 反复出现时天然复用同一行——这是**期望行为**(议题跨辩论/投票延续)。`Poll`/`Vote` 从 `app.models.season` 导入(civic_service.py:27 已有此依赖;season.py 具体列落地前用 `Poll.question` 字符串,不做 FK,避免 backend/app/models/season.py 的列耦合)。

### 任务 2 — `OpinionService`(新建;规则骨架,零 LLM)
**新建文件**:`backend/app/services/opinion_service.py`。

**服务方法签名**(全部 `async`,`db: AsyncSession` 首参):

```python
class OpinionService:
    def __init__(self, db: AsyncSession) -> None: ...

    @staticmethod
    def _normalize_issue_key(topic: str) -> str: ...
        # 去首尾空格 + 折叠内部空白;截断到 300;不做大小写折叠

    async def get_stance(self, issue_key: str, resident_slug: str) -> float | None: ...
        # 无行返回 None(未表态)

    async def _bump_stance(
        self, issue_key: str, resident_slug: str, *,
        target: float, rate: float, source: str,
    ) -> None: ...
        # 原子 upsert(见 §4):stance ← clamp(stance + rate*(target-stance), -1, 1)
        # 有界信任:仅当 |stance - target| <= EPSILON 时才移动(rate 生效);否则不动

    async def update_from_chat(
        self, a_slug: str, b_slug: str, mood: str, *,
        rng: random.Random | None = None,
    ) -> int: ...
        # 门控 polis_opinion_enabled;从 process_chat_wrapup 的 {mood} 派生:
        #   positive → 双方在共同活跃议题上向彼此当前 stance 的中点靠拢(有界信任,Deffuant 式)
        #   negative → 排斥/不收敛(可选:超出 EPSILON 时轻微远离,默认仅"不靠拢")
        #   neutral  → 无操作
        # 只对"双方都已有 stance 且同一 issue_key"的议题动;不凭空建议题
        # 返回被更新的 (issue,resident) 数;关闭门控直接 return 0

    async def update_from_debate(
        self, debate: "Debate", *, seed_only: bool = False,
    ) -> int: ...
        # 门控 polis_opinion_enabled。issue_key = _normalize_issue_key(debate.topic)
        # seed_only=True(create_debate 钩点,可靠):为 a/b 两 slug 建初始对立 stance
        #   —— 用 SBTI A1/A2 维度定符号与幅度(仿 _npc_choice),缺 SBTI 回落 ±SEED_MAG
        # seed_only=False(settle 钩点,机会性):winner stance 向本方极值增强、
        #   loser 向 0 回归;仅当 debate.status=='settled' 时;幂等
        # 返回被更新数;关闭门控 return 0

    async def drift(self, *, rng: random.Random | None = None) -> int: ...
        # 门控 polis_opinion_enabled。nightly 规则漂移,零 LLM,仿 _npc_choice(180-227):
        # 对每个"活跃议题"(见下)聚合该议题所有居民 stance,
        # 对每位居民:取"信任邻居"= 与之 |Δstance|<=EPSILON 的其他表态者,
        #   移动 stance 向邻居的(亲和加权)均值,步长 DRIFT_RATE;
        #   亲和权重来自 relation_service(若 realism_relations_enabled),否则均匀权重
        #   → 亲和加权是可选增强,不硬依赖关系模块(见 §3 回落)
        # 返回被更新数;关闭门控 return 0

    async def issue_variance(self, issue_key: str) -> tuple[float, int]: ...
        # 返回 (该议题 stance 方差, 表态人数) —— 探针与 digest 素材共用

    async def top_active_issues(self, n: int = 5) -> list[str]: ...
        # 按表态人数/近期更新排序的活跃议题键;digest opinion_line 与 drift 用
```

**"活跃议题"定义**(纯 SQL,规则):`issue_stances` 中 `last_update_at` 在近 `OPINION_ACTIVE_WINDOW_DAYS`(默认 14)内、且表态人数 ≥ `OPINION_MIN_PARTICIPANTS`(默认 3)的 `issue_key`。

**REST / 鉴权(可选任务,默认不做,超出本批核心 will_modify 集)**:§6 预告的 `GET /admin/opinions`。若做:新建 `backend/app/routers/admin/opinions.py`,`router = APIRouter(prefix="/opinions")`,**每个端点**加 `admin: User = Depends(require_admin)`(auth 是 per-endpoint,非 router-level;`from app.routers.admin.middleware import require_admin`),在 `backend/app/routers/admin/__init__.py` `router.include_router(...)`。返回 `[{issue_key, participants, variance, mean_stance}]`。**本批默认只交付探针出数(§6),admin 端点留作后续**——避免与其他 KICKOFF 抢改 `admin/__init__.py`。

**WS 事件**:**无**。§6 预告表 S1-3/4 舆论 WS 列为 `—`;舆论只经**日报文本**与(可选)admin 只读端点出面,不做实时 WS 推送。故本模块不碰 `broadcast_world_changed` / `world_changed_event` / OutboxEvent seq。

### 任务 3 — 三条信号接线 + nightly drift(修改)
**修改文件**:
- `backend/app/services/debate_service.py`(`create_debate` 51-56 末尾加机会性钩:`OpinionService(db).update_from_debate(debate, seed_only=True)`,best-effort try/except,门控内);`_resident_aftermath`(262-278)加 `update_from_debate(debate, seed_only=False)`(settle 真跑时才生效)。
- `backend/app/memory/service.py`:`process_chat_wrapup`(521-582)返回 `{summary, mood}` 后,best-effort 调 `OpinionService(db).update_from_chat(a_slug, b_slug, mood)`(门控内)。
- `backend/app/tasks/nightly_cron.py`:在 `run_nightly_jobs`**第 32 行 `generate_village_digest` 之前**插入新 try/except 块调 `OpinionService(db).drift()`(见 §7 顺序硬要求)。块内 `from app.config import settings; if settings.polis_opinion_enabled:` 门控,仿 realism 夜间 job 在 cron 内门控(nightly_cron.py:180/209/221)。

### 任务 4 — 日报 `opinion_line` 素材(修改;零新增 LLM 调用)
**修改文件**:`backend/app/services/digest_service.py`。
- `gather_material`(39-114):仿 `circle_line`(96-102)加 key `opinion_line`——门控 `polis_opinion_enabled` + best-effort try/except,调 `OpinionService(db).top_active_issues` + `issue_variance` 拼一行(如"关于「X」镇上分歧扩大 / 渐趋一致")。
- `_build_prompt`(117-134):仿 circle_line(132-133)加一句 `if material.get("opinion_line"): parts.append(...)`——喂**同一次** `compose_digest`(137-151),**零新增 LLM 调用**。

### 任务 5 — 测试(新建 `backend/tests/test_opinion_service.py`,见 §5)

---

## 3. 门控开关与默认值

**新增独立开关**(`backend/app/config.py`,加到 `Settings` 类,仿 flag-add 范式 config.py:7-19 的类属性写法):

```python
# S1-3 议题立场与舆论动力学 —— 独立门控,默认 False → 行为与现状完全一致
polis_opinion_enabled: bool = False        # 主开关(前缀 POLIS_OPINION_ 见 §6)
polis_opinion_epsilon: float = 0.4         # 有界信任阈值 EPSILON(|Δstance|<=ε 才互相影响)
polis_opinion_chat_rate: float = 0.08      # from_chat Deffuant 步长
polis_opinion_drift_rate: float = 0.05     # nightly 漂移步长
polis_opinion_seed_mag: float = 0.3        # 辩论开场初始对立幅度(缺 SBTI 时)
polis_opinion_active_window_days: int = 14 # "活跃议题"时间窗
polis_opinion_min_participants: int = 3    # "活跃议题"最少表态人数
polis_opinion_neg_repel: bool = False      # negative mood 是否轻微远离(默认仅"不靠拢")
```

- **默认 `False`**,环境变量 `POLIS_OPINION_ENABLED` 自动解析(config.py:375-378 `model_config` + 单例)。
- **关闭时字节级回落**:`update_from_chat`/`update_from_debate`/`drift` 首行 `if not settings.polis_opinion_enabled: return 0`;`gather_material` 的 `opinion_line` 段与 `circle_line` 同样门控 + try/except,关闭时 `opinion_line` 不入 material,`_build_prompt` 的 `if material.get("opinion_line")` 自然跳过。**关闭后:辩论/聊天/日报/nightly 路径与现状逐字节一致,既有测试零改动通过。**
- **注意**:M1–M6 既有 town flag(`civic_polls_enabled` 等 config.py:354-373)**默认 True**,与"新 flag 默认 False"惯例相悖——本模块显式**默认 False**(rollback-safe),对齐 realism 家族(`realism_relations_enabled` 等 config.py:321-352 默认 False)的正确范式。
- **前缀**:`POLIS_OPINION_`(§6 预告一致);数值参数同前缀进 config,不硬编码。

---

## 4. 原子性要求

`stance` 更新走**写路径条件 UPDATE + upsert,禁止读-改-写**——多 worker/并发 wrapup 下丢更新按 `coin_service` 原子化范式同标准对待。

- **`_bump_stance` 原子形态**:一条 `INSERT ... ON CONFLICT (issue_key, resident_slug) DO UPDATE` upsert,新值在 **SQL 内**算出,不在 Python 里先 SELECT 再算再写:

```sql
-- 有界信任 + 封顶,全部在 SQL 内(Deffuant 单步):新值 = clamp(old + rate*(target-old), -1, 1)
-- 仅当 |old - target| <= :epsilon 才移动(有界信任):用 CASE 表达
INSERT INTO issue_stances (id, issue_key, resident_slug, stance, updated_from, interact_count, last_update_at, created_at)
VALUES (:id, :issue_key, :slug, :seed_stance, :source, 1, :now, :now)
ON CONFLICT (issue_key, resident_slug) DO UPDATE SET
  stance = MAX(-1.0, MIN(1.0,
    CASE WHEN ABS(issue_stances.stance - :target) <= :epsilon
         THEN issue_stances.stance + :rate * (:target - issue_stances.stance)
         ELSE issue_stances.stance END)),
  interact_count = issue_stances.interact_count + 1,
  updated_from = :source,
  last_update_at = :now;
```

- **无向对/成对更新**:`update_from_chat` 的双方 Deffuant 靠拢 = 两次独立 `_bump_stance`(a 的 target = b 的当前 stance,b 的 target = a 的当前 stance);为避免"读到对方已被本事务改动后的值",**先在事务开始各读一次快照 target,再各自 upsert**,两次 upsert 各自原子。
- **参照范式(现状缺口:coin_service 不在本模块已核实 anchors 内)**:全局共享基础设施说明将 `coin_service` 原子化列为写路径标准范式。**落地时以 `backend/app/services/coin_service.py` 的条件 UPDATE / upsert 惯用法为准**(其精确 file:line 未在本模块 anchors 核实,不在此臆造行号);本模块的 `_bump_stance` 必须复用同一"条件 UPDATE + ON CONFLICT upsert、禁读改写"标准。SQLite dev DB 与 Postgres prod 的 `ON CONFLICT` 均支持;若走 SQLAlchemy Core,用 `sqlite`/`postgresql` 方言的 `insert(...).on_conflict_do_update(...)`(两方言 API 名不同,落地时按目标方言分支或用可移植等价写法)。
- **drift 幂等/一致性**:`drift` 在单进程 nightly 内串行跑(见 §7),对每议题读快照 → 逐居民 upsert;同一晚重复跑收敛方向一致(Deffuant 单步幂等性弱,但门控 job 每晚只跑一次,nightly_cron.py:343-347 单日 cron)。

---

## 5. 测试口径

全部 seeded RNG(注入 `random.Random(seed)`),门控回落断言必备。

**单测(`backend/tests/test_opinion_service.py`,具体 `test_` 函数名)**:
- `test_bump_stance_upsert_creates_row` — 首次 upsert 建行,stance = seed 值。
- `test_bump_stance_upsert_conflict_no_duplicate` — 同 (issue,resident) 二次 upsert 不产生重复行(`uq_issue_stance` 生效),`interact_count` 递增。
- `test_bump_stance_atomic_concurrent_no_lost_update` — 并发多次 `_bump_stance` 后 `interact_count` == 调用数(无丢更新,原子 UPDATE)。
- `test_bump_stance_clamped_to_unit_interval` — 反复正向 bump,stance 封顶 `1.0` 不溢出;反向封底 `-1.0`。
- `test_bounded_confidence_no_move_outside_epsilon` — `|stance-target| > epsilon` 时 stance 不动(有界信任核心)。
- `test_bounded_confidence_moves_inside_epsilon` — `<= epsilon` 时按 rate 向 target 移动,方向/幅度断言。
- `test_normalize_issue_key_dedup` — 首尾空格/内部多空白的同一 topic 规范化到同一 key(去重),不做大小写折叠。
- `test_update_from_chat_positive_converges` — positive mood 下双方共同议题 stance 互相靠拢(中点方向)。
- `test_update_from_chat_negative_no_converge` — negative mood 不靠拢(默认);`polis_opinion_neg_repel=True` 时轻微远离。
- `test_update_from_chat_only_shared_issues` — 只对双方都已表态的同一 issue_key 动,不凭空建议题。
- `test_update_from_debate_seed_only_creates_opposing_stances` — `create_debate` 钩点为 a/b 建对立初始 stance,符号由 SBTI 定。
- `test_update_from_debate_seed_missing_sbti_fallback` — 缺 `meta_json['sbti']` 时回落 `±seed_mag`(不崩)。
- `test_update_from_debate_settle_only_when_settled` — `seed_only=False` 且 `status!='settled'` 时零更新(现状缺口:settle 不触发也不误伤)。
- `test_update_from_debate_settle_winner_reinforced_loser_regresses` — settle 真跑时 winner 增强、loser 向 0 回归。
- `test_drift_converges_within_epsilon_cluster` — seeded 图:ε 内簇收敛,方差下降。
- `test_drift_polarizes_across_gap` — 两簇间距 > ε 时不合并(极化保留,非白噪声)。
- `test_drift_affinity_weight_when_relations_on` — `realism_relations_enabled=True` 时高亲和邻居权重更大(统计断言)。
- `test_drift_uniform_weight_when_relations_off` — 关系开关关时回落均匀权重,drift 仍工作(不硬依赖)。
- `test_issue_variance_and_active_issues` — variance 计算正确;活跃议题按窗口+人数过滤。

**门控回落单测**:
- `test_disabled_update_from_chat_noop` / `test_disabled_update_from_debate_noop` / `test_disabled_drift_noop` — 三方法门控关时 return 0 且零写入(查表无行变化)。
- `test_disabled_digest_has_no_opinion_line` — `polis_opinion_enabled=False` 时 `gather_material` 无 `opinion_line` key,`_build_prompt` 输出与现状逐字一致。

**集成用例(具体 `test_` 函数名)**:
- `test_integration_chat_wrapup_moves_stance` — 走 `process_chat_wrapup`(mock LLM 返回 `{mood:'positive'}`)→ 断言双方共同议题 stance 靠拢,且**无额外 LLM 调用**(mock 调用计数 == 1,仅 wrapup 本身)。
- `test_integration_create_debate_seeds_stances` — `maybe_spawn_lecture_debate`/`create_debate` 后 `issue_stances` 出现两条对立行。
- `test_integration_nightly_drift_before_digest` — nightly 跑后 drift 在 digest 之前生效:同晚 `opinion_line` 反映漂移后的方差(顺序断言,见 §7)。
- `test_integration_digest_opinion_line_zero_new_llm` — 开启门控,`compose_digest` LLM 调用计数仍为 1(素材增强零新增调用)。
- `test_integration_migration_single_head` — `alembic heads` 单头(链尾校验);`Base.metadata.create_all` 后 `issue_stances` 表存在(`__init__.py` 已注册)。

---

## 6. 探针出数定义

`burnin_report.py` 新增一项(对齐 SOCIETY_EXPANSION_PLAN.md:35 S1-3 验收"立场方差时间序列出现收敛/极化,非白噪声"):

- **出什么数**:对每个**活跃议题**,输出 `stance` 的**方差时间序列**(按模拟天/夜采样)与**双峰性指标**(如 bimodality coefficient 或简单的"最大间隔簇数")。
- **目标形态**:方差随时间**收敛**(趋一致)**或极化**(分裂为 ≥2 稳定簇),**非白噪声**(方差不应无规律抖动)。有界信任模型在 ε 大时收敛、ε 小时极化——探针应能区分两态。
- **对照组(开关关 `polis_opinion_enabled=False`)**:`issue_stances` 无写入 → 无议题/无时间序列(或恒为空/常数),形态为"无动力学"。对照即证明动力学由本模块驱动。
- seeded fixture 演示出数,首轮数值记入 PROGRESS.md。

---

## 7. 边界与"不碰区域"

- **nightly 顺序硬要求**:`drift` **必须**插在 `run_nightly_jobs` 第 32 行 `generate_village_digest` **之前**(nightly_cron.py:28-126),否则当晚 `opinion_line` 读的是漂移前的旧值。**不要**把 drift 追加到 `run_nightly_jobs` 末尾。
- **辩论生命周期不修复**:本模块**不**新增 driver 去把 announced 辩论推到 settled(那是辩论域自己的事)。一手信号靠 `create_debate`(announced,可靠)+ 互聊 wrapup;`settle` 钩点为机会性,settle 真被驱动时才生效。**不假设辩论到达 settled**。
- **零新增 LLM(硬纪律)**:规则做骨架、LLM 做血肉。`drift` **保持纯规则**(仿 `_npc_choice`),**严禁**在 drift/from_chat/from_debate 内调 LLM;`update_from_chat` 只消费 `process_chat_wrapup` 已返回的 `mood`;日报 `opinion_line` 搭**同一次** `compose_digest`。`run_live` 的 6 次 LLM 调用是既有、与本模块无关,不碰。
- **prompt 隔离(SOCIETY_EXPANSION_PLAN.md:110-116 / §9 红线 3)**:全局指标永不进 NPC prompt。本模块的 `stance` 是**逐议题逐居民的局部量**,`opinion_line` 进的是**村日报**(世界内信息物,与 `circle_line` 同级),**不是**任何单个 NPC 的 decide/chat prompt。**严禁**把"全镇 stance 方差/度分布"之类全局聚合注入居民 prompt——写成测试断言(`test_disabled_digest_has_no_opinion_line` + 人工核对 opinion_line 仅限 digest)。
- **性能红线 tick +1**:本模块**不改** perceive/decide 的 tick 循环采样,写入只发生在 chat wrapup(有界事件)与 nightly(离线),**tick 每居民查询增量 0**——比 P2 更轻。不得把 stance 查询塞进 O(N²) 的 perceive。
- **Lab 工程安全不变量**:不碰 `app/lab/`、`app/forge/`、审批门、WorldGuard 信封、prompt 文风、模型计价。
- **自由字符串键的脆弱性(已知取舍)**:`issue_key` 靠字符串相等聚合,topic 措辞漂移会分裂议题——接受此取舍(§2 已说明,同 topic 复用是期望行为),**不**为此引入 issues 注册表/embedding 聚类(超 scope)。

---

## 8. 依赖与冲突声明

### 8.1 依赖前置模块
- **S0-2 内置居民 SBTI 画像补齐**(SOCIETY_EXPANSION_PLAN.md:30):`update_from_debate(seed_only)` 与 `drift` 的初始符号/幅度用 `resident.meta_json['sbti']` 维度(仿 `_npc_choice` civic_service.py:180-227)。**缺 SBTI 时回落** `±polis_opinion_seed_mag` / 均匀权重(`test_update_from_debate_seed_missing_sbti_fallback` 覆盖),**软依赖不阻塞**,但 S0-2 未完成时立场个体差异会退化。
- **`realism_relations_enabled`**(config.py:325,默认 False):`drift` 的亲和加权是**可选增强**;关系开关关时回落均匀权重(`test_drift_uniform_weight_when_relations_off`)。**不硬依赖**——本模块用**自己的独立开关** `polis_opinion_enabled`,不寄生在 relations 开关下(遵循"每 feature 一个独立 bool"惯例)。
- **自主居民互聊在跑**(agent/chat.py:155-247 → wrapup):`update_from_chat` 的输入靠 `chat_resident` 路径;若目标环境未开自主互聊,from_chat 信号会**饿死**,只剩辩论 seed + drift 驱动。开工前确认目标 env 自主互聊在跑(现状缺口第五条)。

### 8.2 本模块会碰的文件
- 新建:`backend/app/models/issue_stance.py`、`backend/app/services/opinion_service.py`、`backend/alembic/versions/NNN_add_issue_stances.py`、`backend/tests/test_opinion_service.py`。
- 修改:`backend/app/models/__init__.py`、`backend/app/services/digest_service.py`、`backend/app/services/debate_service.py`、`backend/app/memory/service.py`、`backend/app/tasks/nightly_cron.py`、`backend/app/config.py`。

### 8.3 与其他 4 份 KICKOFF 的文件交集(逐条点名 + 串行/协调建议)

| 交集文件 | 与哪些模块交集 | 冲突性质 | 协调建议 |
|---|---|---|---|
| **`backend/alembic/versions/041_*.py`** | **S2-5 policies**(也占 `041_add_policies.py`)、S1-5(用 `0XX_add_town_treasury.py`) | **高**:S1-3 与 S2-5 **都想用 041 且都 down_revision=040**,并行会造成 alembic **多头** | 迁移号仅占位符 **NNN**,`down_revision` **落地时按当时链头定**(现头 `040_residents_creator_nullable`)。合并顺序确定后**线性化**为 041/042/043…,只有最先合入者 down_revision=040,其余顺链尾改。**merge 时做 `alembic heads` 单头校验**(硬门)。 |
| **`backend/app/config.py`** | S2-1 / S2-5 / S1-1 / S1-5 **全部** | 中:各模块**追加各自 flag 块**到 `Settings` | 追加式改动,冲突仅在相邻行。各模块用**独立前缀**(本模块 `POLIS_OPINION_`,S1-1 `REP_`/S1-5 `ECON_`/S2-5 `POLIS_POLICY_`/S2-1 `POLIS_OFFICE_`),块间加注释分隔,merge 基本无语义冲突。**注意勿复制 config.py:373 的悬挂注释笔误。** |
| **`backend/app/tasks/nightly_cron.py`** | S2-1 / S2-5 / S1-1 / S1-5 **全部**(均加 nightly 块) | 中:多个 try/except 块插入 `run_nightly_jobs`(S2-1 加 `term_check` 块) | 各加**独立 try/except 块**(nightly_cron.py:76-126 既有范式)。**S1-3 特殊**:drift 必须在**第 32 行 digest 之前**,其他模块的聚合(声誉/财政/任期检查)多在 digest 之后——**S1-3 的插入点与它们不同区**,但 merge 时需确认没人把块插到 digest 之前打乱顺序。**建议 S1-3 的 drift 块紧贴 digest 上方单独成段并注释"MUST run before digest"。** |
| **`backend/app/services/civic_service.py`** | **S2-1 / S2-5 / S1-1 都修改它**;S1-3 **只读引用**(`propose`/`_npc_choice`/`maybe_spawn_lecture_debate` 作模板与信号源) | 低(对 S1-3):S1-3 **不改** civic_service | S1-3 的 will_modify 集**不含** civic_service.py;仅复用其 `_npc_choice` 形状于 opinion_service。无写冲突。 |
| **`backend/app/services/debate_service.py`** | 仅 S1-3 | 无 | 独占修改。 |
| **`backend/app/memory/service.py`** | 仅 S1-3 | 无 | 独占修改。 |
| **`backend/app/services/digest_service.py`** | 仅 S1-3 | 无 | 独占修改。 |
| **`backend/app/models/__init__.py`** | **S2-1(`office`)、S2-5(`policy`)、S1-5(`town_treasury`)** 各加一行 import | 极低 | 各加一行 `import app.models.xxx  # noqa: F401`,相邻行,merge 无语义冲突。 |
| **`backend/app/routers/admin/__init__.py`** | S2-5(改 admin/world.py)等 | 低(本模块**默认不做** admin 端点) | S1-3 的 `GET /admin/opinions` 列为**可选、本批不做**,正为避开 admin 路由注册的抢改;若后续做,per-endpoint `Depends(require_admin)`。 |

**串行/协调结论**:S1-3 与其他模块**唯一的高冲突点是 alembic 迁移号**——按占位符 NNN + 落地定链头 + merge 时单头校验处理。config.py / nightly_cron.py / models/__init__.py 均为**追加式低冲突**,可并行开发,merge 时人工核对相邻块。civic_service.py 虽被 S2-5/S1-1 改,但 S1-3 只读不写,无写冲突。


