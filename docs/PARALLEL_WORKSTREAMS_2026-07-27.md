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

选熟识度而非 `total_conversations` 的理由：后者衡量的是创建者给了多少关注，前者衡量的是这个角色是否真的融入了小镇的社会网络——公民权应当由后者决定。

**门槛口径的两条硬语义**（不满足则整个机制失效，推导见探查报告 §4.2）

- **①的锚点不是 `created_at`，而是「本轮公民资格起算点」**：取 `civic_standing_history` 里该居民最近一条档位变更的世界时间，无历史行时回落 `real_to_world(created_at)`。若锚 `created_at`，T2 把一个已在镇 200 世界日的 UGC 降权后，F2 开闸当晚条件①对它立刻重新满足——**T2 的降权对存量整批走过场**。
- **②的同伴取自「锚定公民集」，不是活的 `is_civic_voter`**：否则判定的转移函数自指，产生级联升降与「脱锚公民团」（某人的 N 位同伴全是已晋升 UGC、零条内置边）。锚定公民集 = 内置阵容（`creator_id == SYSTEM_USER_ID`）∪ 已过考察期的归化公民。

判定整体是 **snapshot 语义**：pass 开始一次性冻结输入，末尾一次 commit，中途不重读选民集，否则结果依赖数据库行序。判定做成纯函数，测试用打乱顺序断言输出集合恒等。

### 4.3 撤销（降级／逐出的第一档）

**状态模型 = 出身（provenance）× 档位（standing）二维。** 档位有序三档，正好对应「降级与逐出是同一套机制的不同强度」：

```
citizen  有票 · 在镇 · 被 loop 驱动          ← 晋升终点
denizen  无票 · 在镇 · 被 loop 驱动          ← 降级落点（本轮实现）
exiled   无票 · 不在镇 · 不被驱动 · 不在地图  ← 逐出落点（本轮不实现，仅预留）
```

v1 档位仍编码在 `resident_type`，**不加列、不加取值**；但业务代码不得再直接读写该列，一律走 `civic_membership` 的派生函数与两个写入口 `grant_citizenship` / `revoke_citizenship(tier="demote"|"exile")`。`"exiled"` 分支现在就写进枚举并 `raise NotImplementedError`——逐出上线时是填空，不是改签名。

**不新增第 5 个 type（如 `"exiled"`）的理由**：地图与感知**不读 type**——公开名录是全表（`app/services/resident_service.py:6-18` 无 where），tile 占用也是全表。新增取值只会掉出 `SIM_RESIDENT_TYPES`，产出「仍在地图上、仍被搭话，只是自己不再 tick」的活体雕像。逐出要收窄的是第四族谓词，这是逐出唯一真正的新增面。

**触发方式：v1 是事件驱动，不是门槛反向。** 夜间任务**只升，永不自动降**——门槛②读的 `familiarity` 有周衰减，接成降级判据等于让公民权跟着社交波动飘；而「违规逐出」本来就是显式事件。自动下滑降级单列开关 `CIVIC_AUTO_DEMOTION_ENABLED` 默认关，开启必须同时具备滞后三件套：滞后区间（`Δ ≥ 0.10`，须严格大于单次最大相关增量 `0.05`）、最短任期（`≥ 12` 世界日 = 一张 poll 的生命周期）、冷却期。

**撤销是有序复合事务，顺序不可颠倒**：防呆 → 卸民选职务 → 清 `meta_json['mayor']` → 清 `system_config['current_mayor']` → 改档位 → 写历史行 → 断言 → 广播。若先改档位再清理，`meta_json['mayor']` 会永久卡死（清扫扫不到他），可产生两个 mayor 并双份工资倍率。

**只卸民选职务**（`fill_strategy == "election"`，今天只有 `mayor`）；`town_clerk` / `postman` / `doctor` 是**劳动职务不受影响**——`offices` 表把两类职务混在一张表里，一刀切会误伤。

**通用约束（不止本线适用）**：凡是清理「已离开集合 S 的居民」的扫描，都不能用 S 本身做 WHERE。`office_service.py:222` 与 `election_service.py:141` 两处现在正是这么写的，降级档侥幸命中，逐出档天然自锁。

**在途投票：开票时冻结分母，不实现撤票。** 快照写进 `options_json[0]['_eligible_at_open']`。幽灵票保留并写成设计语义「投票时具备资格即计票」——`_npc_voters` 是扁平 slug 列表，没存票的归属，撤票要改结构且要兼容存量 poll。

**防呆（对标 `PlayerPurgeRefused`）**：新增 `CivicStandingRefused`，在第一条 UPDATE 之前抛出，使拒绝是真正的 no-op。**raise 而非静默跳过；查库而非信传入对象**。绝对不可碰：玩家化身（`"player"` 且查 `users.player_resident_id` 复核）、内置阵容（`creator_id == SYSTEM_USER_ID`）、admin `preset`、**无晋升记录者**——撤销是晋升的严格逆操作，白名单而非泛谓词。

