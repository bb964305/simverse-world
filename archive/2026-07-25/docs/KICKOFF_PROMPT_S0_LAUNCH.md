# KICKOFF:S0 拍板落地(M1-M6 上线 + 拟真开闸 + 东八区时间 + 世界时钟×4)

> 使用方式:在 Claude Code 中,于仓库根目录 `/Volumes/data/dev/simverse-world` 粘贴本提示词执行。
> 本任务涉及 git 操作与生产部署,全程遵守「不可逆动作前必须人工确认」纪律。

---

## 0. 已拍板决策(2026-07-24,Jimmy)

1. **M1–M6 批准合并并部署** vm212 生产(vm212 按对外生产环境对待;接受与 realism burn-in 并行,覆盖原"burn-in 期间不部署"红线)。
2. **拟真系统开闸**:REALISM P0–P2 生产启用,随部署启动真实 burn-in。
3. **时间以东八区为准**:所有面向玩家的时间语义(作息、日报、节律、显示)锚定 Asia/Shanghai(UTC+8),废除 UTC 锚。
4. **世界时钟加速 4 倍**:`world_time = world_epoch + 4 × 真实流逝时间`(即 1 真实天 = 4 世界天,每 6 真实小时一个完整昼夜)。

顺带处理:内置居民缺 `sbti.dimensions` 的 P2 缺陷(S0-2)本轮一并补齐。

---

## 1. 全局纪律(所有 agent 必须遵守)

- **开工先读**(按序):`docs/testing/PLAN_M1-M6_MERGE_DEPLOY.md`(全文)、`docs/PROGRESS.md` 末尾 3 个条目、`docs/REALISM_OPTIMIZATION_PLAN.md`、`docs/SOCIETY_EXPANSION_PLAN.md` 的 S0-3 与 §3.1B 相关内容、`docs/testing/TEST_REPORT_M1-M6_2026-07-24.md`。
- **不可逆动作**(git push、部署 vm212、对生产库的任何写操作)执行前:打印完整命令清单与影响说明,停下等待人工确认,确认一次执行一步。
- **硬门**:任何合并、部署前,后端全量 pytest 必须 **0 failed**(当前基线 1310 passed / 1 skipped);前端 `tsc -b` + eslint + vitest 全绿。前端构建必须用 **Node 22**(Node 25 下 rolldown 会挂,这是已知问题不要排查)。
- **门控纪律**:所有新行为加配置开关,默认值同步登记进 `backend/.env.example`。
- **并行纪律**:阶段 B 的每个 subagent 必须先 `git worktree add` 建立独立工作树,禁止两个 agent 写同一工作树;彼此不合并对方分支,统一由阶段 C 收口。
- **收尾纪律**:完成后按仓库既有格式在 `docs/PROGRESS.md` 追加条目(进展 + 偏差),不得漏记偏差。

---

## 2. 执行结构

### 阶段 A(串行,最先做,主 agent 亲自执行):M1–M6 合并 + 复验

严格按 `docs/testing/PLAN_M1-M6_MERGE_DEPLOY.md` 的 Phase 1–3 执行:

- **A1 提交切分**:在 HEAD 切分支 `feat/town-m1-m6-20260724`,按方案中的精确文件清单 `git add` 提交 M1–M6(纯应用代码,零 schema 迁移);0723 修复批改动不得混入。
- **A2 叠生产线**:`git worktree` 检出 `port/prod-fixes-onto-044`,cherry-pick M1–M6 提交;预期仅 `config.py` / `nightly_cron.py` / `.env.example` 三处冲突,一律加性解决。
- **A3 硬门复验**:在生产基线上跑全量 pytest(0 failed)+ 6 个 M 系定向测试文件 + 端到端 harness(8/8 + 1/1 + 4/4),不绿不进阶段 B。
- 方案 §6 的四个待确认项按第 0 节拍板结果处理;若发现 master worktree(`simverse-world-master-merge`)存在锁或未知改动,**停下询问**,不要强行处理。

### 阶段 B(并行,3 个 subagent,基于阶段 A 产出的合并结果各开 worktree)

**agent-T「时间系统」(最大件,另两个 agent 的口径以它为准)**

先产出 ≤1 页设计说明再动手,说明两类时间的归属:
- 读**世界时间**(k=4 加速):居民作息调度、星期/节日节律、日报与叙事中的日期、梦境/反思的"每晚"语义、为将来预留的年龄/任期接口。
- 保持**真实时间**:LLM 预算日结、cron 运维调度、TTL/限流/冷却、日志时间戳、TLS/运维一切。

