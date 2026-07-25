# Simverse World 新功能技术规格（可开工版）

> 配套文档：`docs/OPTIMIZATION_PLAN.md`（功能清单与优先级见其第八节）
> 本文将全部 29 个功能细化为：数据模型（含迁移号）、API/WS 协议、文件级改动清单、核心逻辑、验收标准。
> 所有设计锚定当前代码事实：迁移链至 `011_backfill_home_location`，新迁移从 **012** 起编号；表/字段名与 `app/models/` 现状一致。

## 0. 阅读说明与全局约定

- **ID 约定**：所有新表主键沿用现有习惯 `String(uuid4)`；时间字段 `DateTime(timezone=True)`，默认 `datetime.now(UTC)`
- **WS 消息**：沿用现有 snake_case `type` 字段风格（如 `resident_move`）；客户端→服务端消息在 `app/ws/protocol.py` 加 Pydantic 模型
- **鉴权**：REST 用户端点沿用 `Depends(get_current_user)`（`routers/profile.py` 模式）；管理端点挂 `routers/admin/middleware.py::require_admin`
- **SC = Soul Coin**；所有加减币必须走 `services/coin_service.py` 的 `charge/reward`（自动记 `transactions`）
- **🔥 标记**：该功能有常驻 LLM 成本，实现时必须接入 LLM 预算熔断（OPTIMIZATION_PLAN P1-1）
- **前置**：全部功能假定 OPTIMIZATION_PLAN Phase 0 已完成（尤其 P0-1 session 隔离——多个功能会往 agent 阶段里加逻辑）

### 迁移号分配总表

| 迁移 | 内容 | 服务的功能 |
|------|------|-----------|
| 012 | `world_events` | S1 → A2/E6/C3/B2 |
| 013 | `achievements` + `user_achievements` | S2 → D1/E8/E12/D3 |
| 014 | `items` + `purchases` | S3 → D2/B3/A3 |
| 015 | `notifications` | S4 → A3/D1/E7/E11 |
| 016 | `location_visits` | S5 → B2/E8/E4 |
| 017 | `resident_goals` | A1/E13 |
| 018 | `bulletin_posts` | A4/A5/C3 |
| 019 | `digests` | A5/E14 |
| 020 | `commissions` | B1 |
| 021 | `residents.home_decor_json` | B3 |
| 022 | `seasons` + `season_scripts` + `polls` + `votes` + `season_scores` | C3/E12 |
| 023 | `residents.mood_json` | E1 |
| 024 | `time_capsules` | E7 |
| 025 | `debates` + `debate_stakes` | E9 |
| 026 | `follows` + `feed_events` | E11 |
| 027 | `goal_investments` | E13 |
| 028 | `users.login_streak` + `users.last_login_date` + `daily_quests` | D3 |

无迁移的功能：C1（无状态）、C2（聚合查询）、E2/E3/E4（复用 `memories`，新增 type/source 枚举值）、E5（文件缓存）、E10（复用 media）。

> ⚠️ 迁移号是**预分配的文件名前缀**。本仓库的 revision id 即前缀全名（如 `010_add_movement_fields`），`down_revision` 构成单链——若按第 7 节施工序（非号码序）落地，创建时把 `down_revision` 指向**当时的实际链头**即可，号码乱序无碍；介意美观就在开工时重排号。

---

## 1. 共享基建（先建，多功能复用）

### S1 世界事件总线

**表 `world_events`（迁移 012）：**

```python
class WorldEvent(Base):
    __tablename__ = "world_events"
    id: str            # uuid pk
    type: str          # String(20): "festival" | "weather" | "news" | "custom" | "script"
    title: str         # String(200)
    description: str   # Text — 注入 prompt 的文案
    payload_json: dict # JSON — type 专属数据（如 weather: {"kind": "rain", "intensity": 0.6}）
    starts_at: datetime
    ends_at: datetime
    created_by: str | None  # FK users.id，运营手动投放时记录
    is_active: bool    # 索引；cron 负责按时间翻转
```

**注入管线（三个消费点）：**
1. **Agent perceive**：`agent/schemas.py::TickContext` 加字段 `world_events: list[dict]`；`phases/perceive/basic.py` 查询 `is_active=True` 的事件（每轮 tick 查一次、模块级缓存 60s）写入 ctx；`phases/decide/basic.py` 的决策 prompt 加一行 `当前世界事件：{titles}`
2. **玩家对话**：`llm/prompt.py::assemble_system_prompt` 追加活跃事件段落（同一缓存）
3. **前端**：cron 翻转 `is_active` 时 `manager.broadcast({"type": "world_event", "event": {...}, "phase": "start"|"end"})`

**cron**：新文件 `app/tasks/event_cron.py`，仿 `heat_cron.py` 模式，每 60s 扫描 `starts_at/ends_at` 翻转 `is_active` 并广播。在 `main.py` lifespan 注册（P0-3 落地后随 Agent Worker 走）。

**API**：`routers/admin/events.py` — `GET/POST/PATCH/DELETE /admin/events`（require_admin）；公共 `GET /events/active`。

**验收**：投放事件后 60s 内全端广播；活跃期间任意居民决策 prompt 与玩家对话 prompt 含事件文案；`tests/test_world_events.py` 覆盖 cron 翻转与注入。

### S2 领域事件钩子 + 成就引擎

**表（迁移 013）：**

```python
class Achievement(Base):        # 成就定义（代码内 seed，表存元数据便于运营改文案）
    __tablename__ = "achievements"
    code: str          # String(50) pk，如 "first_chat"、"memory_keeper_10"
    title: str; description: str; icon: str
    points: int        # 赛季积分权重（E12 用）
    reward_sc: int     # 解锁奖励
    hidden: bool       # 隐藏成就

class UserAchievement(Base):
    __tablename__ = "user_achievements"
    id: str
    user_id: str       # FK users.id, index
    code: str          # FK achievements.code
    progress_json: dict | None   # 计数型成就的进度 {"count": 7, "target": 10}
    unlocked_at: datetime | None # NULL = 进行中
    # UniqueConstraint(user_id, code)
```

**事件钩子**：新文件 `app/events/bus.py` — 进程内同步发布器（30 行）：

```python
_handlers: dict[str, list[Callable]] = defaultdict(list)
def on(event: str): ...          # 装饰器注册
async def emit(db, event: str, **kw): ...  # 逐个 await handler，单个失败不阻断（logger.warning + exc_info）
```

**埋点位置（首批 6 个事件）：**

| 事件名 | 发射位置 |
|--------|----------|
| `chat_completed` | `ws/handler.py` end_chat 分支（现 :323 附近），kw: user_id, resident_id, turns |
| `memory_written_about_user` | `memory/service.py::add_memory` 当 related_user_id 非空 |
| `personality_shifted` | `personality/evolution.py` 跳变提交处 |
| `login_streak` | `services/daily_reward_service.py::claim_daily_reward` |
| `location_first_visit` | S5 LocationTracker |
| `commission_completed` | B1 完成处理器 |

**成就检查器**：`app/events/achievements.py` — 每个成就一个纯函数，注册到对应事件；解锁时 `coin_service.reward` + S4 通知 + WS `{"type": "achievement_unlocked", "code", "title", "reward_sc"}`。

**API**：`GET /achievements`（全量定义+我的进度合并）、seed 脚本 `backend/seed/achievements.py`。

**验收**：完成首次对话触发 `first_chat` 解锁、到账、toast；重复触发幂等（unique 约束 + upsert）。

### S3 商品目录与购买管线

**表（迁移 014）：**

