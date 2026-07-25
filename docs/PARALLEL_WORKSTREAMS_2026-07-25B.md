# 功能开发并行开工清单(2026-07-25B · 文档归档收口后版)

> 前提:上一批(S2-1 offices / S1-3 opinion / 生产修缮三件套)已收口合并并部署 vm212(alembic `047_add_issue_stances`)。
> 本批结构是 **1 个串行前置(阶段 0)+ 4 线并行**。
> **阶段 0 必须由主会话独占先做完并提交**——当前工作区把 `docs/` 全树迁进了 `archive/2026-07-25/`,
> 而 HEAD 里这些文件还在旧路径。此刻拉 worktree 出来,提示词里写的规格路径
> (`archive/2026-07-25/docs/kickoffs/KICKOFF_S1-5_treasury.md` 等)在分支里**全是不存在的路径**。
> 阶段 0 不提交,四条线一条都不能开。

## 共同纪律(每条提示词已内嵌,此处为总纲)

1. **base 一律 = 阶段 0 收口后的 master**(不是当前 HEAD)。独立 `git worktree` + 独立分支,一任务一提交带任务号,TDD。
2. **规格文件已是归档只读证据**:两份 kickoff 现位于 `archive/2026-07-25/docs/kickoffs/`,方案在
   `archive/2026-07-25/docs/SOCIETY_EXPANSION_PLAN.md`。**读它、不改它**(归档不做原地更新,偏差记自己的报告)。
3. **规格里的环境事实已过期,以实测为准**:
   - 两份规格都写"现链头 `040_residents_creator_nullable`"——**实测链头 = `047_add_issue_stances`**
     (`045_residents_creator_nullable` → `046_add_offices` → `047_add_issue_stances`)。新迁移用 `NNN` 占位、
     `down_revision` 接实测链头,报告登记,收口时统一线性化 + `alembic heads` 单头校验(硬门)。
   - 规格的 `file:line` anchors 按 S2-1/S1-3 落地前的代码写,行号已漂移(例:`_pay_wage` 现为
     `backend/app/services/duty_service.py:147-168`,`_close_one/_execute_outcome` 现为
     `backend/app/services/civic_service.py:254-282 / 284-315`)。**逐条校验,漂移以代码为准并记偏差。**
4. **config.py / nightly_cron.py 仍是多线共碰区,继续用「前缀 + 块」纪律**:`Settings` 类尾已有
   `POLIS_OFFICE_`(`config.py` 尾块)与 `POLIS_OPINION_` 两块,各线只在**最后**追加自己前缀的新块
   (S1-5=`town_`/`TOWN_`,S2-5=`polis_policy_`/`POLIS_POLICY_`),**不改他人行**;nightly 只新增自己的
   独立 `try/except` 块,不改不挪既有块。**工程健康批例外:它不碰 config.py(阈值走 `os.environ`),
   且它是唯一被允许改 `nightly_cron_loop` 调度骨架的线**(见线 3 纪律)。
5. 时间语义:任何"天/周/任期"一律经 `backend/app/world_clock.py`(唯一换算入口),禁止直接 `utcnow` 比对世界节律;
   预算 / TTL / 日志 / 运维 cron 用真实时间。
6. 测试口径:改动范围内 pytest 全绿;全量 pytest **相对 base 基线零新增失败**(本机含 lab-v2 需真 redis/
   testcontainers 的预存失败集,硬门 = 零回归,非 literal `0 failed`)。**开工第一步先在 base 上跑一次全量存
   `/tmp/<线名>-base.txt`**,收工用同命令 diff。
7. 门控纪律:新机制独立 bool 开关默认 `False`;规则做骨架、LLM 做血肉,零新增 LLM 边际成本。
8. 各线进展写 `docs/reports/<分支名>-report.md`(阶段 0 后 `docs/` 下只剩 `ROADMAP.md`,该目录由本批重建;
   本批产出属"新证据"而非归档);**不碰 `docs/ROADMAP.md`**——收口时主会话统一更新一次。

## 开工总览(1 串行 + 4 并行)

