# Simverse World Roadmap

> 状态基线：2026-07-27。本文是项目唯一现行规划文档；旧规格、过程记录、研究、测试报告与运行证据均已移入 [`archive/2026-07-25/`](../archive/2026-07-25/README.md)，只作为历史证据，不再代表当前实现或待办。

居民形象供应商资格、批次生成、审核、安装与恢复命令见 [`RESIDENT_SPRITE_OPERATIONS.md`](RESIDENT_SPRITE_OPERATIONS.md)。

## 当前基线

| 领域 | 当前状态 | 下一道门 |
|---|---|---|
| 核心小镇 | 已完成。账号、角色炼化、实时地图与对话、居民自治、记忆与人格、经济、集市、公告、赛季、辩论和基础治理已经接入主流程 | 继续以生产观测修正体验，不再单列基础里程碑 |
| M1-M6 扩展 | 已完成并部署。经济、故事弧、镇务自治、记忆评估、空间扩展和镇长选举已接电 | 观察长期节律与成本 |
| 拟真与世界时钟 | P0-P2 已实现并在 vm212 开启；世界时钟为 Asia/Shanghai、`k=4` | 继续校准需求、关系、信息扩散和真实账单 |
| 生产修缮 | heat 时区混比、预算静默告警和 SBTI backfill 工具已完成并部署；26/26 居民已有完整 A2 维度；`_npc_choice` 的 option-0 结构性偏向（三条根因）已修复并合入主线 | 算法已修，但回填后的投票样本仍为零（现存 3 张 poll 全部早于回填且不重投）——部署后新开一张 poll 才能取到真实分布 |
| 社会地基 | S2-1 职位实体化、S1-3 舆论动力学已完成并在 vm212 开闸；S1-5 财政闭环（迁移 048）与 S2-5 政策分级审批（迁移 049）**已部署且已开闸**（`TOWN_TREASURY_ENABLED` / `POLIS_POLICY_ENABLED` / `POLIS_POLICY_APPROVAL_ENABLED` 三个开关 2026-07-25 15:31 起全为 true，vm212 迁移链头 049；来源：`ops-deploy-2026-07-26-report.md`，代码默认值仍为关闭）；四个财政条目已接线到 TreasuryService；S1-1 声誉已实现，选举与 NPC 投票已消费同一份数据 | 声誉仍 `REP_ENABLED=false` 且**不可直接开闸**（见运行口径）；市政厅尚未展示声誉 |
| 政治层边界 | 玩家创作居民（forge / import 五条创建路径）自动获得投票权与被选举权的泄漏已修复：新建居民类型为 `resident`，`is_autonomous`（人口）与 `is_civic_voter`（政治权利）按语义拆分；`purge_residents` 加玩家角色防呆；burn-in 报告加政治层边界探针 | **存量数据尚未回填**——07-25 之前泄漏的居民仍是 `npc`、仍持有投票权。回填是独立于代码部署的一次数据变更，须在无 open poll 的窗口执行（会改变法定人数分母） |
| 工程健康 | 夜间任务错过窗口补跑、聊天锁 DB 侧回收、后台 loop 心跳与死亡告警（`GET /health/loops`）代码完成，未部署 | 部署后观察补跑是否被触发、心跳是否误报 |
| 市政厅与实验楼 UI | 只读入口和面板已部署 | 随社会功能补充可操作流程和解释性数据 |
| Lab Agent | Mock、安全边界、审批、预算、制品和部分 OCI 证据已实现 | 未选择真实 Adapter；生产身份、镜像、网络、存储和外部 attestation 仍未满足，保持关闭 |
| 居民原创形象 | M1/M2 已并入主线但默认关闭（`RESIDENT_SPRITE_ENABLED=false`、`VITE_RESIDENT_SPRITE_ENABLED=false`），M3 本地安装已完成、尚未部署：provider v2 资格 `de3b7f662683410f87be7280966403ae` 已通过并签发 capability `e1ed7c34138a8f931b6928678bdc70306c1e65c2be475ee55c5f006187f40d8e`；批次 `9a11b878c6374ee6b28829e0742b6c06` 的 25/25 套候选通过自动 QC、真实 Phaser 四方向渲染与九项人工审核，累计审计 106 次请求；50 个文件已原子安装，资源树摘要为 `84e0f9e2366f84e8f4a856bb7f5a002fd0e787b53d4bafe87eb6133ef567463c` | 继续打磨生成质量、排队与费用控制；开关保持关闭，待 staging 完成迁移、独立 worker、管理员审核、热更新和回滚演练后另行决定是否启用 |
| 美术发布 | 16 个 tilemap 文件已由项目所有者确认授权并补齐署名；25 张居民纹理和 25 张头像已原创替换并带逐文件 receipt，source/dist 两层 release gate 均通过 | 部署构建产物并在 staging/生产抽查实际 Phaser 加载、缓存版本与回滚路径 |

