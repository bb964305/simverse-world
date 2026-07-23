# Kickoff Realism P2 — 关系网络 + 信息梯度(社会结构)

你在 simverse-world 仓库。**目标**:按 `docs/REALISM_OPTIMIZATION_PLAN.md` §7/§8 完成 P2 全部 8 项任务,把世界从"一堆人"变成"一个社会"。前序:P0+P1 已交付在 `feat/realism-p1`(pytest 1181 passed / 1 skipped / 11 deselected(lab_oci),迁移链头 038,全部机制门控 `REALISM_ENABLED` 默认 False),本次从 `feat/realism-p1` 切 `feat/realism-p2`。开工前通读方案 §7/§8/§9.3 与 `docs/PROGRESS.md` 中 P0/P1 的偏差记录(尤其:plan.target 双路解析、节日地点权重简化为社交+0.2、indoor/dining 派生方式)。**目标达成的定义**:8 项任务全绿 + 全量 pytest 基线 1181+ 只增不减、零新增排除 + 新机制带测试且门控开关默认 False(关掉时 1181 既有测试零改动通过)+ 两个新探针在 burnin_report.py 出数 + 进展与偏差记入 PROGRESS.md。**选做项(世界时钟、记忆失真)默认不做**,完成 8 项后停下等我拍板。

## 全局纪律(延续 P0/P1,增量部分加粗)

- **零新增 LLM 调用**:关系更新全部复用既有调用的输出(wrapup 情绪判定、目击、送礼/投资事件);圈子检测纯代码;日报圈子素材搭现有 digest 调用(只改素材组装,不加调用);八卦携带事件引用不改调用结构。无法零调用实现 → PROGRESS.md 记原因,跳过继续。
- **门控**:本次新增三个独立开关 `REALISM_RELATIONS_ENABLED` / `REALISM_INFO_GRADIENT_ENABLED` / `REALISM_CROWD_ENABLED`,config 默认全 False;任一关闭时对应路径行为与现状完全一致。数值参数继续 `REALISM_` 前缀进 config。
- **关系写入必须原子**:familiarity/affinity 增量一律单条条件 UPDATE(`SET familiarity = LEAST(1.0, familiarity + :d)`)+ upsert(ON CONFLICT),禁止读-改-写;关系对用**规范化无向键**(canonical ordering:小 id 在前)防重复行。多 worker 并发下丢更新按 coin_service 原子化(P0 任务 5)同标准对待。
- **P0-2 会话纪律**、跨进程状态一律 Redis、Alembic 链尾校验(038 → 新迁移后单头)、测试口径(每任务带测试,阶段末全量 pytest)照旧。
- **前端**:本次预期不改 WS payload 形状;管理端社交图谱只交付后端 JSON 端点,前端可视化不做(rolldown build 既有问题未解,PROGRESS 有记录)。若确需动前端,先记 PROGRESS 再动。
- **不碰的区域**:`app/lab/`、`app/forge/`、prompt 文风、模型计价。P1 已记偏差不回改(玩家聊天 mood 维持 rating 承载)。

## 阶段 A — 关系双轴(任务 1–4,串行:1 → 2 → 3/4)

1. **resident_relations 表 + RelationService**(方案 §7.1;新迁移,链尾校验)。字段:`party_a, party_b`(规范化无向键,统一承载居民-居民与居民-玩家,带 party 类型)、`familiarity FLOAT [0,1]`(熟悉度)、`affinity FLOAT [-1,1]`(好感度)、`last_interact_at`、`interact_count`。索引:(party_a, party_b) 唯一 + 按单方查询的辅助索引。Service 提供 `bump(pair, d_familiarity, d_affinity)`(原子 upsert)与 `top_relations(party, n, by=...)`。**衰减**挂 nightly:30 天无互动的关系 familiarity ×0.95/周、affinity 向 0 回归 2%/周(疏远是真实的)。现有自然语言关系记忆**保留不动**,数值轴做量、文本轴做质。测试:upsert 原子性(并发 bump 无丢更新)、规范化键去重、衰减数值、封顶。
2. **写入面接线**(方案 §7.1)。四个触发点全部复用既有输出:a) 对话完成(玩家-居民与居民互聊两条路径)familiarity +0.05,affinity 按 wrapup 情绪判定 positive +0.03 / negative −0.03;b) 目击 familiarity +0.01(witness_service 已有事件,搭车);c) 送礼 affinity +0.1(shop_effects,其 metadata 里躺着的 relationship_boost 字段就此接通——诊断报告点名的"未见消费端");d) 投资 affinity +0.1(investment_service)。测试:每个触发点的增量断言;`REALISM_RELATIONS_ENABLED=false` 时零写入。
3. **读取面采样改造**(方案 §7.1,把"均匀随机"换成"按权采样")。五处:a) 偶遇选人权重 `1 + 2×familiarity`;b) 打招呼从"第一个 idle"改为候选中 affinity 最高者;c) 八卦目标按 familiarity 加权(八卦沿强关系流动);d) 居民 CHAT 目标权重 `0.5 + familiarity + max(0, affinity)`,**混入 ε=0.1 的均匀分量**(保留结识陌生人的通道,防圈子僵化——这条是硬要求,不许纯贪心);e) 居民对话轮数上限随 familiarity 线性移动(familiarity<0.2 → 3–4 轮,>0.7 → 6–8 轮,现有 3–8 clamp 内插值)。全部受开关门控,关闭时回落均匀随机/first-idle。测试:seeded RNG 下的加权采样统计断言(高 familiarity 目标被选频率显著更高且 ε 分量存在)、轮数插值、开关回落。
4. **圈子检测与三个消费端**(方案 §7.2)。nightly 用 familiarity ≥0.3 的边跑连通分量(居民数百级,纯 Python 毫秒完成;不引入新依赖,不用 Louvain),圈子标签写入 `meta_json.circle_id` + 圈子摘要表或 JSON 快照。三个消费端:a) 村日报素材加一行圈子动态("XX 圈子本周对话最活跃",进现有 digest 素材组装,零新调用);b) 管理端只读端点 `GET /admin/social-graph`(nodes+edges+circles JSON,复用 admin 鉴权中间件);c) 剧本 secrets 支持按圈子投放(`secrets` 列表允许 `circle:<id>` 语法,展开为圈内居民,现有注入逻辑复用)。测试:构造已知图断言分量划分;端点鉴权;circle 语法展开。

