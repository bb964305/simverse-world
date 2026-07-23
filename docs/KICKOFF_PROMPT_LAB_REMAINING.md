# Kickoff：实验楼 Agent v1 剩余开发（P2 收尾 → P3 → P4 → P5）

> 用法：将本文件整体作为开发 Agent 的任务提示词。
> 顺序执行：`$ralph docs/KICKOFF_PROMPT_LAB_REMAINING.md`
> 协作执行（T3 起可并行）：`$team docs/KICKOFF_PROMPT_LAB_REMAINING.md`

## 角色与工作方式

你是本仓库 `feat/lab-agent-v1` 分支上的实现工程师。开工前必须精读并服从以下权威文档（冲突时以 PRD 的安全约束优先）：

1. `.omx/plans/prd-cyber-lab-agent.md`（产品/架构/验收矩阵 V01–V23）
2. `.omx/plans/test-spec-cyber-lab-agent.md`（测试规格与各阶段门禁）
3. `.omx/plans/art-spec-cyber-lab-agent.md`（美术规格 A0–A5、ART-01–16）
4. `docs/adr/ADR-lab-runtime-adapter.md`（当前未选型状态）
5. `AGENTS.md`（结构、命令、提交规范）

工作循环：每个任务先写/扩测试 → 实现 → 跑对应门禁 → scoped conventional commit（`feat(lab):` / `fix(map):` 等，命令式摘要）→ 更新 `docs/PROGRESS.md` 简记。不跳过任何安全不变量；所有失败路径 fail-closed。

## 当前基线（2026-07-19 已实测验证，禁止重做）

- 后端 249 个 lab/world 测试全绿（沙箱实跑）。
- **已完成**：P0 adapter gate 框架 + ADR（未选型）；P1 全量（signed grants + depth-1 attenuation、policy `deny>ask>allow`、Broker、审批原子消费、lease/fencing、ledger+outbox、8 维硬预算、tenant ACL、artifact 摘要/保留/清理墓碑）；World Governor v1（`add_lore`/`edit_location` apply/revert + before-state + canonical `world_changed` envelope）；supervision（cursor ack/replay、cancel TERM/KILL 升级、kill-switch 演练）；OCI executor + 对抗套件（仅 colima dev-grade 证据）。
- 配置现状：`lab_agent_v1_enabled=False`、`lab_adapter="mock"`、`lab_oci_enabled=False`、Hermes/OpenClaw/computer_use endpoint 全部未配置。
- **未做**：`test_lab_world_e2e.py`；小地图 17×15 修正与 `inclusiveBoundsToTileRect()`；adapter 真实选型；Linux runner OCI 证据；A0–A5 美术全线（无 `paintExperimentBuilding`、无 `verify-lab-art.mjs`、无 `frontend/public/assets/village/lab/`）；P3 API/UI 迁移（前端无 `allowed_actions/can_decide`、无 `researching` 视觉、`ExperimentPanel.tsx` 为旧版）；P4 专家 worker；P5 硬化。

## 硬约束（违反即任务失败）

1. **不伪造 adapter 评分。** runtime endpoint 未配置时保持 Mock、ADR 维持未选型，绝不臆造分数。
2. colima/Docker Desktop 不是生产隔离证据；`lab_oci` 门必须在专用 Linux runner（cgroup v2、rootless OCI、seccomp/AppArmor、受控 egress）通过后才可在 staging 启用真实执行。
3. 原始思维链不落库、不进 WS、不进 DOM；审批控件只由服务端 `allowed_actions/can_decide/decision_scope/status` 投影驱动；R4 永远拒绝且不渲染批准控件。
4. 地图只能经生成脚本落图；禁止手改最终 tilemap JSON；前后端 tilemap 必须字节一致；`Collisions` 是唯一寻路权威；`World/Arena/Sector/Spawning/Special` 块层不动。
5. 第三方素材（CuteRPG/Room Builder/interiors）在 A0 授权审计完成前不得导出衍生资产用于发布。
6. 引入 Playwright 等新前端依赖需显式审批；未批前 V15/V19–V22 用 in-app browser + lint/tsc/build 方案。
7. 现有全量回归（`cd backend && python3 -m pytest tests/`）在每个任务收尾时保持绿。

