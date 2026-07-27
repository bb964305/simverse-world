# 2026-07-27B 审计整改批次设计 · 问题 → 方案 → 推进计划

> 基线 `c2fff2f`（= `origin/master`，含管理系统「立刻做」批次 24 个提交）。
> 本批**独立于在飞的 07-27 批次**（F1 声誉 / F2 公民权 / F3 任期 / T 运维），四条线继续跑，本文只定义与它们的顺序依赖和文件冲突面。
>
> **本文是批次设计，不是执行计划。** 它定的是线怎么切、边界在哪、验收硬门是什么。每条线的 step 级 TDD 计划由 writing-plans 单独产出，一线一份，落在 `docs/plans/`。
>
> 来源：一份外部审查报告的逐条核验（37 agent / 97 条裁决 / 二审推翻 11 条）+ 一轮针对性代码采集（9 agent / 40 条整改项）+ 一轮对抗性评审。**报告本身约 20 条是 ROADMAP 复述、4 条被生产运行时证伪**，本文只收核验后仍成立的部分，并补上报告完全没提到的 24 条。

---

## 0. 执行状态（2026-07-27 晚更新）

**基线已从 `c2fff2f` 推到 `6128ecb`** —— F1/F2/F3 三条线在本文定稿后合入并 push。这推翻了本文的三条决策前提，逐条记在下面；正文其余部分仍然有效。

### 0.1 已完成

| 项 | 状态 | 证据 |
|---|---|---|
| **A3 poll 延期** | ✅ 已执行 | `scripts/postpone_open_polls.py` + 8 个单测；生产三张 poll `closes_at` 已改为 `2026-07-31 23:29:43+00`，完成标记 `system_config.civic_poll_postpone_until`；不可重放防呆在真库上实测拦住且是真 no-op |
| **A2 静默罢免** | ✅ 由 F2 落地 | `e83ed51` + `93a2573`：`install_mayor` 对查不到的 slug 零写入 `return False`，写入包在 `begin_nested()` SAVEPOINT 里 |
| **A1 的核心危险** | ✅ 由 F2 覆盖（语义与本文决策 4 相反） | `d89f5fb` 的 `_winner_lost_civic_rights`：对**已删除**的 slug 返回 True → 走流会分支 |
| **H4 ROADMAP 纠偏** | ✅ 已改 | 四条被生产证伪的断言 + 三处随合并而变的口径，见 `91a4141` |
| **F2/F3 的「零新增失败」硬门** | ✅ 补验通过 | 合并方明说没跑。本文作者补跑双向差集：`comm -23` = **0**、`comm -13` = **0**，失败集 67 条逐条一致（50 failed + 17 errors），passed 2247 → 2558 |

### 0.2 被推翻的决策前提

| 决策 | 原前提 | 现实 | 处置 |
|---|---|---|---|
| **4 次名递补** | A1 与 F2 Task 7 是两个未实现的竞争设计，二选一 | F2 的**流会**实现已合入 master 且测得扎实（红/绿成对 + 三轮复审）。对这三张 poll 两种语义**收敛**（4 个候选全删，次名也递补不出人） | **暂按保留流会执行，不写 A1。** 改它等于重写刚合并的代码，收益只剩「将来有部分候选存活时的世界内规则偏好」。要坚持次名递补须重新拍板 |
| **5 A2 并进 F2** | A2 待实现 | 已随 F2 合入 | ✅ 自动满足 |
| **7 lab 先合** | 让 F2 不必重编迁移号 | F2 已占用 `051`。lab 的 `051_add_lab_codex_model_tier` 与主线的 `051_add_civic_standing_history` **都挂在 `050` 上** | **前提反转**：改为 lab 合入前重编号 052/053/054，`down_revision` 改指 `051_add_civic_standing_history` |

### 0.3 合并方留下的三个缺口（本批需接手）

1. **收口未做 → F1/F2/F3 的代码在运行时是死的。** `grep -n "civic_promotion\|office_audit" backend/app/tasks/nightly_cron.py` **零命中**。git log 很热闹，夜间链上什么都不会发生。
2. **新开关绕过 `config.py`** —— `civic_membership.py:376` 走 `_env_str("CIVIC_PROMOTION_MODE", "off")` 直读 `os.environ`。与本文 D3 标注的代价同型：不进 `Settings` → `test_env_example_consistency.py` 管不到 → `.env.example` 无记录 → 运维看不见。
3. **F1 第 3 项没做** —— `rep_credit_min_score` 仍是 `-0.3`（`app/config.py:576`），`scripts/rep_calibrate.py` 只建了工具没跑过。`REP_ENABLED` 仍不能开。

### 0.4 新的墙钟

三张 poll 现在于 **2026-08-01 23:00 UTC** 关票（`closes_at` 2026-07-31 之后的第一次夜间 cron）。**T1 部署必须在此之前完成**，否则它们仍会在旧镜像下结票——生产跑的还是 049 的镜像，上面所有修复都不在。

---

## 1. 已定决策

| # | 决策 | 落到哪条线 | 连锁后果 |
|---|---|---|---|
| 1 | 审计整改自成一批（07-27B），**不打断**在飞的 F1/F2/F3/T | 全部 | — |
| 2 | 三张脏 poll **延期而非作废**，`_close_one` 单独修 | A | — |
| 3 | 本文只做批次设计；step 级计划一线一份另出 | — | — |
| 4 | 失格候选走 **次名递补**，不走整案流会 | A1 | **F2 计划 Task 7 Step 5 的流会实现整段删除**，含 `_VERDICT_NOTE['winner_ineligible']` 与 `test_close_one_announces_a_failed_vote_when_the_winner_lost_rights` |
| 5 | `install_mayor` 结票复核 **整条并进 F2 Task 7**，A2 不单独立线 | F2 | A2 的「调用方日志」作为 F2 Task 7 的追加 Step；**F2 spec 的 §4.3 独占清单保留 `election_service.py:135-193`** |
| 6 | 用户 custom LLM 功能 **整体删除** | C2 | **C3、C4 全部作废**；C 线缩为 C1（部署验证）+ C2（两步删除）+ C1b（密钥吊销） |
| 7 | `feat/lab-codex-runtime` **先合 master** | 全部 | T1 迁移变成 **049→053**；F2 建表迁移改号；**所有线在 lab 合入后重抓测试基线** |
| 8 | poll 延期用 **heredoc 注入**（`docker compose exec -T api python - ... < script`） | A3 | 不依赖 T1；停 agent-worker 保留为 fallback |

**决策 4 与 5 的交互（必须写明，否则会误排）**：A2 并进 F2 后，它要等 F2 的 `①建表迁移 → ②T2 → ③代码合入` 走完，**赶不上延期到期日**。这是可接受的，因为 **A1 单独就能护住这三张 poll** —— 四个候选全部失格 → 走 `not live` 流会分支 → 根本不会调用 `install_mayor`。A2 是纵深防御的第二层，不是这次的止血层。

> **硬约束**：A1 必须在延期到期日之前部署到 vm212。A2 可随 F2 走。

---

## 2. 开工前已核实的现状（含对上一版判断的纠偏）

逐条查过代码与生产，不是推测。**以下四条推翻了此前流传的说法，先列出来。**

### 2.1 关票时间：真实截止是 2026-07-28 23:00 UTC，不是 07-27 23:29

`close_due_polls` 全仓**唯一调用方**是 `app/tasks/nightly_cron.py:217`，而 nightly 主循环是每天 Beijing 07:00（= 23:00 UTC）跑一次（`nightly_cron.py:29-30` `RUN_HOUR=7`、`:557-563` `_seconds_until_next_run(now_real())`，`now_real()` 锚 Asia/Shanghai）。判定是：