## 阶段路线

| 阶段 | 状态 | 范围 | 完成标准 |
|---|---|---|---|
| **0. 决策与核心基线** | 完成 | M1-M6、世界时钟、拟真开闸、预算和行动量口径 | 决策进入配置与生产环境 |
| **1. 上线收尾** | 基本完成 | vm212 部署、迁移 047、heat 修复、告警、SBTI 回填、TownHall/LabTerminal | 算法偏向已修；投票分布复核仍缺样本（须新开 poll）；成本侧单边口径已核（07-15 起逐日与价目表一致），供应商账单比对未做 |
| **2. 社会地基** | 进行中 | S2-1、S1-3、S1-5、S2-5 已完成且已在 vm212 开闸；S1-1 声誉已实现但未开闸 | 官职、舆论、财政、政策和声誉形成可观察闭环——目前卡在声誉：信号只有负向来源，开闸会让选举反向（见运行口径） |
| **3. 降本提质** | 机制完成，持续优化 | 计量、预算熔断、模型路由、计划跳过、行为与记忆一致性探针 | 用连续生产数据校准单位成本与行为质量 |
| **4. 实验楼开放** | 受阻 | 真实 Adapter、隔离执行器、制品链路、生产审批 | 受保护的真实委托在 staging 跑通并通过外部审批后再灰度 |
| **5. 政治深化** | 未开始 | 抽签任官、轮值议事、集会投票、卸任审计、陪审、丑闻与政体演变 | 权力流转和冲突能形成持续故事 |
| **6. 生命周期** | 未开始 | 健康、医疗、年龄、退休、迁入迁出、死亡与继承 | 人口能自然更替且过程可感知 |
| **7. 扩容成熟** | 未开始 | 人口结构优化、25-40 人稳定运行、实验楼机构化、多镇对照 | 多个小镇可长期、可比较地自运行 |
| **8. 居民原创形象** | 主线代码完成但功能开关默认关闭；M3 本地生成、审核、安装与双层 provenance 清关完成，尚未部署 | 角色锚点、四方向动作条、像素图集后处理、自动 QC、Phaser 审核、动态发布，以及 25 个静态 slot 的批量原创替换 | 继续打磨；staging 完成迁移、独立 worker、动态发布、热更新与回滚演练后再决定启用 |

## 居民原创形象里程碑