**边界**
- 撤销 v1 只做 `demote` 档；`exile` 档只留签名与枚举，不实现
- 开关 `CIVIC_PROMOTION_MODE ∈ {off, shadow, on}` 默认 `off`；`off` 时行为与本批开工前逐字节一致
- **「零迁移」边界改为「零数据迁移」**：允许一次纯建表 additive migration（`civic_standing_history`），且该迁移不得与开闸同批

**为什么必须建表而不是塞 `meta_json`**：硬门「可回滚」需要载体，而 `meta_json` 有 7 个 read-modify-write 写入方、agent loop 也在同一批居民上写，滞后状态被静默覆盖 = 最短任期与冷却期失效，且只在并发窗口发生、测试抓不到；它还是 `sa.JSON` 无法索引，并由多个无鉴权前台接口原样公开——**撤销原因文本绝不能进去**。该表同时是上面「公民时钟锚点」的载体。

**独占文件**：`app/services/civic_membership.py`、新建 `app/tasks/civic_promotion.py`、新建模型与迁移、`app/routers/admin/residents.py:117-118`（唯一的 `resident_type` 运行时写入竞争者，改为调用写入口）、`app/services/election_service.py:135-193`（`install_mayor` 的结票复核与事务化，F1/F3 都不覆盖此区）、对应测试。**不改** `nightly_cron.py`（接线延到收口，位置写死在 `close_due_polls` 之后、`run_npc_voting` 之前）。

**硬门**
- 晋升与撤销均可观测：探针须输出 `resident_type × provenance` 交叉表、晋升队列、翻转统计。判泄漏的条件改为「provenance=UGC 且 `is_civic_voter` 为真、但查不到晋升记录」——现有探针判的是常量集合被拓宽，F2 只改行值不改集合，**永远不会触发**
- 「最近 7 世界日翻转数 > 0」是**告警条件**，不是信息项
- 可回滚：每次档位变更落一行 `civic_standing_history`

### 4.4 顺带收口的既有缺陷

**`reputation_service.py:74` 是第 11 处读点，上一轮收口漏掉了**——它是裸的 `Resident.resident_type == "npc"`，既不走 `is_civic_voter` 也不走 `is_autonomous`（本会话已独立复核）。须归到**人口口径**改为 `is_autonomous`：声誉是社会属性不是政治权利。不改的后果是被降级者退出夜间声誉重算、分数永久冻结在降级前那一刻，而 `election_service.py:53-60` 的候选排序读的正是这个冻结值；将来「违规扣声誉」若先改档位再扣分，扣分会因这行字面量永不生效。

F2 开工前先做一次全仓 `resident_type` 字面量分类，任何未归类的 `== "npc"` 都是半状态源。

## 5. 线 F3 · 官员任期 + 卸任审计

**范围**
1. **补断链**：`term_check()` 使某职位出缺后，触发该职位的补选，世界不得停在「无限期无镇长」
2. **卸任财政审计**：官员离任时，对其任内的财政活动生成审计记录（只读汇总，不改账）

**边界**
- 「声誉影响」**不在本线**，切成收口接线步（依赖 F1 的修复后语义）
- 开关默认关；`polis_office_mayor_term_days` 保持 0 直到本线验收通过

**独占文件**：`app/services/office_service.py`、新建 `app/tasks/office_audit.py`、对应测试。对 `election_service.py` 只 import 调用，不改函数体（`install_mayor` 的收口归 F2，见 §4.3）。**不改** `nightly_cron.py`。

**与 F2 的接口约定**：F2 的撤销只保证职位出缺并广播 `civic_standing_changed`，补选由 F3 的钩子接手（收口时接线）。允许的空缺上限 = 1 个夜间周期，超出由探针报红旗。

**硬门**：任期到期后世界不得出现「无限期无镇长」状态——须有测试推进世界时钟越过 `term_ends_at` 并断言补选已开。注意 `polis_office_mayor_term_days = 0` 且 `term_check` 被 gate 整段跳过时，**gate 开与关都没有自动收回路径**，撤销是唯一的下台方式；两种 gate 状态都要有测试覆盖。

## 6. 线 T · 运维观测

无代码交付，每步须贴运行时证据。

