# Simverse World Roadmap

> 状态基线：2026-07-27。本文是项目唯一现行规划文档；旧规格、过程记录、研究、测试报告与运行证据均已移入 [`archive/2026-07-25/`](../archive/2026-07-25/README.md)，只作为历史证据，不再代表当前实现或待办。

居民形象供应商资格、批次生成、审核、安装与恢复命令见 [`RESIDENT_SPRITE_OPERATIONS.md`](RESIDENT_SPRITE_OPERATIONS.md)。

## 当前基线

| 领域 | 当前状态 | 下一道门 |
|---|---|---|
| 核心小镇 | 已完成。账号、角色炼化、实时地图与对话、居民自治、记忆与人格、经济、集市、公告、赛季、辩论和基础治理已经接入主流程 | 继续以生产观测修正体验，不再单列基础里程碑 |
| M1-M6 扩展 | 已完成并部署。经济、故事弧、镇务自治、记忆评估、空间扩展和镇长选举已接电 | 观察长期节律与成本 |
| 拟真与世界时钟 | P0-P2 已实现并在 vm212 开启；世界时钟为 Asia/Shanghai、`k=4` | 继续校准需求、关系、信息扩散和真实账单 |
| 生产修缮 | heat 时区混比、预算静默告警和 SBTI backfill 工具已完成并部署；`_npc_choice` 的 option-0 结构性偏向（三条根因）已修复并合入主线 | **修复后的投票样本已经取到**：2026-07-25 23:00:12 UTC 夜间任务投出 33 票（worker 日志 `33 NPC civic votes cast`），option-0 占比由 100% 降到 42.4%，归一化熵 0.895~0.994。样本来自事故后重新 seed 的 11 位居民，够证「偏向已消除」，不够做人格-选项相关性分析 |
| 社会地基 | S2-1 职位实体化、S1-3 舆论动力学已完成并在 vm212 开闸；S1-5 财政闭环（迁移 048）与 S2-5 政策分级审批（迁移 049）**已部署且已开闸**（`TOWN_TREASURY_ENABLED` / `POLIS_POLICY_ENABLED` / `POLIS_POLICY_APPROVAL_ENABLED` 三个开关 2026-07-25 15:31 起全为 true，代码默认值仍为关闭）；四个财政条目已接线到 TreasuryService；S1-1 声誉已实现，选举与 NPC 投票已消费同一份数据。**F1 声誉语义修复已合入主线**（`6128ecb`）：tone 改由关系 affinity 决定、base_tone 退为偏置项；候选集选取不再按声誉排序截断，被动选举权与名声解耦；声誉入票收敛到唯一通道 `vote_trust_delta()` | 声誉仍 `REP_ENABLED=false`，但 F1 三项已全部落地：第三项于 2026-08-05 收口——vm212 真实分布跑通 `scripts/rep_calibrate.py`（n=11，affinity 覆盖率 93.0%），`rep_credit_min_score` 由装饰性的 `-0.3`（拒绝 0/11）改为实测 `0.0058`（拒绝 2/11）；市政厅已消费同一份声誉数据（`/townhall/overview` reputation 节 + 声誉 tab）。下一道门：开闸动作本身（单独部署）+ 三条线接入 `nightly_cron`（见近期优先级 5） |
| 政治层边界 | 玩家创作居民（forge / import 五条创建路径）自动获得投票权与被选举权的泄漏已修复：新建居民类型为 `resident`，`is_autonomous`（人口）与 `is_civic_voter`（政治权利）按语义拆分；`purge_residents` 加玩家角色防呆；burn-in 报告加政治层边界探针。F2 已合入主线（`6128ecb`）：晋升/撤销写入口、`civic_standing_history` 表（迁移 051）、三态闸门 `CIVIC_PROMOTION_MODE`、读写双守卫 | **存量泄漏实例实测为 0**——`SELECT resident_type,count(*) FROM residents` → `npc\|11`，全是 07-25 事故后重新 seed 的内置阵容。所以回填（T2）在 vm212 上是**空跑，0 行是正确结果不是失败**。真正的洞是**修复尚未部署**：生产容器内无 `civic_membership.py`、`alembic current` = 049，今天在生产 forge 一个居民照旧拿到投票权与被选举权。**部署（T1）才是关泄漏的动作，不是回填**。T1 已于 2026-07-28 01:26 UTC 完成，生产实测 `CIVIC_VOTER_TYPES=['npc']` / `SIM_RESIDENT_TYPES=['npc','resident']` / `UGC_RESIDENT_TYPE='resident'`，边界探针读数「居民 11 / 有政治权利 11 / 算人口 11」为回填前基线 |
| 工程健康 | 夜间任务错过窗口补跑、聊天锁 DB 侧回收、后台 loop 心跳与死亡告警（`GET /health/loops`）**已部署并在产**，自 2026-07-25 16:53:59 UTC 起生效 | 补跑已被真实触发（07-25 15:32 worker 日志 `nightly: anchor 07:00 … catching up now`）；`/health/loops` 五路 loop 全 `ok`、`stale` 为空。**尚未被检验的是告警路径本身**——线上从未出现过 stale，「loop 真死了会不会报」这一路仍无证据 |
| 市政厅与实验楼 UI | 只读入口和面板已部署 | 随社会功能补充可操作流程和解释性数据 |
| Lab Agent | Mock、安全边界、审批、预算、制品和部分 OCI 证据已实现 | 未选择真实 Adapter；生产身份、镜像、网络、存储和外部 attestation 仍未满足，保持关闭 |
| 居民原创形象 | M1/M2 已并入主线但默认关闭（`RESIDENT_SPRITE_ENABLED=false`、`VITE_RESIDENT_SPRITE_ENABLED=false`），M3 本地安装已完成、尚未部署：provider v2 资格 `de3b7f662683410f87be7280966403ae` 已通过并签发 capability `e1ed7c34138a8f931b6928678bdc70306c1e65c2be475ee55c5f006187f40d8e`；批次 `9a11b878c6374ee6b28829e0742b6c06` 的 25/25 套候选通过自动 QC、真实 Phaser 四方向渲染与九项人工审核，累计审计 106 次请求；50 个文件已原子安装，资源树摘要为 `84e0f9e2366f84e8f4a856bb7f5a002fd0e787b53d4bafe87eb6133ef567463c` | 继续打磨生成质量、排队与费用控制；开关保持关闭，待 staging 完成迁移、独立 worker、管理员审核、热更新和回滚演练后另行决定是否启用 |
| 美术发布 | 16 个 tilemap 文件已由项目所有者确认授权并补齐署名；25 张居民纹理和 25 张头像已原创替换并带逐文件 receipt，source/dist 两层 release gate 均通过 | 部署构建产物并在 staging/生产抽查实际 Phaser 加载、缓存版本与回滚路径 |