```python
class Item(Base):
    __tablename__ = "items"
    id: str
    code: str          # String(50) unique："rename_card" | "portrait_redraw" | "gift_flower" | "decor_lamp" ...
    kind: str          # String(20): "consumable" | "gift" | "decor" | "cosmetic"
    name: str; description: str; icon: str
    price_sc: int
    payload_json: dict # decor: {"sprite": "lamp_01", "w":1, "h":1}；gift: {"relationship_boost": 0.1}
    active: bool

class Purchase(Base):
    __tablename__ = "purchases"
    id: str
    user_id: str       # index
    item_code: str
    qty: int
    total_sc: int
    context_json: dict | None  # gift 的目标居民等
    created_at: datetime
```

**服务**：`app/services/shop_service.py` — `purchase(db, user_id, item_code, qty, context)`：查 Item → `charge()`（reason=`purchase:{code}`，不足则 400）→ 写 Purchase → 按 kind 分发效果处理器（注册表模式，D2/B3/A3 各自注册自己的效果函数）。

**API**：`GET /shop/catalog`、`POST /shop/purchase {item_code, qty, context}`；管理端 `GET/POST/PATCH /admin/items`。

**验收**：余额不足购买返回 400 且无副作用（事务内）；购买成功 transactions 有记录。

### S4 通知中心

**表 `notifications`（迁移 015）：** `id, user_id(index), kind(String 30), title, body, payload_json, read_at(nullable), created_at`。kind 枚举：`resident_greeting | achievement | capsule_delivered | commission | feed | system`。

**服务**：`app/services/notification_service.py::notify(db, user_id, kind, title, body, payload)` — 写表；若 `manager.active` 中在线则同时 `manager.send(user_id, {"type": "notification", ...})`。

**API**：`GET /notifications?unread_only=&cursor=`、`POST /notifications/read {ids}`。
**前端**：`TopNav.tsx` 加铃铛 + 未读角标；WS 分发在 `services/ws.ts:17` 的 `onmessage` 处理器加 `notification` 分支（本文所有新 WS 消息类型的前端接入点都在这里）；新组件 `components/NotificationDrawer.tsx`。

**验收**：离线产生的通知上线后可拉取；已读状态持久。

### S5 位置进入检测（LocationTracker）

**表 `location_visits`（迁移 016）：** `id, user_id(index), location_id(String 50), first_visited_at, visit_count, last_visited_at`；`UniqueConstraint(user_id, location_id)`。

**实现**：`app/services/location_tracker.py`：
- 启动时从 `agent/map_data.py` 的 20 个命名位置构建 tile→location 查找表（bbox 展开为 dict，O(1) 查询）
- `ws/handler.py` move 快路径（现 :120-131）加一行 `location_tracker.on_move(user_id, tile_x, tile_y)`：**纯内存**比较上次 location，未变化立即返回（不能在 move 热路径查库）
- 发生"进入新位置"事件时投递到 `asyncio.Queue`，由单个后台消费者任务批量落库（upsert visit_count）并 `emit("location_first_visit")`（首访时）、调用 B2 偶遇判定

**验收**：move 消息处理 P99 无可测退化（队列异步化）；首访写库一次、成就事件恰好一次。

---

## 2. A 组 — AI 居民能力

### A1 居民人生目标与长线剧情 🔥

**现状锚点**：`residents.daily_goal_json / daily_plans_json` 已存在（迁移 007）；`agent/schemas.py` 已有 `DailyGoal/HourlyPlan`；plan 阶段在 `agent/phases/plan/basic.py`。

**表 `resident_goals`（迁移 017）：**

```python
class ResidentGoal(Base):
    __tablename__ = "resident_goals"
    id: str
    resident_id: str       # FK residents.id, index
    kind: str              # "life"（长期，1个active）| "arc"（阶段剧情，串行）
    title: str             # "在自由区开一家咖啡馆"
    motivation: str        # Text — 与 persona 的关联
    status: str            # "active" | "achieved" | "failed" | "abandoned"
    progress: float        # 0.0-1.0
    milestones_json: list  # [{"title", "done", "note", "at"}]
    created_at / updated_at / resolved_at
```

**逻辑改动：**
1. **生成**：`forge/build_stage.py` 产出角色时附带生成 1 个 life goal（build prompt 加输出字段，不增加调用次数）；存量居民用一次性脚本 `backend/seed/backfill_goals.py` 批量生成（🔥 一次性成本，按居民数预估）
2. **日目标对齐**：`phases/plan/basic.py` 的 daily goal 生成 prompt 注入 `人生目标：{title}（进度 {progress:.0%}，动机：{motivation}）`，要求日目标服务于人生目标；`TickContext` 加 `life_goal: ResidentGoal | None`（perceive 阶段随居民一起载入，避免额外查询——JOIN 或二次 select 一次）
3. **周评估 cron**：并入 A5 的夜间管线（每周日执行）：取该居民本周 `memories`（importance≥0.5，limit 30）→ LLM 输出 JSON `{progress_delta, milestone?, verdict: none|achieved|failed}` → 更新表；`achieved/failed` 时：写 reflection 记忆（importance 0.9）→ 调用 `personality/evolution.py` 关键事件跳变入口 → 发 A4 公告贴 → 触发 E13 结算 → E11 feed 事件
4. **对话可见**：`llm/prompt.py::assemble_system_prompt` 注入当前 life goal 一行，玩家能聊出来

**API**：`GET /residents/{slug}/goals`（公开，返回 active + 最近 resolved 3 条）。

**前端**：`NpcTooltip.tsx` 加目标一行；`components/profile/ResidentList.tsx` 详情弹窗加目标卡片与里程碑时间线。

**LLM 预算**：周评估 = 居民数 × 1 次/周（小模型）；日目标 prompt 增量 ≈ +80 tokens。

**验收**：`tests/test_resident_goals.py` — 周评估 mock LLM 后 progress 正确累积；achieved 路径触发人格跳变与公告贴；对话 prompt 含目标文案。

### A2 世界事件系统

**依赖 S1（含表、cron、注入、admin API）——本功能即 S1 的运营化收尾：**

1. **事件模板库**：`app/tasks/event_templates.py` — 内置节日表（春节/万圣节等按日期自动排期）+ 随机新闻池（每周随机 1-2 条投放，规则驱动零 LLM）
2. **集体记忆**：事件 start 时，event_cron 为**活跃居民**（近 7 天有行为）各写一条 `memories(type="event", source="world_event", importance=0.5, content=事件文案)`——批量 insert，无 LLM
3. **admin 面板**：`frontend/src/components/admin/EventsPanel.tsx`（表格 + 投放表单 + 预览注入文案），挂到 `AdminPage.tsx` tab
4. **前端表现**：`TopNav.tsx` 事件横幅；`GameScene.ts` 收到 `world_event` 后按 `payload_json.ambience` 调整（复用 E6 的渲染通道）

**验收**：投放"丰收节"→ 两个居民自主对话中出现节日话题（prompt 注入生效）；玩家问"最近有什么新鲜事"NPC 能答出。

### A3 居民主动找玩家

**无新表**（复用 S3 礼物 + S4 通知；问候去重用 `memories.metadata_json`）。

**触发点**：`ws/handler.py` 连接初始化完毕处（现 :97 广播 join 之后）追加 `asyncio.create_task(greeting_service.maybe_greet(user_id))`——**不阻塞连接流程**。