```python
# civic_service.py:456-459
due = poll.closes_at
if due is not None and due > now:
    continue
```

三张 poll 的 `closes_at = 2026-07-27 23:29:43 UTC`。**今晚 23:00 那次 cron 跑时它还在 29 分钟之后 → 跳过、不关。** 下一次 cron 是 2026-07-28 23:00 UTC，那时才会关。

> **口径纠正**：poll 不在 `closes_at` 关，而在它之后**第一次夜间 cron** 才关。按 `closes_at` 读出来的「还剩 N 小时」一律偏早一天。此前「今晚 23:29 结票」的说法错了一天，A3 原研究稿的「硬截止 07-27 22:30 UTC」同错。

### 2.2 三项整改「代码完成」但**尚未部署**，生产仍是修复前状态

管理系统「立刻做」批次（24 个提交）已合进 `c2fff2f`，但**一个都没上生产**。按 ROADMAP `:78` 的四态口径，它们停在「代码完成」，不是「已部署」，更不是「生产验证」。

| 原编号 | 项 | 代码 | 生产实测 |
|---|---|---|---|
| D1 | 8100 公网绑定 | ✅ `deploy/backend/docker-compose.yml:64` → `127.0.0.1:8100:8000`（`61969f5`） | ❌ `/opt/skills-world/deploy/docker-compose.yml:60` 仍是 `0.0.0.0:8100:8000`；`docker ps` → `deploy-api-1  0.0.0.0:8100->8000/tcp` |
| C1 | 管理端明文回传平台 API Key | ✅ 读侧掩码 + 写侧把掩码/空串当「不修改」（`c26fdd1`+`f7a8430`） | ❌ 容器内 `grep -c "MASKED_VALUE\|_is_secret_key" app/routers/admin/system_config.py` → **0**。**平台密钥明文回传在生产是活的** |
| G4 | `.env.example` 缺 METRICS | ✅ `METRICS_ENABLED=false` / `METRICS_TOKEN=` 已补（`0d66c78`） | — 模板类，不影响运行中环境 |

> **推论 1**：这三项**不重复排整改**，但必须排**部署与生产验证**。D1/C1 的生产验证挂在 T1 上，且 D1 还需要一步 —— compose 是宿主文件，`git archive backend` 送不上去（§7 投递路径缺口）。
>
> **推论 2**：C1 与 H1 是同一类错位 —— 「已修复」指的是仓库，生产上洞是开的。本批所有「已修」结论都必须带部署态限定词。
>
> **推论 3**：哨兵铸币修复（`1fbc87a` + 四处 guard）、lab 时间炸弹修复（`f046210`）同样未部署（容器内无 `app/services/system_users.py`）。

**仍未修的邻项**：`Dockerfile:26 --forwarded-allow-ips=*` 字面仍在（D2）；`models/user.py:38` 用户 `custom_llm_api_key` 仍是裸 `String(500)`（C2）。

### 2.3 「UGC 居民有投票权」的真实形态：不是脏数据，是没部署

生产容器内 `ls app/services/civic_membership.py` → **No such file**；`alembic current` → `049_add_policies`。生产跑的是 hotfix 之前的镜像，`Resident.resident_type` 的 `default="npc"` 在五个构造点全部生效 —— **今天在 vm212 上 forge 一个居民，它照旧拿到投票权和被选举权。**

而**存量泄漏实例为 0**（`SELECT resident_type,count(*) FROM residents` → `npc|11`，全是 07-25 重新 seed 的内置阵容）。

> **推论**：T1（部署 master）一做完，泄漏就关闭；T2 回填在 vm212 上是**空跑**，跑出 0 行是正确结果不是失败。ROADMAP `:16` 与「近期优先级 2」把这件事描述反了 —— 见 H4。

### 2.4 A4「UGC 进候选池」在 master 上已修

`fbb72af` 已把候选池谓词从人口口径换成 `is_civic_voter`，并有回归测试 `test_ugc_resident_no_political_rights.py`。残留只有两处：(a) 尚未部署到 vm212（T1 职责）；(b) 一张开票于修复前的 poll 把 UGC 候选人 slug 固化在了 `options_json` 里 —— 开票时的资格判定不追溯，只能靠**结票时复核**，即 A1。

---

## 3. 问题总表

40 条整改项，按线归类。`级` 为核验后的真实优先级（与外部报告给的优先级常有出入）。

| 线 | ID | 问题 | 级 |
|---|---|---|---|
| **A** | A1 | `_close_one` 结票不复核候选人在籍 | P0 |
| | ~~A2~~ | `install_mayor` 静默罢免在任镇长 —— **已并进 F2 Task 7**（决策 5），不在 A 线交付 | → F2 |
| | A3 | 三张脏 poll 的延期动作（生产数据变更） | P0 |
| | ~~A4~~ | UGC 进候选池 —— **master 已修**，只剩文档口径纠偏 | — |
| **B** | B1 | `delete_account` 只覆盖 7 张表 / 30+ 个 user 关联列 | P1 |
| | B2 | `resident_sprite_runs` 三个 users FK 无 `ondelete` → T1 部署 050 后删号 500 | P1 |
| | B3 | `resident_relations` 多态 party 列存 user id，删号从不引用这张表 | P2 |
| | B4 | `memories` 只置空 `related_user_id`，正文继续进 NPC 提示词 | P1 |
| | B5 | 托管币永久冻结：用户消失 → refund 抛错 → hold 永远 held、LabTask 永远非终态 | P1 |
| | B6 | 排行榜把已注销用户以「空名字 + 裸 UUID」继续挂榜，季末奖金静默烧掉 | P2 |
| **C** | C1 | 管理端明文回传平台 Key —— **代码已修、生产仍在漏**（§2.2），只剩部署与生产验证 | P1 |
| | C2 | 用户 custom LLM **整体删除**（决策 6）：步 1 删代码路径、步 2 单独 DDL 删列 | P2 |
| | ~~C3~~ | `allow_user_custom_llm` 策略开关零读点 —— **随功能删除作废** | — |
| | ~~C4~~ | 前后端 LLM 字段名断层 —— **随功能删除作废** | — |
| | C1b | 两把真实 API Key 留在**公开仓库**的 git 历史里 | P2 |
| **D** | D1 | 8100 公网绑定 —— **代码已修、生产仍公网可达**（§2.2），需补投递路径 | P1 |
| | D2 | `--forwarded-allow-ips=*` 的安全论证前提曾经是假的，需补不变量测试锁死 | P2 |
| | D3 | REST 限流是 per-worker 内存计数，生产 2 worker 下额度翻倍 | P1 |
| | D4 | OAuth 回调用 URL query 明文回传 24h JWT；重定向目标取 `cors_origins[0]` | P1 |
| | D5 | WS 仍兼容 `?token=`，前端已完全不用，纯死代码 | P2 |
| **E** | E1 | Markdown 编辑器 live preview 无条件启用 `rehype-raw` + 全站零 CSP → 可窃 JWT | P1 |
| | E2 | `ability_md` 漏过敏感词过滤 —— 真正的洞是另外 4 条写入路径整条无过滤 | P3 |
| | E3 | 举报/屏蔽/审核/申诉全线为零，聊天文本零过滤 | P2 |
| **F** | F1 | 默认测试集选中 41 条需真 PG/Redis/staging 的测试 | P1 |
| | F2 | 17 errors 全部来自 3 个文件用 `pytest.fail` 而非 `pytest.skip` | P1 |
| | F3 | master CI 连红 17 次 / 10 天，唯一挂的 job 是 backend-tests | P0 |
| | F4 | CI 的 Typecheck 步骤检查 **0 个文件**（solution-style tsconfig + `--noEmit`） | P2 |
| | F5 | CI 两处装依赖都用 pip 无锁解析，`uv.lock` 完全没被用上 | P2 |
| | F6 | 存量失败基线清零口径：**0 条缺环境、约 44 条是测试与实现契约漂移** | P1 |
| **G** | G1 | `deploy.sh` 的 `rsync --delete` 会删远端 `.env` 与 `tmp/` | P2 |
| | G2 | `deploy/frontend/deploy.sh` 只透传 `VITE_API_URL`，前端 Sentry 静默丢失 | P2 |
| | G3 | `/metrics` 有埋点无采集；真正有用的世界指标在 agent-worker，压根没有 HTTP 端点 | P2 |
| | ~~G4~~ | `.env.example` 缺 METRICS —— **已修**；但 `test_env_example_consistency.py` 的不变式仍错，见下 | P1 |
| | G4' | `test_env_example_consistency.py` 已红 5 天，且它检查的根本不是这一对文件 | P1 |
| | G5 | `app/llm/client.py` 未关 `trust_env` → 全部 LLM 出网静默走环境代理 | P2 |
| **H** | H1 | UGC 政治边界修复根本没部署（§2.3） | P0 |
| | H2 | 生产三开关比代码默认更开，且与迁移同批落地（相隔 75 秒） | P1 |
| | H3 | 生产真值报告不在 master，ROADMAP 引用了一个 master 上打不开的文件 | P2 |
| | H4 | ROADMAP `:14`/`:16`/`:17` 已被生产证伪，方向判断也错 | P1 |
| | H5 | `feat/lab-codex-runtime` 与本批三处硬冲突：alembic 双头 / 测试基线失效 / ROADMAP 同文件 | P1 |