| # | 工作线 | 分支 | 依据 | 规模 | 并行性 |
|---|---|---|---|---|---|
| **0** | **文档归档收口(6 条提交)** | 直接在 `master` | 当前工作区已完成的 `archive/2026-07-25/` 重组 | 中 | **主会话独占串行,其余四线的硬前置** |
| 1 | **S1-5 镇财政闭环**(税 / 薪 / 公共支出) | `feat/s1-5-treasury` | `archive/2026-07-25/docs/kickoffs/KICKOFF_S1-5_treasury.md` | 大 | 与 2/3/4 并行 |
| 2 | **S2-5 policies 表 + 四级分级审批** | `feat/s2-5-policies` | `archive/2026-07-25/docs/kickoffs/KICKOFF_S2-5_policies.md` | 大 | 与 1/3/4 并行(财政类条目 effect 留占位) |
| 3 | **工程健康批(R3+R4+P2)**:夜间补跑 + 聊天锁 DB 侧回收 + 多实例心跳告警 | `fix/eng-health-batch` | `docs/ROADMAP.md` 近期优先级 #5 | 中 | 与 1/2/4 并行,**收口排最后** |
| 4 | **只读运维审计(R5+P3)**:投票分布复验 + 账单对账 | 无分支(纯只读 + 报告) | `docs/ROADMAP.md` 近期优先级 #1 | 小 | 全程并行,零代码冲突 |

> **编号说明(已确认)**:`R3/R4/P2/R5/P3` 在仓库里查不到出处(全仓 md、`git log`、`.omx` 均无留痕),
> 本文按 `docs/ROADMAP.md`「近期优先级」映射展开:工程健康批 = 优先级 #5 的三项(夜间补跑 / 聊天锁 TTL /
> 多实例状态与告警可观测性);只读运维审计 = 优先级 #1 的两项(SBTI 回填后投票分布复核 / `llm_usage` 与
> 真实账单核对)。如与你手上的原始条目表有出入,以原始条目为准,本文对应节改标题不改纪律。

## 文件集冲突核验(四线互不相撞的依据)

| 共碰文件 | 线 1 S1-5 | 线 2 S2-5 | 线 3 工程健康 | 冲突性质 / 处置 |
|---|---|---|---|---|
| `backend/app/config.py` | 追加 `town_` 块 | 追加 `polis_policy_` 块 | **不碰**(走 `os.environ`) | 追加式,按前缀分块,merge 手工拼 |
| `backend/app/tasks/nightly_cron.py` | 新增 1 个独立块(公共支出) | 新增 ≤1 个独立块(到期政策 poll) | **改骨架**(`nightly_cron_loop` + `run_nightly_jobs` 顶部守卫) | **唯一真冲突点**:线 3 只动调度骨架、绝不移动既有 job 块;收口把线 3 排最后,由它负责在最新块集合上重放 |
| `backend/app/models/__init__.py` | 加 `TownTreasury` | 加 `Policy` | 不碰 | 追加式 import 行 |
| `backend/app/services/civic_service.py` | **不碰** | 改 `_close_one`/`_execute_outcome` | 不碰 | 线 2 独占 |
| `backend/app/services/duty_service.py` | 改 `_pay_wage` | 不碰 | 不碰 | 线 1 独占(须锁 S2-1 的镇长加成回归门) |
| `backend/app/services/{coin,shop}_service.py`、`shop_effects.py` | 线 1 独占 | 不碰 | 不碰 | 无 |
| `backend/app/services/proposal_service.py`、`routers/admin/*` | 不碰 | 线 2 独占 | 不碰 | 无 |
| `backend/app/ws/manager.py`、`app/agent/chat.py`、`app/main.py` | 不碰 | 不碰 | 线 3 独占 | 无 |
| 新迁移 | `NNN_add_town_treasury` | `NNN_add_policies` | 无迁移 | 均接 047,收口按合并顺序线性化为 048/049 |

## 暂缓清单(想开也别开)