**`app/services/greeting_service.py::maybe_greet`：**
1. 开独立 session；查该玩家的强关系居民：`memories WHERE type='relationship' AND related_user_id=:uid ORDER BY importance DESC LIMIT 3`
2. 过滤：24h 冷却（该居民 `metadata_json->>'last_greet_user'` 记录，或直接查当日是否已有 source='greeting' 的记忆）；居民 status 必须 idle
3. 生成问候：**默认模板池**（按 SBTI 外向维度选语气，零成本）；`importance≥0.8` 的挚友关系才用小模型生成一句个性化问候（🔥 限每玩家每天 1 次）
4. 概率礼物：挚友且 7 日未送过 → 从 items 表 gift 类随机 1 件（系统赠送不走 charge），附在问候里
5. 下发：在线 `manager.send({"type": "resident_greeting", "resident_slug", "text", "gift"})`；同时写 S4 通知（离线可见）；给居民写一条低重要度事件记忆"我跟 X 打了招呼"

**前端**：`GameScene.ts` 收到 `resident_greeting` → 该居民头顶冒泡（复用现有 bubble 逻辑，`GameScene.ts:588` 一带）+ `ChatDrawer` 快捷"回应"按钮直接 start_chat。

**验收**：新玩家（无关系）连接不触发；老玩家 24h 内最多 1 次；离线期间问候进通知中心。

### A4 居民创作物 🔥

**现状锚点**：`JOURNAL` ActionType 已存在（`agent/actions.py:20`）；`routers/bulletin.py` 目前只有一个 GET。

**表 `bulletin_posts`（迁移 018）：**

```python
class BulletinPost(Base):
    __tablename__ = "bulletin_posts"
    id: str
    author_resident_id: str | None   # FK residents.id — 居民创作
    author_user_id: str | None       # 运营公告
    kind: str        # "journal" | "poem" | "notice" | "digest" | "clue"
    title: str; content_md: Text
    likes: int; tips_sc: int         # 累计打赏
    pinned: bool
    created_at
```

**逻辑：**
1. `phases/execute/basic.py` 处理 `JOURNAL` 动作时（现只写记忆），按条件升级为"发表创作"：该居民当日未发过 且 `random() < 0.5` → LLM 生成 120 字内短文（体裁按 SBTI：内向→日记/诗，外向→吐槽/告示；prompt 注入今日记忆 top3 作素材）→ 同时写 `bulletin_posts` 与 reflection 记忆
2. 预算：全局每日创作上限（`settings.creation_daily_cap`，默认 20 篇）用 Redis `INCR` 计数
3. 打赏：复用 S3 `POST /shop/purchase {item_code:"tip_5sc", context:{post_id}}` → 效果处理器给 post.tips_sc 累加、创作者居民的 creator 分成 80%（`coin_service.reward`）

**API**：`GET /bulletin/posts?kind=&cursor=`（游标分页）、`POST /bulletin/posts`（require_admin，运营公告）。

**前端**：`BulletinBoard.tsx` 从静态板改为分页 feed：作者头像（portrait_url）+ 体裁标签 + 打赏按钮；居民详情页列其作品。

**验收**：mock LLM 跑 execute 阶段产出帖子且当日第二次 JOURNAL 不再发帖；打赏后余额/分成/tips_sc 三处对账一致。

### A5 村落日报 🔥

**表 `digests`（迁移 019）：** `id, scope("village"|"personal"), date(Date, index), user_id(nullable, personal 用), title, content_md, stats_json, created_at`；`UniqueConstraint(scope, date, user_id)`。

**夜间管线**：新文件 `app/tasks/nightly_cron.py`（每日 00:30，仿 heat_cron；此文件同时承载 A1 周评估、E2 梦境、E7 胶囊投递的调度——**一个 cron 四个职责，按序执行，互相隔离 try/except**）：

1. 取材（纯 SQL，无 LLM）：当日 `memories(source='chat_resident')` 的对话摘要 top10（按 importance）、`personality_history` 当日变更、`world_events` 当日活跃、`bulletin_posts` 当日创作、heat top3 变化
2. 组稿：单次 LLM 调用（系统通道，标准模型）→ 输出 markdown（标题 + 3-5 段小报体，≤600 字）
3. 落库 scope=village + 写一条 `bulletin_posts(kind="digest", pinned=True)`（替换昨日 pinned）+ `manager.broadcast({"type": "digest_ready", "date"})`

**API**：`GET /digest/latest`、`GET /digest?date=`。

**前端**：`TopNav.tsx` 报纸图标（digest_ready 后显示红点）→ 新组件 `components/DigestModal.tsx`（复用 react-markdown 渲染，已在依赖中）。

**LLM 预算**：固定每日 1 次调用（输入 ≈ 2k tokens）。

**验收**：素材为空的冷启动日生成兜底文案不调 LLM；重复执行幂等（unique 约束）。

---

## 3. B 组 — 玩法系统

### B1 委托任务系统 🔥

**表 `commissions`（迁移 020）：**

```python
class Commission(Base):
    __tablename__ = "commissions"
    id: str
    issuer_resident_id: str    # FK residents.id, index — 发布委托的居民
    kind: str                  # "deliver_message" | "chat_topic" | "visit_location"
    title: str                 # "帮我带句话给阿珍"
    payload_json: dict         # deliver: {target_slug, message}；chat_topic: {target_slug, topic, min_turns}；visit: {location_id}
    reward_sc: int             # 10-50，按 kind 定价
    status: str                # "open" | "accepted" | "completed" | "expired"，index
    acceptor_user_id: str | None  # index
    created_at / expires_at(默认 48h) / completed_at
```

**生成（居民侧）：**
- `phases/plan/basic.py` 日计划生成后追加判定：若 daily_goal 含社交/信息类意图且该居民 open 委托 < 1 → 从模板池实例化委托（`deliver_message` 的内容用日目标文案改写，**模板优先，零 LLM**；仅 A1 剧情类委托用小模型生成，🔥）
- 全局上限：open 状态 ≤ `settings.commission_global_cap`（默认 15），防刷屏

**完成判定（三种 kind 各一个处理器，`app/services/commission_service.py`）：**

| kind | 判定点 | 逻辑 |
|------|--------|------|
| deliver_message | `ws/handler.py` end_chat 事件（S2 `chat_completed`） | 对话对象 = target_slug 且玩家消息中含委托关键词（`payload.message` 的分词命中 ≥60%，规则判定；不达标不判失败，继续等） |
| chat_topic | 同上 | 对话对象匹配且 `turns >= min_turns` 且话题词命中；边界情况用一次小模型仲裁（🔥 仅当规则判定为"接近"时） |
| visit_location | S5 `location_first_visit` / on_enter | location_id 匹配即完成 |

**结算**：`commission_service.complete()` — reward_sc 入账 → 双方居民各写一条事件记忆（"X 帮我把话带到了"，importance 0.7，关系加成）→ S4 通知 → `emit("commission_completed")`（喂 D1/E12）。

**API**：`GET /commissions?status=open`（任务板）、`POST /commissions/{id}/accept`（并发点：`UPDATE ... WHERE status='open'` 乐观锁，失败返回 409）、`POST /commissions/{id}/abandon`；过期由 nightly_cron 扫描翻转。

**前端**：`BulletinBoard.tsx` 加"委托板"tab（open 列表 + 我接受的）；`GameScene.ts` 目标居民头顶挂"❗"标记（gameStore 存 activeCommission，渲染层读取）；完成时 toast + 金币动画（复用 `CoinNotification.tsx`）。

**验收**：两玩家并发接同一委托只有一人成功；deliver 委托完整链路（接受→找到目标→对话提及→结算→双居民记忆落库）e2e 测试；过期委托不可接受。

