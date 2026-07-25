# Kickoff S1-1 — 公共声誉轴(public reputation axis)

> **本模块**:nightly 从八卦 + 目击信号聚合每位居民一个标量 `reputation`;消费端为**赊账额度**、**选人权重 / 投票信任**、(后续)八卦可信度仲裁。对应 `docs/SOCIETY_EXPANSION_PLAN.md` §2 表 S1-1 行(:33)、§6 接口面预告 S1-1/2 声誉行(:215),依赖 §2 依赖链 S0-1(:68)。格式照 `docs/KICKOFF_PROMPT_REALISM_P2.md`。

## 结论先行(三条硬事实,决定本模块 v1 的形态)

1. **规格里的两个输入源都有现状缺口,不能"读一个已存在的字段"**。八卦 Memory **没有** `情绪基调 / sentiment / tone / valence` 字段(`backend/app/models/memory.py:53-85`、`backend/app/services/gossip_service.py:49-141`);"目击"记录的是**居民看玩家**、中性、无褒贬(`backend/app/services/witness_service.py:59-110`)。因此 nightly 聚合必须**纯规则派生**语气(读 importance / distorted / hops / 主体 mood valence),**零新增 LLM 调用**;不得假装读一个不存在的字段。
2. **v1 零迁移**:声誉标量落 `Resident.meta_json['reputation']` 命名空间(`backend/app/models/resident.py:30-84`),复用 `mayor` / `circle_id` 同款约定,**不加列、不加表**。这是本模块相对 S1-3 / S1-5 / S2-5 的协调优势(它们都要加 041/0XX 迁移 → 多头)。若日后消费端需要 SQL 侧排序再升列(占位符 NNN,见 §3)。
3. **"赊账被拒"是 greenfield**:全代码 grep `赊账|credit|IOU|owe|arrears` 为空(`backend/app/services/coin_service.py:16-256` 只有即时 charge/hold/settle/transfer)。v1 交付的是**声誉信用判定原语 + 一个门控回落的 hold_pending 守卫接口**,真正的赊账机制不在本模块范围内(如实记为缺口,不编赊账流水线)。

---

## 1. 现状锚点(逐文件逐行,仅用已核实 file:line)

### 1.1 声誉的两个"输入源" —— 都不是现成字段

- **八卦写路径**:`backend/app/services/gossip_service.py:49-141` `maybe_gossip(db, speaker, listener, rng)`。八卦落成一条 `Memory`,`source="gossip"`,只带 `content(str)` / `importance(float, 上限 IMPORTANCE_CAP=0.7,每跳 ×0.8 衰减,:113)` / `related_resident_id(被议论主体)` / `metadata_json = {origin_memory_id, hops, distorted, event_id?}`(:117-121)。**全程无任何 sentiment / tone / valence / polarity 属性。**
- **八卦唯一的情绪触点**:`backend/app/services/gossip_service.py:129-139`。当 `new_hops>=2` 且主体是真实居民时,回写**主体本人**的 mood:`apply_mood_event_by_id(db, origin.related_resident_id, settings.realism_gossip_victim_valence, settings.realism_gossip_victim_arousal)`。valence/arousal 落在**被议论者的 `mood_json`**,不在八卦记忆上 —— 也就是说现有模型已把"被八卦"当作**负向**信号处理(victim_valence 为负),但这是主体的整体 mood,不是逐条八卦的语气。
- **Memory 模型**:`backend/app/models/memory.py:53-85` `class Memory`。列:id, resident_id, type(`'event'|'relationship'|'reflection'`), content, importance(Float 默认 0.5), source(String20:`'chat_player'|'chat_resident'|'observation'|'reflection'|'media'|'gossip'|'witness'`), related_resident_id, related_user_id, media_*, embedding, metadata_json(JSON), created_at, last_accessed_at, archived_at。**无 sentiment/valence/tone/polarity 列。**
- **目击机制**:`backend/app/services/witness_service.py:59-110` `record_witnesses(resident_id, tile_x, tile_y, home_location_id)`。为**居民**记录其看到的**在线玩家**:`Memory(resident_id, type='event', content=f'在{loc}看到{player}{phrase}', importance=0.25, source='witness', related_user_id=uid)`(4h Redis 去重、半径 10 tile、每居民上限 20;并 bump 关系熟悉度 :99-109)。**方向是"居民→玩家",中性,无褒贬;不存在"居民目击居民行为"的记录器。**