| 工作项 | 为什么缓 |
|---|---|
| S1-1 声誉 | 同时碰 `civic_service` / `election_service` / `coin_service`,与线 1、线 2 三方撞同一批函数;规格 §8 明确排最后,等本批收口 |
| `backend/skills_world_dev.db` 从 git 摘除 | 该文件被 git 跟踪且本次有 1.4MB→1.5MB 的二进制漂移。**属既有问题,本批一律不提交它**;摘除(`git rm --cached` + `.gitignore` + 本地 dev DB 重建口径)单独立项 |
| 夜间任务调度器抽象化 / per-job 调度 | 线 3 只做"补跑 + 心跳",不做调度器重构;重构等本批全部 nightly 追加块收口后单独做 |
| Lab 真实 Adapter / staging 灰度 | ROADMAP 阶段 4 受阻:生产身份、镜像、网络、存储、外部 attestation 未满足,保持 mock-only |
| 25 张居民纹理授权 | 需项目所有者逐项确认,非代码任务;`release provenance gate` 保持阻断 |
| 生产写操作(部署 / 迁移 / 改 .env / 重启容器) | 本批只有线 4 碰 vm212,且**严格只读**;任何写动作等本批收口后单独排 |

---

## 阶段 0 · 文档归档收口(主会话独占,6 条提交)

**为什么必须先做**:当前工作区已把 `docs/` 全树迁进 `archive/2026-07-25/` 并重写了 `docs/ROADMAP.md`,
但这些改动**还没提交**。四条线的提示词全部引用新路径;不提交就拉 worktree,分支里只有旧路径,
规格读不到、anchors 全废、报告目录也不存在。

**通用纪律**:
- 全程**显式 `git add <path>`**,**禁 `git add -A` / `git add .` / `git commit -a`**——`backend/skills_world_dev.db`
  会被顺手带进去。每条提交前 `git status --porcelain` 确认暂存区。
- 提交顺序有依赖:`.gitignore` 的 png 白名单必须先落,否则 `archive/**/docs/**/*.png` 与
  `assets/screenshots/*.png` 加不进去。

| # | 提交 | 内容(文件集) | 验证 |
|---|---|---|---|
| 1 | `chore(gitignore): png 白名单随 assets/ 与 archive/ 迁移改写` | `.gitignore`(`!docs/renders/*.png` → `!assets/screenshots/*.png` + `!archive/**/docs/**/*.png`) | `git check-ignore -v assets/screenshots/forge-main.png` 无命中 |
| 2 | `docs(archive): 建立 archive/2026-07-25 快照——docs/ 全树 + 过期记忆台账` | 全部 `docs/**` → `archive/2026-07-25/docs/**` 的 rename(含 `adr/ art/ kickoffs/ renders/ reports/ research/ testing/`)、`backend/docs/design/WORLD_CLOCK_DESIGN.md` → `archive/2026-07-25/memory/backend/...`、新增 `archive/2026-07-25/README.md`、`archive/2026-07-25/memory/README.md`、`archive/2026-07-25/docs/superpowers/`、`archive/2026-07-25/docs/art/post-office-*.png` | `git show --stat HEAD` 全是 R(rename)+ 新增,零内容改写;`ls docs/` 只剩待迁的 ROADMAP |
| 3 | `docs(roadmap): docs/ROADMAP.md 取代 ROADMAP_2026-07-24 成为唯一现行规划` | `docs/ROADMAP_2026-07-24.md` → `docs/ROADMAP.md`(rename + 重写)、`archive/2026-07-25/docs/ROADMAP_2026-07-24.md`(原件快照) | `ls docs/` = `ROADMAP.md`;新旧两份内容差异可读 |
| 4 | `docs(assets): README 媒体迁到 assets/screenshots 并改写 README/DESIGN/AGENTS 引用` | `docs/screenshots/*` → `assets/screenshots/*`、`README.md`、`DESIGN.md`、`AGENTS.md` | `rg -n "docs/screenshots" -- . ':!archive'` 零命中;README 图片链接可解析 |
| 5 | `chore(provenance): asset-provenance.json 迁到 frontend/config + 校验脚本对齐 + THIRD_PARTY_NOTICES` | `docs/art/asset-provenance.json` → `frontend/config/asset-provenance.json`(rename + 内容更新)、`frontend/scripts/verify-asset-provenance.mjs`、新增 `THIRD_PARTY_NOTICES.md` | `cd frontend && node scripts/verify-asset-provenance.mjs`(贴真实输出;25 张居民纹理仍应阻断 = 预期行为) |
| 6 | `chore(refs): 后端/部署内文档路径引用随归档改写` | `backend/.env.example`、`backend/app/{agent/map_data.py,lab/__init__.py,lab/adapter_gate.py,lab/sandbox/simverse_ref.py,models/resident_treasury.py,world_clock.py}`、`backend/scripts/{burnin_report.py,sbti_backfill.py}`、`backend/tests/{test_lab_adapter_selection.py,test_sbti_backfill.py}`、`deploy/lab-oci-runner/{README.md,provision-runner.sh}`(证据目录默认值改 `artifacts/lab-oci-evidence`) | `cd backend && python3 -m pytest tests/test_lab_adapter_selection.py tests/test_sbti_backfill.py -q`(贴输出);`rg -n "docs/(FEATURE_SPEC|adr/|renders/|research/|PROGRESS|LAB_HANDOFF|design/)" backend deploy frontend` 零命中 |

