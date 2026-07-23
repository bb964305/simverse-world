# SimVerse World 算法全景分析报告

> 分析对象:`simverse-world` 后端(FastAPI + PostgreSQL/pgvector + Redis),重点为世界模拟、居民行为、情绪记忆、社交交流、Forge 角色铸造、LLM 支撑层与 Lab 任务编排。
> 分析方法:通读约 90 个核心源码文件(agent、personality、memory、services、tasks、forge、llm、lab 及 ws 处理器),关键结论均已回源码逐条核实。文中引用形如 `文件:函数`。
> 日期:2026-07-21

---

## 1. 总体架构与算法地图

这个项目本质上是一个 **"规则驱动骨架 + LLM 生成血肉"** 的混合模拟系统。全局的算法分层非常清晰:

| 层 | 驱动方式 | 典型模块 |
|---|---|---|
| 环境层(天气/事件/热度/赛季) | 几乎纯规则,零 LLM | `tasks/weather.py`、`tasks/event_cron.py`、`services/heat_service.py` |
| 行为层(作息/决策/移动) | 规则为主,LLM 仅在"计划"与"临场改主意"时介入 | `agent/scheduler.py`、`agent/phases/*` |
| 心智层(记忆/情绪/性格/梦) | 情绪纯规则;记忆提取、性格演化、梦境用 LLM,但层层限流 | `memory/`、`personality/`、`services/mood_service.py` |
| 交流层(聊天/八卦/辩论) | LLM 生成内容,规则控制触发概率、冷却与成本 | `ws/handlers/chat.py`、`agent/chat.py`、各社交 service |
| 铸造层(Forge) | 多阶段 LLM 流水线 + 确定性降级 | `forge/pipeline.py` 及各 stage |
| 执行层(Lab) | 纯分布式系统工程:状态机、租约、outbox、沙箱 | `lab/orchestrator.py`、`lab/broker.py` 等 |
| 支撑层(LLM 客户端/预算/计价) | 三级预算熔断 + 分档降级 | `llm/budget.py`、`llm/pricing.py` |

贯穿全项目的最一致的设计决策是**成本工程**:环境系统完全不花钱;行为系统通过"照计划执行则跳过决策 LLM"节省 29–37% 成本(代码注释中的实测数据);夜间生成任务(日报、梦境)设有次数上限与免调用兜底;全局有按日预算的四档熔断(NORMAL→THROTTLE→RULE_ONLY→PLAYER_ONLY),100% 超支时世界"仍在动但不花钱"。这条成本主线是评价一切子系统取舍的背景。

与之对应的三个**系统性隐患**也贯穿始终,后文会反复出现:其一,大量后台循环隐含"单实例部署"假设,靠先查后插而非数据库约束/分布式锁去重;其二,不少冷却与去重状态存在**进程内 dict**(居民对话冷却、偶遇冷却、目击去重),多 worker 部署即失效、重启即清零;其三,记忆 `importance` 这个由 LLM 无校准自评的分数,同时是检索排序、性格剧变触发、梦境选材三个子系统的枢纽,单点漂移会同时扭曲三处。

---

## 2. 世界变化与环境系统

### 2.1 天气:季节化马尔可夫链

`tasks/weather.py` 是教科书式的轻量设计:天气是一条**按季节切换转移矩阵的马尔可夫链**。`ensure_weather_event` 每 60 秒由事件 cron 调用,若当前存在未结束的天气事件则跳过(保证同一时刻天空唯一);否则取上一段天气为前态,按真实月份映射季节(北半球硬编码),从 `TRANSITIONS[season][prev]` 按累积概率采样次态。状态空间为 sunny↔cloudy↔rain→storm,storm 只能经 rain 到达,snow 仅在冬季出现(如冬季 cloudy→snow 概率 0.30)。强度按天气种类在区间内均匀采样(storm 为 0.7–1.0),持续时长均匀采样 2–6 小时。

最巧妙的一点是"**天气即世界事件**":天气不是独立子系统,而是作为一条 `WorldEvent` 插入,自动复用世界事件的整套管线——prompt 注入、集体记忆、WebSocket 广播、前端粒子效果,新增天气种类几乎零管线成本。`sample_next_kind` 是可注入随机源的纯函数,可测性好。

缺陷:去重靠"先查后插",没有数据库唯一约束兜底,多实例部署会重复插入天气段;文案 `WEATHER_COPY` 硬编码中文,扩展需改源码。

### 2.2 世界事件、节日与集体记忆

`tasks/event_cron.py` 的 60 秒主循环做四件事:确保天气、翻转事件活跃态(`flip_active_events` 按 starts/ends 与当前时间比较)、对新激活事件给所有非沉睡居民批量写入 importance=0.5 的**集体记忆**(截 200 字,直接写表、不走 LLM)、触发剧本幕与赛季结算,最后统一经 Redis pub/sub 广播。节日模板(`event_templates.py`)由夜间任务每日排程:向前看 3 天,命中 6 个硬编码节日则建事件,幂等靠"同日同标题已存在"检查;另以 0.15 概率(约每周一次)从 4 条新闻池抽一条 1 天期新闻。整个环境事件层**零 LLM**。

缺陷有二:`flip_active_events` 每分钟对 `WorldEvent` 做无过滤全表扫描,而事件表只增不删,复杂度随运行时间线性恶化;集体记忆是"居民数 × 事件数"的写放大,且进程本地 60 秒缓存导致 API worker 感知事件最多滞后 60 秒。

### 2.3 热度与情绪衰减