**F6 是本表里最反直觉的一条**：项目一直相信那 51 failed「需要 redis/testcontainers」。实测**缺环境的是 0 条**——真实构成是 2026-07-22 lab-v2 大合并（`45bc0b7`/`a161cd5`）把实现从 protocol-v1 推到 v2、executor 改成 remote broker、runtime store 推到 v3，但测试、静态清单与 `.env.example`↔`Settings` 对账全没同步。合并当天分支从未 push，CI 从没在它上面跑过。

---

## 4. 线 A · 选举结票收口

### A1 · `_close_one` 结票不复核候选人在籍

**现象**（vm212 生产数据 + 本机运行时探针）：

```
镇长选举 poll: 克劳斯 17 / 夜风侦探 2 / 伊莎贝拉 5 / 亚当 1，SELECT count(*) FROM votes = 0
四个候选 slug 在 residents 表全部 EXISTS = false（residents 只剩 11 行）

PROBE-A1 closed=1 status=closed
PROBE-A1 opts[0]=True final_votes=17
PROBE-A1 mayor flags after close = set()
PROBE-A1 current_mayor = None
PROBE-A1 bulletin: 「镇长选举:谁来当下一任镇长?」投票结束,「克劳斯」以 17 票胜出。议案生效时遇到问题,已记录。
```

公告白纸黑字宣布克劳斯当选，库里 `meta_json['mayor']` 为空、`system_config.current_mayor` 不存在、`offices.mayor.holder_slug` 为空。poll 已 `closed`，**无任何重试路径**；下次自动选举要到 2026-08-21（`election_interval_days=28`）。

**根因**：被动选举权只在 `open_election` 开票那一刻判定，slug 固化进 `options_json` 后，`_close_one`（`civic_service.py:469-518`）到 `install_mayor` 全链再无复核。开票到结票之间有 3 个世界日窗口，其间候选人可能被删、被 purge、或（F2 上线后）被降级。

**关键的不可能性**：`npc_votes` 是每个 option 上的**匿名整数计数器**，`_npc_voters` 是只写在 `opts[0]` 上的**扁平全局名册**（`civic_service.py:165-173`），两者之间无映射。25 名册 = 17+2+5+1 票，但**哪 17 个人投了克劳斯无从得知**。→ **「按在籍重算票数」在现结构下做不到**，且 07-27 批次 `:98` 已明确否决改结构。

**修法**：票不重算（保留幽灵票，与「投票时具备资格即计票」一致），**复核发生在候选人侧**。新增 `_CANDIDATE_EFFECT_TYPES = {"mayor"}` + `_candidate_slug(o)` + `_ineligible_candidate_options(db, opts)`，在 `win = max(...)` 之前剔除失格候选，从剩余候选里取 winner；全部失格则流会、不发「当选」公告。

非候选类 poll（`dynamic_location` / `system_config` / `narrative` / `policy` / `None`）`_candidate_slug` 返回 `None` → `live == range(len(tally))` → `max` 与现在**逐字节等价**，建筑提案/政策 poll 行为零变化。

**硬门**：

```bash
cd <worktree>/backend && source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_m3_civic.py tests/test_m6_election.py \
  tests/test_policy_approval_integration.py tests/test_ugc_resident_no_political_rights.py \
  -q -p no:randomly
```

断言必须**同时覆盖两条路径**（评审指出原稿只覆盖了边缘路径）：
1. **流会路径**：用生产 `options_json` 原样构造（四候选全失格）→ `opts[0]['won']` 不存在、公告不含「以 17 票胜出」、不含候选人名。
2. **主路径（次名递补）**：2 名候选、1 名失格 → 另一名 `won=True`、`install_mayor` 返回 True、`meta_json['mayor']` 落地。

**独占文件**：`app/services/civic_service.py`、`tests/test_election_close_eligibility.py`（新建）、`tests/test_m3_civic.py`

> ✅ **已按 §1 决策 4 定案：次名递补。** F2 计划 Task 7 Step 5 的竞争实现（「winner 照旧选出 → `install_mayor` 返回 False → 追加 `_VERDICT_NOTE['winner_ineligible']` → 整案流会」）**整段删除**，连同它的 `test_close_one_announces_a_failed_vote_when_the_winner_lost_rights`。
>
> 落地时必须同步改 `docs/plans/2026-07-27-F2-civic-standing.md`，否则 F2 执行者会照着已作废的 Step 5 写代码，然后与 A1 撞在同一段 `effect / result_note` 上。**这条 spec 修订先于 F2 Task 7 开工。**

### A2 · `install_mayor` 静默罢免在任镇长 —— **已按 §1 决策 5 并进 F2 Task 7，不在 A 线交付**

> 本节保留问题描述与修法，作为 F2 Task 7 的**输入规格**；A 线不再单独排这条。
> **不赶延期窗口**：A1 的 `not live` 分支已使这三张 poll 根本走不到 `install_mayor`。

**根因**：写操作（清空全镇 mayor 标 + commit）排在读校验（winner 是否解析得到）**之前**。函数把「扫描顺带发现 winner」和「安装 winner」耦合在同一个循环里，于是「找不到人」这条失败路径不是 no-op，而是**一次已提交的罢免**。叠加返回值 False 在 `_execute_outcome` 里被静默吞掉、公告文案在执行前就已拼好 —— 失败在世界内和运维侧都不可见。

**修法**：把 winner 解析提到任何写操作之前；解析不到则整函数 no-op 并 `logger.error`；`_execute_outcome` 的 mayor 分支（`civic_service.py:612-614`）不再吞掉 False。