**阶段 0 硬门(全通过才放四线)**:
1. `git status --porcelain` 只剩一条 `M backend/skills_world_dev.db`(且**不提交**)。
2. `git log --oneline -6` 是上面 6 条,顺序一致。
3. `ls archive/2026-07-25/docs/kickoffs/` 能看到 `KICKOFF_S1-5_treasury.md` 与 `KICKOFF_S2-5_policies.md`
   (四条线提示词的路径前提)。
4. `cd backend && python3 -m pytest tests/ -q` 相对 base 零新增失败(基线先存 `/tmp/stage0-base.txt`)。
5. `cd frontend && npx tsc --noEmit && npm run lint` 绿(第 5 条提交动了脚本)。

**做完立刻广播**:四条线的 worktree 全部从阶段 0 收口后的 master 拉,不要复用旧 HEAD。

---

## 提示词 1 · S1-5 镇财政闭环(税 / 薪 / 公共支出)

```
任务:实现社会扩展 S1-5 镇财政闭环。严格按 archive/2026-07-25/docs/kickoffs/KICKOFF_S1-5_treasury.md
执行——规格已含任务切分/表结构/接口签名/测试用例名/探针定义/不碰区域;本提示词只补充环境事实与
并行纪律,两者冲突时以规格为准、偏差记报告。

准备:
- git worktree add ../sv-s1-treasury master && 新建分支 feat/s1-5-treasury
  (master 必须已含阶段 0 的 6 条文档归档提交,否则规格路径不存在——先确认 ls archive/2026-07-25/docs/kickoffs/)
- 先读:规格全文、archive/2026-07-25/docs/SOCIETY_EXPANSION_PLAN.md §2/§3.1/§6 相关行、
  services/{coin,duty,shop}_service.py、shop_effects.py、tasks/nightly_cron.py、
  agent/phases/execute/basic.py 现状。规格的 file:line anchors 先逐条校验(S2-1/S1-3 已落地,行号漂移),
  漂移以代码为准记偏差。
- 开工先跑全量 pytest 存基线:cd backend && python3 -m pytest tests/ -q > /tmp/s1-5-base.txt

环境事实(规格写作后发生的变化,以此为准):
- alembic 实测链头 = 047_add_issue_stances(规格写 040 已过时);
  新迁移 NNN_add_town_treasury 接实测链头,报告登记,收口时统一重排。
- _pay_wage 现位于 backend/app/services/duty_service.py:147-168;S2-1 落地后镇长加成仍读
  meta_json['mayor'](:157-158),而 install_mayor/current_mayor 已在 election_service 内部
  改道 offices(gated by polis_office_enabled,默认 False)。**你只改资金来源,不改加成语义**——
  S2-1 冻结的三道回归门(_pay_wage 加成、_execute_outcome mayor 分支、current_mayor 消费者行为)
  必须逐条不变,既有测试零改动通过。
- config.py Settings 类尾已有 POLIS_OFFICE_ 与 POLIS_OPINION_ 两块;你的 town_* 块追加在**最后**,
  不改他人行。nightly_cron.py 只新增一个独立 try/except 块,追加在既有治理块之后,不改不挪他人块。
- 并行线 S2-5(policies)会读你的 tax/disburse/balance 做财政类政策条目的 effect,
  **签名一旦落地即冻结**,变更须在报告里显式声明。

要求:
1. 按规格逐任务 TDD(任务 1 表/迁移/模型 → 2 TreasuryService → 3 税 hook → 4 发薪拦截 → 5 nightly → 6 REST/WS),
   串行门不跳步,一任务一提交带任务号(如 s1-5-2: TreasuryService.tax/disburse)。
2. town_treasury_enabled 默认 False,关闭时**字节级回落现状**(工资继续 MINT、售货不抽税、nightly 整块跳过);
   税率等旋钮按规格 §3 默认值(sales=0.1、gift=0.0、unfunded_policy="skip")。
3. 原子性按规格 §4 逐字抄 coin_service 惯式:守卫 UPDATE + upsert、禁读改写、零行守卫命中绝不 rollback
   (MissingGreenlet 回归门)、synchronize_session=False 后必须刷新 set_wallet_cache。
4. MVP 只做居民售货销售税作为镇财政主入口;**不重定向 EAT 餐费与 'sink' 分账**(会改变货币供给模型,规格 §7)。
5. 测试:规格 §5 列出的单测 + 集成用例全实现,重点含守恒断言、并发无丢更新、flag=False 字节级回落三类;
   全量 pytest 相对 /tmp/s1-5-base.txt 零新增失败;规格 §6 探针用 seeded fixture 出数。
红线:不合并不 push 不部署;不碰 civic_service / election_service / proposal_service / app/lab 内核;
不改 transactions ledger 的 FK;财政数字永不进 NPC prompt(写成测试断言);不提交 backend/skills_world_dev.db。
产出:docs/reports/feat-s1-5-treasury-report.md——任务状态表、全部偏差(含 anchors 行号漂移)、
迁移占位登记(NNN → 实测链头 047)、收口时需进 .env.example 的配置清单、探针数字、
给 S2-5 的接口冻结声明(tax/disburse/balance 签名 + 财政类政策条目的对接方式)。
```