### B2 位置偶遇事件

**依赖 S5。无新表**（冷却记录进程内 dict + Redis 兜底）。

**判定（`location_tracker.on_enter` 回调里）：**
1. 冷却：同一 user+location 1h 内不重复触发；全局每玩家每日 ≤ 5 次
2. 条件：该 location 半径 8 tile 内存在 status ∈ (idle, walking) 的居民（查 `manager` 无居民位置——居民位置在 DB，用 60s 缓存的居民位置快照，agent loop 每轮 tick 后刷新）
3. 概率：`0.3 × (1 + 关系强度加成)`，命中后生成情境开场白：**模板库**按 location × 居民当前 action 组合（如图书馆×STUDY→"她正埋头翻一本旧书"），零 LLM
4. 下发：`manager.send(user_id, {"type": "encounter_prompt", "resident_slug", "location_id", "opener"})`

**接受偶遇**：前端弹卡片，点击后走现有 `start_chat`，携带新可选字段 `context`（`ws/protocol.py::StartChat` 加 `context: str | None`）；`ws/handler.py` start_chat 分支把 context 拼进本次对话的 system prompt（"你们在图书馆偶遇，你正在看书"）。

**前端**：新组件 `components/EncounterCard.tsx`（右下角滑入，10s 自动消失）；gameStore 加 `pendingEncounter` 状态。

**验收**：站着不动不触发（必须 location 变更）；冷却生效；带 context 的对话首条回复能体现场景。

### B3 家园装修

**依赖 S3（decor 类商品）。迁移 021**：`residents` 加列 `home_decor_json: JSON | None` — `[{item_code, x, y, rot}]`（相对住房 bbox 的 tile 偏移）。

**规则**：仅 `resident_type='player'` 的玩家居民可装修自己 `home_location_id` 对应住房；上限 12 件；坐标必须落在该 location bbox 内（服务端校验，bbox 来自 `agent/map_data.py`）。

**API**：`PUT /residents/{slug}/home/decor`（owner 校验：`resident.creator_id == current_user.id`；全量替换写入，校验 item 已购买——查 purchases 聚合 qty ≥ 摆放数）；`GET /residents/{slug}/home/decor`（公开，参观用）。

**前端：**
- `GameScene.ts`：新增 `decorLayer`（depth 在角色层之下）；进入任意住房 bbox 视野时懒加载该居民 decor 并渲染；装修家具精灵图新增 `public/assets/decor/` 图集
- 新组件 `components/DecorEditor.tsx`：进入自家范围时出现"装修"按钮 → 编辑模式（网格高亮 + 拖放 + 撤销），保存调 PUT
- NPC 反应彩蛋：居民路过（perceive 阶段发现新 decor hash 变化）写一条低重要度记忆"X 家新添了一盏灯"，对话中可能提起（免费的惊喜感）

**验收**：越界/未购买/非 owner 三类非法请求 400/403；另一玩家视野内能看到我的装修（广播 `decor_updated` 消息刷新）。

---

## 4. C 组 — 社交与 UGC

### C1 灵魂卡片分享

**无新表、无 LLM。**

**导出/导入 API（`routers/residents.py` 扩展）：**
- `GET /residents/{slug}/card` — 公开 JSON：name、persona 摘要（soul_md 首段）、SBTI 类型、star_rating、portrait_url、总对话数、代表性反思记忆 1 条（importance 最高且非隐私 source）
- `GET /residents/{slug}/export` — owner only：完整 `{name, ability_md, persona_md, soul_md, meta_json.sbti}`，schema_version 字段
- `POST /residents/import` — 登录用户：校验 schema → 走 forge 管线的 **validation_stage**（`app/forge/validation_stage.py` 复用，拦违规人设）→ 创建居民（限额：每用户每日 3 个，Redis 计数）

**卡片图（纯前端）**：新组件 `components/SoulCard.tsx` — `<canvas>` 手绘：像素相框（新增 `public/assets/card-frame.png`）+ portrait + 名字 + SBTI 徽章 + 一句话，`canvas.toBlob()` 下载/分享；入口在居民详情与 `ResidentList.tsx`。

**验收**：导出→导入还原度（SBTI/三档案一致）；违规 persona 被 validation_stage 拦截；日限额生效。

### C2 关系图谱可视化

**无新表。后端聚合端点：**

`GET /graph/relationships?min_importance=0.3`（`routers/graph.py` 新文件）：

```sql
SELECT resident_id, related_resident_id,
       MAX(importance) AS strength,
       (array_agg(content ORDER BY created_at DESC))[1] AS label
FROM memories
WHERE type='relationship' AND related_resident_id IS NOT NULL
GROUP BY resident_id, related_resident_id
HAVING MAX(importance) >= :min_importance
```

返回 `{nodes: [{slug, name, portrait_url, district}], edges: [{a, b, strength, label, mutual}]}`；`mutual` = 双向都有记录。结果模块级缓存 10 分钟（图谱不需要实时）。玩家自己作为节点：related_user_id 聚合同理，仅返回当前用户自己的边（隐私：不暴露其他玩家的关系）。

**前端**：新页面路由 `/graph`（懒加载）：`pages/GraphPage.tsx` + 自研力导向布局（~120 行：斥力+弹簧+阻尼迭代，canvas 渲染，**不引 d3**，节点 <100 个性能无忧）；边宽=strength，hover 显示 label 原文，点击节点跳居民详情。

**验收**：空关系冷启动显示引导文案；100 节点 300 边布局 <16ms/帧；他人隐私边不可见。

### C3 剧本季 🔥

**表（迁移 022，与 E12 共用 seasons）：**

```python
class Season(Base):
    __tablename__ = "seasons"
    id: str; title: str; theme: str      # "神秘失踪案"
    status: str                          # "voting" | "active" | "settled"
    starts_at / ends_at
    payload_json: dict                   # 剧本全局设定，注入所有居民 prompt 的世界观补丁

class SeasonScript(Base):                # 剧本的"幕"
    __tablename__ = "season_scripts"
    id: str; season_id: str              # index
    act: int                             # 第几幕
    trigger_at: datetime                 # 到点自动执行
    event_payload_json: dict             # 展开为一条 world_event + 可选 bulletin_posts(kind="clue") + 指定居民注入私有记忆
    status: str                          # "pending" | "fired"

class Poll(Base):
    __tablename__ = "polls"
    id: str; season_id: str | None; question: str; options_json: list
    closes_at: datetime; status: str     # "open" | "closed"

class Vote(Base):
    __tablename__ = "votes"
    id: str; poll_id: str; user_id: str; option_idx: int
    # UniqueConstraint(poll_id, user_id)
```

**执行链**：event_cron（S1）扩展扫描 `season_scripts.trigger_at` → 每幕执行三件事：
1. 创建对应 `world_event`（全体居民可感知）
2. 发线索公告 `bulletin_posts(kind="clue")`
3. **私有记忆注入**：`event_payload_json.secrets: [{resident_slug, memory_content, importance}]` — 给指定居民写"只有他知道的事"（`memories source="script"`），玩家必须通过对话+关系挖出来 ← **这是玩法核心，复用现有记忆检索，无新机制**

**投票**：`GET /polls/open`、`POST /polls/{id}/vote`；季主题投票 → 运营按结果上架剧本。

**收尾**：ends_at 到点 → 结算 Poll（真相投票）→ A5 日报特刊 → E12 赛季积分结算。

**admin**：`components/admin/SeasonPanel.tsx` — 幕编辑器（时间线视图 + 每幕的事件/线索/密秘注入表单）。