## 任务队列（按序执行；T3 之后美术线与后端线可并行）

### T0 快速收尾（先做，约 0.5 天）

1. 新建 `backend/tests/test_lab_world_e2e.py`：按 test-spec V13/V15 的 world 段覆盖「draft → 验证 → Compiler → preflight → admin approve → 一次 revision → revert 恢复 before-state → 二次 apply 冲突拒绝」，含 stale `base_world_revision` 与未验证 draft 的负路径。
2. 修 `frontend/src/components/minimap/districtZonesData.ts` 实验楼 `tileRect` 为 `w:17,h:15`；新建共享 `inclusiveBoundsToTileRect()`（`x2-x1+1`/`y2-y1+1`），静态与动态地点统一调用；补前端单测或类型级断言。

### T1 P2 收尾与 adapter 选型（条件分支）

- **若** `LAB_HERMES_BASE_URL` 等真实 endpoint 已配置：对两个候选跑 `adapter_gate.run_conformance`，记录五维得分与证据链接；仅当 ≥80 且三个 mandatory 全过才接入**唯一**选中 adapter（handshake、cursor/ACK/replay、cancel/kill、health、resume/checkpoint），更新 ADR 为 Accepted，落选者留档。开启 V04–V06 全量断言。
- **否则**：输出书面阻塞报告（缺哪些 env、如何配置），ADR 保持未选型，跳过接入继续后续任务（Mock 路径不受影响）。
- OCI：编写专用 Linux runner 的准备脚本/文档，在该 runner 上跑通 `pytest -m lab_oci tests/integration/test_lab_executor_oci.py` 并存证（V11）；此前 `lab_oci_enabled` 保持 False。

### T2 A0 素材授权审计（美术线前置门）

建立受版本控制的资产来源清单（文件、作者/商店、原始 URL、许可证/版本、修改记录、商用/衍生许可），覆盖 CuteRPG、Room Builder、interiors 与后续新增资产；接入发布打包检查（V23）。

### T3 A1 确定性地图落图（V16/V17，可与后端线并行）

1. 将 `frontend/scripts/expand-town-map.mjs` 拆为纯函数 `buildExpandedTilemap(source)` + CLI 写盘包装。
2. 新增幂等 `paintExperimentBuilding()` 阶段：按 art-spec blockout 落 17×15 分区（入口门廊/任务台/西沙箱/中央走廊/东验证舱/档案区/Governor 台/维护带）；清 `(108,72)–(124,86)` 视觉 allowlist 内森林；清 `(115,66)–(117,72)` 三格接入路；`Collisions` 按平面图重新作者化（入口 `(116,72)`、中心 `(116,79)`、主走廊 ≥2 格、五热点正面 GID 0）；`Object Interaction Blocks` 写作者元数据（v1 无运行时消费者）。
3. 新建 `frontend/scripts/verify-lab-art.mjs`：内存双次生成 hash 一致、17×15 bounds、原始 Collisions 独立 flood-fill（hub `(75,56)` 与接入点 `(116,65)` → 入口与各热点）、语义块层字节不变、前后端 `cmp` 字节一致、`maze.size=[128,180]`、atlas 尺寸/帧名校验；不污染工作树。
4. 生成并提交前后端两份 tilemap；GID 按 tileset name + local id 解析，不写死全局 GID。

### T4 A2 地图精修与状态 FX（V18、V19 场景部分）

`lab_fx_32.png/json` 信标动效（idle/queued/running/approval/verifying/completed/failed/conflict/disconnected，帧率与 reduced-motion 静态帧按 art-spec）；`researching` 16/32px 图标 + Resident 头顶视觉接入 `GameScene`/`StatusVisuals`；仅当现有 tileset 无法表达时才新增 `lab_cyber_32.png`（≤512×512）。

### T5 P3 后端 API 与投影（可与 T3/T4 并行）

1. cursor 事件 API（stable `seq`、断点续读）；`LabRunStep`/`approvals_json` 保留为兼容投影并加弃用遥测。
2. 审批 API：canonical action preview + digest、`allowed_actions/can_decide/decision_scope/status` 服务端投影；观察者/非 owner/非 admin 只读；伪造决策按策略返回 403/404。
3. Artifact manifest/verification API（类型、producer、provenance、scan、verification、retention hold）。
4. 任务发布 capability profiles 与显式 `deliverable kind`（含 `world_change`）。
5. `world_changed` WS/outbox 投影按 art-spec「World Changed v1 事件契约」逐字段冻结；`GET /world/locations` 与 Codex snapshot 返回相同 `world_revision_id + source_cursor`。

