# Kickoff Realism — 世界拟真优化(P0 接线 + P1 规则拟真)

你在 simverse-world 仓库。**目标**:按 `docs/REALISM_OPTIMIZATION_PLAN.md`(方案)完成 P0 + P1 两阶段全部改造,依据是 `docs/ALGORITHM_ANALYSIS_REPORT.md`(诊断,含文件:函数级定位)。两份文档先通读,再动手。**目标达成的定义**:下方 12 项任务全绿 + 全量 pytest 零排除通过(基线 711+,只增不减)+ 新增机制全部带测试 + 验收指标探针落入 burnin_report.py + 每步进展记入 PROGRESS.md。P2(关系网络/信息梯度)**不在本次范围**,完成 P0+P1 后停下等我拍板。

## 全局纪律(每个任务都受约束)

- **零新增 LLM 调用**:本次所有拟真改造必须是纯规则实现。唯一例外是梦境 tone 字段——搭现有 dream 调用的车,在同一次调用的输出 JSON 里加字段,不许新开调用。若某任务你发现无法零调用实现,停下来在 PROGRESS.md 写明原因,跳过该任务继续,不要擅自加调用。
- **P0-2 会话纪律**:延续现有惯例,任何 LLM 调用不得持有 DB session;新增的规则计算放在短会话内完成。
- **跨进程状态一律 Redis**:新增的冷却/计数/去重不许用进程内 dict(现存的迁移是任务 5)。
- **可调参数进 config.py**:所有新增数值(速度、概率、衰减率、需求代谢率)定义为 Pydantic Settings 字段,前缀 `REALISM_`,给出方案中的建议默认值;新机制整体受 `REALISM_ENABLED=true` 总开关控制,关掉时行为与现状完全一致(便于 burn-in A/B 与回滚)。
- **迁移**:新字段用 Alembic 迁移,链尾校验(`ls backend/alembic/versions` 确认单头);`meta_json`/`mood_json` 类 JSON 字段扩展不需要迁移的就不要建表。
- **测试口径**:每个任务完成即跑该模块相关测试;每完成一个阶段跑全量 pytest 零排除。改动 WS 广播 payload 或移动速度后,前端四连(tsc strict / vitest / eslint 0 问题 / build)+ 检查 Phaser 端居民移动插值是否假定"每 tick 一格"(在 `frontend/src/game/` 搜 tween/lerp 逻辑;若硬编码步长,做最小适配,不重构)。
- **不碰的区域**:`app/lab/`、`app/forge/`(除非任务明确要求)、prompt 文风、模型与计价配置。

## 阶段 P0 — 一致性修复(先全绿,再进 P1)