`heat_cron.py` 每小时执行两件事。热度:`heat = 近 7 天对话数`,状态机为 heat≥50→popular、heat==0 且 7 天无对话→sleeping(从未被聊过的新居民永不入睡——很好的新手保护)、否则 idle,聊天中的居民永不被 cron 改状态。注意 `heat_service.py:45` 的 `resident.heat = max(new_heat, resident.heat)`(仅 new_heat>0 时)——展示热度**只升不降**,以保护手动调高的值,但状态判定用的是真实 new_heat,于是可能出现"显示 heat 80 却处于 idle"的口径分裂,所谓"热度衰减"实际只靠 7 天滑窗自然滑落。

情绪衰减(见 §4.1)搭同一班车:valence/arousal 每小时向中性点几何回归 5%,约 48 小时回到 calm。两者均为纯规则、零成本。

### 2.4 夜间批处理

`nightly_cron.py` 睡到每日 00:30 UTC,串行执行:村落日报(当日唯一必然的 LLM 调用,SQL 聚合素材后单次成文,无素材走模板零调用,(scope,date) 唯一约束幂等)→ 委托过期 → 梦境(三重闸门:今日记忆≥3 条才候选、每人 50% 掷币、全局每晚 ≤10 次调用)→ 时间胶囊 → 节日排程 → Lab 过期任务与孤儿 run 回收(heartbeat TTL 判定,退还预扣款)→ 产物保留清理 → 周日追加逐居民的生涯目标周评(LLM)。每个子任务独立 try/except,单项失败不拖垮整晚。

最大缺陷是**无补跑机制**:`_seconds_until_next_run` 只会睡到"下一个" 00:30,进程若恰在执行窗口附近重启,当晚所有任务静默丢失,且无任何标记可供事后补跑。

### 2.5 世界治理与修订:全项目工程质量最高的模块

`proposal_service.py` + `world_revision_service.py` 实现"居民/玩家提案 → 审批 → 原子改世界"的治理闭环。提案生命周期 pending→approved→applied/failed(或 rejected/reverted),审批用**条件 UPDATE 的 CAS** 防止两个管理员同时通过;经济上提案成本在创建时从提案者金库冻结,拒绝或应用失败自动退款;风险评估目前是纯规则(按 kind 定级:npc=high、location/mechanic=medium、lore=low,LLM 评估留了钩子未接)。

真正出彩的是修订路径(add_lore/edit_location):approve 时以 `current_revision_id` 做**乐观并发基线**,提案若 pin 了过期基线则以 "stale base" 失败退款;apply 前后各读一次目标对象得到 before/after 快照;overlay 落盘、`WorldRevision` 记录、outbox 事件行、提案终态**在同一个数据库事务中提交**,世界重载与广播严格放在 commit 之后;`world_changed` 事件的 seq 复用 outbox 自增主键,给下游一个单调可收敛的游标;revert 用 before_state 精确恢复并以 CAS 防双重回滚。

残余风险:CAS 推进到 approved 之后、apply 完成之前进程崩溃,提案会**永久卡在 approved 非终态**,没有任何回收 cron;`_fail_apply` 先提交 failed 再退款,退款失败即资金丢失;创建提案时扣款与插入提案不在同一事务。

### 2.6 赛季与剧本季

赛季计分(`season_service.py`)走事件总线:聊天完成每日前 5 次各 5 分(Redis `INCR` 原子判次)、委托 15 分、首访地点 10 分,叠加"每用户每日 100 分"的 Redis 日封顶与 DB 侧 upsert;结算以 `payload.settled` 幂等,快照最终排名,前三名奖 200/120/80 SC。防刷是双层的(Redis 次数 + 日封顶),成本极低。但封顶是先 `get` 后 `incrby` 的 check-then-act,并发可小幅穿顶;Redis 扣额与 DB 提交非原子,DB 失败时额度已被消耗。

剧本季(`script_service.py`)是最有叙事野心的机制:剧本 = 赛季 + 若干"幕"(trigger_at + 事件载荷),每分钟检查到期幕,触发时同时做三件事——建一条**直接激活**的剧本世界事件进 prompt、在公告板发公开线索、按 `secrets` 列表给指定居民**注入私密记忆**(importance 0.7)。"不同居民各知一角"的信息不对称是很好的涌现叙事原语。投票一人一票(预检 + DB 唯一约束双保险),结算取最多票、平票取最小下标。

但剧本事件因为绕过 `flip_active_events` 的"start 转变"直接置 active,**既不触发 WS 广播也不写集体记忆**,与天气/节日的行为不一致,前端只能等 60 秒缓存过期才感知——疑似遗漏而非有意设计。

**本章小评**:环境层"规则驱动、生成层限流"的成本结构非常成熟,治理/修订模块的事务纪律达到了生产级分布式系统的水准;但单实例假设、进程本地缓存、O(全表) 周期扫描这三类债务在本层最为集中。

---

## 3. 居民活动与行为决策

### 3.1 Tick 调度:性格推导作息 + 概率门

居民行为由独立的 agent-worker 进程驱动(`agent/main.py`),主循环每 `AGENT_TICK_INTERVAL`(默认 60s)一轮。每轮先查预算档位(见 §7.2):THROTTLE 档睡眠间隔翻倍,RULE_ONLY 档强制"只照计划执行"并禁止居民互聊,PLAYER_ONLY 档整轮跳过。然后用短会话取出所有非沉睡居民后立刻释放连接,再以 `asyncio.gather` + 信号量并发跑每个居民的 tick,每个 tick 独立开 DB 会话——会话纪律相当好。