## 提示词 2 · S2-5 policies 表 + 四级分级审批

```
任务:实现社会扩展 S2-5 policies 表与四级分级审批。严格按
archive/2026-07-25/docs/kickoffs/KICKOFF_S2-5_policies.md 执行;本提示词只补环境事实与并行纪律,
冲突以规格为准、偏差记报告。

准备:
- git worktree add ../sv-s2-policies master && 新建分支 feat/s2-5-policies
  (master 必须已含阶段 0 的 6 条文档归档提交)
- 先读:规格全文(含末尾 anchors 清单)、services/{civic,proposal,config,election}_service.py、
  models/{system_config,world_change_proposal}.py、routers/admin/{world.py,__init__.py,middleware.py}、
  lab/transitions.py、tasks/nightly_cron.py 现状。anchors 逐条校验,漂移记偏差。
- 开工先跑全量 pytest 存基线:cd backend && python3 -m pytest tests/ -q > /tmp/s2-5-base.txt

环境事实(规格写作后发生的变化,以此为准):
- alembic 实测链头 = 047_add_issue_stances(规格写 040 已过时);新迁移 NNN_add_policies 接实测链头,
  **绝不硬编码 041**(规格自己也提示过撞号),收口时统一重排。
- civic_service 行号已漂移:_close_one 现 :254-282(仍是纯 plurality,取 max,tie-break -i)、
  _execute_outcome 现 :284-315(四类型 system_config/dynamic_location/narrative/mayor)。
  **mayor 分支不要动**——S2-1 已把 install_mayor 内部改道 offices(gated),你只在 dispatcher 上
  新增 policy 类型,mayor 分支保持字节不变。
- config.py Settings 类尾已有 POLIS_OFFICE_ 与 POLIS_OPINION_ 两块;你的 POLIS_POLICY_ 块追加在最后。
  nightly_cron.py 只在必要时新增一个独立 try/except 块(若 close_due_polls 已覆盖则复用,不重复关闭)。
- 并行线 S1-5(财政)同期开发,尚未合并:财政类政策条目(tax_rate/医疗补贴/住房规模)的 value 语义
  依赖它。**本线只落存储 + 审批骨架**,财政类条目可 amend 但 effect 走 no-op 占位,
  在报告里登记待接线清单(等 S1-5 的 tax/disburse/balance 冻结签名)。

要求:
1. 按规格逐任务 TDD(1 表/模型/迁移 → 2 PolicyService 矩阵 → 3 track A 行政审批接线 → 4 track B 阈值 +
   policy effect → 5 config flag),串行门不跳步,一任务一提交带任务号(如 s2-5-1: policies table + migration)。
2. 两个开关都默认 False 且相互独立:polis_policy_enabled(存储层)、polis_policy_approval_enabled(审批路由)。
   关闭时**字节级回落**:get 回落 ConfigService、approve_proposal 回落单-admin CAS、_close_one 回落纯 plurality、
   _execute_outcome 不识别 policy 类型。
3. 原子性:apply_amend 用 version 乐观并发条件 UPDATE(rowcount==1 胜出),seed_defaults 用幂等 upsert
   (注意 SQLite dev / Postgres prod 方言差异);多步门一律复用 lab/transitions.py 的 cas_proposal_status,不自造锁。
4. constitutional_core 直接修改必须 raise PolicyImmutableError(自指保护:approval_routing 本身置
   absolute_majority);admin 逐端点 Depends(require_admin),不用 router 级依赖。
5. 测试:规格 §5 单测 + 集成用例全实现,重点含两个门控关闭的字节级回落断言与 alembic 单头校验;
   全量 pytest 相对 /tmp/s2-5-base.txt 零新增失败;规格 §6 两个探针(政策漂移距离、核心条款触碰计数)
   用 seeded fixture 出数,constitutional_core 漂移恒 0 写成硬断言。
红线:不合并不 push 不部署;不给 world_change_proposals 加列;不碰 duty/coin/shop/election 写路径;
不碰 app/lab 的 apply/preflight 内核(只复用 transitions/broadcast 两个 helper);
政策指标永不进 NPC prompt;不提交 backend/skills_world_dev.db。
产出:docs/reports/feat-s2-5-policies-report.md——任务状态表、偏差、迁移占位登记、
收口 config/.env.example 清单、探针数字、**待接 S1-5 的财政类条目清单**(键名 + 期望 effect 语义)。
```