| 步 | 内容 | 约束 |
|---|---|---|
| T1 | 部署当前 master 到 vm212，迁移 049 → 050 | 对存量数据是**零行为变化**：库中当前无任何 `resident_type='resident'`，`is_civic_voter` 圈定的人与部署前一致 |
| T2 | **存量回填**：已泄漏的 UGC 居民 `npc` → `resident` | **独立于 T1 的一次数据变更**。须在无 open poll 的窗口执行（会缩小法定人数分母）。回填前后各跑一次边界探针，读数即验收证据 |
| T3 | 新开一张 poll 取真实投票分布 | 复核 `_npc_choice` 修复与 SBTI 回填的效果；现存 3 张 poll 全部早于回填且不重投，取不到样本 |
| T4 | 25 / 40 名自治居民的扩容与成本测试 | 真实 PostgreSQL / Redis / WebSocket，非 mock |

**部署前须复核**（本会话未实测，来源为 `ops-deploy-2026-07-26-report.md`）：vm212 的 alembic 链头是否确为 049；`TOWN_TREASURY_ENABLED` / `POLIS_POLICY_ENABLED` / `POLIS_POLICY_APPROVAL_ENABLED` 是否确为 true；`REALISM_RELATIONS_ENABLED` 是否确为 true（`.env.example:448` 记录的生产决策是 true，F2 条件②的 familiarity 主增长路径挂在它上面）；`offices` 表是否有迁移 046 遗留的陈旧 `holder_slug`。

**T2 的三条硬约束**

1. **目标谓词必须排除已晋升者**：`meta_json.origin ∈ {forge, import, quick_forge}` 或 `creator_id` 为真实用户 id，**且**无 `to=citizen` 的历史行。不要用 `creator_id` 单条判定——迁移 045 让账号注销后它变 NULL，内置阵容是 `SYSTEM_USER_ID`，admin preset 写字面量 `"system"`，三值混合；也不要用 `origin` 单条一刀切，极老的 UGC 行不保证带 origin。残差人工点名复核。
2. **不可重放**：执行后在 `system_config` 写 `civic_backfill_done`；脚本启动时标记已存在且未带 `--force-rerun` 则拒绝退出。F2 上线后重放一次 = 大规模剥夺公民权且零告警。
3. **必须是进仓库、被评审、`--dry-run` 为默认值的 `backend/scripts/` 脚本**。禁止在 vm212 上手写一次性 SQL——07-25 的根因正是「手工脚本自带 id 列表绕过 `find_targets`」。

T2 与 F2 共用同一份「谁是 UGC」判定（落在 `civic_membership.py`，两边 import），但触发路径分离、不得由同一次部署同时首跑。两边各写一份必然漂移：T2 降了一批、F2 认为其中一部分不是 UGC 因而永不晋升 → 永久二等公民，正是本线要修的问题复发。

## 7. 顺序约束（红线）

原「T2 先于 F2」这一条不足以覆盖，实际须切成**四次独立变更，顺序不可合并**：

```
① 建表迁移 civic_standing_history（纯 DDL，零数据行为）   ← F2 交付的第一步，必须先于 T2
② T2 存量回填（一次性脚本，数据变更）
③ F2 代码合入（CIVIC_PROMOTION_MODE=off，零数据写）
④ shadow 观察 ≥ 3 个夜间周期 → 开闸（单独一次变更，只翻开关）
```

①先于②的理由：T2 需要写一行历史行作为「公民时钟锚点」，否则 §4.2 的锚点回落到 `created_at`，存量整批在开闸当晚被升回。若运维时序不允许，则走降级方案——F2 首次读取时把无历史行的 UGC 的 anchor 取 `max(created_at 对应世界时间, T2 完成标记的世界时间)`，spec 实现时须写清哪条路径生效。

④的 shadow 态执行完整候选计算与**全部防呆检查**，把当晚会晋升/撤销的名单与每人的证据写进日志与探针，**不执行任何 UPDATE**。理由是首夜爆炸半径不可预演——不是「规模无人知晓」（§4.2 的只读标定本来就能测出候选规模）。

其余三条功能线之间无顺序依赖。

## 8. 收口

线全部完成后，按序统一处理：

1. `config.py` / `.env.example`：三条线的新开关一次性补齐（`REP_*` 重标定值、`CIVIC_PROMOTION_MODE` / `CIVIC_AUTO_DEMOTION_ENABLED` / 门槛与滞后参数、`POLIS_OFFICE_*` 任期相关）
2. `nightly_cron.py`：接入 F2 的 `civic_promotion` 与 F3 的 `office_audit`。**F2 的位置写死在 `close_due_polls` 之后、`run_npc_voting` 之前**——当晚晋升、当晚补投，新公民参与的第一次关票分子分母同源；接在末尾只会把危害推迟一晚（每晚 close 先于 vote，夜 N 末尾晋升的人在夜 N+1 关票时仍是「进了分母、一票未投」）。对应回归测试须按 **N+1 晚**断言。用与 `nightly_cron.py:142-145` 同样的注释形式锚住位置
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