> **缺口 A(必须如实写进 plan)**:声誉规格假设的"八卦情绪基调"与"目击行为"两个输入,前者**字段不存在**(只能派生)、后者**方向不对**(是居民看玩家,不是居民互相目击品行)。v1 只能用**八卦派生语气 + 主体 mood 趋势**;"居民品行目击"这条输入要等 S1-2 越轨-制裁链(§2:34)补上违规事件→witness→八卦的居民侧链路后才有真数据。

### 1.2 三个消费端的现状挂点

- **赊账 / 信用**:`backend/app/services/coin_service.py:83-119` `hold_pending / hold`。`hold_pending` 用原子守卫扣款:`UPDATE users SET balance=balance-amount WHERE id=:u AND soul_coin_balance >= amount`(:94-98),`rowcount==0` → 余额不足 → 返回 None(:100-103)。这是最接近"信用判定"的地方,但**纯余额检查,无任何声誉/拒贷逻辑**。整模块 `backend/app/services/coin_service.py:16-256` 只有即时 charge/hold/settle/refund/reward/treasury_*,**无赊账/欠款/信用额度概念**。
- **选人权重 / 投票信任**:`backend/app/services/civic_service.py:180-227` `_npc_choice(db, resident, poll, opts, relation_service, by_slug)`。NPC 投票打分**仅**由三项构成:SBTI A2 保守(维持现状倾向,:199-203)、义务/经济利益(:204-208)、与提案人关系亲和微调 `scores[0] += 1.5 * pair.affinity`(:216-222);**无任何声誉项**。胜者 argmax + 索引平票(:226)。
- **候选人权重**:`backend/app/services/election_service.py:32-63` `open_election(db, candidate_slugs, days)`。镇长候选默认取 SBTI Ac1=H 或 So1=H(:47-50),兜底取 heat 前三(:52),上限 4(:53)。**无声誉筛选/排序。**
- **mayor 如何存(读时不要碰)**:`backend/app/services/election_service.py:127-172` `install_mayor(db, slug)` —— 双存储:`Resident.meta_json['mayor']=True`(`flag_modified`,:138-149)+ `ConfigService.set('current_mayor', slug, group='civic', updated_by='election')`(:154-158)。声誉走**另一个** meta_json 命名空间,不动 mayor 存储。

### 1.3 nightly 挂点 / 批量写范式 / 存储先例

- **nightly 挂点**:`backend/app/tasks/nightly_cron.py:28-230` `run_nightly_jobs()`。每个 job 是独立 try/except(单 job 失败不阻断其它);已有先例:arc `evaluate_arcs`(:76-84)、civic `close_due_polls`/`run_npc_voting`(:86-126)、relation `decay`(仅周一、门控 `realism_relations_enabled`,:204-217)、circle `refresh_circles`(每日、门控,:219-229)。声誉聚合新增一个 try/except 调 `reputation_service.recompute(db)`,门控 `settings.rep_enabled`。cron 每日 00:30 触发(RUN_HOUR=0/RUN_MINUTE=30,`:343-347`)。
- **单 process 保证**:`backend/app/main.py:85-93` —— nightly loop 仅在 `settings.run_background_tasks` 时注册进主 API,否则由 agent-worker 独占。所以聚合**恰在一个进程跑,不会双跑**(也意味着 nightly 是单写者,见 §4)。
- **JSON 列批量写范式**:`backend/app/services/mood_service.py:83-91` `decay_all` —— 批量取带 `mood_json` 的居民、在 Python 里改、`flag_modified` 回写,因为 JSON 列**无法用一条可移植 SQL 更新**。同款约束适用于 meta_json 声誉写。另 `backend/app/tasks/nightly_cron.py:233-246` `run_memory_eviction` 是"一次遍历居民"的先例。
- **每人存储先例**:`backend/app/models/resident.py:30-84` —— `meta_json` 是松类型每特性命名空间(sbti/lab/duty/mayor/circle_id),**声誉标量的天然家,零迁移**;`heat`(Integer,:15)+ pinned/display_heat(:82-84)是已有的每人人气标量,带 pin/decay 范式可参照;`mood_json`(:69)存 `{valence,arousal,label}`。
- **批量读范式(读路径性能)**:`backend/app/services/relation_service.py:170-188` `relations_for` —— 一条查询批量取一方全部关系。声誉的 `get_many` 镜像此模式。`_clamp` 可移植 CASE(SQLite/PG 通用夹取)在 `:60-63`;原子条件 UPDATE + upsert 在 `:78-134`(仅当声誉升级为真列时才需要)。
- **声誉不是关系轴**:`backend/app/services/relation_service.py:66-147`(ResidentRelation,迁移 039)是**成对**(party_a↔party_b)的 familiarity/affinity。声誉是**每人一个标量**(所有人对他的公共看法)。二者不可混用 —— relation_service 不是声誉存储。