## 提示词 3 · 工程健康批(R3 夜间补跑 + R4 聊天锁 DB 侧回收 + P2 多实例心跳告警)

```
任务:三件工程健康修缮,对应 docs/ROADMAP.md 近期优先级 #5(夜间任务错过窗口补跑机制、聊天锁 TTL、
多实例状态与告警可观测性)。全部纯本地开发 + 测试,不部署。

准备:
- git worktree add ../sv-eng-health master && 新建分支 fix/eng-health-batch
- 先读:tasks/nightly_cron.py(整文件,尤其 RUN_HOUR/RUN_MINUTE :28-29、_seconds_until_next_run :39-43、
  _world_week_gate :46-60、nightly_cron_loop :427-436)、app/main.py:86-99(后台 loop 注册与
  run_background_tasks 门)、ws/manager.py:196-278(Redis 侧锁)、agent/chat.py:175-205 与 :280-286
  (DB 侧 status 置位与 finally 复位)、agent/budget.py 的静默失效告警(commit a3a32ec,本批告警范式)。
- 开工先跑全量 pytest 存基线:cd backend && python3 -m pytest tests/ -q > /tmp/eng-health-base.txt

A. R3 — 夜间任务错过窗口补跑:
   现状缺口:nightly_cron_loop(:427-436)只 `sleep(_seconds_until_next_run(now_real()))` 然后跑一次,
   没有"上次成功跑到哪天"的台账。进程崩溃/容器重启/部署窗口跨过 07:00 北京锚点 → 当天全部夜间作业
   静默丢失,无日志无告警。
   做法:Redis 台账(如 sv:nightly:last_run_date,存已完成的锚点日期,语义与 RUN_HOUR 一致)+ 启动时判定
   "今天锚点已过且台账日期 < 今天" → 立刻补跑一次;run_nightly_jobs 顶部加幂等守卫防同日重入。
   参照既有 _world_week_gate(:46-60)的 Redis 幂等范式,别自造新机制。
   **纪律(硬):只动 nightly_cron_loop 与 run_nightly_jobs 的顶部守卫,绝不移动、改写、重排任何既有
   job 的 try/except 块**——S1-5 与 S2-5 正在并行往那里追加新块,你动一行都会变成手工合并地狱。
   测试:注入假时钟/假 Redis 覆盖 正常按时跑 / 跨锚点重启补跑一次 / 同日重启不重复跑 三态。

B. R4 — 聊天锁 DB 侧回收(Redis 侧已有 TTL,缺口在 DB):
   现状:ws/manager.py 的 lock_resident(:198-214,SET NX + 重入续期)与 lock_socializing(:255-270,
   双键 TTL + 半持有回滚)已经带 TTL,worker 猝死能自愈。**但** agent/chat.py:203-204 把两位居民的
   Resident.status 置成 "socializing",只在 finally(:280-286)复位;worker 被 kill / 容器重启 →
   DB 里的 status 永久卡住,之后所有互聊在 chat.py:180-182 命中 target_busy 静默跳过,居民社交永久哑火。
   做法:给 status 置位带上时间戳(meta_json 或既有列,按代码现状定,不臆造新列前先核实),
   加一个 stale-status 回收器(阈值与 SOCIAL_LOCK_TTL 对齐),挂在 heat_cron 或 nightly 的**自己的**
   独立块里,fail-open。
   测试:构造"崩溃遗留 socializing 状态"→ 超阈值后被回收 → 互聊恢复;未超阈值的活跃会话不被误杀。

C. P2 — 多实例状态与告警可观测性:
   现状:main.py:86-99 的五个后台 loop(heat/event/nightly/agent_loop/embedding_backfill)只在
   run_background_tasks=true 的那个进程里跑,loop 死了没有任何
   信号;预算熔断静默失效已有告警(a3a32ec)可作范式,但 loop 存活/夜间漏跑没有。
   做法:每个 loop 每轮写一个 Redis 心跳(sv:hb:<loop>,含时间戳),加只读健康视图(既有 /health 扩展或
   独立只读端点)+ 心跳过期时发 Sentry event + WARN 日志。阈值与开关走 os.environ 读取,
   **不改 config.py**(避免与 S1-5/S2-5 的 config 尾块相撞;收口时统一登记进 .env.example),默认保守、可一键关。
   测试:心跳新鲜 → 健康;心跳过期 → 恰好一次告警且不刷屏;开关关 → 完全静默。

红线:不碰 config.py;不碰 civic/election/duty/coin/shop/proposal 与 app/lab;不改任何既有 nightly job 块的
内容或顺序;不合并不 push 不部署;不提交 backend/skills_world_dev.db。
测试:三件各带单测;全量 pytest 相对 /tmp/eng-health-base.txt 零新增失败。
产出:docs/reports/fix-eng-health-batch-report.md——三件的现状缺口证据(file:line)、实现方式、
测试清单、**收口时需进 .env.example 的环境变量清单**、以及"本线与 nightly 并行块的合并说明"
(收口时本线排最后,由本线负责在最新块集合上重放骨架改动)。
```