1. **计划移动真的移动**(方案 §2.1,诊断 §3.4)。`app/agent/phases/decide/basic.py::_force_execute_plan` 现把 `target_tile=None` 塞给 execute,导致 plan-skip 默认路径上 VISIT_DISTRICT 永远原地不动。修复:强制执行路径与 LLM 决策路径的 `target_tile` **一律由服务端从 `target_slug` 解析**(现成 `map_data.get_valid_target_tile`),禁用模型自报坐标(schema 保留字段但忽略)。同时改 `memorize/basic.py`:按 `ctx.new_tile` 与是否到达生成"正在前往 X"/"到达了 X"两种记忆文本,未发生的位移不许写成已发生。测试:新增"计划含地点→若干 tick 后位置到达该地点入口"的集成测试;"决策为移动但不可达→记忆不含'去了'"断言。
2. **接通语义检索与遗忘**(方案 §2.2+§6.1,诊断 §4.2)。`memory/service.py` 的 `search_events_vector` 与 `evict_memories` 目前零调用方。改造 `retrieve_context`:query = 本次对话最近 3 条消息拼接,事件记忆检索走向量通路,打分 `0.45×relevance + 0.30×recency + 0.25×importance`,`recency = exp(-Δt/τ)`、`τ = 72h×(1+importance)`;命中即刷新 `last_accessed_at`(保鲜)。pgvector 不可用/embedding 为 NULL 时回落现有 importance 排序(fail-open,现有行为即兜底)。`evict_memories` 改为 score_floor 语义(importance<0.35 且 90 天未访问的事件记忆软删归档)并挂入 `nightly_cron.py`。测试:同一居民两条记忆,语义相关的低分记忆应排在语义无关的高分记忆之前(mock embedding 给定向量);归档不删 relationship/reflection/dream 类型。
3. **情绪回写与闪光灯**(方案 §2.3+§5,诊断 §4.1)。聊天五合一 wrapup 已输出 positive/neutral/negative 却不回写:映射 positive→`apply_mood_event(+0.15,+0.05)`、negative→`(-0.2,+0.1)`,玩家聊天与居民互聊两条路径都接。memorize 阶段 importance 加情绪增强项 `+0.2×|valence|×arousal`(封顶 1.0)。测试:wrapup 后 mood_json 变化断言;高唤醒负面情绪下动作记忆 importance 高于平静时。
4. **剧本事件走统一管线**(方案 §2.4,诊断 §2.6)。`script_service.py::fire_due_scripts` 现直接置 `is_active=True`,绕过 `flip_active_events` 的 start 转变,既不广播也不写集体记忆。改为 `is_active=False, starts_at=now`,交由 event_cron 统一翻转(secrets 私密记忆注入逻辑保持不动)。测试:幕触发后,下一次 flip 会广播 `world_event` 且非沉睡居民获得集体记忆。
5. **口径统一与回收**(方案 §2.5,诊断 §2.3/§2.5/§5)。三件事:a) heat 双口径——`resident.heat` 回归真实值(允许下降),人工调值迁到新字段 `pinned_heat`,展示取 `max(heat, pinned_heat)`,状态判定只用真实值;b) 回收 cron×2——approved 卡死超 10 分钟的提案标记 failed 并退款(挂 nightly;照抄 Lab 孤儿 run 回收的写法),Lab 预算孤儿 reservation 超时 release;c) 进程内状态迁 Redis——居民聊天 pair 冷却(`agent/chat.py` 的 dict)、偶遇冷却与日计数(`encounter_service.py`)、目击去重(`witness_service.py`),键带 TTL,语义与现值一致。顺手修 `coin_service.py::reward_creator_passive` 的非原子 `+= 1`,改用文件内现成的原子 UPDATE 惯用法。
6. **P0 阶段验收**:全量 pytest 零排除通过;`burnin_report.py` 新增两个探针——**计划到达率**(计划含地点的时段内实际到达比例,修复前≈0,目标>70%)与**行为-记忆一致率**(抽样动作记忆能对应真实状态变更的比例,目标>95%)。跑一次本地短 burn-in(或用现有测试夹具模拟若干 tick)确认两个探针出数。

## 阶段 P1 — 规则拟真