## 阶段路线

| 阶段 | 状态 | 范围 | 完成标准 |
|---|---|---|---|
| **0. 决策与核心基线** | 完成 | M1-M6、世界时钟、拟真开闸、预算和行动量口径 | 决策进入配置与生产环境 |
| **1. 上线收尾** | 基本完成 | vm212 部署、迁移 047、heat 修复、告警、SBTI 回填、TownHall/LabTerminal | 算法偏向已修，**投票分布样本已取到**（07-25 23:00 的 33 票，option-0 由 100% 降至 42.4%）；成本侧单边口径已核（07-15 起逐日与价目表一致），供应商账单比对未做 |
| **2. 社会地基** | 进行中 | S2-1、S1-3、S1-5、S2-5 已完成且已在 vm212 开闸；S1-1 声誉已实现，F1 语义修复已合入但未开闸 | 官职、舆论、财政、政策和声誉形成可观察闭环——F1/F2/F3 的接线与配置面已于 2026-08-05 收口（近期优先级 5），`rep_credit_min_score` 亦已同日用 vm212 真实分布重标定（0.0058，拒绝面 2/11）；现在卡的只剩开闸动作本身（`REP_ENABLED` 等，独立变更，走评估报告 §5 六步单） |
| **3. 降本提质** | 机制完成，持续优化 | 计量、预算熔断、模型路由、计划跳过、行为与记忆一致性探针 | 用连续生产数据校准单位成本与行为质量 |
| **4. 实验楼开放** | 受阻 | 真实 Adapter、隔离执行器、制品链路、生产审批 | 受保护的真实委托在 staging 跑通并通过外部审批后再灰度 |
| **5. 政治深化** | 已开局（F2/F3 已接线，开关默认关） | 抽签任官、轮值议事、集会投票、卸任审计、陪审、丑闻与政体演变 | 权力流转和冲突能形成持续故事。**已落代码**：F3 的任期到期触发补选（补掉「无限期无镇长」断链）+ 卸任财政审计；F2 的公民权晋升/撤销三档模型。**已接线未开闸**：F2 pass 在夜间链钦定位（163e9b1），F3 审计+补选经 `term_check` 在链上；三条线旋钮已进 Settings 与两份 .env.example（2026-08-05 收口），开关全默认关，开闸是独立变更 |
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

