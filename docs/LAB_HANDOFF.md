# 实验楼（Lab）P0–P3 实施交接 · 给明天的 Claude Code 测试

> 分支 `feature/lab`。四个阶段各一 commit：
> `c00b104` P0 建筑与骨架 · `3ad71ad` P1 委托与托管经济 · `1be0731` P2 真实沙箱 · `f4884b7` P3 世界自改治理。
> （P0 与 P1 之间夹了一条外部提交 `46e86bd docs: PROGRESS`，非本特性改动，仅动 `docs/PROGRESS.md`。）
>
> **全部代码未经运行验证**（本环境 Python 3.10，仓库要求 ≥3.11，只做了 `py_compile` + grep 一致性核对；未跑 pytest / tsc / build，未执行 alembic）。本文件是明天在正式环境跑测试的清单。

---

## 1. 隔离测试库

后端测试**不依赖 alembic**：`tests/conftest.py` 用内存 SQLite（`db_engine` fixture + session 级全局引擎）跑 `Base.metadata.create_all`。本特性所有新表都已在 `app.main` 导入链里注册（`lab_task_service` / `coin_service` / `proposal_service` / `apply` / 路由都 import 了对应 model），故 `create_all` 会自动建出 `lab_tasks / lab_runs / lab_run_steps / lab_artifacts / coin_holds / resident_treasuries / world_change_proposals / dynamic_locations / dynamic_mechanics`。Redis 用 `fakeredis`（autouse），无需真实服务。

因此第一步只需在 3.11 环境装依赖后直接 `pytest`，无需先建库。

## 2. 要跑的测试文件与顺序

新增（本特性）：

1. `tests/test_lab_building.py` — P0：建筑注册/非重叠/查询、RESEARCH 门控、叙事 execute、入场帧、`load_dynamic_locations` 异步用例。
2. `tests/test_lab_economy.py` — P1：原子 `charge`/`transfer`/`hold`/`settle`(守恒)/`refund`、金库 credit/debit。
3. `tests/test_lab_task_flow.py` — P1：发布→run(mock)→验收放款(创作者+金库)、取消/过期退款、公开招募自动分派、拒收限 1 次、余额不足拒绝。
4. `tests/test_lab_sandbox_guard.py` — P2：scope 白名单、金融硬拒/敏感断点/超时默认拒、预算熔断、脱敏、出口白名单+SSRF、未配置适配器 `start` 报错、runner 级越权退款。
5. `tests/test_world_governance.py` — P3：add_location 校验、apply→合并 LOCATIONS+tile 索引→回滚、bounds 冲突置 failed、金库燃料冻结/驳回退回/不足拒绝。

**回归重点**（本特性改了既有文件，务必确认没跑挂）：

- `tests/test_agent_actions.py` — 已把 `test_all_14_action_types_exist` 改名 `test_all_15_action_types_exist` 并加入 `RESEARCH`。
- `tests/test_coins.py` — `charge` 改成行级原子 `UPDATE ... WHERE balance>=amount`；既有断言（扣款/不足不变/流水一条）应仍成立。
- `tests/test_location_tracker.py` — `location_tracker` 加了 `experiment_prompt` 分支与 `rebuild_lookup()`，索引构建逻辑未动。
- `tests/test_agent_phases.py` — execute 加了 `RESEARCH` 分支（其余分支未动）。

> 跨会话脆弱点：`test_lab_task_flow` / `test_world_governance` 沿用 `test_location_tracker` 的做法 patch `async_session` 到测试引擎。SQLite `:memory:` 用 StaticPool 共享单连接，runner/apply 内部再开 session 时与外层 session 复用同一连接——已尽量「每步一个独立 `async with factory()`、先提交再读」规避，但如遇 `database is locked` 或身份映射陈旧，优先怀疑这里（真实 Postgres 无此问题）。

## 3. alembic 032 / 033（在隔离 Postgres 上单独验证，勿在生产跑）

链路：`031_add_home_decor` → `032_add_lab_core` → `033_add_world_governance`（`down_revision` 已核对成链）。

```bash
# 在一个隔离的空 Postgres 上（切勿指向生产/burn-in 库）：
alembic upgrade head          # 应从 031 依次建到 033
alembic downgrade -1          # 回滚 033（drop 3 张治理表）
alembic downgrade -1          # 回滚 032（drop 6 张 lab 表）
alembic upgrade head          # 再次前滚，确认幂等/无残留
```