**硬门**：种入持 `meta_json={"mayor": True}` 的在任者 + 一个路人 → `assert await install_mayor(db, "ghost") is False` **且在任者的 mayor 标仍在**。必须点名 `tests/test_office_integration.py:162`（选举结果 → `_execute_outcome` → `install_mayor` 全链用例）为硬门 —— 它才是唯一能抓到「公告说当选、库里没人」的回归。

**独占文件**：`app/services/election_service.py:135-193`、`app/services/civic_service.py:612-614`、`tests/test_m6_election.py` —— **全部归 F2 Task 7**，07-27 批次 `:109` 的独占声明保持不变，不需要改 F2 的 spec 边界。

> **为什么不选「A2 先合、F2 rebase」**：A2 的修法明写「`:141` 的 `is_autonomous` 扫描谓词保持不动」，而 F2 Task 7 的 `test_stale_mayor_flag_on_a_demoted_resident_is_still_swept` 依赖 `:141` 改成全表扫描。A2 先合之后 F2 仍要回到同一个函数再改一次 —— 同一函数两次 TDD 循环、两条 commit、一次强制 rebase。

### A3 · 三张脏 poll 的处置

**真实窗口**：2026-07-28 23:00 UTC（§2.1）。今晚不会关。

**原研究稿给的生产命令不可执行**（评审发现）：`docker compose exec -T api python scripts/postpone_open_polls.py` —— api 镜像由 `Dockerfile:10` 的 `COPY . .` 构建，compose 里 api 的 volumes 只有 media/artifacts/receipt-trust，**没有 backend 源码 bind mount**，新脚本不在运行中的镜像里，必然 `No such file or directory`。「不依赖 T1 部署，用现镜像就能跑」这句话是错的。

**四条可行姿势，已按 §1 决策 8 钦定 ①**：

| # | 姿势 | 判定 |
|---|---|---|
| **①** | `docker compose exec -T api python - --until ... --apply < backend/scripts/postpone_open_polls.py` | ✅ **采用**。T 计划 Task 10 Step 1 已有同款 heredoc 先例；`python -` 会把后续参数原样给 `sys.argv` |
| ② | `docker cp` 脚本进容器再 exec | 备选，多一步 |
| ③ | 等 T1 重建镜像 | 否决 —— 是另一次变更，且要先做完 T1 |
| ④ | **停 agent-worker**（nightly cron 跑在 `app/agent/main.py:59`，api 侧 `RUN_BACKGROUND_TASKS=false`） | ✅ **保留为正式 fallback**：零代码、零数据变更、零迁移，代价是世界停摆。2026-07-28 22:5x 执行可无损买到一天 |

脚本仍须满足 T2 三条硬约束：进仓库、被评审、`--dry-run` 为默认值、不可重放（`system_config` 写完成标记，无 `--force-rerun` 则拒绝）。

**`--until` 取值不能拍脑袋**：评审发现原稿的 14 天（08-10）会与 T2 死锁 —— T2 的回填脚本在存在 open poll 时 `raise CivicBackfillRefused`（T 计划 `:1371-1377`），而 `closes_at=2026-08-10 23:29` 的实际关票日是 **08-11**，于是 T2 在 08-11 前没有任何合法窗口，`§7 ②→③→④` 全线顶到 08-11 之后，而 08-21 是下次自动选举的硬边界，余量从 11 天塌缩到不足一周。

> **改为：`--until` = A1/A2 部署完成后 +1 个夜间周期量级（2026-07-31 ~ 08-02）。** 双向约束写进文档：延太短 = A1/A2 来不及；延太长 = T2/F2 全线堵死。

**独占文件**：`backend/scripts/postpone_open_polls.py`、`backend/tests/test_postpone_open_polls.py`（均新建，与所有在飞线零相交）

---

## 5. 线 B · 账户删除与数据治理

**B 组不可 fan-out** —— B1/B3/B4/B5/B6 全部改 `app/services/account_deletion.py` 与 `tests/test_account_deletion.py`，只能一条线顺序做。

### B1 · 骨架步：user 列注册表

**根因**：清理清单是 2026-07-23 那轮 P1 修复时按「PG 会 500 的 NOT NULL FK」逐个手写的（`settings_service.py:129-132` docstring 自陈），判据是**「会不会让 DELETE 报错」**而不是「这列是不是指向用户」。绝大多数表的 user 列没有 DB FK（纯 String 列），删 users 行不报错，于是从未进过清单，也没有任何机制在新增表时提醒补登记。

**修法**：把 `delete_account` 从「手写清单」改成「注册表驱动」——`USER_LINKED_COLUMNS` 显式登记 (表, 列, policy∈{erase, null, anonymize})，加 `scan_user_residue(db, uid)` 探针。

**硬门**：
1. `test_no_residue_after_delete` —— 建用户并在注册表覆盖的每张表各写 1 行，删号后 `assert await scan_user_residue(db, uid) == {}`
2. `test_user_column_registry_is_exhaustive` —— 从 `Base.metadata` 反射出所有「列名属于 `USER_ID_COLUMN_NAMES` 或带指向 users 的 ForeignKey」的 (table, column)，断言 `suspects - set(USER_LINKED_COLUMNS) == set()`。**新增表漏登记即红。**

> ⚠️ **原稿的防呆必须推翻**（评审）：原稿提议 `users.player_resident_id` 非空 → `raise AccountDeletionRefused`。但 `app/services/onboarding_service.py:99` 给**每一个完成新手引导的用户**都写了 `player_resident_id` —— 生产 45 个用户里凡建过化身的全部删不掉号。这不是防呆，是把一个数据残留缺陷升级成「删号功能下线」（还有合规含义）。**正确修法是给化身定归属策略**（转无主 UGC / 匿名化 / 按 B4 同款 anonymize 正文），不是拒绝删除。B5 的托管防呆方向是对的（钱不能凭空烧掉），B1 这条不是。

### B2 · `resident_sprite_runs` FK 无 ondelete

`050_add_resident_sprites.py:67-69` 给 `reviewed_by`/`published_by`/`rolled_back_by` 加了指向 `users.id` 的硬 FK 且无 `ondelete`；三列都是 nullable，天然适合 `SET NULL`，只是漏写。**同型错误 2026-07-23 犯过一次并留下迁移 045**（`residents.creator_id` NOT NULL 导致删号 500），050 是新表却没继承教训。

**时序耦合（本组唯一跨线约束）**：迁移 050 **尚未部署**（生产在 049），所以现在还没爆 —— **T1 部署 050 后就会爆**。

- 若 B2 在 T1 **之前**落地 → 就地改 050，revision id 不变，F2 的 `civic_standing_history` 仍以 `050_add_resident_sprites` 为 `down_revision`，无影响。
- 若 T1 先跑 → B2 必须改成新迁移，此时与 F2 建表迁移 + H5 的 lab 迁移**三方抢链头**。

> **建议：B2 排在 T1 之前。** 若不可行，T1 部署检查单里必须加一条「050 的三个 users FK 是否已带 ondelete」。
> **另注**：「就地改 050」的前提「从未 apply 到任何环境」只对生产成立 —— 本机/dev 库跑过 `alembic upgrade head` 的不会重跑 050，真库仍是 `NO ACTION`，而测试走 `create_all` 会绿。**改 050 后 dev 库必须重建。**

### B3 / B4 / B5 / B6