> 状态基线 2026-07-27 晚。`origin/master` = `6128ecb`（含管理系统「立刻做」批次 + F1/F2/F3 三条线）；vm212 仍停在 `049`，跑的是 2026-07-25 的镜像。**代码与生产之间隔着 3 个批次**，下面 1、2 两项是解开这个错位的动作。

1. ~~部署主线到 vm212（迁移 049 → 051）~~ —— **已完成**（2026-07-28 01:26 UTC，报告见 [`reports/ops-deploy-2026-07-28-T1.md`](reports/ops-deploy-2026-07-28-T1.md)）。落地内容：
   - 政治层边界修复（`civic_membership.py` 等）——**这才是关 UGC 投票权泄漏的动作**，不是回填
   - 管理系统批次：平台密钥读侧掩码、哨兵账号铸币收口、`0.0.0.0:8100` 收回回环
   - F1/F2/F3 的代码（开关全默认关，接线未做，**部署本身不改变任何运行时行为**）
   - bootstrap 会往 `users` 插一行 `id='system'` 哨兵（幂等 additive）。**部署前后各查一次** `SELECT id,email FROM users WHERE id='system';`（前应为空，后应恰好一行）
   `docker-compose.yml` 是宿主文件，`git archive backend` 送不上去，端口收窄要单独一步 scp + `docker compose up -d`。
2. ~~回填存量泄漏居民~~ —— **在 vm212 上已无对象**。实测 `residents` 只有 11 行且全是 `npc` 内置阵容，泄漏实例为 0。回填脚本仍要写（其它环境可能有存量，且它是 F2 晋升判定的锚点载体），但在本环境跑出 0 行是正确结果。
3. **在途 3 张 poll 已延期到 2026-07-31T23:29:43Z**（2026-07-27 执行，`scripts/postpone_open_polls.py`，完成标记 `system_config.civic_poll_postpone_until`）→ 实际关票在 **2026-08-01 23:00 UTC**。第 1 项已于 07-28 完成，**结票会走安全路径**：候选人全部不在籍 → `install_mayor` 零写入 + 流会公告，不会出现「公告说某人当选、库里没有镇长」。届时复核公告正文与 `SELECT slug, meta_json->'mayor' FROM residents` 读数即为验收证据。
4. ~~跑 `scripts/rep_calibrate.py` 用真实分布重标定 `rep_credit_min_score`；再让市政厅消费同一份声誉数据~~ —— **已完成**（2026-08-05，评估见 [`reports/2026-08-05-rep-gate-assessment.md`](reports/2026-08-05-rep-gate-assessment.md)）：vm212 实测 n=11、affinity 覆盖率 93.0%，阈值 `-0.3`（拒绝 0/11，装饰性）改为 `0.0058`（拒绝 2/11）；`/townhall/overview` 新增只读 `reputation` 节，市政厅面板加「声誉」tab（信用受限徽记 + 未开闸横幅）。开闸动作本身单独部署，不与本批同车。
5. **统一收口**：~~`config.py` / `.env.example` 补齐三条线的旋钮；`nightly_cron` 接入 F2 晋升 pass（位置写死在 `close_due_polls` 之后、`run_npc_voting` 之前）与 F3 `office_audit`。**不做这一步，F1/F2/F3 的代码在运行时是死的。**~~ **已完成（2026-08-05）**：F2 接线在 163e9b1；F3 经 term_check 在链上（回归钉 tests/test_nightly_office_audit_wiring.py）；CIVIC_×12 进 Settings、F3 空缺阈值旋钮、deploy env 25 键补齐（tests/test_civic_settings_knobs.py / test_env_example_consistency.py 守住）。开关全默认关，开闸另行变更。
6. 在真实 PostgreSQL / Redis / WebSocket 上以 25 与 40 名自治居民跑扩容与成本测试（确定性测试工具已完成）。
7. Lab 继续保持 Mock-only；`feat/lab-codex-runtime` 合入前必须把它的 `051/052/053` 重编号——它的 `051_add_lab_codex_model_tier` 与主线的 `051_add_civic_standing_history` **都挂在 `050` 上，合并即双头**。
8. 在 staging 演练迁移 050、独立 worker、管理端生成/审核/发布、`sprite_updated` 热更新和回滚；确认 provider 密钥不进入 API 容器。
9. 部署已清关的 25-slot 构建产物，抽查 source/dist receipt、静态 URL batch 缓存和生产 Phaser 四方向加载，并保留安装事务的恢复材料。