## 阶段 B — 信息梯度与人流(任务 5–7,可与阶段 A 的 3/4 并行,但 6 依赖 5)

5. **废除全知广播**(方案 §8.1,诊断 §2.2 的写放大)。`write_collective_memories` 改造:事件激活时一手记忆只写两类居民——**地理相关者**(事件 payload 指定地点的半径内居民,或该地点近 7 天到访者,location_visits 已有数据)importance 0.6,加**随机 20% 消息灵通样本** importance 0.5;其余居民不写。**天气例外保留全员**(抬头可见)。一手记忆 metadata 必须带 `event_id`。受 `REALISM_INFO_GRADIENT_ENABLED` 门控,关闭时回落全员广播。测试:知情比例断言(非天气事件 <50% 居民一手知情)、天气仍全员、metadata 带 event_id。
6. **八卦成为二手信息通道**(方案 §8.1 后半)。确保带 `event_id` 的事件记忆能进入八卦候选池(核对现有 importance ≥0.6 与 related_resident 过滤不会把它们排除,必要时对事件类记忆放宽 related 条件);传播时二手记忆继承 `event_id` 与 hops/失真语义(现有机制,确认不丢字段即可)。这样"知情者→朋友→朋友的朋友"的扩散链自然成立。测试:seeded 链路——A 获一手事件记忆 → A/B 对话触发八卦 → B 拥有带同 event_id、hops=1 的二手记忆。
7. **人流聚集**(方案 §8.2;**补完 P1 任务 9 记录的偏差**——当时节日地点 ×3 权重简化为社交+0.2,本次做全)。两条规则,受 `REALISM_CROWD_ENABLED` 门控:a) 节日/剧本事件活跃期间,事件地点进入全体居民 VISIT_DISTRICT 候选并权重 ×3(decide 的可用动作/候选地点组装处);b) 从众微规则——perceive 发现某地点当前人数 ≥5 且自己 social 需求 <0.5(P1 需求底座已有该值)时,decide prompt 注入一行"那边好像很热闹"。测试:事件期间加权候选生效;从众提示注入条件断言。

## 任务 8 — 验收探针 ×2 + 出数

`burnin_report.py` 新增(方案 §9.3 最后两项):a) **社交网络度分布偏度**——以 resident_relations(familiarity>0.1)为边建图,输出度分布直方图与偏度值;目标右偏(存在社交明星与边缘者),关闭开关的对照组应近均匀;b) **信息扩散半衰期**——对每个非天气世界事件,统计"拥有该 event_id 记忆(一手+二手)的居民比例"随模拟时间的曲线与到 50% 的时长;目标:数小时量级、且知情顺序与关系强度正相关(抽样验证),对照组(开关关)为瞬时全知。seeded fixture 演示出数,首轮数值记入 PROGRESS.md。

## 边界

- 串行门:任务 1 全绿并提交后才开 2;5 全绿才开 6。阶段 A 与阶段 B 之间无硬依赖,可交叉推进,但**每任务独立提交、提交信息带任务号**(`realism-p2-3: weighted encounter/gossip/chat sampling`)。
- 方案与代码漂移以代码为准,偏差记 PROGRESS.md 后继续,不停等(P1 惯例延续)。
- 采样类改动必须可复现:所有随机路径接受注入 RNG,测试用 seeded 断言,不许写"跑三次看大概"。
- 性能红线:读取面改造不得把 tick 循环的每居民查询次数抬升超过 +1(关系查询批量取、进 TickContext 复用;诊断报告点名过 perceive O(N²) 的前科,不要再添一笔)。
- 不合并、不部署。交付到"feat/realism-p2 CI 绿 + 两探针出数 + PROGRESS 更新"为止;真实 burn-in 与 REALISM_* 开关的生产默认值等我拍板。
- **选做项不做**:世界时钟(§3.1B)与记忆失真(§6.3,唯一有 LLM 成本项)本次明确排除;若我中途说"开",再单独立项。

## 附:预期收益速查(供自检方向感)

任务 1–3 落地后,偶遇/招呼/八卦/互聊四条社交路径从均匀随机变为亲疏有别,老友话长、陌生人话短;任务 4 让圈子可被日报讲述、被剧本利用、被管理端看见;任务 5–6 让"你听说了吗"成为真实现象——消息灵通者先知道,边缘者从八卦听说,且二手消息可能失真;任务 7 让节日的广场真的有人流。两个探针就是这四句话的量化版:度分布右偏 = 社会有结构,扩散半衰期 >0 = 信息有距离。