## 提示词 4 · 只读运维审计(R5 投票分布复验 + P3 账单对账)

```
任务:对 vm212 生产环境做两项**纯只读**审计,不改任何代码、不动任何生产状态。对应
docs/ROADMAP.md 近期优先级 #1(SBTI 回填后核对投票分布是否消除 option-0 偏差;llm_usage 与真实账单核对)。

准备:
- 不建分支、不建 worktree(除非最后要提交报告文件——那就在 master 上单独一条 docs 提交)。
- 环境:vm212,远端目录 /opt/skills-world,compose 项目在 /opt/skills-world/deploy
  (服务名 db=pgvector/pgvector:pg16、redis、api、agent-worker)。当前 alembic=047。

A. R5 — SBTI 回填后的 NPC 投票分布复验:
   背景:回填工具已执行,26/26 居民已有完整 A2 维;回填前的症状是 NPC 投票垄断 option 0。
   只读取数(全部走只读事务:BEGIN; SET TRANSACTION READ ONLY; ... COMMIT;):
   - polls 表:回填时点前后各取若干张已 closed 的 poll,读 options_json 里每个选项的 npc_votes;
   - votes 表:按 poll_id/option_idx 聚合玩家票,与 NPC 票分开统计(别混在一起看分布)。
   判据(先写死再取数,禁事后挪门):option-0 得票占比、winner 分布的熵或基尼、
   "全票压在单一选项"的 poll 占比——回填后相对回填前必须显著下降;样本不足以判定时,
   **如实写"样本不足,需再等 N 轮 poll"**,不要凑结论。
B. P3 — llm_usage 与真实账单对账:
   - docker compose exec -T api python scripts/burnin_report.py --days <N> --residents 26
     (脚本纯读 llm_usage,零 LLM 调用);
   - 另按天聚合 llm_usage 的 cost_usd,与 deepseek 侧真实账单逐日对照,给出偏差率与偏差方向;
   - 顺带核对全局日预算($10)占用率与 $/居民·天,标出异常日并给出可能成因(不下结论就写"未定位")。

红线(硬,违反即停):
- **只读**:禁 UPDATE/DELETE/INSERT/DDL、禁 alembic、禁改 .env、禁 docker compose up/down/restart/build、
  禁重启任何容器、禁清理任何数据。psql 一律显式只读事务;脚本只跑已知只读脚本。
- 禁把生产数据里的用户隐私字段(邮箱等)抄进报告;只留聚合数字与 slug 级别标识。
- 有疑似需要写操作才能查清的问题:写进报告的"待办",不要自己动手。
产出:docs/reports/ops-audit-2026-07-25B.md——两项各自的原始命令、真实输出粘贴(不做美化/不做估算)、
判据与结论(含"样本不足"这种诚实结论)、以及给 ROADMAP 的一句话状态更新建议
(阶段 1「下一轮投票分布复核」这一门是否可以判绿)。
```