"谁在此刻行动"是纯规则概率门(`agent/scheduler.py`),这是全项目最有味道的算法之一。SBTI 性格五维先推导作息:`wake_hour = max(5, 9-(Ac1+Ac3))`(行动力越强起得越早,5–9 点),睡觉时间按社交/情感维映射 21/22/23 点,高峰时段 1–3 个、社交时段 1–3 个由对应维度决定,`rest_ratio = 0.6 - 0.2×Ac3`。当前小时的活动概率 = 窗外为 0;基线 `(1-rest_ratio)×0.5`;峰值高斯加成 `0.4×exp(-0.5×(d/2)²)`(σ=2 小时);社交时段 +0.2;上限 0.95。最后加 ±0.1 均匀抖动做一次伯努利掷骰。错峰完全是概率性的,没有分片轮转。另有 Redis 键做**跨进程的每日行动数硬上限**(TTL 2 天),防止概率失控烧钱。

### 3.2 五阶段认知流水线

每次 tick 按 YAML 装配的插件链执行 perceive→plan→decide→execute→memorize(`agent/registry.py` 按性格解析配置:So1=L→introvert,So1=H 且 Ac3=H→extravert,否则 default),任一阶段异常即短路,状态经 `TickContext` 传递:

**Perceive(纯代码)**:全表拉居民、曼哈顿距离 ≤ 感知半径(default 18 / extravert 24 / introvert 10)入列;顺带取活跃世界事件(60s 缓存)、记录目击玩家、感知装修变化,全部 fail-open。社交变体额外为每个邻居查关系记忆——但**该数据从未被任何下游消费**(死数据,每邻居一次白查)。

**Plan(每天 1 次 LLM)**:日期翻转时生成当日计划(max_tokens 1200,JSON)。prompt 里放:人设前 200 字、SBTI、清醒时段切成的时间槽(default 7 / extravert 9 / introvert 5 个)、全部地点列表(含加成动作与入口坐标)、约束(importance≥6 的高优先事项至多 2/3/1 个、社交槽至多 2/4/1 个)、性格化动作权重(extravert 的 CHAT:3,introvert 的 REFLECT:3、CHAT:0.5),以及昨日事件、高重要度记忆、关系记忆和长期人生目标。之后的 tick 纯代码按 `hour_range` 命中当前槽。

**Decide(混合)**:先纯规则算可用动作集(15 种,带前置条件:社交需附近有 idle/walking 目标、EAVESDROP 需有人在聊、RESEARCH 需白名单+身处实验楼)。然后三条路径:计划 importance ≥ 中断阈值(6/8/5)→ 零 LLM 强制照做;`skip_decide_when_planned`(三份配置全开,注释称实测全服省 29–37% 成本)且无规则级中断信号(最新记忆 importance≥0.8,或附近可聊而计划非社交)→ 照计划执行;否则才调 LLM(max_tokens 200,输出 `{action, target_slug, target_tile, reason}`,严格枚举校验,选了不可用动作则本 tick 作废)。决策 prompt 里注入位置、SBTI、可用动作、附近居民、8 条记忆、今日已做、世界事件、天气软提示、心情软提示(valence<-0.4 提示独处、>0.5 提示社交)与"你原计划…但可改变主意"。

**性格变体**:introvert 的 `CautiousDecidePlugin` 在 LLM 选了计划外社交动作时 50% 概率改写为 OBSERVE;extravert 的 `SpontaneousDecidePlugin` 每 tick 30% 直接丢弃计划,且附近有人时 40% 零 LLM 直接对随机目标发起聊天。性格由此在**时间(作息)× 空间(半径)× 社交(权重/冲动/抗拒)**三层同时分化,这套"YAML 插件链 + 数值差异"的性格外化设计非常干净。

**Execute(纯代码)**:移动动作 A* 寻路后只走 `path[1]` **一格**(每 tick 一格),状态置 walking;IDLE/NAP/REFLECT/JOURNAL 归位 idle(JOURNAL 有规则门控概率变成公告板发帖);CHAT_RESIDENT 不在阶段内执行,由主循环转交 `resident_chat`。

**Memorize(纯代码模板)**:按"在{地点}做了{事}"模板写记忆,importance = 基础 0.3 + 偏离计划 +0.2;introvert 变体额外 15% 概率触发一次 LLM 反思。

### 3.3 寻路、地图与夜归

`pathfinder.py` 是标准 **A\***:heapq 开放集、4 邻接单位代价、曼哈顿启发(可采纳,保证最优)、max_steps=500 按 g 值截断。地图(`map_data.py`)为硬编码地点字典 + tilemap 碰撞层求差得可行走集,并从中央广场 **BFS 求连通分量**剔除"孤岛格",动态建筑可运行时并入并重建索引——先验证连通性再落位的做法避免了一类经典的"NPC 卡死"bug。夜归(`night_homing.py`)是 burn-in 实测后加的补丁:作息窗外且不在睡眠/聊天状态的居民,每 tick 零 LLM 朝家走一格,不计入每日行动数——没有它,居民会在睡点冻结在街头。居民安置(`resident_placement.py`)做位置规范化(职业关键词→场所映射)、2 格步进网格找空槽、按序溢出、确定性兜底,住房按独栋 cap1/公寓 cap5 顺序分配。

### 3.4 评价要点

优点已如上述:成本压缩极致且有实测数据、性格外化架构优雅、burn-in 驱动的补丁(夜归、社交软推)显示真实观察迭代、每 tick 独立会话与 Redis 跨进程计数的一致性纪律。

缺陷中有一个**实质性 bug**:`decide/basic.py:130` 的 `_force_execute_plan` 把计划目标放进 `target_slug` 而把 `target_tile` 置 None,但 `execute/basic.py` 对 WANDER/VISIT_DISTRICT **只读 `target_tile`**(仅 GO_HOME 会自行解析家的坐标),None 即"无有效目标→归位 idle"。也就是说,走 plan-skip 快路径(默认路径!)时,**计划里的"去图书馆"永远原地不动**,而 memorize 阶段却按模板写下"前往了图书馆"的记忆——行为与记忆脱节,居民会"记得"从未发生的移动。同理,LLM 决策路径的 target_tile 由模型自填坐标,幻觉坐标寻路失败后也是静默 idle 但记忆照写。这是行为可信度层面最值得优先修复的一处。