7. **移动提速与通勤感知**(方案 §3.1A)。execute 每 tick 沿 A* 路径走 `REALISM_MOVE_SPEED`(默认 8)格,调制叠乘:rain×0.75、storm×0.5、snow×0.6、arousal>0.7×1.2。plan 阶段 prompt 的地点列表每项附"约 N 分钟路程"(曼哈顿距离÷速度,纯代码估算)。注意 WS 位置广播与前端插值检查(见全局纪律)。测试:限速断言(单 tick 位移 ≤ speed)、天气因子生效。
8. **天气影响行为**(方案 §3.2,诊断"调度器 `del weather`")。`scheduler.py::get_activity_probability` 增加天气乘子(sunny 1.0/cloudy 0.95/rain 0.7/storm 0.4/snow 0.75);`map_data.LOCATIONS` 每地点加 `indoor: bool`(据现有描述判断,户外:广场/公园/街道类);雨/暴风时 VISIT_DISTRICT 候选剔除或减半户外地点,正在户外的居民以 0.6 概率改道最近室内地点("躲雨");storm 开始加入 decide 规则级中断信号(现仅两条);天气心情微项:rain/storm 每小时 `(-0.02,-0.01)`、sunny 晨间 `(+0.02,0)`(挂 heat_cron 顺风车)。测试:storm 下活动概率显著低于 sunny;躲雨改道触发。
9. **星期与节日节律**(方案 §3.3)。`build_schedule` 加 weekday:周六日 wake_hour+1~2、rest_ratio+0.1、social_slots+1;节日事件活跃期间全员社交时段概率+0.2、节日地点 VISIT_DISTRICT 权重×3。测试:同一居民工作日/周末 schedule 差异断言。
10. **三需求底座**(方案 §4,本阶段最大项,先写设计小结进 PROGRESS.md 再动手)。`meta_json.needs = {energy, satiety, social}`(0–1,初始 0.8):代谢按方案表格(energy 清醒-0.004/tick、walking-0.006、睡眠+0.02;satiety -0.005/tick、EAT+0.5;social 独处-0.003、introvert-0.001/extravert-0.006,对话+0.4、打招呼+0.1)。新增 EAT 动作(纯状态变更,需身处餐饮类地点——LOCATIONS 加 `category` 字段标注餐饮)。接入点两个:a) 决策裁决层——任一需求<0.25 时零 LLM 强制对应行为(energy→GO_HOME 后置 **sleeping**(顺手把夜归到家置 idle 改为 sleeping,作息窗内醒来);satiety→前往最近餐饮地点+EAT;social→CHAT 权重×2),优先级低于"高重要度计划强制执行"、高于 plan-skip;b) decide/聊天 prompt 注入一行需求摘要("你有点饿了")。日常 tick 的需求代谢不计入每日行动数。测试:代谢速率、危急裁决触发、EAT 恢复、sleeping 状态进出、`REALISM_ENABLED=false` 时行为与现状一致。
11. **情绪环补全**(方案 §5 剩余项)。输入面:梦境 tone(现有调用加输出字段,按 tone ±0.1)、目标周评 verdict(achieved `(+0.4,+0.2)`/failed `(-0.3,+0.1)`,goal_service 已有 verdict 零新调用)、被八卦(失真 hops≥2 且主角是自己时 `(-0.1,+0.15)`)。输出面:活动概率 `×(1+0.2×valence)`;valence<-0.4 时 CHAT 权重×0.5、REFLECT/JOURNAL×2(软提示升硬权重);居民对话轮数采样均值随双方 valence 均值移动;对话收尾情绪传染 `v += 0.1×(v̄−v)`(双方)。测试:传染收敛方向、抑郁态概率下降。
12. **importance 校准与剧变门**(方案 §6.2,诊断"无校准单点")。每居民对近 100 条记忆维护 importance 分位映射,新记忆按分位数归一化后再入库(原始值存 metadata 便于回溯);性格 shift 触发从"importance≥0.9"改为"归一化分位≥P95 **且** 当前 |valence|>0.5"双条件。测试:人为膨胀打分(全 0.9)时归一化后不再全员触发剧变评估;双条件门单元测试。
13. **P1 阶段验收**:全量 pytest + 前端四连;`burnin_report.py` 再加两个探针——**地点小时人流曲线**(输出各 category 地点按小时的到访计数,期望餐饮出现午晚双峰、雨天户外人流下降)与**需求健康度**(全体居民三需求的日内均值/最低值,不应出现持续饥饿的死锁)。本地短 burn-in 出数后,把四个探针的首轮数值记入 PROGRESS.md。

## 边界

- 串行推进:P0 任务 1–6 全绿并提交后,才进 P1;P1 内 7–9 可并行,10 必须单独一个提交批次(改动面最大,便于单独回滚)。
- 分支惯例:新开 `feat/realism-p0` 与 `feat/realism-p1`,小步提交,每任务至少一个提交,提交信息带任务号(如 `realism-p0-1: resolve plan target_tile server-side`)。
- 任何任务发现方案与代码现实冲突(方案基于 07-21 快照,代码可能已漂移):以代码现实为准,在 PROGRESS.md 记录偏差与你的决策,继续推进,不要停等。
- 不部署。vm212 部署与真实 burn-in 开跑等我拍板;你交付到"双分支 CI 绿 + 探针出数"为止。
- 若全量 pytest 出现与本次改动无关的既有红名单,记录后跳过,不要顺手修不相关的东西。