## 已确定的运行口径

- 世界时间唯一入口为 `backend/app/world_clock.py`；世界时锚 Asia/Shanghai，速度 `WORLD_CLOCK_K=4`。
- 行动配额按世界日计数；代码默认 `AGENT_MAX_DAILY_ACTIONS=20`，vm212 当前配置为 100。
- 全局 LLM 日预算上限为 `$10`；预算、TTL、日志和运维 cron 使用真实时间。
- `POLIS_OFFICE_ENABLED` 与 `POLIS_OPINION_ENABLED` 代码默认关闭，vm212 测试环境已显式开启。
- `TOWN_TREASURY_ENABLED`、`POLIS_POLICY_ENABLED`、`POLIS_POLICY_APPROVAL_ENABLED` 代码默认仍为关闭，但 **vm212 自 2026-07-25 15:31 起三个全部为 true**。关闭时行为与开发前逐字节一致（工资继续凭空铸造、售货不抽税、审批走单人 CAS、投票是相对多数）——该回退语义只对未开闸环境成立。
- **`REP_ENABLED` 默认仍为关，但三条开闸前置已全部清空。** F1（`6128ecb`）修掉前两条：tone 不再是常量负值、改由该条八卦对应的关系 affinity 决定（`rep_gossip_base_tone` 退为偏置项）；`election_service` 的候选集选取不再按声誉排序截断，声誉只经 `vote_trust_delta()` 影响得票、不决定谁能参选。第三条已于 2026-08-05 收口：`rep_credit_min_score` 用 vm212 真实分布标定为 `0.0058`（`app/config.py`，拒绝面 2/11 非空非全量；旧值 `-0.3` 实测拒绝 0/11 属装饰性闸门），标定证据与开闸操作单见 [`reports/2026-08-05-rep-gate-assessment.md`](reports/2026-08-05-rep-gate-assessment.md)。注意 `credit_allowed()` 尚无生产消费方（唯一活读者是市政厅 `credit_ok` 展示），开闸影响面 = 夜间 recompute 落库 + `vote_trust_delta()` 入票。
- 政治层有两条**不同**的边界，不得合并成一个谓词：`Resident.is_autonomous`（人口／仿真，读它的有 agent loop、市政厅名册、职务查找、mayor 清扫、讲座池）与 `Resident.is_civic_voter`（政治权利，只有投票、法定人数分母、镇长候选池三处）。取值定义在 `app/services/civic_membership.py`。玩家创作居民为 `resident`：算人口、无政治权利。任何新增的 `Resident(...)` 构造点必须显式写 `resident_type`——依赖模型默认值 `"npc"` 正是 07-25 泄漏的根因，`test_ugc_resident_no_political_rights.py` 会扫描并拦截。
- 迁移链头为 `051_add_civic_standing_history`（047 → `048_add_town_treasury` → 049 → 050 → 051），单头，实测 `ScriptDirectory.get_heads()` 返回一个。**vm212 已于 2026-07-28 01:26 UTC 部署到 051**（见 [`reports/ops-deploy-2026-07-28-T1.md`](reports/ops-deploy-2026-07-28-T1.md)）。`feat/lab-codex-runtime` 分支上的 `051_add_lab_codex_model_tier` 也挂在 `050` 上，**合入前必须重编号为 052 并把 `down_revision` 改指 `051_add_civic_standing_history`**，否则一合就是双头 + 文件名撞号。
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