| ID | 要点 |
|---|---|
| **B3** | `resident_relations` 是刻意做的「无 FK 多态两方表」（模型 docstring `:10-27`），DB 层不知道 party 列有时是 `users.id`，于是没进按 FK 反推的清单。5 处写入点带 `type2="player"`。 |
| **B4** | 「detach 而非 delete」只解决了 FK 约束，被当成了隐私清理；**指针不是数据，正文才是**。且 `extract_events` 从一开始就没把 user_id 传下去，「玩家相关记忆」这个集合在库里从未被完整标注过。硬门两条：正文不含用户名 + `retrieve_context` 不再召回。 |
| **B5** | 账户删除与实验室托管是两套独立演进的子系统：托管侧不变式是「hold 的对手方全程存在」，账户侧从没被告知。二者之间既无 FK（`coin_holds.user_id` 是裸 String），也无应用层握手。**防呆必须排在注册表的 erase/null 两段之前执行**，顺序不可颠倒（先删后拒 = 半删状态）。 |
| **B6** | `season_scores.user_id` 是无 FK 裸列，排行榜把「解析不到」当成显示层降级（补空名字）而非数据层错误。**本组唯一需要改既有测试断言的项**（`tests/test_seasons.py:87` 现在断言的正是待修的旧行为），执行时要显式说明「改的是规格，不是为了让它绿」。<br>⚠️ 评审：**只批准 (a) 删号擦除 + (b) inner join 过滤两步**；「移除 `user_id` 字段」单列为待定 —— 那是一次未验证的公开端点响应形状变更。 |

**B 组生产验收**（评审指出整组零生产验收）：部署后在 vm212 建一个测试账号 → 走完整用户路径（聊天/送礼/目击/委托）→ 删号 → 跑 `scan_user_residue` 探针贴读数。否则「注册表穷尽性」永远只被它自己的测试证明。

---

## 6. 线 C · 密钥与用户配置

**按 §1 决策 6，C3 与 C4 全部作废** —— 用户 custom LLM 功能整体删除，没有「校验策略开关」和「修字段名断层」可言。C 线缩为三项。

### C1 · 部署并生产验证密钥掩码

代码已在 `c2fff2f`，**生产仍在明文回传**（§2.2）。本项无代码交付，产出是 T1 之后的一次生产验证：

```bash
# 带 admin token 的线上只读调用；判定只贴「不含 sk- 前缀 / 长度为掩码长度」
# 响应体本身绝不进任何文档、日志或 commit message
```

### C2 · 删除用户 custom LLM 功能（两步，不得合批）

**根因**：custom LLM 是只落地了「存」这一半的功能 —— 写入路径完整（`settings_service.py:294`），消费路径预留了签名却从未接线（`ws/handlers/chat.py:255` 不传）。`grep custom_llm backend/app/llm/ backend/app/agent/` → **0 命中**。用户交出明文密钥，承担 100% 泄漏风险，换零功能收益。

> ⚠️ **必须拆两步**（评审指出原稿把 `drop_column`×5 与删路由/删 UI/删 Settings 字段放同一次变更，违反「迁移与行为变更不同批」红线）：
>
> **步 1 · 删代码路径，列留着不用** —— 删 `routers/settings.py` 的 LLM 路由、`schemas/settings.py` 的 `LLMUpdateRequest`/`LLMTestRequest`、`schemas/admin.py` 的四个 custom_llm 字段、前端 `LLMSection.tsx` 与 `services/api/settings.ts` 的对应面、`config.py` 的 `allow_user_custom_llm`（延到收口，§1 决策 6 锁了 config.py）。零迁移。
> **步 2 · 单独一次 DDL 删列** —— `users.custom_llm_*` 五列。迁移号见 §14 链序。

**硬门**：
```bash
grep -rn "custom_llm" backend/app/ frontend/src/ | grep -v node_modules | wc -l   # 期望 0
cd backend && .venv/bin/python -m alembic heads | wc -l                            # 期望 1
```

### C1b · 公开仓库 git 历史里的两把真实密钥

| ID | 根因 | 动作 |
|---|---|---|
| **C1b** | 开源前 sanitize 只做工作区替换，没做历史重写，也没有任何前置检查 | 历史 blob 现在仍可取：`git log --all -S 'sk-sp-2f85' --oneline` 有输出。**已核实两把历史密钥的 SHA-256 与生产在用的均不匹配**（比对只用哈希前 16 位，未打印明文）→ 泄漏的不是当前在产密钥，P2 而非 P0。<br>**动作**：① 供应商侧吊销（公开历史里的密钥一律按已泄漏处理）；② 重写历史收益有限（GitHub 缓存 + 已有 clone），**吊销优先于重写**；③ 防复发测试 `test_no_hardcoded_secrets.py`，正则复用 `app/lab/guard.py:38-41` 的 `_SECRET_RE` 避免两份规则漂移 |

---

## 7. 线 D · 网络边界与认证

**D2/D3 建议与 D1 的既有改动合成同一条线**（共抢 `deploy/backend/docker-compose.yml` 与 `tests/test_deploy_compose.py`），一次容器重建同时生效。

| ID | 要点 |
|---|---|
| **D2** | 注释把「部署形态的假设」当成「已强制的不变量」写进安全论证，而这假设从未被任何测试锁住。D1 已修好前提，但**要补跨文件不变量测试**：`--forwarded-allow-ips=*` 只有在 api 仅回环发布时才成立，任何一天有人把发布面放宽回 `0.0.0.0` 必须立刻红 |
| **D3** | P0-3b 把跨进程状态搬进 Redis 时只覆盖了 WS 限流器，REST 侧 slowapi `Limiter` 没一起迁移（`rate_limit.py:39` 无 `storage_uri`），而 Dockerfile 同期把 worker 从 1 提到 2 —— 两个变更叠加使默认值 `memory://` 从「正确」变成「静默失效」。<br>⚠️ 推荐修法刻意走 `os.environ` 以避开 §1 决策 6 锁给收口的 `config.py`；**代价必须写明**：该开关不进 `Settings` → `test_env_example_consistency.py` 管不到 → `.env.example` 不记录 → 生产上多一个没人知道没人测的变量。**要么标注「收口时并回 Settings 并补 .env.example」，要么整体延到收口批。** |
| **D4** | OAuth 回调需要把凭证从后端域交回前端域，当时选了最省事的 URL query；同时缺一个「前端落地页」配置项，就近拿 `cors_origins[0]` 顶替，把安全边界配置和路由配置耦合在同一个列表上。<br>Referer 泄漏已实测：同源子资源请求照带完整 URL（含 JWT）→ CF Workers 边缘日志每条 asset 请求记一次。<br>两个方案（HttpOnly Cookie / 一次性 code 交换）改造面差异较大，step 级计划里给取舍 |
| **D5** | 纯死代码，前端已只走 first-message auth。删 `ws/handlers/connection.py:44-47` 四行即可。<br>注意：若 D4 选方案 A 会与 D5 抢同文件；选方案 B 则冲突消失 |

> **评审补充 —— 取证命令迁移清单**：D1 收窄 8100 之后，所有 `curl http://<vm212 公网>:8100/...` 形态的取证命令全部失效。T 线健康检查、T4 压测入口都在这个形态上，需统一改宿主回环或隧道域名。
>
> **投递路径缺口**：T 线的部署是 `git archive --format=tar master backend`，**只打包 `backend/`**；`deploy.sh` 虽被 T Task 2 修改但明确「本线实际部署走 git archive，不用 deploy.sh」。于是 D 组改的 `docker-compose.yml` / `Dockerfile` **在本批次里没有任何一步会把它们送上 vm212**。必须补一步（scp 到 `/opt/skills-world/deploy/` + `docker compose up -d`）并写进 D 组硬门。