其余问题:perceive 每居民每 tick 全表扫描,整轮 O(N²),居民上量后是首个性能墙;每 tick 一格 + 60s tick 意味着穿越约 180 格宽的地图需要小时级时间,"计划时段内到达目的地"在几何上经常不成立,与计划系统的语义相互矛盾;`should_tick` 注释声称"±15 分钟 jitter"而实现是 ±0.1 概率抖动,文档漂移;introvert 配置里的 `interrupt_only_for` 无任何代码消费(死配置);两个 extravert 可在同轮经 40% 冲动路径抓取同一聊天对象(TOCTOU);聊天冷却是进程内 dict,重启清零;作息用服务器本地时间而预算用 UTC 日,跨时区部署时两套"一天"错位。

---

## 4. 情绪、记忆与性格演化

### 4.1 情绪:valence-arousal 二维连续模型

`mood_service.py` 采用心理学上站得住脚的**环状情绪模型**:`mood_json = {valence∈[-1,1], arousal∈[0,1], label}`,8 个离散标签(excited/content/calm/furious/annoyed/gloomy/anxious/tired)由二维坐标查表映射(|valence|≥0.15 判正负、≥0.5 判强弱,arousal≥0.6 判高唤醒,近中性时 arousal≥0.75→excited、≤0.3→tired)。更新是外部事件加法叠加后 clamp,衰减由每小时 cron 做 5%/小时的指数回归(约 48h 归于 calm)。纯规则、零成本、参数语义清晰。

问题在于**这套引擎接线太少**:全代码库只有两个写入点——辩论获胜(+0.3/+0.1)和收到商店道具(+0.25/+0.1)。日常聊天不改情绪(聊天收尾的 LLM 明明输出了 positive/neutral/negative 情绪判定,却只用于广播、不回写 mood);梦境、性格剧变、被八卦、目标达成/失败统统不触碰情绪。情绪对行为的影响也仅是往 decide/聊天 prompt 里注入一个 label 软提示。一个设计良好的引擎最终只是**装饰层而非反馈环**,是心智层最可惜的一处。

### 4.2 记忆:结构对标 Generative Agents,但两条关键通路是死代码

`memory/service.py` 的记忆分四类:event(事件)、relationship(关系,每对关系一条、LLM 改写式 upsert,项目没有数值好感度,"好感"就是这条自然语言记忆)、reflection(反思)、dream(梦)。

**提取**:玩家聊天结束后异步执行,LLM 按 prompt 内写死的锚点打 importance(0.1–0.3 闲聊、0.4–0.6 实质话题、0.7–0.8 涉及感受/价值观、0.9–1.0 重大事件),每条截 80 字符;居民互聊收尾用**五合一调用**(双方事件提取+双方关系更新+第三人称摘要+情绪判定合并为 1 次,替代旧 5 次,失败重试 1 次后兜底通用摘要)。所有提取 prompt 注入 SBTI 着色块,让性格影响"记住什么、看多重"。**反思**:自上次反思新增 ≥15 条事件后触发,取近 20 事件+10 关系提炼 2–3 条高层洞察。

**嵌入**:三模式(OpenAI 兼容端点/本地 Ollama/禁用),向量统一截断或补零到 1024 维入 pgvector;失败返回 None 保持列 NULL 而非零向量,避免污染余弦检索,另有每小时补齐任务。这些细节处理得很专业。

但核实源码后确认(grep 全库无调用方):**`search_events_vector`(pgvector 余弦检索)和 `evict_memories`(超 500 条按 importance+last_accessed 淘汰)都没有任何调用者**。实际检索走 `_search_events`,只按 `importance DESC, created_at DESC` 排序且**完全忽略查询文本**。也就是说:README 语境下的"语义记忆检索"实际是静态重要性排序,与对话内容无关;遗忘从未执行,记忆表无限增长;也没有 Stanford Generative Agents 式的 α·relevance + β·recency + γ·importance 加权。嵌入基础设施建好了、backfill 也在跑,但整条语义通路"接线未通",长期后果是高分旧记忆霸屏、上下文相关性退化。

### 4.3 性格演化:双通道 + 多层防漂移

`personality/evolution.py` 设计了两条演化通道。**Drift(渐变)**:累计足够新事件后 LLM 审阅近 20 条,提议维度微调;**Shift(剧变)**:任一新事件 importance≥0.9 即触发评估,LLM 按四类场景(深度共鸣/信任背叛/认知冲突/群体排斥或接纳)判定。`guard.py` 施加硬约束:drift 单维步长 ≤1(L↔M↔H,禁 L→H)、每轮 ≤2 维、需距上次 ≥15 条事件;shift 步长 ≤2、每次 ≤3 维、24h 冷却;两者共享**每月 8 次维度变更预算**(按当月 personality_history 统计)。变更应用后重匹配 SBTI 类型、LLM 重写 persona_md(soul_md 仅在剧变且触及 S3/A3 时小幅重写),全程写入 PersonalityHistory(触发类型、触发记忆、变更明细、新旧类型),类型迁移推送动态流。

这套"步长+频率+冷却+月度预算+双文本层差异化保守度"的防漂移设计在同类项目中相当完整,可追溯性也好。风险:剧变门槛完全系于 LLM 自评的 importance≥0.9,打分膨胀会频繁叩门(靠 24h 冷却兜底);drift 触发计数复用的是"距上次 reflection"而 guard 内部查的是"距上次 drift",两处口径不一;guard 不校验提议中的 `from` 值是否等于当前真实值,LLM 幻觉的基线仍可写入;persona 文本重写失败被静默吞掉,维度与文本可能长期不一致。