**验收**：一个两幕测试剧本：第 1 幕后指定居民对话中可挖出私有记忆、其他居民不知道；第 2 幕线索贴出现在公告栏；投票唯一性约束生效。

---

## 5. D 组 — 留存与经济

### D1 成就系统

**依赖 S2（引擎已含表/事件/奖励/通知）。本节交付内容 = 首批成就 + 前端。**

**首批 12 个成就（seed）：**

| code | 条件 | 事件源 | reward_sc |
|------|------|--------|-----------|
| first_chat | 首次完成对话 | chat_completed | 20 |
| deep_talk | 单次对话 ≥ 10 轮 | chat_completed(turns) | 30 |
| remembered | 首次被居民写进记忆 | memory_written_about_user | 20 |
| memory_keeper_10 | 被 10 条记忆记住（计数型） | 同上 | 50 |
| soul_shaper | 首次触发居民人格跳变 | personality_shifted | 100 |
| week_streak | 连续登录 7 天 | login_streak | 50 |
| explorer_5 / explorer_all | 到访 5 / 全部 20 个位置 | location_first_visit | 30/100 |
| errand_runner | 完成首个委托 | commission_completed | 30 |
| patron | 首次打赏创作 | purchase(tip) | 10 |
| socialite | 与 10 位不同居民聊过 | chat_completed | 50 |
| dreamt_of | 首次被居民梦到（E2 上线后启用） | dream_generated | 66 |

计数型成就用 `progress_json.count` 累积（引擎在 upsert 时 `count+1` 并对照 target）。

**前端**：`ProfilePage.tsx` 加 achievements tab（网格徽章墙，未解锁灰显，hidden 成就显示"???"）；解锁 toast 复用 S4 notification 分支 + 专属动画组件 `components/AchievementToast.tsx`。

**验收**：12 个成就各一条 pytest（伪造事件 → 断言解锁/进度/到账）；徽章墙灰显与进度条正确。

### D2 Soul Coin 消耗场景

**依赖 S3。本节交付 = 首批商品 + 效果处理器（`app/services/shop_effects.py` 注册表）：**

| item_code | 价格 | 效果处理器逻辑 |
|-----------|------|---------------|
| rename_card | 50 | context.new_name → 校验敏感词 → 改 `residents.name`（owner 的玩家居民）→ 广播刷新 |
| portrait_redraw | 80 | 调 `services/portrait_service.py` 重绘（异步任务，完成走 S4 通知）；**成本对冲**：80 SC ≈ 2× 图像 API 成本 |
| gift_flower / gift_book / gift_snack | 15/25/10 | context.resident_slug → 给居民写事件记忆（"X 送了我一束花"，importance 0.75，关系加成 payload.relationship_boost）→ 居民下次问候必提及；创作者分成 20% |
| tip_5sc / tip_20sc | 5/20 | A4 打赏（见 A4） |
| capsule_ticket | 10 | E7 时间胶囊配额 +1 |
| commission_boost | 20 | B1 我接受的委托奖励 ×1.5（单次） |
| decor_lamp / decor_plant / decor_rug ... | 30-60 | B3 家具（purchase 即入库存，效果处理器空操作） |

**经济仪表盘扩展**：`components/admin/EconomyPanel.tsx`（已存在，458 行）加"发行/回收"日曲线（transactions 按 reason 前缀聚合正负），通胀率 = 7 日净发行/流通量——给运营调价依据。

**验收**：每个效果处理器一条测试；改名卡敏感词拦截；礼物记忆在下次对话 prompt 中出现。

### D3 每日循环强化

**迁移 028**：`users` 加 `login_streak: int default 0`、`last_login_date: Date | None`；新表 `daily_quests`：`id, user_id(index), date, quest_json({resident_slug, topic, min_turns}), status("pending"|"done"), reward_sc`；`UniqueConstraint(user_id, date)`。

**逻辑：**
1. **连续签到**：`services/daily_reward_service.py::claim_daily_reward` 扩展——比较 `last_login_date`：连续则 streak+1，中断归 1；奖励阶梯 `[10,15,20,25,30,40,50]`（第 7 天封顶，之后循环第 7 档）；`emit("login_streak")` 喂成就
2. **每日话题**：首次登录时生成（`ws/handler.py` 连接流程里 daily reward 之后）：从玩家 top 关系居民 + 1 个随机低热度居民（**给长尾角色导流**）中选一位；话题来源优先级：该居民 active goal > 昨日日报关键词 > 模板池——**全规则，零 LLM**
3. **完成判定**：S2 `chat_completed` 处理器：resident 匹配且 turns ≥ min_turns → done → reward + 通知

**API**：`GET /daily/quest`（当日任务与状态）。
**前端**：`TopNav.tsx` 签到弹窗改造（streak 进度条 + 明日预告）；每日话题卡片入口。

**验收**：跨日连续/中断/封顶三个 streak 用例；话题任务完成链路；同日幂等。

### D4 创作者仪表盘

**无新表（纯聚合）。**

**API `GET /creator/stats`（`routers/profile.py` 扩展，聚合近 30 天）：**

```sql
-- 每居民每日对话数
SELECT resident_id, DATE(started_at) d, COUNT(*) FROM conversations
WHERE resident_id IN (SELECT id FROM residents WHERE creator_id=:uid)
GROUP BY resident_id, d
-- 评分趋势：AVG(rating) 按周分桶
-- SC 收益：transactions WHERE reason LIKE 'creator_passive%' OR reason LIKE 'purchase:tip%' 按日求和
-- 记忆足迹：memories 中提及该创作者居民的计数
```

单端点返回全部序列（数据量小），加 `Cache-Control: max-age=300`。

**前端**：`ProfilePage.tsx` 加 creator tab：`components/profile/CreatorDashboard.tsx` — 手绘 SVG 折线/柱状（~80 行工具函数 `utils/sparkline.ts`，**不引图表库**，与 C2 同理保持零依赖）；各居民卡片：对话量、评分、收益、被记住次数，附"优化建议"静态规则文案（如评分 <3.5 → 提示调整 persona 冲突项）。

**验收**：无创作居民的用户显示引导态；聚合口径与 transactions 明细对账一致。

---

## 6. E 组 — 候选池

### E1 居民情绪引擎

**迁移 023**：`residents` 加 `mood_json: JSON | None` — `{"valence": -1.0~1.0, "arousal": 0~1.0, "label": "content", "updated_at": iso}`。

**更新规则（纯规则，零 LLM，`app/services/mood_service.py`）：**

| 触发 | 位置 | 变化 |
|------|------|------|
| 居民对话结束 mood=positive/negative | `agent/chat.py` 结果处理处 | valence ±0.2，arousal +0.1 |
| 玩家对话 rating ≥4 / ≤2 | ws handler rate_chat 分支 | valence ±0.15 |
| 收到礼物（D2）/ 委托完成（B1）/ 目标里程碑（A1） | 各效果处理器 | valence +0.2~0.3 |
| 每轮 tick 衰减 | `agent/loop.py::_tick_round` 末尾批量 | 向 0 回归 5%（`UPDATE` 一条 SQL 全体处理，勿逐行） |

label 由 (valence, arousal) 四象限映射 8 词（excited/content/calm/tired/gloomy/anxious/annoyed/furious）。

**消费点：**
1. `phases/decide/basic.py` prompt 加一行 `当前心情：{label}`；且行为候选加权：valence<-0.4 时 `GO_HOME/JOURNAL/NAP` 提示优先，>0.5 时社交类优先（改 prompt 提示而非硬过滤，保持 LLM 自主性）
2. `llm/prompt.py` 玩家对话注入心情行 → 语气随之变化
3. 前端：`resident_status` 广播扩展 `mood_label` 字段；`NpcTooltip.tsx` 显示心情 emoji；`StatusVisuals.ts` 按心情微调头顶状态图标

