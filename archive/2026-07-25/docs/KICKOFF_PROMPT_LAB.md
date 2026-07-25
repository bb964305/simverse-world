# 实验楼（Lab）P0–P3 代码实施 · 启动提示词

> 用法：整段粘贴到新窗口作为首条消息。执行环境：Cowork（已连接 simverse-world 仓库）。

---

请通读 docs/FEATURE_SPEC_LAB.md（v0.2，已按代码逐条核对修订），然后一次性实施 P0→P3 全部代码。§13 是文件级实施清单，§4 数据模型，§5–§7 各层详设，动手前先读完。

## 现状（已备好，勿重复）
- 分支 `feature/lab` 已创建并 checkout（基于 feat/rate-limiting-p1 @3586ebb），HEAD=de31d13（spec 提交）。直接在此分支工作，不要新建/切换分支。
- 仓库现有未跟踪文件（Fable5-提示词指南.md、docs/KICKOFF_PROMPT_V6.md、backend/skills_world_dev.db.bak-20260713）不要动、不要提交。
- git 提交身份已配好（Simverse Agent）。

## 硬约束（违反任何一条立即停手）
1. 只写代码 + git commit。禁止：执行 alembic upgrade（迁移文件要写，但绝不执行）、重启/部署任何服务、改生产 Redis/DB、git push、切换分支。
2. 生产环境正在跑 48h burn-in（至 2026-07-17 11:00 UTC 退出评估），以上禁令在此之前无例外。
3. 不跑 pytest / npm install / npm build（测试明天在 Claude Code 做）。允许的自检仅限：`python3 -m py_compile`、语法级检查、grep 一致性核对。
4. 若 git 报 "Operation not permitted"（.git 下 lock/tmp 残留，本环境挂载有 unlink 限制）：清理 `.git/HEAD.lock` 与 `.git/objects/**/tmp_obj_*` 后重试；rm 被拒就先申请文件删除权限，不要放弃提交。

## 实施顺序与提交
严格按 spec §13 依次 P0 → P1 → P2 → P3，**每阶段一个 commit**（`feat(lab): P0 建筑与骨架` 等）。写每个文件前先读同目录/同类现有文件，严格贴合既有约定（模型命名、router 注册、ws 帧格式、admin 面板接入；§13 开头有约定回顾）。

各阶段强调（v0.2 修订点，spec 内都有）：
- **P0**：ExperimentPanel 挂 `TopNav.tsx`（不是 GamePage）；**不要改 tilemap.json**（美术后补），实验楼 bounds 选不与现有 LOCATIONS 重叠的空地并在报告注明坐标；RESEARCH 是第 15 个 ActionType，只追加，不改既有 14 个的名称/顺序/语义；地点数据三处（map_data.py / districtZonesData.ts / decor.ts）同步。
- **P1**：金库用 `resident_treasuries` 表（§4.7，**禁止放 meta_json**）；budget/cost 用整数分；`approvals_json` 是列表；队列 BRPOPLPUSH + processing list + ack + `heartbeat_at`；settle 断言 sum(splits)==hold.amount；验收默认 manual + 72h 自动放款、产物放款后解锁、拒收限 1 次；公开招募由后端规则自动分派。迁移 `032_add_lab_core.py` 的 `down_revision="031_add_home_decor"`，**只写不执行**。
- **P2**：kill switch 用 Redis 运行时标志 `sv:lab:enabled`（config 键仅部署级开关）；审批超时（`lab_approval_timeout_s`）默认拒绝；真实 adapter 全走可配置 base_url（空串=未配置，对齐 portrait/tts 分组约定），无外部依赖也必须能 import。
- **P3**：`GET /world/locations` 静态+动态合并接口；map_data 动态重载走 Redis pub/sub 且 API/agent-worker/lab-runner 各进程订阅并重建 location_tracker tile 索引；apply 前结构校验 + bounds/slug/出生点冲突检测；location_lore 实际在 `backend/app/agent/location_lore.py`；ProposalsPanel 只参照 EventsPanel 的列表布局（它没有批准/驳回交互，审批 UI 需新设计）；minimap 改「静态 + 运行时动态」合并渲染，动态地点不进 LocationKey 联合类型；迁移 `033_add_world_governance.py` down_revision 指向 032，只写不执行。
- 各阶段测试文件（test_lab_building.py / test_lab_economy.py / test_lab_task_flow.py / test_lab_sandbox_guard.py / test_world_governance.py）**都要写好但不执行**。

## 完成后输出
1. `git log --oneline`（4 个阶段 commit）+ `git diff --stat` 总览。
2. 写 `docs/LAB_HANDOFF.md`（随最后一个 commit）：明天 Claude Code 的测试交接清单——隔离测试库搭建、要跑的测试文件与顺序、alembic 032/033 执行与回滚说明、已知未验证风险点（至少含：全部代码未经运行验证、前端未过 tsc/build、tilemap 未画、真实 adapter 未连通、Redis pub/sub 重载未实测）。
3. 与 spec 的一切偏离逐条列出；实施中发现 spec 与实际代码冲突时，以实际代码为准并记录。