---

## 2. 任务切分

> 串行门:任务 1(聚合)全绿并提交后才开任务 2/3/4(消费端);每任务独立提交,commit 带模块号(如 `s1-1-1: reputation nightly aggregate`)。

### 任务 1 — `reputation_service.recompute` nightly 聚合(核心,零 LLM)

- **改哪些文件**:新建 `backend/app/services/reputation_service.py`;`backend/app/tasks/nightly_cron.py`(新增门控 try/except 块);`backend/app/config.py`(新增 flag 组,见 §3)。
- **存储**:`Resident.meta_json['reputation'] = {"score": float, "updated_at": iso8601, "samples": int}`。`score ∈ [rep_min, rep_max]`(默认 `[-1.0, 1.0]`,0 为中性)。**零迁移**(v1)。写用 mood_service 同款:改 meta_json 副本 → `flag_modified(r, 'meta_json')`。
- **派生规则(纯规则,零 LLM;解决缺口 A)**:对每位居民 `r`,以"关于 r 的八卦记忆"为证据 —— 即 `Memory.source=='gossip' AND related_resident_id==r.id`。每条贡献一个 `signed_salience`:
  - **权重(显著度)** = `importance`(已含每跳 ×0.8 衰减,`gossip_service.py:113`)。
  - **语气(派生,非读字段)** = 基线 `rep_gossip_base_tone`(默认 `-0.3`,呼应现有 `realism_gossip_victim_valence` 把"被议论"当负向);`metadata_json.distorted==True` 再叠加 `rep_distortion_penalty`(默认 `-0.2`,失真谣言更伤);`metadata_json.hops` 越大越稀释(× `1/(1+hops)`)。
  - **主体 mood 慢趋势**:折入 `r.mood_json['valence']`(:69)× `rep_mood_weight`(默认 `0.2`),给"最近整体处境"一个低权分量。
  - `raw = rep_mood_weight * valence + Σ(signed_salience) / max(1, samples)`,再 `EMA`:`new = (1-α)*prev + α*raw`(α=`rep_ema_alpha`,默认 `0.3`,慢变量防抖),最后 `_clamp` 到 `[rep_min, rep_max]`(照 `relation_service._clamp:60-63` 可移植 CASE 思路,Python 侧 min/max 即可,因是 JSON 值)。
  - **无证据居民**:`raw` 缺八卦项时仅由 mood 分量驱动,`samples=0`;首次无任何信号 → 保持/初始化为 0.0。
- **签名**(service method signatures):
  ```python
  # backend/app/services/reputation_service.py
  async def recompute(db: AsyncSession) -> int
      # nightly 聚合;flag-off 时直接 return 0(no-op);返回被更新居民数
  async def get(db: AsyncSession, resident_id_or_slug: int | str) -> float
      # 读单人 score;缺失/flag-off → 返回中性 rep_neutral(0.0)
  async def get_many(db: AsyncSession, resident_ids: list[int]) -> dict[int, float]
      # 批量读(镜像 relation_service.relations_for:170-188 单查询批量),供消费端读路径
  def credit_allowed(score: float) -> bool
      # 纯函数信用判定原语:score >= rep_credit_min_score(默认 -0.3)
  ```
- **nightly 块形状**(加进 `run_nightly_jobs()`,照 realism 门控先例 :204-217):
  ```python
  try:
      from app.config import settings
      if settings.rep_enabled:
          from app.services.reputation_service import recompute
          async with async_session() as db:
              n = await recompute(db)
          if n:
              logger.info("reputation recompute: %d residents", n)
  except Exception:
      logger.error("reputation recompute failed", exc_info=True)
  ```
- **REST / WS**:v1 **无新 WS 事件**(声誉后续经"公报"S4-3 承载,§6:229 标注 `—（公报承载）`)。可选只读 admin 端点见任务 4。

### 任务 2 — 投票信任项接线(`civic_service._npc_choice`)