---

## 8. 线 E · XSS、内容与治理

| ID | 要点 |
|---|---|
| **E1** | `@uiw/react-md-editor` **无条件强制启用** `rehype-raw@7` 且包内零 sanitizer；`ResidentEditor.tsx:154-158` 用 `preview="live"` 渲染服务端存量内容且无 `previewOptions`。实测 `<iframe srcdoc>` payload 原样进 DOM。**全仓无 CSP**。可用链：构造 soul-card → 受害者 import → 进 /profile 编辑 → token 外传。<br>修法：`previewOptions={{ rehypePlugins: [...] }}` 关掉 raw HTML + CSP（层级按部署形态定：后端 middleware 还是 CF Workers response header） |
| **E2** | 真正的洞不是 `ability_md` 一个字段，是另外 4 条写入路径整条无过滤。回归断言：干净创建后 `PATCH ability_md="fuck"` 必须 400（当前 200）。<br>⚠️ 原稿的顺序判断错了：F2 抢的是 `routers/**admin**/residents.py`，不是 `routers/residents.py`；且 F2 的 AST 守卫只扫 `*.resident_type = ` 属性赋值，扫不到构造器 kwargs。**E2 不必排在 F2 之后，可立即并行。**<br>但 **E2 必须先于 F2 开闸** —— UGC 晋升为一等公民后，未过滤的 `ability_md` 正文会进 NPC prompt、公开名录与议政文本。只要 5 分钟。 |
| **E3** | 内容治理从未列入任何里程碑（`grep -rln "XSS\|CSP" docs/` 0 命中）。`lab/moderation.py` 是唯一一次有意识建执行点的尝试，但**只建了插座没插电**（`config.py:232 lab_task_blocklist: list[str] = []`，生产 .env 未设 → 一个词都不拦）。<br>**档 0（本批内）**：给 `lab_task_blocklist` 填词表 —— 纯配置，无迁移无开闸。<br>**档 1/2（举报表 + 管理端队列 + 屏蔽）延后**，两个硬理由：① 处置动作要落在 F2 建的 `civic_standing` 三档阶梯上，先做会造出平行的第二套处罚机制；② 要新增迁移，与 F2/F3 同批抢号必撞。<br>**守望触发条件**：`messages` 或 UGC `residents` 任一离开 0，本条从 P2 升 P1。 |

---

## 9. 线 F · CI 与测试基线

**F1/F3/F4/F5 共抢 `.github/workflows/ci.yml` 与 `backend/pyproject.toml`，必须串在同一条线。建议顺序 F1 → F5 → F4 → F3。**

| ID | 要点 |
|---|---|
| **F1** | `addopts` 的 `-m` 表达式是 lab_oci 单独落地那次写的，后续 4 个 marker 加进 `markers` 列表时没同步进 `-m`。**marker 注册与默认选择两处口径分叉。**<br>硬门：`pytest tests/integration -q --tb=no \| tail -1` 必须含 `52 deselected` 且不含 `error`（改前是 `24 skipped, 11 deselected, 17 errors`）；同时 `-m lab_postgres --collect-only` 仍为 `39/2311`，证明 opt-in 路径未破坏 |
| **F2** | 3 个文件用 `pytest.fail` 做环境守卫。作者意图（release 跑不能靠 skip 蒙混）是对的，实现选错了层 —— 把「必须有证据」硬编码进 fixture，而不是交给已有的 `LAB_*_REQUIRED` 开关。正确写法就在隔壁 `test_lab_runtime_v2_postgres.py:29-31` 的 `_require_or_skip`。<br>⚠️ 与 `feat/lab-codex-runtime` 抢 `test_lab_terminalization_postgres.py`（行区不重叠但 git 需定先后），另两个文件可先做 |
| **F3** | **两层根因**。表层：master 门里含存量失败集。深层：合并前没有任何 CI 把关 —— lab 分支从未 push、`on.push.branches` 不覆盖 `fix/**`/`claude/**`，`cancel-in-progress: true` 让批量 push 里只有 tip commit 留 run，看不出是哪条 commit 破的；红了之后又没有「红即回滚/隔离」机制，于是连红 10 天变成新常态。<br>修法含 `quarantine.txt`（每行带工单注释），**必须后于 F1**（error 不是 xfail 能罩住的） |
| **F4** | 前端是 Vite 默认 solution-style tsconfig（`"files": []` + `references`）。这种布局下只有 `tsc -b` 会递归进引用项目；`tsc --noEmit` 只看根项目 root files，而那是空集。实测 `--listFiles \| wc -l` → **0**，永远 exit 0。改成 `tsc -b --force`（实测 1099 个文件） |
| **F5** | 依赖声明链断成两截：开发侧用 uv（`uv lock --check` 今天仍 exit 0），CI 侧还是早期 `setup-python` + `pip install -e`。锁变成只在本地生效的装饰品。`pytest-timeout` 直接写在 workflow 命令行而非依赖声明里，是同一毛病的极端例 |
| **F6** | **清零口径**：0 条缺环境，约 44 条是测试与实现契约漂移，其中 38 个 LAB_* 配置项只合进了 `.env.example`、实现侧没合进 `app/config.py`。<br>分簇推进，硬门用**双向差集**（`comm -13` / `comm -23`），新增失败恒为 0。<br>⚠️ **簇 3（26 条）必须后于 `feat/lab-codex-runtime` 合并** —— 它改的正是那 26 条要断言的实现，先做等于对齐即将被改掉的契约。**簇 0 不要与 codex 线并行**（都改 `.env.example`/`config.py`）。簇 1/簇 2（14 条）与所有在飞线零重叠，可立刻开工 |

> **基线必须重抓**：`/tmp/<line>-base.txt` 原先在 `918c5fd` 上取（51 failed）。管理系统批次已把 master 推到 `c2fff2f`（据其收口报告为 `50 failed / 2247 passed / 17 errors`）。**所有在飞线与本批各线都要以 `c2fff2f` 重取基线**，否则双向差集的比较对象是错的。

---

## 10. 线 G · 部署脚本与可观测性

| ID | 要点 |
|---|---|
| **G1** | `--delete` 让远端 `backend/` 成为本机的精确镜像；`.env` 与 `tmp/` 都是「只在远端产生、本机天然没有」的路径（`.gitignore:12`/`:57`），每次跑都被清掉。<br>⚠️ **不要在 G 线重写** —— `docs/plans/2026-07-27-T-ops.md:236` 起的 Task 2 已是这条的 step 级计划且独占同样两个文件。正确做法是把「补 `--exclude 'tmp/'` + 第 4 条断言」作为 amendment 并进 T-ops Task 2 |
| **G2** | Vite 的 `import.meta.env.VITE_*` 在 build 时静态替换，`deploy.sh` 是唯一构建入口却只前缀了 `VITE_API_URL`；其他 VITE_ 变量被替换成 `undefined` 且**失败是静默的**。线上 bundle `sentry.io` 命中 0。<br>DSN 没到位前可先合脚本改动（空 DSN 只 warning，行为与今天一致） |
| **G3** | 三层叠加：(1) 没有任何 scraper；(2) API 侧 2 个 uvicorn worker 各持一份 in-process registry 且**共享同一监听 socket，无法分别寻址**——「scrape each worker」在这个拓扑下不可行；(3) tick/LLM 这些真正想看的指标产生在 agent-worker 进程，而该进程**不暴露端口**。<br>⚠️ 必须与 T1 **分开一次发布**：`UVICORN_WORKERS 2→1` 与新增 prometheus 容器都是生产行为变更，T1 是迁移 —— 按红线不得同批 |
| **G4'** | `.env.example` 的缺行已由管理系统批次补上（§2.2），但**根因未修**：`test_env_example_consistency.py` 的不变式 1 假设「`backend/.env.example` 的每个键都必须是 API 进程的 `Settings` 字段」，而这份文件实际充当**整套部署**的 env 参考（含各 sidecar 自读的键），假设从 lab 服务落地那天起就不成立。已红 5 天。<br>**G4' 应先于 F1/F2/F3 功能线合入** —— 否则它们的每次提交都在一个已红的闸门上跑，新字段漏行的额外变红会被噪声淹没 |
| **G5** | P1-2 那次收口只把「共享 httpx 客户端」一个对象关了 `trust_env`，没升级成进程级出网策略；Anthropic SDK 自建的私有 httpx 客户端从未纳管。与 `app/http.py:6-7` 的策略注释和 `tests/test_http_client.py:34` 的回归测试直接矛盾。<br>安全含义：**生产宿主一旦设了 `HTTP_PROXY`/`ALL_PROXY`，全部 LLM 出网静默走代理**。修复即一行；配套加静态扫描测试防复发 |