| 里程碑 | 状态 | 范围 | 完成标准 |
|---|---|---|---|
| **M1：CLI 可生成** | 本地完成；provider v2 capability 已通过资格审核并实际用于批次生成 | 已完成严格 capability 绑定、独立 7-request/费用确认门、锚点和方向条生成、`96x128` 图集后处理、自动 QC、manifest、失败隔离、断点恢复，以及对中转 signed URL 的公网 DNS、无重定向、无凭据下载、PNG/尺寸上限和摘要化请求证据校验；非 HTTPS 结果下载仅可用显式 `insecure_http_test` receipt | 在 staging 以同一 capability 与 worker-only 密钥复验可恢复生成，并保持自动 QC、请求证据和预算门闭环 |
| **M2：管理员可审核发布** | 代码已并入主线，后端、worker 与前端入口默认关闭，未部署 | 已完成任务持久化、租约 worker、管理端进度、请求数与保守成本上界（未配置时明确显示未知）、真实 Phaser 四方向预览、九项人工审核、批准/拒绝/派生重试/回滚、原子静态发布和 `sprite_updated` 热更新 | 保持双开关关闭；部署迁移 050 与独立 worker后，配置经复核的单次请求成本上界，并在 staging 完成“生成 -> 审核 -> 发布 -> 热更新 -> 回滚”演练；未经批准的资源继续留在非公开 volume |
| **M3：25 套替换与清关** | 本地完成，尚未部署 | 已明确对象是 25 个可复用静态 sprite slot，不是当前 11 位 seed NPC；版本化文字外观清单禁止旧图视觉参考；批次 `9a11b878c6374ee6b28829e0742b6c06` 在 275 次 / 保守 `$275.00` 上限内累计审计 106 次请求，并以不可变合并证据复用同 capability、同 catalog、同 request hash 的已通过运行；25 套均经真实 Phaser 四方向渲染、截图固证与九项人工批准，50 个文件已原子替换，source/dist provenance gate 均通过 | 部署构建产物，在 staging 和生产抽查 25 个 slot 的实际加载、静态 URL batch 缓存更新与安装恢复路径 |

## 近期优先级

1. 部署主线到 vm212（迁移 049 → 050）。对存量数据是零行为变化的部署：库里当前没有任何 `resident_type='resident'`，`is_civic_voter` 圈定的人与部署前逐字节一致。部署后立即跑一次 burn-in 报告，政治层边界探针的读数即为回填前基线。
2. 回填存量泄漏居民（`resident_type` 由 `npc` 改为 `resident`）。**独立于第 1 项的一次数据变更**，不与代码部署同批；须在无 open poll 的窗口执行，因为它会缩小法定人数分母。验收以探针的前后读数为准。
3. 新开一张 poll 取真实投票分布，复核 `_npc_choice` 修复与 SBTI 回填的效果；账单侧仍缺供应商控制台数字。
4. 修复声誉语义后再开 `REP_ENABLED`：补正向信号源、把 `election_service` 的候选排序由截断改为加权、按实测分布重标定 `rep_credit_min_score`；然后让市政厅也消费同一份声誉数据。
5. 部署工程健康批并观察：夜间补跑台账、socializing 卡死回收、五个 loop 的心跳告警是否误报。
6. 在真实 PostgreSQL / Redis / WebSocket 上以 25 与 40 名自治居民跑扩容与成本测试（确定性测试工具已完成）。
7. Lab 继续保持 Mock-only；只有真实端点、rootless 隔离、制品存储、网络策略和外部 attestation 全部具备后才进入 staging。
8. 在 staging 演练迁移 050、独立 worker、管理端生成/审核/发布、`sprite_updated` 热更新和回滚；确认 provider 密钥不进入 API 容器。
9. 部署已清关的 25-slot 构建产物，抽查 source/dist receipt、静态 URL batch 缓存和生产 Phaser 四方向加载，并保留安装事务的恢复材料。

## 已确定的运行口径

