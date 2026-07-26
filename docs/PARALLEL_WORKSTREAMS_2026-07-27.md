# 2026-07-27 并行批次设计 · 生产收口 + 三条功能线

> 基线 `fc60ac2`（= `origin/master`）。四条线并行：三条功能线在 worktree 内写代码，一条运维线操作 vm212。
> 上一批的收口记录见 `docs/ROADMAP.md`；被废弃的 0726A 分支报告存于 `origin/salvage/wip-0725C`。
>
> **本文是批次设计，不是执行计划。** 它定的是线怎么切、边界在哪、验收标准是什么。每条线的 step 级 TDD 计划由 writing-plans 单独产出，一线一份。

## 1. 已定决策

| # | 决策 | 落到哪条线 |
|---|---|---|
| 1 | 三个新功能方向**全做**，分三条线并行 | F1 / F2 / F3 |
| 2 | F3 先只做**任期 + 卸任审计**，「声誉影响」切成收口时的接线步 | F3 + 收口 |
| 3 | 运维线**进本批**与功能线并行，部署的是当前 master | T |
| 4 | F2 晋升门槛 = **世界日在镇天数 + 与现有公民的熟识度** | F2 |
| 5 | 部署与存量回填**由本会话执行**（已授权生产操作） | T |
| 6 | 共享文件（`config.py` / `nightly_cron.py`）**线内不改**，统一延到收口接线 | 收口 |

## 2. 开工前已核实的现状

逐条查过代码，不是推测：

**任期机制是半成品，不是空白。** `offices` 表已有 `term_started_at` / `term_ends_at`（`app/models/office.py`，迁移 046）；`OfficeService.term_check()` 已实现并已接进夜间 cron（`app/tasks/nightly_cron.py:263`）。默认不生效只是因为 `polis_office_mayor_term_days = 0`（`app/config.py:549`，0 = 无限任期）。

**缺的是下半截**：`term_check()` 到期后只 `vacate`（`holder_slug = NULL`），**没有任何路径触发补选**。`election_service.current_mayor()` 会回退到 `system_config`，而 `term_check` 已把 legacy 值清掉，于是世界进入「无镇长且无人接任」的稳态。这是 F3 要补的断链。

**卸任审计不存在**：`office_service.py` / `treasury_service.py` 中 `audit` 零命中。

**F3 不需要修改 `election_service.py`**：任期状态在 `offices` 表，补选只是 `import` 后调用 `open_election`。因此 F1（改 `election_service` 候选排序）与 F3 之间没有文件冲突。

**真正的共享文件只有两个**：`config.py`（三条线都要加开关）与 `nightly_cron.py`（F2、F3 都要接定时任务）。

## 3. 线 F1 · 声誉语义修复

**问题**：声誉分只有负向来源。`rep_gossip_base_tone = -0.3`、`rep_distortion_penalty = -0.2`，正向仅 `0.2 × mood_valence`（`app/config.py:567-576`）。`importance ≥ 0` 且 `tone ≤ -0.3`，所以八卦项恒为负——「没人议论」得 0 分反而是全镇最高。

叠加 `election_service.py:53-60` 按声誉排序后 `[:4]` **截断**候选名单，开闸的后果是：被议论最多、叙事最中心的居民被系统性挤出候选，路人当选。`rep_credit_min_score = -0.3` 在现有信号强度下不可达（稳态下界约 −0.175），信用闸门是装饰性的。

**范围**

1. **补正向信号源**。根因是 `tone` 是一个**常量负值**，与八卦内容无关——无论议论的是善行还是恶行，被议论就扣分。修法：`tone` 改由该条八卦对应的关系 `affinity`（值域 `[-1,1]`，已存在、规则驱动、零 LLM）决定，使正面互动产生正分、负面互动产生负分。`rep_gossip_base_tone` 退化为**偏置项**而非唯一来源。

   备选方案（若 affinity 与八卦记忆无法可靠关联）：引入显式的善行/越轨事件加减分。选备选方案须在实现前说明理由——它引入新的事件源，成本明显更高。