---

## 11. 线 H · 生产时序与流程

| ID | 要点 |
|---|---|
| **H1** | **整个批次的时序根节点**（§2.3）。本身不产生新代码 —— 产出的是「T1 必须先做，且做完泄漏就关」这条判断，以及 T2 在 vm212 上降级为「跑一次证明 0 目标」。<br>07-27 批次 `:159-174` 的四次独立变更顺序仍成立，只是 ② 在 vm212 上是空跑 |
| **H2** | 两个洞叠加：(1) 「代码默认」与「生产实际」之间没有任何机器化对照物 —— `.env.example` 是第三份真值，与前两者都可能不一致（`TOWN_TREASURY_ENABLED` 模板 false / 生产 true；`REALISM_RELATIONS_ENABLED` 模板 true / 代码默认 False，**两个方向都存在且都无人管**）；(2) 部署脚本把「rsync 代码 + alembic upgrade + 改 .env」压成一条链路，没有任何环节强制把「翻开关」切成独立变更。<br>产出：`flag_drift_report.py` + `deploy/flag-drift-allowlist.toml`。**刻意设计成零共享文件**，可与 F1/F2/F3/T 任意一条并行 |
| **H3** | 仓库里没有任何约定说「生产真值落在哪」，也没有检查阻止 ROADMAP 引用一个 master 上不存在的文件。`salvage/wip-0725C` 这个名字本身就说明了问题：证据是被「抢救」下来的，不是被归档的。<br>产出：7 份报告合进 `docs/reports/` + `test_doc_citations_resolve.py` 引用解析门 |
| **H4** | ROADMAP `:14`/`:17` 已被生产证伪（工程健康**已在产**、投票样本**已有 33 票**）。`:16` 与「近期优先级 2」更危险：**让 T2 去回填一批不存在的行**，执行者跑出 0 行时会以为是自己谓词写错了。<br>**必须在 T1 之前落**，这是它评 P1 而非 P3 的唯一理由 |
| **H5** | 07-27 批次 §2 盘点共享文件时**完全没把 `feat/lab-codex-runtime` 算进盘子**。三处硬冲突：① alembic —— 两边各自拿 `050` 做 `down_revision` 并同时选了 `051` 前缀，双方的单头断言都写在自己的 worktree 里，**两边都能自证绿，只有合并那一刻才爆**；② 测试基线的双向差集硬门隐含「master 在四条线开工期间不变」这个未写明的前提，而没人守；③ ROADMAP 同文件。<br>**必须先于 F2 Task 1 落地**，否则 F2 会照 plan `:41` 写下 `051_add_civic_standing_history` 然后返工 |

> **`docs/ROADMAP.md` 是本批冲突最密集的单个文件，三方争用**：H4 改 `:14/:16/:17/:28` 与近期优先级 2/5；H3 在「维护规则」段末尾加一条；`feat/lab-codex-runtime` 已改 `:18/:19/:31/:53/:66/:68`。行区不重叠，git 能自动合并，**但必须约定由一个人在收口时一次性落**。建议 H3+H4 合成一个 commit。
> **`:66`（迁移链头）是唯一真正的三方争用行**：现文写 050、lab 改成 052、收口第 4 条要求单头校验后再写。约定：**谁最后落谁写，且必须贴真实 `alembic heads` 输出，不许手写。**

---

## 12. 文件所有权裁决表

评审指出：20+ 项集中在少数几个文件上，没有这张表 fan-out 出去必然互相 rebase —— **这正是 0726A 四分支被废弃的原因之一。**

| 文件 | 争用方 | 裁决 |
|---|---|---|
| `app/services/civic_service.py` | A1（`_close_one`）、F2 线（`_execute_outcome:612-614` 随 Task 7）、F1 线（`:366-371` vote-trust） | **A1 先合**（赶延期窗口）；F1/F2 行区不重叠但需 rebase |
| `app/services/election_service.py:135-193` | **F2 线 Task 7 独占**（决策 5） | A 线不碰 |
| `app/services/account_deletion.py` | B1 B3 B4 B5 B6 | **B 线独占，不可 fan-out** |
| `app/routers/settings.py` | B1 B5 C2 | B 线先，C2 后；跨组串行 |
| `app/services/settings_service.py` | B1 B4 C2 | 同上 |
| `app/ws/handlers/chat.py` | B4（`extract_events` 透传）| 冲突消失 —— C2 走删除分支，不再需要 `user_config` 接线 |
| `backend/scripts/burnin_report.py` | B1（账户残留探针）、**F2 线 Task 11** | B 的探针追加在文件尾部并**延到收口接线** |
| `deploy/backend/docker-compose.yml` | D2 D3 G3 | D 线一次重建同时上；G3 单独一次发布 |
| `backend/tests/test_deploy_compose.py` | D2 D3 G3 | 同上 |
| `.github/workflows/ci.yml` | F1 F3 F4 F5 | **一条线串行**：F1 → F5 → F4 → F3 |
| `backend/pyproject.toml` | F1（addopts）、F5（dev deps + relock） | F1 先，F5 后（relock 时带上 F1 的状态） |
| `deploy/backend/.env.example` | G3、G4' | G4' 先立闸门，G3 后加键 |
| `deploy/backend/deploy.sh` | G1、**T 线 Task 2** | **让给 T 线**，G1 只做 amendment |
| `backend/alembic/versions/` | B2、**lab-codex 051/052/053**、**F2 建表**、C2 步 2 | 决策 7 定序：**B2（改 050）→ lab 051/052/053 → F2 054 → C2 055**；每项 gate 都加 `alembic heads \| wc -l == 1`，且**必须在集成分支上跑** |
| `backend/app/config.py` | C2 步 1 的 `allow_user_custom_llm`、D3 备选、D4 收口项 | **§1 决策 3 锁给收口**，线内不改 |
| `docs/ROADMAP.md` | H3、H4、lab-codex | H3+H4 合一个 commit；收口时一人落笔 |

**真正能并行的粗线**：`A ∥ B ∥ C ∥ D ∥ E ∥ F ∥ G ∥ H` 八条，**不是 40 条细项**。

---

## 13. 阻塞决策 —— 已全部拍板（2026-07-27）