**验收**：送礼后 label 上移一档；48h 无事件回归 calm；决策分布统计上可见偏移（测试用固定随机种子跑 100 tick 断言分布）。

### E2 梦境系统 🔥

**无迁移**：复用 `memories`，新 type 值 `"dream"`（type 是 String(20)，无枚举约束）。

**生成（挂 A5 nightly_cron 第 2 步）：**
1. 选人：当日有 ≥3 条新记忆的居民（活跃者才做梦，天然限预算）且 `random() < 0.5`，每晚全局上限 10 个（Redis 计数）
2. 素材：该居民当日 importance top3 记忆 + 1 条随机旧记忆（`ORDER BY random()` 限 importance≥0.6）——"旧事入梦"的错位感是趣味来源
3. LLM（小模型）：`按人格 {sbti} 把素材揉成一段 80 字内的梦，允许荒诞混搭，第一人称` → 写 `memories(type="dream", source="reflection", importance=0.4, metadata_json={"date", "involves_user_id?"})`
4. 若素材含玩家相关记忆 → `emit("dream_generated", user_id)`（喂成就 dreamt_of）+ S4 通知"有人梦到了你"

**消费**：`llm/prompt.py` 对话 prompt 注入最近一条 dream（24h 内）："昨晚你做了个梦：{content}"——玩家问"睡得好吗"会得到惊喜回答；日报（A5）随机引用一条梦增加文学感。

**验收**：不活跃居民不做梦；全局上限生效；通知只发给被梦到的玩家。

### E3 八卦与谣言传播 🔥

**现状锚点**：`GOSSIP` ActionType 已存在（`agent/actions.py:10`）。**无迁移**：传播链用 `memories.metadata_json`。

**机制（改 `agent/chat.py`，居民对话收尾处）：**
1. 对话结束后 `random() < 0.3` 触发一次"信息交接"：从说话方记忆中选 1 条 `type='event' AND importance≥0.6 AND related_resident_id IS NOT NULL AND related_resident_id != listener.id`（关于第三者的事）
2. 失真：`hops = origin.metadata_json.hops + 1`；失真概率 `min(0.2 × hops, 0.8)`——命中则 LLM 小模型改写（"夸大或改错一个细节，保留主干"），未命中原样转述（省调用）
3. 写入听者：`memories(type="event", source="gossip", importance=origin×0.8, related_resident_id=第三者, metadata_json={origin_memory_id, hops, distorted: bool})`
4. **对质玩法（自动涌现）**：第三者居民的对话检索天然会召回这条 gossip 记忆 → 玩家告诉他"听说你……" → 他否认 → 玩家可回溯谣言链（管理端可视化）

**风控**：gossip 记忆 importance 上限 0.7（谣言不该压过亲历）；hops ≥ 4 不再传播（衰减终止）。

**admin**：`ForgeMonitorPanel` 旁加谣言链查询（按 origin_memory_id 树状展示，SQL 递归 CTE 或应用层循环，深度 ≤4 无压力）。

**验收**：三居民 A→B→C 两跳传播链 metadata 完整；hops=4 终止；失真版与原版可通过 origin_memory_id 关联对照。

### E4 目击记忆

**依赖 S5。无迁移**（复用 memories，source 新值 `"witness"`）。

**机制（挂 `phases/perceive/basic.py`，纯规则零 LLM）：**
1. perceive 已扫描半径 10 tile 内居民；扩展同时读取**在线玩家位置快照**（来源：`ws/manager.py::positions`——注意 P0-3 拆 Agent Worker 后此快照改从 Redis 读，spec 按 Redis 版设计：manager 的 `update_position` 同步 `SETEX player_pos:{user_id}`，worker 侧 `MGET`）
2. 半径内有玩家 → 去重（同一居民对同一玩家每 4h 最多 1 条，进程内 LRU）→ 写 `memories(type="event", source="witness", importance=0.25, related_user_id, content="在{location_name}看到{player_name}{情境短语}")`；情境短语模板：路过/和别人聊天（该玩家 status）/在我家附近（居民 home bbox 命中）
3. 每居民 witness 记忆存量上限 20 条（写入前 `DELETE` 最旧的，防膨胀）

**消费**：对话检索自然召回 →"昨天看到你去图书馆了"；A3 问候语可引用最近一条 witness（"好久不见，前天还看到你在广场"）。

**验收**：玩家路过居民身边 → 4h 内该居民仅 1 条 witness；存量上限触发裁剪；离线玩家不产生记忆。

### E5 居民语音（TTS）🔥

**无迁移**。配置（`config.py` 追加）：`tts_base_url / tts_api_key / tts_model / tts_daily_free_quota: int = 30`。

**音色映射**：`app/services/tts_service.py` 内置 `SBTI 维度 → 音色参数` 表（外向度→语速/音调，情感维→风格），映射到 TTS 提供商的 voice 预设名；居民可在 `meta_json.voice` 覆写。

**API `POST /tts {resident_slug, text}`（≤300 字）：**
1. 限额：Redis `INCR tts:{user_id}:{date}`，超 `tts_daily_free_quota` 返回 429（或消耗 D2 商品 `tts_pack`）
2. 缓存：`sha256(voice + text)` → `backend/static/tts/{hash}.mp3` 命中直接返回（重复台词如问候语命中率高）
3. 未命中 → httpx 调 TTS 端点（**复用 P1-2 的共享 client**）→ 落文件 → 返回 `{url, duration}`

**前端**：`ChatDrawer.tsx` 每条居民消息加 🔊（点击才合成，不自动播放——控成本）；播放态用单例 `Audio` 防叠音。

**验收**：同文本二次请求走缓存（无外呼，测试断言 mock 调用次数）；限额 429；不同 SBTI 居民 voice 参数不同。

### E6 天气与季节

**依赖 S1（weather 是 world_event 的一个 type）。无新迁移。**

**天气机（`app/tasks/event_cron.py` 扩展，纯规则）：**
- 状态机：`sunny ↔ cloudy ↔ rain → storm`（+冬季 `snow`），每 2-6h 按转移矩阵抽签；季节由真实月份映射，影响转移概率与文案
- 产出：写 `world_events(type="weather", payload_json={"kind","intensity"}, starts_at=now, ends_at=+抽签时长)`，自动经 S1 管线广播/注入

**行为影响**：`agent/scheduler.py::build_schedule` 接受可选 `weather` 参数：rain/storm 时室外行为（WANDER/VISIT_DISTRICT）在 decide prompt 中标注"（下雨，不太想出门）"；`agent/loop.py::_tick_round` 取当前天气一次传入。

**前端渲染（`GameScene.ts`）：**
- `world_event(type=weather)` → `applyWeather(kind, intensity)`：rain/snow 用 Phaser ParticleEmitter（预算 ≤200 粒子）；cloudy 全屏半透明 tint 层；storm 加周期性闪白
- 记得在 scene shutdown 时销毁 emitter（对齐 OPTIMIZATION_PLAN 的泄漏审计）

**对话**：S1 注入已覆盖（"当前世界事件：暴雨"），NPC 自然聊天气。

**验收**：转移矩阵单测（10k 次抽样分布）；雨天居民外出行为占比统计下降；粒子层在场景切换后无残留。

### E7 时间胶囊信件