- `032` 建：`coin_holds / resident_treasuries / lab_tasks / lab_runs / lab_run_steps / lab_artifacts`（含索引）。
- `033` 建：`world_change_proposals / dynamic_locations / dynamic_mechanics`（含唯一索引）。
- 迁移只做建表 + 索引，未加外键约束（沿用仓库既有风格：`lab_tasks.issuer_user_id`、`lab_runs.task_id` 等为逻辑 FK，无 DB 级 FK）。真实 Postgres 上请确认 `sa.JSON()`、`server_default=sa.true()` 的方言表现。

## 4. 手动/集成冒烟（可选，验证闭环）

1. `LAB_ENABLED=true`、`LAB_ADAPTER=mock` 起 API + `python -m app.lab.main`（Lab Runner）。
2. 给某居民 `meta_json["lab"] = {"access": true, "tier": "senior", "skills": ["web_search"]}`。
3. `POST /lab/tasks` 发委托 → 观察扣款冻结、run 入队、runner 跑 mock、`review`。
4. `POST /lab/tasks/{id}/accept-result` → 分账（创作者+金库）+ 产物解锁。
5. `deliverable_kind="world_change"` 的任务成功后 → `GET /admin/world/proposals?status=pending` 应见提案 → `approve` → `GET /world/locations` 出现动态地点 → minimap 刷新 → `revert` 回滚。
6. Admin `POST /admin/lab/kill-switch {enabled:false}` → 新发委托应 503（运行中不影响结算）。

---

## 5. 已知未验证风险点（务必人工过一遍）

1. **全部代码未经运行**：仅 `py_compile` + grep；3.10 环境无法 import（`datetime.UTC` 需 3.11）。运行期错误（拼写、await 遗漏、SQLAlchemy 行为）只能靠明天 pytest 暴露。
2. **前端未过 tsc / build**：本环境禁 `npm install/build`。新增/改动的 `.ts/.tsx`（ExperimentPanel、LabRunsPanel、ProposalsPanel、DistrictZones、api/lab.ts、adminWorld.ts、ws.ts、api.ts barrel、TopNav、districtZonesData、gameStore 未动）需在正式环境 `tsc --noEmit` + 构建核验。
3. **tilemap 未画**：`experiment_building` 只有逻辑坐标 `bounds=(108,72,124,86)`、`entrance=(116,72)`（已校验不与任何 LOCATIONS 重叠），**未改 `tilemap.json`**。寻路能否真正走到入口取决于该片空地在 tilemap 里是否 walkable；美术贴图与碰撞层留待 P4。若入口不可达，测试里 RESEARCH 门控仍过（门控只看 bounds + access），但真机寻路可能到不了。
4. **真实 adapter 未连通**：`openclaw/hermes/computer_use` 是 HTTP 骨架，`base_url` 默认空串→`start()` 抛 `LabAdapterUnconfigured`；线协议（`/runs`、`/steps`…）是占位，需按真实 runtime 对齐。`isolation.py` 只产出隔离规格 + SSRF 判定，**没有真正起容器**——容器/网络白名单落地是部署期集成点。
5. **Redis pub/sub 世界重载未实测**：`sv:world:reload` 的跨进程订阅（API/agent-worker/lab-runner 三处 lifespan 都接了 `world_reload_subscriber` + 启动时 `reload_world`）只在 fakeredis 单测里间接覆盖；多进程真机下的时序/重连、`location_tracker` 索引重建是否及时未验证。
6. **金库不进 transactions 流水**（对 spec §4.7 的偏离，见下）：economy 统计会把金库余额视作「已消耗」，`resident_treasuries` 表自身是审计源。
7. **`charge` 原子化的爆炸半径**：改成 `UPDATE ... WHERE balance>=amount`（`synchronize_session=False`）。已核对所有调用点只用布尔返回值，唯一读余额处（ws chat）走的是 charge 后的新 SELECT，不读陈旧 ORM 对象。仍建议重点回归充值/购物/投资/辩论/委托各扣款路径。
8. **审批 pause/resume 只在同会话+超时=0 路径被测**：真实「置 needs_approval → 玩家 `POST approval` → runner 轮询 resume」的跨会话链路未端到端测；mock 不产生 approval。
9. **WS 高频步骤未合批**（spec §10 建议 ≥1s 聚合）：当前逐条发帧/落库，mock 步骤少无压力，真实 adapter 高频时需补节流（P4）。
10. **孤儿 run 清扫**依赖 `heartbeat_at` 有值：仅对已 `running`（写过心跳）的 run 生效；`queued` 但从未被 runner 领取的 run（`heartbeat_at` 为 NULL）由 `expire_lab_tasks` 的 deadline 兜底退款，不由心跳清扫覆盖。