实现要求:
- `backend/app/config.py` 新增 `WORLD_CLOCK_K=4`、`WORLD_EPOCH`、`TIMEZONE=Asia/Shanghai`;新建独立 world_clock 模块作为**唯一换算入口**,禁止散落各处各自换算。
- `WORLD_EPOCH` 对齐北京时间整点,保证北京时间白天登录的玩家总能看到完整昼夜循环。
- 居民作息调度从 UTC 真实时改读世界时;`nightly_cron` 保持真实时间但触发时刻改锚北京时间(日报在北京时间清晨可读)。
- **连带成本核算**:`AGENT_MAX_DAILY_ACTIONS` 语义改为"每世界天",意味着真实每天行动量 ×4;重算预计 $/真实天,核对仍低于 `BUDGET_GLOBAL_DAILY_USD=1.5`;若不够,给出建议值报告,**不得擅自修改预算上限**。
- 测试:换算纯函数单测(k=4、UTC+8 边界、跨日、闰点)、作息在世界时下的调度测试、cron 仍按真实时间运行的回归测试。

**agent-R「拟真开闸」**

- 梳理全部 `REALISM_*` 开关,产出生产默认值表(逐项 true/false + 一句话理由),写入 `.env.example` 与部署 env 清单。
- 移动速度、需求节律、情绪环等在世界时钟 k=4 下重校准;与 agent-T 的设计说明对齐,口径冲突时以 agent-T 为准。
- 确认 burn-in 六项拟真度探针脚本可运行并能出数(计划到达率 >70%、关系度分布右偏等,见 `REALISM_OPTIMIZATION_PLAN.md` 指标节)。

**agent-S「内置居民人格补齐」(小件)**

- 给 `backend/seed/preset_characters.py` 的 11 位内置居民补 `sbti.dimensions`(可从各自 persona 文本派生,风格与既有测试居民一致)。
- 验证修复效果:NPC 投票不再恒选 option 0、选举候选不再回落 heat 排序;回归 civic / election / duty 相关测试。

### 阶段 C(串行,收口,主 agent 亲自执行)

- **C1 集成**:依次合并三个 worktree 分支(先 T 后 R 后 S),冲突以时间系统口径为准。
- **C2 硬门**:全量 pytest 0 failed + 前端三件全绿 + M 系定向 + harness 复跑。
- **C3 部署 vm212**【人工确认后执行】:先核对 vm212 当前 SHA = `8a0449c`,记录回滚点;按方案 Phase 4 步骤部署;部署后跑冒烟(参考 `docs/testing/smoke_test.py` 关键用例:登录、聊天、地图、forge、admin)。
- **C4 回写 master**【人工确认后执行】:按方案 Phase 5 merge 回 master 并 push origin;注意 master 落后生产,方向必须是生产线并入 master,不得反向。
- **C5 burn-in 启动**:确认 `AGENT_ENABLED=true` 与 REALISM 开关生效;按 `docs/BURNIN_PLAN.md` 的观测矩阵开始 48h 稳态观察;记录部署后首个 24h 的 llm_usage 真实成本。
- **C6 记录**:`docs/PROGRESS.md` 追加条目;更新 `docs/ROADMAP_2026-07-24.md` 中阶段 0/1 的状态。

---

## 3. 验收清单(全部满足才算完成)

- [ ] M1–M6 已独立提交、已合并生产线、已部署 vm212、master 已回写并 push
- [ ] 生产 REALISM 开关按默认值表开启,默认值表已存档
- [ ] 世界时间 = 北京时间锚定 + 4 倍速,换算只有一个入口,日志/接口可查询当前世界时
- [ ] 一个真实白天(北京时间)内可观察到居民完整昼夜作息
- [ ] 日报在北京时间清晨生成
- [ ] 成本:部署后 24h 真实花费 ≤ $1.5 预算,数字已记录;若预测超限,已产出建议值报告
- [ ] 11 位内置居民 sbti 补齐,投票/选举偏差消除
- [ ] 后端 pytest ≥1310 passed / 0 failed;前端 tsc + eslint + vitest 全绿(Node 22)
- [ ] PROGRESS.md 已记录,含全部偏差

## 4. 边界(本轮明确不做)

- 不动 Lab 生产开关(`lab_adapter=mock`、`lab_oci_enabled=False` 维持现状)
- 不修 NPC 图片理解(独立任务,另开 KICKOFF)
- 不做社会扩展 S1/S2(offices、声誉、财政等,等本轮 burn-in 数据后另开 KICKOFF)
- 不改 `BUDGET_GLOBAL_DAILY_USD` 预算上限(仅报告建议)
- 不做邮局美术落图(独立美术任务)