**表 `time_capsules`（迁移 024）：** `id, user_id(index), carrier_resident_slug, deliver_on(Date, index), content(Text ≤500), resident_note(Text|None), status("sealed"|"delivered"), created_at, delivered_at`。

**写入**：`POST /capsules {carrier_resident_slug, deliver_on(3天~1年), content}` — 消耗 1 张 `capsule_ticket`（D2 商品，10 SC）或首个免费；给承运居民写事件记忆"X 托我保管一封信"（importance 0.6）。

**投递（nightly_cron 第 3 步）**：扫 `deliver_on <= today AND status='sealed'`：
1. 居民附言：模板池（按 SBTI + 与玩家关系强度选一句，如"这封信我守了 200 天，一个字都没偷看……好吧我看了一眼"）——**默认零 LLM**；挚友关系才用小模型写个性化附言 🔥
2. S4 通知（kind=capsule_delivered，payload 含信件全文 + 附言）+ 若在线，承运居民走 A3 问候通道当面"递信"
3. 双方记忆落库（居民："今天把信交给了 X"）

**前端**：写信入口在 `ChatDrawer.tsx` 菜单（与该居民对话中"托付信件"）；新组件 `components/CapsuleComposer.tsx`（日期选择器限制范围）；收信是全屏信纸样式弹窗。

**验收**：到期日投递恰好一次（status 翻转幂等）；提前查询不泄露内容（sealed 状态 API 不返回 content 给非本人）。

### E8 探索图鉴

**依赖 S5（location_visits 已建）+ S2（成就）。无新迁移。**

**位置典故**：`agent/map_data.py` 的 20 个位置定义扩展 `lore: str` 字段（手写文案，一次性内容工作）+ `hidden_spot: {tile, hint} | None`（每位置 1 个彩蛋点）。

**逻辑：**
1. 首访任意位置 → S4 通知展示该地 lore + 图鉴进度 `n/20` → `location_first_visit` 事件已喂 explorer 成就（D1）
2. 彩蛋点：S5 on_move 精确命中 `hidden_spot.tile` → `location_visits` 表复用（location_id 加后缀 `:secret`）→ 奖励 5 SC + 图鉴亮星
3. 图鉴数据 `GET /exploration/me`：20 位置 × {visited, secret_found, visit_count}

**前端**：`components/minimap/` 扩展：未访问位置在小地图上呈剪影；新组件 `components/ExplorationCodex.tsx`（图鉴册翻页样式，入口在 TopNav）；首访 lore 弹卡（复用 EncounterCard 样式）。

**验收**：20 位置 lore 文案齐备；彩蛋精确 tile 命中判定；图鉴进度与 visits 表一致。

### E9 居民辩论擂台 🔥

**表（迁移 025）：**

```python
class Debate(Base):
    __tablename__ = "debates"
    id: str
    topic: str                    # "菜市场该不该通宵营业"
    resident_a_slug / resident_b_slug: str   # 选 SBTI 对立维度的两位
    status: str                   # "announced"(押注开放) | "live" | "voting" | "settled"
    transcript_json: list         # [{speaker_slug, text, round}]
    winner: str | None            # "a" | "b" | "draw"
    pool_a / pool_b: int          # 押注池
    starts_at / settled_at

class DebateStake(Base):
    __tablename__ = "debate_stakes"
    id: str; debate_id: str(index); user_id: str; side: str; amount: int  # 10-200 上限
    payout: int | None
    # UniqueConstraint(debate_id, user_id)
```

**流程（每周五晚，nightly_cron 排期 + event_cron 触发）：**
1. **周三 announced**：选题（近期 gossip 高频词 / 世界事件派生 / 运营指定）；选手 = 该话题相关记忆最多且 SBTI 对立的两位；公告栏预告 + 押注开放（`POST /debates/{id}/stake {side, amount}`，charge 入池）
2. **周五 live**：改造 `agent/chat.py` 派生 `debate_chat()`：立场 prompt（各自注入"你坚决支持 X 方，用你的记忆举例"）× 6 轮 × 120 字；**每轮实时广播** `{"type": "debate_turn", "debate_id", "speaker", "text", "round"}` —— 全服围观是关键体验；期间两居民 status="debating"
3. **voting（24h）**：`POST /debates/{id}/vote {side}`（免费，与押注独立；押注者自动计入己方票）
4. **settled**：多数票胜 → 胜方按注额比例瓜分败方池的 95%（5% burn，通缩）；`coin_service.reward(reason="debate_win")`；平票全额退；双居民各写辩论记忆 + 胜者 valence↑（E1）

**前端**：新页面 `/debates`（懒加载）：直播视图（两侧立绘 + 弹幕式轮次滚动）+ 押注/投票面板 + 历史战绩。

**风控**：押注上限 200 SC/人/场；居民选手每月最多上场 2 次（防人设疲劳）。

**验收**：结算数学（含 burn、平票退款）property-based 测试；live 中断（LLM 失败）自动 draw 全退；押注并发唯一约束。

### E10 合影纪念

**无迁移**（产物走现有 media 上传管线）。

**前端主导（`GameScene.ts` + 新组件 `components/PhotoBooth.tsx`）：**
1. 与居民对话中点"合影" → GameScene 将镜头对准双方、隐藏 UI 层 → `this.game.renderer.snapshot(callback)` 取帧
2. Canvas 合成：像素相框（`public/assets/photo-frame.png`）+ 日期戳 + 居民"签名"（居民名 + 一句话，从其最近 reflection 记忆截取或模板）
3. 产物：本地下载 + 可选上传（复用 `routers/media.py` 上传端点，kind 沿用 image；**上传前压缩到 ≤500KB**）

**后端一小步**：`POST /photos/log {resident_slug, media_url?}` → 给居民写事件记忆"和 X 合了影"（importance 0.5，关系加成）→ 返回居民即兴一句话（模板池，按心情 E1 选）。

**验收**：截图不含 UI 元素；签名文案随居民变化；记忆落库。

### E11 关注与动态流

**表（迁移 026）：**

```python
class Follow(Base):
    __tablename__ = "follows"
    id: str; user_id: str(index); resident_slug: str(index); created_at
    # UniqueConstraint(user_id, resident_slug)

class FeedEvent(Base):
    __tablename__ = "feed_events"
    id: str; resident_slug: str(index); kind: str
    # kind: "goal_achieved" | "goal_milestone" | "personality_shift" | "creation" | "debate" | "mood_swing"
    payload_json: dict; created_at(index)
```

**写入方**（各功能完成时顺手一行，`app/services/feed_service.py::push(resident_slug, kind, payload)`）：A1 目标里程碑/达成、A4 发表创作、E9 辩论出场/获胜、personality_evolution 跳变、E1 情绪剧烈波动（|Δvalence|>0.5）。

**读取**：`GET /feed?cursor=` — JOIN follows 过滤当前用户关注的居民，倒序游标分页；在线时实时推送：feed_service.push 内查关注者交集 `manager.active` → `manager.send({"type": "feed_event", ...})`。

**关注**：`POST/DELETE /follows/{resident_slug}`；上限 50。

**前端**：居民详情/Tooltip 加关注按钮；`ProfilePage` 加动态 tab（`components/profile/FeedList.tsx`，事件卡片按 kind 配图标文案）；TopNav 铃铛与 S4 通知合流（feed 归入 kind=feed 通知，仅红点不写库重复）。

**验收**：未关注不推送；游标分页稳定（同 created_at 用 id 二级排序）；取关后历史不再出现。

### E12 赛季排行与徽章