- 世界时间唯一入口为 `backend/app/world_clock.py`；世界时锚 Asia/Shanghai，速度 `WORLD_CLOCK_K=4`。
- 行动配额按世界日计数；代码默认 `AGENT_MAX_DAILY_ACTIONS=20`，vm212 当前配置为 100。
- 全局 LLM 日预算上限为 `$10`；预算、TTL、日志和运维 cron 使用真实时间。
- `POLIS_OFFICE_ENABLED` 与 `POLIS_OPINION_ENABLED` 代码默认关闭，vm212 测试环境已显式开启。
- `TOWN_TREASURY_ENABLED`、`POLIS_POLICY_ENABLED`、`POLIS_POLICY_APPROVAL_ENABLED` 代码默认仍为关闭，但 **vm212 自 2026-07-25 15:31 起三个全部为 true**。关闭时行为与开发前逐字节一致（工资继续凭空铸造、售货不抽税、审批走单人 CAS、投票是相对多数）——该回退语义只对未开闸环境成立。
- **`REP_ENABLED` 在声誉语义修复前禁止开启。** 声誉分只有负向来源（`rep_gossip_base_tone=-0.3`、`rep_distortion_penalty=-0.2`，正向仅 `0.2 × mood_valence`），「没人议论」反而得分最高；而 `election_service` 按声誉排序后截断候选名单前 4，开闸会把被议论最多的居民系统性挤出候选，路人当选。`rep_credit_min_score=-0.3` 在现有信号强度下不可达（稳态下界约 −0.175），信用闸门是装饰性的。
- 政治层有两条**不同**的边界，不得合并成一个谓词：`Resident.is_autonomous`（人口／仿真，读它的有 agent loop、市政厅名册、职务查找、mayor 清扫、讲座池）与 `Resident.is_civic_voter`（政治权利，只有投票、法定人数分母、镇长候选池三处）。取值定义在 `app/services/civic_membership.py`。玩家创作居民为 `resident`：算人口、无政治权利。任何新增的 `Resident(...)` 构造点必须显式写 `resident_type`——依赖模型默认值 `"npc"` 正是 07-25 泄漏的根因，`test_ugc_resident_no_political_rights.py` 会扫描并拦截。
- 迁移链头为 `050_add_resident_sprites`（047 → `048_add_town_treasury` → 049 → 050）；vm212 当前停在 049。
- 后台 loop 心跳与聊天锁回收的旋钮以环境变量为运行时来源（`LOOP_HEARTBEAT_*`、`SOCIAL_STATUS_*`），`Settings` 中的同名字段只提供默认值。
- Lab 的代码默认仍为 `lab_adapter=mock`、`lab_oci_enabled=false`；任何真实执行能力都必须经过独立审批。
- 运行时生成的居民精灵发布到后端持久化 static/media volume，不能写入 Vite 构建产物；`sprite_key` 继续作为逻辑标识，生成资源使用内容哈希 URL 规避 Phaser/CDN 旧缓存。
- 居民原创形象首版只开放给管理员；所有候选资源必须经过自动 QC 与人工审核，采用“暂存写入 -> 完整校验 -> 原子发布 -> 数据库切换”的顺序，玩家自助生成须在成本和滥用控制具备后另行决策。
- 供应商请求采用持久化预算和阶段 claim 防止并发重复；若进程在已提交请求但本地结果尚未落盘时中断，系统保留 claim 与证据并要求人工恢复，不宣称跨外部供应商的 exactly-once。
- M3 静态替换以 `frontend/config/resident-sprite-generation.json` 的 25 个 slot 为唯一清单；当前 11 位 seed NPC 继续通过 `sprite_key` 复用这些 slot，环境数据库 resident ID 和旧 Smallville `agent.json` 人设都不能反向生成外观。
- M3 每个原始批次只允许每个 slot 一个预分配 run；新授权目标批次只能在 catalog、source policy、模型、基线树、价格快照、request hash 与 capability receipt 全部一致时，以不可变证据继承旧批次中通过自动 QC 的原始 run，并累计保留目标失败尝试成本；不得通过派生或链式继承绕过单项与批次请求上限。

## 路线图维护规则

- 只记录当前事实、未完成能力和下一道门，不累积提交日志、测试流水或一次性 kickoff。
- 功能“代码完成”“已部署”“已开闸”“生产验证”必须分开表述，不能互相替代。
- 阶段状态变化直接更新本文；详细证据进入新的日期归档，不再新增第二份现行计划。
- 历史文档中的 fallback、Mock、默认关闭和旧分支说明只描述当时状态，除非本文重新确认，否则不构成当前约束。