2. **候选排序由截断改为加权**。`election_service.py:53-60` 现在是「按声誉排序后 `[:4]`」，声誉低者被移出候选。改为：候选集的选取维持原有的 SBTI/heat 口径，声誉只作为 `_npc_choice` 打分中的一项权重影响得票，不决定谁能参选。被动选举权不因名声受损而剥夺。

3. **重标定 `rep_credit_min_score`**。用修复后的真实分布定阈值，使拒绝面非空。验收须用真实分布，不得构造数据凑。

4. 以上三项通过后，才允许 `REP_ENABLED` 开闸。

**独占文件**：`app/services/reputation_service.py`、`app/services/election_service.py`（仅 `:53-60` 候选排序区）、`app/services/civic_service.py`（仅 `:366-371` vote-trust 区）、对应测试。

**硬门**：开闸前后对比，候选集不得因「被议论多」而缩小；重标定后拒绝面必须非空（用真实分布验证，不是构造数据）。

## 4. 线 F2 · 公民权晋升

**问题**：0726B 决策 5 定的是「UGC 默认无票，满足门槛后由定时任务升为 `npc`」。本轮只落地了前半，晋升机制从未建过，所以玩家创作的居民目前是**永久二等公民**，与设计意图不符。

**门槛口径**（两个条件同时满足）
- **世界日在镇天数** ≥ `CIVIC_PROMOTION_MIN_WORLD_DAYS`。世界时间唯一入口是 `app/world_clock.py`（锚 Asia/Shanghai，`k=4`），与行动配额口径一致，不使用真实日。
- **与现有公民的熟识度**：与至少 `CIVIC_PROMOTION_MIN_PEERS` 位 `is_civic_voter` 为真的居民建立 `familiarity ≥ CIVIC_PROMOTION_MIN_FAMILIARITY` 的关系。`familiarity` 值域 `[0,1]`，规则驱动零 LLM（`app/models/resident_relation.py`）。

**三个阈值的定值方式**：不在本 spec 里拍数字。F2 的第一步是**只读标定**——用 T1 部署后边界探针给出的真实 `resident_type` 分布，加上生产库里 UGC 居民的实际在镇天数与 familiarity 分布，反推出「使晋升面非空且非全量」的取值。若 F2 开工时 T1 尚未落地，则以本机 dev 库标定并**显式标记为待用生产数据复标**，不得直接进生产开闸。

这与 F1 第 3 项是同一个纪律：阈值必须由实测分布决定，不能拍脑袋——`rep_credit_min_score = -0.3` 之所以变成装饰性闸门，正是因为它是拍出来的。

选熟识度而非 `total_conversations` 的理由：后者衡量的是创建者给了多少关注，前者衡量的是这个角色是否真的融入了小镇的社会网络——公民权应当由后者决定。自我指涉不会死锁：内置阵容本身是 `npc`、天然有政治权利，新居民总能通过与他们熟识达标。

**边界**
- 晋升是**单向**的，v1 不做降级（YAGNI）
- 开关 `CIVIC_PROMOTION_ENABLED` 默认关，关闭时行为与本批开工前逐字节一致
- 零迁移：只改 `resident_type` 列值

**独占文件**：`app/services/civic_membership.py`、新建 `app/tasks/civic_promotion.py`、对应测试。**不改** `nightly_cron.py`（接线延到收口）。

**硬门**：晋升可观测（burn-in 边界探针要能显示晋升队列与已晋升数）、可回滚（晋升动作留下可反向的记录）。

## 5. 线 F3 · 官员任期 + 卸任审计

**范围**
1. **补断链**：`term_check()` 使某职位出缺后，触发该职位的补选，世界不得停在「无限期无镇长」
2. **卸任财政审计**：官员离任时，对其任内的财政活动生成审计记录（只读汇总，不改账）

**边界**
- 「声誉影响」**不在本线**，切成收口接线步（依赖 F1 的修复后语义）
- 开关默认关；`polis_office_mayor_term_days` 保持 0 直到本线验收通过