- **改哪些文件**:`backend/app/services/civic_service.py`(仅 `_npc_choice:180-227` 增一个打分项)。
- **接法**:在既有三项打分后,追加声誉项 —— 对携带候选人/提案人 slug 的选项,`scores[i] += settings.rep_vote_trust_weight * reputation_of(candidate_i)`(权重默认 `1.0`)。声誉经 `get_many` 批量取(读路径 +0 查询,见 §4/§7 性能)。**整段用 `if settings.rep_enabled:` 包裹**,flag-off 时 scores 与今日**逐位相等**(保住既有平票/tie-break 测试字节级不变,gotcha #7)。
- **签名**:`_npc_choice` 签名**不变**;新增内部读取用 `reputation_service.get_many`。

### 任务 3 — 候选人排序 + 赊账守卫(`election_service` / `coin_service`)

- **改哪些文件**:`backend/app/services/election_service.py`(`open_election:32-63` 候选排序);`backend/app/services/coin_service.py`(`hold_pending:83-119` 增可选守卫参数)。
- **候选排序**:`open_election` 兜底/排序阶段,`rep_enabled` 时把候选按 `reputation` 降序作为**次序 tie-break**(不改默认 SBTI 选取逻辑,只在同分/兜底时优先高声誉);`install_mayor:127-172` **只读不改**(mayor 双存储不动)。
- **赊账守卫(greenfield 原语,回落安全)**:`hold_pending` 增可选形参 `require_reputation: bool = False`。仅当 `settings.rep_enabled and require_reputation` 时,在原子扣款 UPDATE(:94-98)**之前**先查发起人声誉,`not credit_allowed(score)` → 返回 None(拒贷)。**默认 `require_reputation=False` → 现有全部调用行为字节级不变**;真正的赊账调用方不在本模块(如实记为 greenfield,gotcha #3)。
  ```python
  # coin_service.hold_pending 新签名(仅加尾参,默认关)
  async def hold_pending(db, user_id: str, amount: int, *, require_reputation: bool = False) -> str | None
  ```

### 任务 4 —(可选)只读 admin 端点 `GET /admin/reputation`

- **改哪些文件**:`backend/app/routers/admin/__init__.py`(include 新子路由);新建 `backend/app/routers/admin/reputation.py`(可选,若本轮做)。
- **鉴权**:**每端点** `admin: User = Depends(require_admin)`(admin 无 router 级鉴权,`economy.py:115-159` 先例;漏写即裸奔)。
- **签名**:`@router.get("/reputation")` → 返回 `[{slug, name, score, samples, updated_at}, ...]`(读 meta_json,无写)。对应 §6:215 `GET /admin/reputation`。若本轮不做,记 PROGRESS 跳过,聚合仍可经 §6 出数验证。

---

## 3. 门控开关与默认值

沿用 `backend/app/config.py` 现有 pydantic-settings 范式(每字段一个带默认字面量的类属性,`:7-19`;env 自动大写映射,`:375-378`)。**§6 接口面预告给声誉的 config 前缀是 `REP_`(:215)**,据此在 `Settings` 类新增一组(**主开关默认 `False`,回落安全** —— 对齐 realism 家族 `:246-268`/`:321-352`,不学 M1–M6 默认 True 的 `:354-373`):

```python
# --- S1-1 reputation (default False → 行为与现状字节级一致) ---
rep_enabled: bool = False            # 主开关:门控 nightly 聚合 + 全部消费端
rep_min: float = -1.0                # score 下界
rep_max: float = 1.0                 # score 上界
rep_neutral: float = 0.0             # flag-off / 无数据时的读默认
rep_ema_alpha: float = 0.3           # 慢变量 EMA 系数(防抖)
rep_gossip_base_tone: float = -0.3   # 派生语气基线(呼应 realism_gossip_victim_valence)
rep_distortion_penalty: float = -0.2 # metadata.distorted 叠加惩罚
rep_mood_weight: float = 0.2         # 主体 mood valence 折入权重
rep_vote_trust_weight: float = 1.0   # 任务2 投票信任打分权重
rep_credit_min_score: float = -0.3   # 任务3 赊账放行阈值(credit_allowed)
```

**门控语义(每处在函数顶部短路,照 `election_service.py:35` / `civic_service.py:146` 先例)**:

| 位置 | flag-off 行为(字节级回落) |
|---|---|
| `nightly_cron` 声誉块 | 整块不进入;`recompute` 即使被调也 `return 0` |
| `_npc_choice`(任务2) | 声誉项整段跳过,scores 与今日逐位相等 |
| `open_election`(任务3) | 候选排序回落纯 SBTI/heat,不引入声誉 tie-break |
| `hold_pending`(任务3) | `require_reputation` 默认 False;即使传 True,flag-off 也不查声誉 |

运行期可调值(若需)走 `ConfigService(db).get/set(key, group='civic', updated_by=...)`(`config_service.py:11-27`,与 election `current_mayor` 同 group);静态部署默认仍在 config.py。

**迁移号**:v1 **无迁移**(声誉落 meta_json)。若日后升级为 `residents.reputation` 真列(消费端需 SQL 排序时),迁移号写占位符 **NNN**,`down_revision` 落地时按当时链头定 —— **现链头 = `040_residents_creator_nullable`**(`backend/alembic/versions/040_residents_creator_nullable.py:14-17`,单头已核实);列 ALTER 用 `op.batch_alter_table`(SQLite dev DB 兼容,:20-32)。

---

## 4. 原子性要求

- **v1 声誉写在 nightly、单进程、单写者**(`main.py:85-93` 保证 nightly 恰在一个进程跑,§1.3),且落 JSON 列 —— **无法用一条可移植 SQL UPDATE**(`mood_service.py:83-91` 明确此约束)。故写路径为"批量取居民 → Python 计算 → `flag_modified` 回写",**不存在并发读改写窗口**(唯一写者是 nightly 自己)。**禁止**把逐条八卦读进来再逐次读改写单个居民 score;必须一次遍历、累加、末尾一次写回(照 `run_memory_eviction:233-246` 单遍范式)。
- **消费端只读声誉,不写**;赊账守卫(任务3)复用 `coin_service.hold_pending` 既有原子扣款守卫 —— `UPDATE users SET balance=balance-amount WHERE id=:u AND soul_coin_balance >= amount`(`coin_service.py:94-98`,rowcount==0 → None,:100-103)。声誉检查作为该原子 UPDATE **之前**的一道纯读闸门,**不改**扣款本身的原子性范式(这是本项目 coin 原子化范式的 file:line 依据)。
- **升级为真列时(仅未来)**:score 增量若变成并发写,必须用 `relation_service.py:78-134` 同款单条条件 UPDATE + upsert-on-miss,配 `_clamp` 可移植 CASE(:60-63),禁读改写。v1 不涉及。

---

## 5. 测试口径

> 全部 seeded RNG(构造确定的八卦/mood 输入,不用真随机);每条消费端测试都含 `rep_enabled=False` 的字节级回落断言。文件建议 `backend/tests/test_reputation_service.py` + 在既有 civic/election/coin 测试文件加消费端用例。

**单测(reputation_service)**:
- `test_recompute_disabled_is_noop` —— `rep_enabled=False` 时 `recompute` 返回 0 且不写任何 meta_json。
- `test_recompute_negative_gossip_lowers_score` —— 构造关于某居民的多条 `source='gossip'` 记忆,聚合后其 score < 0。
- `test_recompute_distorted_gossip_heavier_penalty` —— `metadata_json.distorted=True` 的证据使 score 显著更低(对照非失真)。
- `test_recompute_hops_dilutes_signal` —— 高 hops 八卦对 score 影响被 `1/(1+hops)` 稀释。
- `test_recompute_folds_subject_mood_valence` —— 负 valence 的 `mood_json` 拉低 score(rep_mood_weight 生效)。
- `test_recompute_ema_smoothing` —— 单晚剧烈输入不使 score 跳变超过 α 允许幅度(慢变量)。
- `test_recompute_clamps_to_bounds` —— 极端输入 score 仍夹在 `[rep_min, rep_max]`。
- `test_recompute_no_gossip_resident_stays_neutral` —— 无任何八卦证据的居民 score≈0、`samples==0`。
- `test_recompute_ignores_witness_source` —— **显式断言 v1 不把 `source='witness'` 计入**(缺口 A:目击是居民→玩家,方向不对),锁定行为、防误接。
- `test_get_many_batch_single_query` —— `get_many` 一次取多人(镜像 relations_for)。
- `test_credit_allowed_threshold` —— score 跨 `rep_credit_min_score` 两侧的放行/拒绝。

**消费端单测**:
- `test_npc_choice_reputation_term_gated_off` —— `rep_enabled=False` 时 `_npc_choice` 结果与不含声誉项**逐位相等**(锁 tie-break)。
- `test_npc_choice_high_reputation_candidate_gains_weight` —— seeded 下高声誉候选选项得分更高。
- `test_open_election_ranks_by_reputation_on_tie` —— 同 SBTI/heat 时高声誉者入选。
- `test_hold_pending_denies_credit_for_low_reputation` —— `require_reputation=True` + 低声誉 → 返回 None;`require_reputation=False`(默认)→ 与今日行为一致(照常扣款)。

**集成测试**:
- `test_nightly_reputation_writes_meta_json` —— 跑 `run_nightly_jobs()`(`rep_enabled=True`),断言居民 `meta_json['reputation']` 被写、值合理。
- `test_nightly_reputation_disabled_no_write` —— `rep_enabled=False` 跑同一 nightly,声誉块不产生任何 meta_json 变更(其它 nightly job 不受影响)。
- `test_reputation_end_to_end_influences_vote` —— 种子八卦 → nightly 聚合 → 一场 civic 投票中高声誉提案人选项胜出;flag-off 对照组结果回落到今日基线。

---

## 6. 探针出数定义(`burnin_report.py`)

对应 §2 验收 S1-1 行(:33)"声誉与被选为互动对象频率正相关;低声誉者赊账被拒",新增两个探针(照 REALISM P2 探针 seeded fixture 演示出数、首轮记 PROGRESS 的惯例):

1. **声誉分布形态(结构性)**
   - **出什么数**:遍历全体居民 `meta_json['reputation'].score`,输出直方图 + 偏度值 + (min/median/max)。
   - **目标形态**:开关开时应出现**分层**(存在高声誉者与低声誉者,方差 > 0、非退化);越贴近右偏/长尾说明"社会声望"有结构。
   - **对照组(开关关)**:全员 `rep_neutral`(0.0),分布退化为单点 / 零方差 —— 二者对比即"声誉机制是否产生了区分度"。

2. **声誉—被选频率相关性(消费端有效性)**
   - **出什么数**:统计一段模拟内每居民被选为互动对象 / 投票信任目标的频次,与其 score 求相关系数(Spearman)。
   - **目标形态**:开关开时相关系数**显著为正**(高声誉者更常被选/被信任)。
   - **对照组(开关关)**:相关系数≈0(选择与声誉无关,回落到今日的均匀/关系驱动)。
   - **附**:低声誉者赊账被拒率(`credit_allowed==False` 命中次数 / 总请求),开关关时恒为 0(守卫不触发)。

seeded fixture 演示两探针出数,首轮数值 + 与对照组差异记入 `PROGRESS.md`。

---

## 7. 边界与"不碰区域"

- **串行门**:任务 1(nightly 聚合)全绿并提交后才接任务 2/3/4 消费端;`rep_enabled` 关闭时既有测试**零改动通过**(§9:326 门控习惯红线)。
- **性能红线(tick +1 以内)**:声誉存 meta_json,**随居民实体一并加载,读路径 +0 查询**;消费端(`_npc_choice`/`open_election`)用 `get_many` 批量取,或直接读已在手的 `resident.meta_json`,**不得**在 tick 循环里逐居民单查(§6 通用要求:242、REALISM P2 边界 :36 点名过 perceive O(N²) 前科)。聚合只在 nightly 跑,不进 tick。
- **prompt 隔离(与"零 LLM"同级红线,§3.5:116 / §9:322)**:`reputation.score` 是**全局/聚合指标**,**永不进入任何 NPC prompt**;它只经**代码**影响规则打分(vote weight)与信用闸门(credit_allowed),不得作为文本注入 decide/chat prompt。写成断言(可在集成测试里断言 prompt 载荷不含 score)。
- **零 LLM 边际成本**:聚合纯规则派生(读 importance/distorted/hops/mood),**不新增任何 LLM 调用**;缺口 A 的"语气"靠派生而非 LLM 打标。
- **不碰 mayor 存储**:`install_mayor` 双存储(meta_json['mayor'] + system_config current_mayor,:127-172)只读不写;声誉走独立 `meta_json['reputation']` 命名空间。
- **不碰八卦/记忆写路径**:`gossip_service` / `witness_service` / `Memory` 模型**不在本模块修改清单**;聚合只**读** gossip 记忆,不给八卦加 tone 字段(避免与 S1-3 舆论/S1-2 越轨链抢改写路径)。若未来要在八卦写时打 tone,单独立项。
- **Lab 工程安全不变量不动**(§9:320):本模块与 `app/lab/`、`app/forge/`、模型计价无交集。
- **greenfield 如实标注**:赊账机制本体不存在(gotcha #3),v1 只交付信用判定原语 + 回落安全的 `hold_pending` 守卫接口,不编造赊账流水线;"居民目击居民品行"输入不存在(缺口 A),v1 不接 witness 源,等 S1-2 补链。

---

## 8. 依赖与冲突声明

**前置依赖**:S0-1(REALISM/M 系合并 + burn-in,§2:33 依赖列)—— 声誉聚合需要真实运行中的八卦流才有数据;开发/测试期用 seeded fixture 不阻塞。**S1-2 越轨-制裁链**(§2:34)是声誉"品行目击"输入的补链方(缺口 A),但 S1-1 v1 不依赖它即可交付(仅八卦派生 + mood)。

**本模块碰的文件**(修改 5 + 新建 1):
- 修改:`backend/app/tasks/nightly_cron.py`、`backend/app/services/civic_service.py`、`backend/app/services/coin_service.py`、`backend/app/services/election_service.py`、`backend/app/config.py`
- 新建:`backend/app/services/reputation_service.py`(+ 可选 `backend/app/routers/admin/reputation.py`、`backend/tests/test_reputation_service.py`)

**与其他 4 份 KICKOFF 的文件交集(逐条点名 + 串行/协调建议)**:

| 交集文件 | 与谁交集 | 冲突性质 | 协调建议 |
|---|---|---|---|
| `backend/app/config.py` | **S2-1、S2-5、S1-3、S1-5**(全部 5 份都改) | 都往同一个 `Settings` 类加 flag 组 | **各用独立前缀块不重名**:S1-1=`REP_`、S1-3=`POLIS_OPINION_`、S1-5=`ECON_`、S2-5=`POLIS_POLICY_`、S2-1=`POLIS_OFFICE_`。合并时按前缀分块追加,几乎必然产生文本冲突但**逐前缀无语义重叠**,merge 时顺序拼接即可。 |
| `backend/app/tasks/nightly_cron.py` | **S2-1、S2-5、S1-3、S1-5**(5 份都挂 nightly) | 都在 `run_nightly_jobs()` 加独立 try/except 块(S2-1 加 `term_check` 块) | 每份加**自己独立的** try/except 块(互不嵌套、各自门控),块间无数据依赖 → 语义可并存,仅文本相邻冲突。建议 merge 时按模块号排序追加;**五块都遵守"单 job 失败不阻断"隔离**。 |
| `backend/app/services/civic_service.py` | **S2-5、S2-1** | S1-1 改 `_npc_choice:180-227` 打分项;S2-5 改 dispatcher 审批路由;S2-1 改 `_execute_outcome` mayor 分支 | 若 S2-5/S2-1 也动 `_npc_choice` 需**串行**;否则改的是不同函数,可并行。建议 S2-1 先落 offices-backed 任免路径,再 S1-1(加门控打分项)、S2-5(叠审批路由)。 |
| `backend/app/services/coin_service.py` | **S1-5** | S1-1 给 `hold_pending` 加可选 `require_reputation` 尾参;S1-5 用 coin_service 原子范式包 `TreasuryService.tax/disburse` | **不同接触点**(S1-1 只加 hold_pending 尾参且默认 False;S1-5 主要新建 treasury_service 复用范式)。低冲突,建议 S1-1 的尾参改动先落,S1-5 只读/复用不改 hold_pending 签名。 |
| `backend/app/services/election_service.py` | **S2-1** | S1-1 改 `open_election:32-63` 候选排序(声誉→选人权重);S2-1 改 `install_mayor:127-172` / `current_mayor:175-180`(mayor→office 桥) | **不同函数,可并行**。S2-1 先落 offices-backed `current_mayor`,**S1-1 消费其返回、不绕过直读 `system_config`**;merge 时同文件手工合(触碰不同函数,语义不重叠)。 |

**无迁移冲突**:S1-1 v1 **不加 alembic 迁移**(meta_json),而 S2-5(`041_add_policies.py`)、S1-3(`041_add_issue_stances.py`)、S1-5(`0XX_add_town_treasury.py`)各自 branch off `040_residents_creator_nullable` → **它们之间会撞多头**(两个 041),需 merge 时重排链尾单头;**S1-1 不参与这场多头争用**,是本批次里唯一零迁移的模块(协调优势,可最先或最后合入均无迁移负担)。

---

## 附:本文档引用的全部 file:line anchors(供校验)

- `backend/app/services/gossip_service.py:49-141`(maybe_gossip 写路径,无 tone)
- `backend/app/services/gossip_service.py:113`(每跳 ×0.8 importance 衰减)
- `backend/app/services/gossip_service.py:117-121`(metadata_json 字段)
- `backend/app/services/gossip_service.py:129-139`(victim mood 回写)
- `backend/app/models/memory.py:53-85`(Memory 列,无 sentiment/valence)
- `backend/app/services/witness_service.py:59-110`(record_witnesses,居民→玩家中性目击)
- `backend/app/services/witness_service.py:99-109`(familiarity bump)
- `backend/app/services/coin_service.py:83-119`(hold_pending/hold 原子扣款守卫)
- `backend/app/services/coin_service.py:94-98`(原子条件 UPDATE)
- `backend/app/services/coin_service.py:100-103`(rowcount==0 → None)
- `backend/app/services/coin_service.py:16-256`(coin 模块,无赊账概念)
- `backend/app/services/civic_service.py:180-227`(_npc_choice 打分,无声誉项)
- `backend/app/services/civic_service.py:199-208`(SBTI + 义务/经济打分)
- `backend/app/services/civic_service.py:216-222`(relation affinity 微调)
- `backend/app/services/civic_service.py:226`(argmax + 索引 tie-break)
- `backend/app/services/civic_service.py:146`(门控在函数顶部先例)
- `backend/app/services/election_service.py:32-63`(open_election 候选选取)
- `backend/app/services/election_service.py:47-53`(SBTI Ac1/So1 + heat 兜底)
- `backend/app/services/election_service.py:35`(门控顶部短路先例)
- `backend/app/services/election_service.py:127-172`(install_mayor 双存储)
- `backend/app/services/election_service.py:138-158`(meta_json['mayor'] + system_config)
- `backend/app/services/relation_service.py:66-147`(ResidentRelation 成对轴,非声誉)
- `backend/app/services/relation_service.py:60-63`(_clamp 可移植 CASE)
- `backend/app/services/relation_service.py:78-134`(原子条件 UPDATE + upsert)
- `backend/app/services/relation_service.py:170-188`(relations_for 单查询批量读范式)
- `backend/app/tasks/nightly_cron.py:28-230`(run_nightly_jobs 挂点)
- `backend/app/tasks/nightly_cron.py:76-126`(arc/civic/election nightly 先例)
- `backend/app/tasks/nightly_cron.py:204-217`(relation decay 门控 try/except 先例)
- `backend/app/tasks/nightly_cron.py:219-229`(circle refresh 门控先例)
- `backend/app/tasks/nightly_cron.py:233-246`(run_memory_eviction 单遍范式)
- `backend/app/tasks/nightly_cron.py:343-347`(nightly_cron_loop 00:30 触发)
- `backend/app/main.py:85-93`(background_tasks 单进程注册)
- `backend/app/models/resident.py:30-84`(meta_json 命名空间 / heat / mood_json)
- `backend/app/models/resident.py:15`(heat 列)
- `backend/app/models/resident.py:69`(mood_json)
- `backend/app/models/resident.py:82-84`(pinned/display_heat)
- `backend/app/services/mood_service.py:83-91`(decay_all JSON 列批量写范式)
- `backend/app/config.py:7-19`(Settings pydantic 范式)
- `backend/app/config.py:375-378`(model_config / settings 单例)
- `backend/app/config.py:246-268`(realism_enabled 主开关+调优块)
- `backend/app/config.py:321-352`(realism P2 独立门控默认 False)
- `backend/app/config.py:354-373`(M1–M6 默认 True 反例)
- `backend/app/services/config_service.py:11-27`(ConfigService.get/set 运行期 KV)
- `backend/app/routers/admin/middleware.py:10-33`(require_admin 依赖)
- `backend/app/routers/admin/economy.py:115-159`(每端点 Depends(require_admin))
- `backend/app/routers/admin/__init__.py:18-31`(admin router include 范式)
- `backend/alembic/versions/040_residents_creator_nullable.py:14-17`(当前单链头)
- `backend/alembic/versions/040_residents_creator_nullable.py:20-32`(batch_alter_table 范式)