### 4.4 梦境与 SBTI

梦境(`dream_service.py`):夜间对"今日新记忆≥3"的居民洗牌后逐个 50% 掷币,全局每晚 ≤10 次调用;素材 = 今日 importance 最高 3 条 + 随机 1 条高分旧记忆,交给"梦境编织者" prompt(第一人称、80 字、允许荒诞混搭),产物存为 importance=0.4 的 dream 记忆;梦到玩家会触发成就与"有人梦到了你"通知,24h 内的梦会注入后续对话。成本闸门与"今日+旧忆混搭"的隐喻都不错;但梦不影响情绪、不参与演化,且固定 0.4 的 importance 使它几乎不会被静态排序检索命中——又一个"造出来但半悬空"的机制。

SBTI(`sbti_service.py`)不是问卷,而是 LLM 读三层人设文档输出 5 组×3 维共 15 个 L/M/H,再与 25 个预设类型模式串逐维求**曼哈顿距离**,`similarity = (1 - distance/30)×100`,<60% 兜底特殊类型。计分透明廉价,且与演化系统天然耦合(维度变→重匹配→类型迁移)。弱点是 L/M/H 粒度粗、25 模式在 3^15 空间中极稀疏,大量档案落在兜底边缘;LLM 评级不可复现。

**本章小评**:这是 Generative Agents 思路的一次务实的轻量工程化——凡花钱处皆有闸门,凡失败处皆有降级。但两大实质问题必须点名:向量检索与遗忘是死代码(宣称能力与实际行为不符),importance 无校准单点(检索、剧变、梦境三系统共用),外加情绪引擎的反馈环几乎断路。

---

## 5. 社交互动与交流

### 5.1 玩家-居民聊天:上下文组装与三段式事务

`ws/handlers/chat.py` 是玩家侧核心路径。开聊:居民忙则入队返回位次;沉睡居民需付 3 倍单轮价唤醒(唤醒后 heat 提至 ≥10 并刷新时间戳,保 7 天不再入睡)。上下文经 `retrieve_context` 装载:1 条关系记忆 + ≤3 条反思 + ≤10 条事件(即 §4.2 的静态排序)。每条消息走**三段式**:会话1(Redis 60s 滑窗限流 → 日预算检查 → 原子扣费 → 落库 → 读世界事件/人生目标/昨夜梦境);**关闭 DB 会话后**组装 system prompt(三层人设 soul/persona/ability + 记忆 + 世界事件 + 心情 label 语气指令 + 目标进度 + 梦境 + 200 字回复限制)并流式生成,历史窗口固定 10 轮(全量仍持久化);会话2(落库回复、媒体消息写 0.6 记忆、创作者被动分成 1 SC/轮)。结束后异步做记忆提取→关系更新→(满 15 条)反思→SBTI 类型变化检测与广播。

"LLM 调用绝不持有数据库连接"(代码注释称 P0-2 纪律)、"扣费在前、限流在扣费前"的顺序在所有聊天路径中一以贯之,这是该项目事务素养的集中体现。

### 5.2 居民间对话与偶遇

居民互聊由 agent 循环的 CHAT_RESIDENT 动作触发:进程内 dict 记 pair 冷却,目标忙则跳过,双方锁 socializing;3–8 轮逐轮 LLM 对话(每轮 system 含 SBTI+人设+对对方的关系记忆,历史只取近 6 行,回复截 200 字),注释自估一次完整对话约 11–13 个调用;收尾五合一调用 + 双向八卦掷骰,finally 中双方复位——这是模拟世界里最贵的单一行为,也解释了为什么 RULE_ONLY 档第一个禁的就是它。

玩家偶遇(`encounter_service.py`)纯规则:进入地点时,(用户,地点) 冷却 1 小时、每用户每日 ≤5 次,命中场所内 idle/walking 居民后掷 0.3 基础概率,按 8 个地点模板生成开场白,接受后场景文本注入聊天 prompt。打招呼(`greeting_service.py`):玩家连线时取其 importance 最高的 3 条关系记忆,找一个 idle 且 24h 未打过招呼的居民,按 heat≥30 分热情/矜持模板池;关系 importance≥0.85(挚友)且 7 天未送礼则随机送一件系统礼物——冷却状态巧妙地用"标记记忆"实现,复用了记忆表。

### 5.3 八卦:带失真链与溯源的传播模型

`gossip_service.py` 是社交层算法密度最高的文件:居民对话收尾双向各掷 0.3;内容选择为"说话者的 importance≥0.6、且主角不是听者本人"的事件记忆按分取 top20 中第一条 hops<4 者;传播时 hops+1,**失真概率 = min(0.2×hops, 0.8)**,失真才调 LLM 改写(夸大或改错一个细节),不失真则原文转述**免调用**;importance×0.8 衰减、封顶 0.7;metadata 记录 origin_memory_id 形成可回放的**溯源链**(管理端可看谣言传播路径)。信息随跳数衰变+失真的模型有真实传播学味道,免调用路径又一次体现成本意识。缺陷:候选只看高分记忆且无"已传给此人"去重,高分旧闻会被反复传播。

### 5.4 辩论、目击与摘要

辩论(`debate_service.py`):announced→live→voting→settled 生命周期。押注 10–200 SC、每人一注(唯一约束+IntegrityError 退款兜底)且押注自动计一票;live 阶段两居民交替 LLM 发言 6 轮(每轮 120 字,逐轮广播),LLM 失败自动判平局全额退款;结算按票数,败方奖池**烧 5%** 后按押注额 pro-rata 分给胜方,以 stakes 表而非缓存计数为准;胜者情绪 +0.3。设计完整,但胜负纯由投票决定、与辩论内容无关,且"押注即投票"使一号多押被唯一约束挡住后仍可女巫多号;结算逐笔各自 commit,中途崩溃会留下不一致(此处没有 outbox 保护,与 Lab/治理模块形成对比)。