| # | 决策 | 定案 | 落地动作 |
|---|---|---|---|
| **1** | `_close_one` 失格候选的世界内语义 | **次名递补当选** | 删 F2 plan Task 7 Step 5 的流会实现与 `test_close_one_announces_a_failed_vote_when_the_winner_lost_rights` |
| **2** | `install_mayor` 结票复核归谁 | **整条并进 F2 Task 7** | A2 不单独立线；A1 独自护住待决 poll（`not live` 分支根本不调 `install_mayor`） |
| **3** | 用户 custom LLM 功能的去留 | **整体删除** | C3/C4 作废；C2 拆两步（步 1 删代码路径 → 步 2 单独 DDL 删列） |
| **4** | `feat/lab-codex-runtime` 何时合 | **先合 master** | T1 迁移变 049→053；F2 建表迁移改 054；**全部线在 lab 合入后重抓基线** |
| **5** | 三张脏 poll 的执行姿势 | **heredoc 注入** | `docker compose exec -T api python - --until ... --apply < backend/scripts/postpone_open_polls.py`；停 agent-worker 保留为 fallback |

**仍需确认（非阻塞但有生产影响）**：下次部署 bootstrap 会往生产 `users` 插一行 `id='system'` 哨兵（幂等 additive）。T1 本身要跑迁移 049→053 —— 合起来一次部署带**迁移 + 数据插入**，蹭到「迁移不与开闸/数据变更同批」红线边上。要么拆两次，要么显式接受并记录。部署前后各查一次 `SELECT id,email FROM users WHERE id='system';`（前应为空，后应恰好一行）。

---

## 14. 顺序约束（红线）

```
── 前置（谁都不依赖，可立刻做）────────────────────────────
  H4  改 ROADMAP :14/:16/:17（必须先于 T1，否则 T 执行者会找不存在的存量泄漏）
  H5  定案迁移号 + 改 F2 plan：删 Task 7 Step 5 流会实现、迁移号 051→054
      （必须先于 F2 Task 1 与 Task 7）
  G4' 修 env 一致性不变式（必须先于 F1/F2/F3 功能线合入）
  C1b 供应商侧吊销两把历史密钥（外部动作，不占文件锁）
  E2  敏感词过滤补全（5 分钟，必须先于 F2 ④开闸）

── A 线（有墙钟，2026-07-28 23:00 UTC）──────────────────
  A3 延期(heredoc) ──> A1 次名递补 ──> 部署 vm212 ──> 延期到期前必须完成
     └ fallback: 2026-07-28 22:5x 停 agent-worker（零变更，买一天）
     └ A2 已并进 F2，不赶这个窗口

── 迁移链（严格串行，每步 gate 在集成分支上跑 heads == 1）───
  B2(改 050) ──> lab-codex 051/052/053 ──> F2 建表 054 ──> C2 步2 删列 055

── 生产变更（每项独占一次，不得合批）───────────────────────
  T1 部署 master(049→053，含 lab 三个 additive 迁移) + system 哨兵行
     └ 同时把 C1 掩码、哨兵铸币 guard、lab 时间炸弹修复带上生产（§2.2）
     └ D1 需额外一步：compose 是宿主文件，git archive 送不上去
  T2 回填（vm212 上是空跑，0 行是正确结果）
  C2 步2 删列（纯 DDL，独立一次）
  D2/D3 容器重建（避开 T 线 Task 3/9/10/11 窗口）
  G3 UVICORN_WORKERS 2→1 + prometheus 容器

── 可全程并行 ────────────────────────────────────────
  B 线 / F 线 / G1 G2 G5 / H2 H3 / E1
```

**四条不可合并的红线**（继承 07-27 批次 §7，本批新增两条）：
1. 迁移 / 数据变更 与 开闸 / 行为变更**不得同一次变更**
2. 数据变更独立于代码部署
3. **（新）容器重建独占一次变更**，不与数据回填、不与压测取数同批
4. **（新）任何抢 alembic 链头的分支，单头断言必须在集成分支上跑**，隔离 worktree 里的自证无效

---

## 15. 收口

线全部完成后按序统一处理（继承 07-27 批次 §8，本批追加第 6、7 条）：

1. `config.py` / `.env.example`：三条 F 线的新开关 + D3 若走 Settings 形态的字段 + **删掉** `allow_user_custom_llm`（C2 步 1 延到这里，§1 决策 3 锁了 config.py）
2. `nightly_cron.py`：接 F2 `civic_promotion` 与 F3 `office_audit`
3. 声誉影响接线（F3 卸任审计 × F1 声誉数据）
4. `alembic heads` 单头校验（**在集成分支上跑**，见红线 4）
5. 更新 `docs/ROADMAP.md`（H3+H4 一次落笔，`:66` 贴真实 `alembic heads` 输出）
6. **（新）所有新探针接进 `backend/scripts/burnin_report.py::_run`** —— B1 账户残留、E1 的 CSP header、D 组的端口面、A1/A2 的失格候选计数、H2 的开关漂移。收口当晚跑一次全量并把读数存进 `docs/reports/`
7. **（新）取证命令迁移清单**：D1 收窄 8100 后，T 线健康检查 / T4 压测入口 / 各线验收命令统一改宿主回环或隧道域名

---

## 16. 各线共同约定

**工作区**：每条线在 `/Volumes/data/dev/simverse-world/.worktrees/<name>` 独立 worktree。**base 为 `feat/lab-codex-runtime` 合入后的 master**（决策 7）—— 不是 `fc60ac2`、不是 `918c5fd`、也不是 `c2fff2f`。worktree 必须在 Mac 本机创建。不要在 worktree 内创建 `backend/.env`（会破坏 conftest 的测试隔离）。

> **例外**：A3（延期脚本）与 A1 有墙钟压力，**允许以 `c2fff2f` 为 base 先行**，因为它们碰的文件（`scripts/postpone_open_polls.py`、`civic_service.py` 的 `_close_one`）与 lab 线零交集。合并时正常 rebase。

开工前路径守卫，逐字照抄：

```bash
cd <worktree>/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -c "import app; p=app.__file__; assert '.worktrees/' in p, f'WRONG: {p}'; print('OK',p)"
```

**基线捕获**：第 0 步先跑全量并存档，收工同命令做差集。**必须在 lab 合入后的 master 上重抓** —— 基线已经从 918c5fd 的 `51 failed` 移到 c2fff2f 的 `50 failed / 2247 passed / 17 errors`，lab 合入会再动一次。用旧基线做双向差集，比较对象是错的。

```bash
python -m pytest tests/ -q -p no:randomly > /tmp/<line>-base.txt 2>&1; tail -3 /tmp/<line>-base.txt
```

**硬门 = 相对基线零新增失败**。判定用失败集的**双向差集**（`comm -13` / `comm -23`），不是数量比较——数量相同不等于集合相同。

**TDD**：每条线严格红→绿，一 step 一 commit，commit 末尾带真实 `Verified-by:` 输出。禁 `--no-verify` / `amend` / `squash` / 编造测试数据。

**完成的定义**：build/lint/单测绿不等于完成。功能线须在真实进程上走一遍用户路径并贴运行时证据；涉生产的每步都要贴 vm212 上的真实输出。

**本批特有**：A1/A2 在批次内**拿不到自然发生的生产运行时证据**（唯一能触发该路径的 poll 已被 A3 推后）。取证步骤须显式安排：照 T 计划 Task 10 的 heredoc 姿势在 vm212 开一张短窗镇长选举 poll，候选里塞一个当场删掉的 slug，手工跑一次 `close_due_polls`，贴出公告正文 + `SELECT slug, meta_json->'mayor' FROM residents` 读数。