### T6 A3/A4 前端 UI 迁移（V19–V22、ART-06–16；依赖 T5）

1. 重构 `ExperimentPanel.tsx`：计划/动作/证据/结果四轨时间线、Agent 临时工作单元徽标、预算/耗时展示；不渲染原始思维链。
2. 双状态体系：Task 10 态与 Run 6 态分开渲染，`verifying` 仅为 phase，`researching` 只读 Resident activity，connection overlay 冻结动效不改写 badge，未知值显示静态"未知状态"；解析顺序按 art-spec 六条规则。
3. 执行审批（琥珀盾牌，actor/tool/target/side effect/cost/expiry/digest）与世界治理审批（地图+印章、before/after、只读 bounds/entrance、conflict、rollback）视觉分离；hard deny 红色终态无批准按钮。
4. Artifact 列表（6 类型 + 7 角标）；未扫描外链零远程缩略图请求。
5. apply/revert：主地图、小地图、Codex 按同一 revision 收敛，`event_id` 去重、`world_revision_id+action+location_slug` 单次动效、gap → resyncing；三档视口（1440×900/768×1024/390×844）+ reduced motion + 44×44px 触控区 + 对比度达标。
6. 每轮视觉迭代跑 `$visual-verdict`，≥90 且无阻断项（溢出/遮挡/空白/错误动画/非整数缩放/不可达）。

### T7 P4 专家 worker（依赖 T1 接入或 Mock 多 worker 模拟）

Scout/Builder/Verifier/Archivist/World Cartographer 角色化：depth-1 委托、并发上限 3、子授权严格子集（复用 `grants` 衰减规则）、聚合预算持久化、独立 Verifier 只读+测试执行、Archivist 脱敏摘要入长期记忆；扩展 V03/V10 断言与取消/清理测试。

### T8 P5 硬化

retention/cleanup 证据（tombstone、hold、quarantine 演练）、content-free 遥测与告警（orphan 心跳、stale epoch、blocked egress、审批超时、预算耗尽、apply/revert 失败）、chaos/容量测试、staging kill/rollback runbook 演练、生产隔离候选（gVisor/Kata/Firecracker）评估记录。

## 每任务门禁命令

```bash
# 后端（T0 起 world_e2e 纳入）
(cd backend && python3 -m pytest tests/test_lab_protocol.py tests/test_lab_grants.py \
  tests/test_lab_policy_engine.py tests/test_lab_approval_flow.py tests/test_lab_fencing.py \
  tests/test_lab_outbox.py tests/test_lab_budgets.py tests/test_lab_tenant_acl.py \
  tests/test_lab_artifacts.py tests/test_lab_retention.py tests/test_world_revision.py \
  tests/test_lab_e2e.py tests/test_lab_world_e2e.py)
(cd backend && python3 -m pytest tests/)                     # 全量回归
(cd backend && python3 -m pytest -m lab_oci tests/integration/test_lab_executor_oci.py)  # 仅 Linux runner

# 前端
(cd frontend && npm run lint && npx tsc --noEmit && npm run build)

# 地图（T3 起）
node frontend/scripts/expand-town-map.mjs
node frontend/scripts/verify-lab-art.mjs
cmp frontend/public/assets/village/tilemap/tilemap.json backend/app/assets/village/tilemap/tilemap.json

# 部署
docker compose -f deploy/backend/docker-compose.yml config
```

## 完成定义

- V01–V23 中除依赖真实 endpoint（V04–V06 真机段）与专用 Linux runner（V11）的项外全部落地并绿；被阻塞项有书面阻塞报告 + ADR 记录，不以降级实现冒充通过。
- 三份规格各自的「完成定义/Completion Rule」全部满足；`$visual-verdict` ≥90 无阻断项；资产来源清单完整。
- 最终跑一次完整 verifier pass：上述全部门禁 + E2E world apply/revert + kill-switch 演练记录，结果写入 `docs/PROGRESS.md`。