目击(`witness_service.py`):每 tick 感知阶段调用,5 秒 TTL 的在线玩家位置快照,半径 10 格内写"看到某玩家路过"的低权记忆(importance=0.25,每对 4h 去重、每居民留 20 条),随后自然进入对话上下文,实现"我昨天看到你了"的惊喜时刻——用极低成本买高感知价值,是很聪明的机制。摘要(`digest_service.py`):村日报 SQL 聚合素材 + 1 次 LLM 成文(无素材走罐头文案),个人周报用 8 个规则化标签(聊天≥10 次=健谈者),<2 次对话直接跳过 LLM。

### 5.5 社交-经济联动

目标系统:居民生涯目标每周由 LLM 评估(输出 progress_delta 0–0.3 与 verdict),达成/失败写 0.9 高权反思记忆**直接喂给性格演化**;玩家可对目标投资(单笔 50–500、池上限 2000,achieved 赔 1.5x、failed 退 0.5x、abandoned 全退),投资即写 0.85 记忆("有人资助了我的梦想")。送礼写 0.75 记忆 + 情绪提升 + 创作者 20% 分成。货币层 `coin_service.py` 用 `UPDATE ... WHERE balance >= amount` 的原子条件扣款防超卖,注释详述了 rollback 使 ORM 对象过期的坑——但同文件 `reward_creator_passive`(coin_service.py:244)仍是非原子的 `user.soul_coin_balance += 1`(已核实),与自家原则矛盾,是明确的并发丢更新点。

**本章小评**:社交层最大的架构亮点是**记忆表成为统一社交中枢**——聊天、八卦、目击、送礼、投资、打招呼冷却全部落到同一张 Memory 表,由 importance 统一驱动检索与触发,涌现路径清晰、机制间天然互通。最大的短板与心智层同源:检索退化为静态排序后,这个中枢的"相关性"名不副实;另有进程内状态多worker失效、辩论结算无 outbox、被动分成非原子三处工程债。

---

## 6. Forge 角色铸造流水线

### 6.1 机制

`forge/pipeline.py` 把"一段描述/一个网上人物"铸成三层人设(ability/persona/soul)的 AI 居民。入口先跑**路由**(`router_stage.py`,LLM 判断"是否为网上能搜到足够资料的知名人物":公众/历史/知名虚构角色→deep,私人/原创/模糊→quick;JSON 解析失败一律回落 quick)。quick 路径只跑构建;deep 路径为 Research→Extraction→Build→Validation→Refinement 五阶段,每阶段更新会话状态并推送进度,异常统一捕获置 error,无阶段级重试或断点续跑。

Research 纯 SearxNG 检索、零 LLM:6 个人格维度(写作/对话/表达DNA/外界评价/决策/年表)× 每维 2 条查询模板 = 12 次串行搜索(每次 top5、间隔 1 秒),用户素材置顶标注"一级来源权重最高"。Extraction 把研究文本截到 8000 字符后做心智模型提取,提示词要求对每个模型做**三重验证**(跨域复现/生成力/排他性),全过→core_model、过 1–2 项→heuristic、全不过→丢弃——用验证标准约束 LLM 编造,是提示词工程里较扎实的一段。Build 三次串行调用分别产出三层 Markdown。Validation 让 LLM 做三问验证、边缘测试(未公开话题应显不确定)、100 字风格仿写并给 overall_score(0–1)。Refinement 双评审(Optimizer 看证据一致性、Creator 看趣味深度)提建议后对三层各做一次全文重写。deep 全程约 11 次 LLM 调用,预算闸复用 llm_usage 表按 session 累计,超过单请求上限(注释称 deep 实测约 $0.30)抛异常终止。铸成后生成 slug(保留中文)、自动落位、固定 2 星与 heat=10,创建者奖 50 币。

### 6.2 评价

优点:**每个 JSON 解析点都有确定性兜底**(路由→quick、提取→空桶、校验→0 分报告、精炼→原文),流水线几乎不因单点解析失败而崩;成本闸、输入截断、system/user 双客户端与前后台模型分流齐备;注释保留 E-xx 事故编号可追溯。

缺陷:**校验分数不做门控**——overall_score 再低也照常进精炼与上线,validation 沦为纯观测;精炼是无条件全文重写,没有 diff 校验,可能吞掉原有的好内容;预算闸是"事前查累计"的事后闸,末段 5 连调仍可显著超支;失败即整单作废、已花费沉没;quick 模式构建后清空 research_data,原始输入不可追溯、无法重跑;视频理解仅把 URL 当文本发给预处理模型,若端点不真正抓取视频,"摘要"实为幻觉。

---

## 7. LLM 支撑层

**客户端**(`llm/client.py`):system/user 双客户端,system 走便宜的 background_model、user 走 effective_model(E-18 模型分流)。刻意**不做自研重试/超时**,异常仅计数后上抛,"调用方自带 fallback"——配合前述各处的确定性兜底,这个选择自洽,但也意味着没有统一的超时保护;Forge 各 stage 直接裸调 `messages.create`,绕过了包装层的错误观测,形成指标盲区。

**预算熔断**(`llm/budget.py`):三个粒度——全局日预算映射四档(≥80% THROTTLE、≥95% RULE_ONLY、≥100% PLAYER_ONLY,分别对应 tick 放缓、后台全规则化、只保玩家可见调用)、每用户日上限、Forge 单请求上限。查询失败 **fail-open 到 NORMAL**(可用性优先于成本,方向正确)。这套"花钱的世界在超支时优雅退化成免费的世界"的分档降级,是整个项目成本工程的顶层设计,也确实在 agent 循环、聊天、Forge 三处都接了线。