**独占文件**：`app/services/office_service.py`、新建 `app/tasks/office_audit.py`、对应测试。对 `election_service.py` 只 import 调用，不改函数体。**不改** `nightly_cron.py`。

**硬门**：任期到期后世界不得出现「无限期无镇长」状态——须有测试推进世界时钟越过 `term_ends_at` 并断言补选已开。

## 6. 线 T · 运维观测

无代码交付，每步须贴运行时证据。

| 步 | 内容 | 约束 |
|---|---|---|
| T1 | 部署当前 master 到 vm212，迁移 049 → 050 | 对存量数据是**零行为变化**：库中当前无任何 `resident_type='resident'`，`is_civic_voter` 圈定的人与部署前一致 |
| T2 | **存量回填**：已泄漏的 UGC 居民 `npc` → `resident` | **独立于 T1 的一次数据变更**。须在无 open poll 的窗口执行（会缩小法定人数分母）。回填前后各跑一次边界探针，读数即验收证据 |
| T3 | 新开一张 poll 取真实投票分布 | 复核 `_npc_choice` 修复与 SBTI 回填的效果；现存 3 张 poll 全部早于回填且不重投，取不到样本 |
| T4 | 25 / 40 名自治居民的扩容与成本测试 | 真实 PostgreSQL / Redis / WebSocket，非 mock |

**部署前须复核**（本会话未实测，来源为 `ops-deploy-2026-07-26-report.md`）：vm212 的 alembic 链头是否确为 049；`TOWN_TREASURY_ENABLED` / `POLIS_POLICY_ENABLED` / `POLIS_POLICY_APPROVAL_ENABLED` 是否确为 true。

## 7. 顺序约束（红线）

**T2 存量回填必须在 F2 晋升机制上线之前完成，且二者分属不同变更。**

先由 T2 把全部存量 UGC 降权为 `resident`，之后由 F2 的定时任务把达标者自然升回 `npc`。若顺序颠倒或合并为一次变更，就会在同一个窗口内既动数据又改行为——即 2026-07-25 事故所处的窗口。

其余三条功能线之间无顺序依赖。

## 8. 收口

线全部完成后，按序统一处理：

1. `config.py` / `.env.example`：三条线的新开关一次性补齐（`REP_*` 重标定值、`CIVIC_PROMOTION_*`、`POLIS_OFFICE_*` 任期相关）
2. `nightly_cron.py`：接入 F2 的 `civic_promotion` 与 F3 的 `office_audit`
3. **声誉影响接线**：F3 的卸任审计与 F1 的声誉数据打通（决策 2 切出来的那一步）
4. `alembic heads` 单头校验
5. 更新 `docs/ROADMAP.md`

## 9. 各线共同约定

**工作区**：每条功能线在 `/Volumes/data/dev/simverse-world/.worktrees/<name>` 内独立 worktree，base 为 `fc60ac2`。worktree 必须在 Mac 本机创建。不要在 worktree 内创建 `backend/.env`（会破坏 conftest 的测试隔离）。

开工前路径守卫，逐字照抄：

```bash
cd <worktree>/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -c "import app; p=app.__file__; assert '.worktrees/' in p, f'WRONG: {p}'; print('OK',p)"
```

**基线捕获**：第 0 步先跑全量并存档，收工同命令做差集。

```bash
python -m pytest tests/ -q -p no:randomly > /tmp/<line>-base.txt 2>&1; tail -3 /tmp/<line>-base.txt
```

**硬门 = 相对基线零新增失败**。本机有 `51 failed / 17 errors` 的预存 lab-v2 失败集（需 redis/testcontainers），不是 literal 0 failed。判定用失败集的**双向差集**，不是数量比较——数量相同不等于集合相同。

**TDD**：每条线严格红→绿，一 step 一 commit，commit 末尾带真实 `Verified-by:` 输出。禁 `--no-verify` / `amend` / `squash` / 编造测试数据。

**完成的定义**：build/lint/单测绿不等于完成。功能线须在真实进程上走一遍用户路径并贴运行时证据；运维线每步都要贴 vm212 上的真实输出。