**依赖 S2 + C3 的 seasons 表。迁移 022 中已含 `season_scores`：** `id, season_id(index), user_id(index), points, breakdown_json({chat: 120, explore: 40, ...}), updated_at`；`UniqueConstraint(season_id, user_id)`。

**积分规则（事件驱动，S2 bus 上挂一个 `season_scorer` 处理器统一累计）：**

| 事件 | 积分 |
|------|------|
| chat_completed（每日前 5 次计分） | 5 |
| commission_completed | 15 |
| location_first_visit / secret | 10 / 20 |
| achievement unlocked | achievement.points |
| debate 押中 / 剧本季投票命中真相 | 20 / 30 |

每日计分上限 100（Redis 计数），防刷。

**结算（season ends_at，event_cron）**：榜单快照进 `seasons.payload_json.final_ranks`；Top10/Top100 发放赛季限定成就（S2 动态注册 `season_{id}_top10` 徽章）+ SC 奖池；`season_scores` 保留供历史查询。

**API**：`GET /seasons/current/leaderboard?around_me=true`（前 50 + 我的邻位 ±2，两段查询）。

**前端**：`components/SeasonLeaderboard.tsx`（TopNav 入口，倒计时 + 榜单 + 我的积分构成雷达/条形）。

**验收**：每日上限截断；结算幂等（重跑不重复发奖，按 payload_json.settled 标记）；around_me 分页数学正确。

### E13 居民养成投资

**依赖 A1（resident_goals）。表 `goal_investments`（迁移 027）：** `id, goal_id(FK resident_goals.id, index), user_id(index), amount(50-500), status("active"|"paid"|"refunded"), payout, created_at, settled_at`；每目标总额上限 2000 SC（超出 400）。

**投资**：`POST /goals/{goal_id}/invest {amount}` — 仅 kind="life" 且 status="active" 的目标可投；charge 入池 → 居民写高价值记忆"X 资助了我的梦想"（importance 0.85，关系强加成）→ S4 通知创作者。

**结算（挂 A1 周评估的 verdict 分支）：**
- `achieved` → 每笔 payout = amount × 1.5（reward，reason="goal_dividend"）+ 专属纪念记忆（"没有 X 就没有今天的咖啡馆"）+ 投资人限定成就
- `failed` → 退 50%（reason="goal_refund"），居民写愧疚记忆 → 下次对话会道歉（自然涌现）
- `abandoned`（运营干预/居民删除）→ 全额退

**经济平衡**：1.5 倍派息的净增发由"目标达成率"控制（A1 评估 prompt 保持严格：预期达成率 ~40% → 系统净发行 ≈ 投资额 × (1.5×0.4 + 0.5×0.6 − 1) = **-10%，实为回收**）；EconomyPanel 加投资池监控。

**验收**：三种结算路径 SC 对账；上限拦截；achieved 后所有投资人收到纪念记忆（对话可验证）。

### E14 每周个人回顾 🔥

**复用 `digests` 表（迁移 019）**：scope="personal"，user_id 非空，date=周日。

**懒生成策略（控成本核心）**：**不进 cron**。`GET /digest/weekly/me` 时：
1. 查本周（上周日~昨日）是否已有记录 → 有则直接返回
2. 无 → 聚合素材（纯 SQL）：conversations 数与轮次、被写入的 memories 计数与摘录 top3、成就解锁、探索增量、SC 收支；**素材不足（<2 次对话）返回"本周太安静了"兜底，不调 LLM**
3. 达标 → 单次 LLM（标准模型）生成 ≤400 字第二人称回顾（"X 把你写进了 3 条记忆，其中一条是……你的人格标签本周是『夜行诗人』"）→ 落库缓存

**人格标签**：从玩家行为向量（聊天时段分布/对象多样性/探索度）规则映射 12 个预设标签，LLM 只负责文案润色——标签本身可复现。

**前端**：ProfilePage 顶部周日后横幅"你的本周回顾已就绪"；`components/WeeklyRecap.tsx` 卡片翻页样式，尾页一键分享（复用 C1 卡片渲染管线）。

**验收**：同周重复请求仅 1 次 LLM 调用（mock 断言）；素材不足走兜底；跨周边界（周日 00:00）归属正确。

---

## 7. 落地顺序与依赖图

```
共享基建:  S1(事件) ─┬─→ A2(世界事件) ─→ E6(天气) ─→ C3(剧本季) ←─ 022(seasons)
                     │
S2(成就引擎) ─┬─→ D1(成就) ─→ E12(赛季榜) ←──────────┘
              └─→ D3(每日循环)
S3(商店) ──┬─→ D2(消耗场景) ─→ B3(装修)
           └─→ A3(问候礼物)   E7(胶囊票)
S4(通知) ──→ A3 / D1 / E7 / E11
S5(位置) ──┬─→ B2(偶遇) ─→ E8(图鉴)
           └─→ E4(目击)
A1(人生目标) ──┬─→ B1(委托·剧情类)
               └─→ E13(投资)
A5(夜间cron) ──→ E2(梦境) / E7(投递) / A1(周评估) / E14(共用 digests 表)
E1(情绪) ──→ E9(辩论·情绪反馈) / E10(合影签名)
独立可先行: C1 / C2 / D4 / E5 / E10 / E3
```

**建议施工序（与 OPTIMIZATION_PLAN 第八节批次对齐）：**

1. **底座周**：S1→S5 全部共享基建 + 迁移 012-016（~5 人日，全程可并行两人）
2. **第一批**：A3 → A5 → D2 → D1（问候/日报/消耗/成就）
3. **第二批**：A1 → B1 → B2 → D3 → E1 → E4（目标/委托/偶遇/每日/情绪/目击）
4. **第三批**：C1 → C2 → A2 → A4 → E8 → E7
5. **第四批**：E2 → E3 → E14 → E11 → E10 → E5
6. **赛季装配**：022 迁移 → E12 → C3 → E9 → E13（这五个构成一个完整赛季内容包，建议作为一次版本发布）

**每批完成定义（DoD）**：迁移可升可降（downgrade 实测）、pytest 新增用例全绿、`npm run build + tsc --noEmit` 通过、LLM 调用点全部过预算熔断器、新 WS 消息在 `ws/protocol.py` 有 Pydantic 模型、admin 可观测（至少日志含 feature 标签）。

## 8. 新增文件总清单

**后端**：`app/events/{bus,achievements}.py`、`app/services/{shop_service,shop_effects,notification_service,location_tracker,greeting_service,commission_service,mood_service,tts_service,feed_service}.py`、`app/tasks/{event_cron,nightly_cron,event_templates}.py`、`app/routers/{shop,notifications,graph,commissions,debates,capsules,follows,seasons,tts,exploration}.py`、`app/routers/admin/{events,items,seasons}.py`、`alembic/versions/012-028`、`seed/{achievements,backfill_goals}.py`

**前端**：`components/{NotificationDrawer,EncounterCard,DecorEditor,SoulCard,DigestModal,AchievementToast,CapsuleComposer,ExplorationCodex,PhotoBooth,SeasonLeaderboard,WeeklyRecap}.tsx`、`components/profile/{CreatorDashboard,FeedList}.tsx`、`components/admin/{EventsPanel,SeasonPanel}.tsx`、`pages/{GraphPage,DebatesPage}.tsx`（懒加载）、`utils/sparkline.ts`、`public/assets/{decor/,card-frame.png,photo-frame.png}`

> 注意：每个新 router 记得在 `app/main.py` 注册；每个新模型在 lifespan 的模型 import 列表加一行（或按 OPTIMIZATION_PLAN P0-6 改为纯 Alembic 后删除该机制）。