**计价**(`llm/pricing.py`):按模型名**最长前缀匹配**四元组价格(input/output/cache_read/cache_creation),未知模型回落 haiku 价——注释明说宁可虚高 7 倍触发误熔断也不漏计,保守方向正确。**JSON 容错**(`llm/json_extract.py`):去围栏→首个 `{` 起做**括号深度扫描**(正确处理字符串字面量与转义)取第一个平衡对象→失败去尾逗号重试,比正则方案严格正确,是全库 JSON 输出的统一入口。**媒体路由**(`media/model_router.py`):图片直接进主模型多模态;视频先用便宜模型出 200 字中文摘要再注入文本(失败降级为附原始链接照常回复),预处理单独计量。

---

## 8. Lab 任务编排系统

Lab 让 AI 居民在沙箱里执行真实任务(跑代码、发 HTTP 请求),再把结果经治理管道写回世界。这一模块与模拟层气质完全不同——是标准的**分布式任务编排系统**,工程密度全库最高。

### 8.1 机制要点

**三层状态机**:Run(queued→running⇄needs_approval→succeeded/failed/cancelled)、Task(活跃态→终态,成功进 review)、ToolAction(requested→policy 裁决→approved/waiting_approval/denied→executing→succeeded/failed/reconciliation_required)。所有终态推进都用"条件 UPDATE + rowcount==1"的 CAS,`approved→executing` 的原子转换关掉了 allow 直通的双执行竞态;执行器抛"结果不确定"时停在 reconciliation_required 而不盲目重试——对副作用类工具是正确的语义。

**租约 + fencing 防脑裂**(`leases.py`):TTL 30s、心跳 10s,过期接管用单条条件 UPDATE 保证竞争者只有一个成功并使 `fencing_epoch+1`。防双写三道防线:心跳按 owner+epoch 条件更新,失败抛 StaleEpoch;账本 append 前先 assert_epoch,被 fence 后一字不写;Broker 在请求与执行前都对权威租约行做对账,且审批消费的 UPDATE 谓词内嵌 epoch 条件,把 TOCTOU 关进同一原子写。被 fence 的旧 owner 明确不写终态、不退款、不撤新 owner 的授权(注释注明这是修复过的"fence 反转"bug)。

**预算**(`budgets.py`):per-run 单行八维(tokens/工具调用/墙钟/出网请求/出网字节/产物数/产物字节/并发 worker),原子性靠 `WHERE used+reserved+amount<=limit` 的条件 UPDATE,超限即标记维度、终止 run、退款、撤授权。工具调用类走 reserve→confirm/release 两阶段,消耗类直扣。

**Outbox**(`ledger.py`+`outbox_dispatcher.py`):事件、outbox 行、投影在**同一事务**写入,派发器 CAS 租行、指数退避(2^attempts 封顶 300s)、5 次后死信、未知 topic 直接隔离,整体 at-least-once + 消费端幂等。

**策略与监督**:12 个工具各带能力标签与 R0–R4 风险级,决策序为"未注册硬拒→R4 硬拒→能力未授权拒(审批不能替代授权)→R3 走世界治理→R2 人工审批→R0/R1 放行";出网另有反 SSRF(内网/metadata IP 一律阻断)+ 白名单 fail-closed。取消采用 cooperative→TERM→KILL 三级升级,`finally` 无条件"撤授权+epoch+1+置终态"——**熔断不依赖被熔断者配合**,这是关键的正确姿态。

**沙箱**(`sandbox/oci_executor.py`):`--network none --read-only --tmpfs /scratch:64m --memory 256m --cpus 0.5 --pids-limit 128 --cap-drop ALL --no-new-privileges --user 65534`,无 bind mount,文件 base64 物化,20s 墙钟即 kill,输出各 64KB 截断且持续排空(防 host 内存 DoS);teardown 验证容器确实消失,无法确证即**执行器永久自我隔离**。结果回写世界唯一入口是治理提案管道(§2.5),复用同一套 CAS+单事务+outbox。

### 8.2 评价

优点:CAS 惯用法全模块统一;"runtime 不可信"的纵深防御(签名授权、租约对账、执行前重评、审批绑定 epoch 四层互备);失败姿态一致 fail-closed(Redis 故障拒准入、空白名单拒出网、teardown 不可证即自毁、未知 topic 隔离);事务边界成熟(模块不自开 session,outbox 与状态同事务)。

风险:事件 seq 用 MAX+1 派发、靠唯一约束兜底,高并发下冲突重试频繁;预算函数各自 commit 与调用方事务割裂,reservation 可孤儿化且无回收器(docstring 自认);审批用 0.2s 忙轮询占连接,难扩展;敏感动作靠子串匹配(如名字含 "pay"),误杀与改名绕过双向风险,真正防线只剩注册表白名单;`fs.write` 不入沙箱、Mock 执行器恒成功,隔离证据自陈为开发级;监督会话状态纯内存,重启丢失。

---

## 9. 总体评价

### 9.1 分维度评分