---

## 6. 与 spec 的偏离 / 实施决策（逐条）

> 原则：spec 与实际代码冲突时以实际代码为准并记录（kickoff 硬约束）。

1. **金库流水（§4.7 冲突→以代码为准）**：spec 要金库每笔收支镜像进 `transactions`（合成账户 `treasury:<slug>`）。但 `transactions.user_id` 是 `users.id` 硬 FK，合成账户会违约。改为：金库以 `resident_treasuries`（原子 `UPDATE` + `updated_at`）为唯一审计源，**不写 transactions**。`settle` 的守恒断言 `sum(splits)==hold.amount` 仍成立（treasury/sink 作为逻辑 split 参与求和，只是 treasury 落表、sink 不再分配）。
2. **LabTask 增字段**（超出 §4.1 列举）：加 `reject_count`（实现「拒收限 1 次」）与 `review_deadline_at`（实现「72h 自动放款」）。
3. **config 增键**（超出 §13 列举）：`lab_auto_release_hours=72`、`lab_task_deadline_hours=24`、`lab_computer_use_base_url/api_key`（§5.2 提到 computer-use 作为第三个真实 runtime，补齐其分组配置）。分账/费率默认：`lab_creator_share=0.2`（创作者 20%，其余入金库）、`lab_platform_fee_rate=0.1`、`lab_sc_per_usd=100`——均为可配默认，属 spec「开放问题」里的待拍板项，先给保守默认。
4. **RESEARCH 计数回归**：新增第 15 个 action 必然改动既有 `test_all_14_action_types_exist`；已更新为 15 并加 `RESEARCH`（只追加，未改既有 14 个名称/顺序/语义）。
5. **真实 adapter 共享骨架**：`HttpAgentAdapter` 放进 `sandbox/base.py`（而非另起文件），三个真实 adapter 仅设 `base_url/api_key`。协议为占位。
6. **edit_location 仅作用于动态地点**：静态 `LOCATIONS` 由代码拥有、不可经提案改；`edit_location` 只改 `dynamic_locations` 的描述性字段（name/description/boosted_actions/role）。
7. **`GET /world/locations` 未鉴权**：作为公开地图数据（对齐 `/bulletin` 的开放读）。如需收敛，加 `_require_user` 即可。
8. **提案生产者是最小实现**：runner 对 `deliverable_kind="world_change"` 的成功任务产出一个 `add_lore`（挂 `experiment_building`、`cost_sc=0`）pending 提案，供治理闭环演示；从产物结构化抽取真实 `patch_json` 属后续工作。
9. **敏感动作判定顺序**：approval 事件先于 scope 后备判定处理，使「金融工具名不在任何 scope 白名单」也能被硬拒（而非先被 scope 拦成 ScopeViolation）。
10. **公开招募**：`create_task` 同步 fund+assign+start；无空闲研究员时任务停在 `funded`，由 `nightly_cron` 的 `dispatch_open_tasks` 后续认领（不依赖 tick 内 LLM）。
11. **minimap LocationKey**：`experiment_building` 作为**静态** key 加入联合类型（它是真实静态建筑）；P3 的**动态**地点不进联合类型，走运行时 `/world/locations` 数据渲染（符合 spec「动态地点不进 LocationKey」）。
12. **gameStore 未改**：ExperimentPanel 用面板本地状态 + `onWSMessage` 扇出 + REST 轮询兜底（spec §13 P1「或面板内 local state」允许）。

---

## 7. 附：新增/改动进程与运行方式

- **Lab Runner**（新独立进程）：`python -m app.lab.main`，消费 `sv:lab:queue`。生产部署需拉起它，否则委托只会停在 `assigned/queued`。
- API 与 agent-worker 的 lifespan 均已接入 `world_reload_subscriber` + 启动 `reload_world`。
- 运行时 kill switch：Redis `sv:lab:enabled`（admin 台即时切换）；`settings.lab_enabled` 仅部署级总开关，默认 **False**（上线需显式打开）。