---

## 收口顺序(本批完成后,主会话统一执行)

1. **线 4 只读运维审计**:随时可并入(零代码冲突),其结论决定 ROADMAP 阶段 1 那道门能不能判绿。
2. **线 1 S1-5**(迁移定 `048_add_town_treasury`)→ 每合一条跑全量 pytest 相对基线零新增失败。
3. **线 2 S2-5**(迁移重排为 `049_add_policies`,`down_revision` 接 048)→ 合并后把 S2-5 报告里
   "待接 S1-5 的财政类条目清单"逐条接线或转为下一批任务,不留悬空占位。
4. **线 3 工程健康批排最后**:它改 `nightly_cron_loop` 骨架,让它在 1/2 的新增块全部落地后再重放,
   冲突由它一次性解掉(反序会让 1/2 的追加块反复撞骨架)。
5. 全部合完:`alembic heads` 单头校验(硬门)+ 统一补 `config.py` / `.env.example`
   (S1-5 的 `TOWN_*`、S2-5 的 `POLIS_POLICY_*`、线 3 的 `os.environ` 阈值)+ 全量 pytest 一次
   + 更新 `docs/ROADMAP.md`(阶段 2 社会地基状态、近期优先级 #1/#5 的完成情况),按 ROADMAP 维护规则
   把本批 `docs/reports/` 证据归入新的日期归档。
6. 部署 vm212 与开闸(`TOWN_TREASURY_ENABLED` / `POLIS_POLICY_*`)另开会话单独排,
   本批一律"代码完成"表述,不与"已部署""已开闸""生产验证"混谈。