| 维度 | 评分 | 一句话理由 |
|---|:---:|---|
| 成本工程 | ★★★★★ | 全库最强主线:分层限流、四档熔断、免调用兜底、保守计价,处处有实测依据 |
| 分布式/事务正确性 | ★★★★☆ | Lab 与治理模块达到生产级(CAS/租约/outbox);但模拟层大量进程内状态与单实例假设拖后腿 |
| 行为可信度 | ★★★☆☆ | 作息/性格/夜归/目击等机制出色;但"计划移动不动而记忆照写"与"检索无关语义"两处直接伤害可信度 |
| 心智模型完整性 | ★★★☆☆ | 记忆/演化/梦境框架对标 Generative Agents 且防漂移完整;向量检索与遗忘为死代码,情绪近乎断路 |
| 涌现叙事设计 | ★★★★☆ | 八卦失真链、剧本 secrets、目击、投资入记忆都是高质量原语 |
| 性能可扩展性 | ★★☆☆☆ | O(N²) 感知、全表事件扫描、逐行衰减,居民/事件上量后多处线性恶化 |
| 代码工程素养 | ★★★★☆ | 会话纪律、降级链路、事故编号注释、burn-in 迭代痕迹都很专业;少量死代码与文档漂移 |

### 9.2 最突出的三个优点

**第一,成本-可信度的权衡是自觉的、量化的、分层的。** 这个项目对"LLM 模拟世界最大的敌人是账单"有清醒认识:规则做骨架(作息、概率门、冷却、衰减),LLM 只在"内容必须新鲜"的地方出场(计划、对话、记忆提取、剧变判定),且每个出场点都有次数闸门和免调用降级。四档预算熔断保证超支时世界退化成"免费但仍在动"的规则世界,而不是死掉。同类开源项目里很少见到如此完整的成本工程。

**第二,该严肃的地方非常严肃。** 凡涉及钱和世界状态的路径——治理提案、世界修订、Lab 编排、货币扣减——用的是 CAS、乐观并发基线、单事务原子提交、outbox、租约 fencing 这一整套正经分布式手法,且注释里保留了 bug 编号与修复理由,能看出是被真实事故打磨过的。

**第三,涌现机制的"原语"选得好。** 记忆表作为统一社交中枢(聊天、八卦、目击、送礼、投资全部落进同一张表、由同一个 importance 驱动),使机制间天然互通:玩家资助目标→高权记忆→影响对话与性格演化,这种链路不需要额外代码就自然发生。八卦的"跳数递增失真+重要性衰减+溯源链"、剧本的"secrets 信息不对称"、目击的"低成本高感知",都是设计感很强的小算法。

### 9.3 最需要正视的三个问题

**第一,"宣称的能力"与"实际运行的算法"存在缺口。** 向量语义检索和记忆遗忘两条通路是无调用方的死代码,实际检索是"按重要性静态排序、无视查询内容";计划驱动的移动因 `target_tile=None` 在默认路径上根本不发生,记忆却照写"我去了"。这两处的共性是:基础设施建了、测试可能也过了,但**端到端的接线没通,而表层行为看起来正常**,恰恰是这类模拟系统最难自察的缺陷。建议为"居民真的移动了吗""检索结果真的和问题相关吗"补端到端断言。

**第二,importance 是无校准的单点。** 一个由 LLM 自评、无跨对话归一化的分数,同时决定检索排序、性格剧变触发(≥0.9)、梦境选材、八卦候选(≥0.6)。任何打分漂移(换模型、改 prompt、模型自然的分数膨胀)会同时扭曲四个子系统,且难以回归测试。建议引入分位数归一化或对"≥0.9 剧变门"改用独立的二次判定。

**第三,模拟层与基础设施层的一致性标准不统一。** 同一个代码库里,Lab 用租约+fencing 防脑裂,而居民聊天冷却是进程内 dict;治理用 outbox 保证投递,而辩论结算逐笔 commit 无补偿;coin_service 上半部原子 UPDATE 防超卖,下半部 `balance += 1` 裸写。这说明团队完全具备正确做法的能力,只是模拟层默认了"单实例、可容忍丢失"。这个默认值需要被显式写进部署文档,否则未来横向扩容时会在最难排查的地方(概率性社交行为)出现重复与丢失。

### 9.4 改进建议(按优先级)

1. **修复计划移动 bug**:`_force_execute_plan` 应把 `plan.target` 解析为地点入口坐标填入 `target_tile`(现成的 `get_valid_target_tile` 即可),或在 execute 阶段对 `target_slug` 做兜底解析;同时让 memorize 阶段只记录实际发生的位移。
2. **接通向量检索与遗忘**:`retrieve_context` 把当前对话文本作为 query 走 `search_events_vector`,按 relevance+recency+importance 加权;夜间任务挂上 `evict_memories`。
3. **给 approved 卡死的提案与孤儿化的预算 reservation 各加一个回收 cron**(Lab 的孤儿 run 回收已有先例可抄)。
4. **把进程内冷却/去重状态迁到 Redis**(聊天冷却、偶遇计数、目击去重),与已上 Redis 的限流/日行动数对齐。
5. **情绪接线**:聊天收尾已产出的情绪判定回写 mood;梦境、目标达成/失败、被八卦纳入 `apply_mood_event` 触发面,让情绪成为真正的反馈环。
6. **性能三件套**:`flip_active_events` 加活跃/未过期过滤;perceive 用位置索引或空间网格替代全表 O(N²);`decay_all` 改批量 SQL 表达式更新。
7. Forge 对低 overall_score 设门槛(重试或降级为草稿),精炼重写加 diff 审查;`reward_creator_passive` 改原子 UPDATE;剧本事件补广播与集体记忆。

### 9.5 结语

作为一个"AI 居民小镇 + 角色铸造 + 任务执行"的完整系统,simverse-world 的算法设计整体处于同类项目的第一梯队:它没有把"全部交给大模型"当作架构,而是用规则系统承担节律、约束与降级,把 LLM 用在边际价值最高的生成点上,并用真实的分布式工程手段守护钱与世界状态。当前的短板不在设计理念,而在**接线完整性**——几条关键通路(语义检索、遗忘、计划移动、情绪反馈)停在"建好未通"的状态。把这几根线接上之后,这个世界的"活性"会有一次不需要新增任何大机制的显著跃升。

