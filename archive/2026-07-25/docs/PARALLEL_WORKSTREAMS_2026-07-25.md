# 功能开发并行开工清单(2026-07-25 · 服务器故障期版)

> 前提:测试服务器(vm212)故障,**一切部署、生产库操作、生产验证全部冻结**。本批为纯本地功能开发,
> base 一律 = 当前 master(已含 M1–M6 + 世界时钟 + 拟真开闸 + lab 收尾 + 五线收口)。
> 做完只留分支,**不合并、不 push、不部署**,统一等主会话收口。

## 共同纪律(每条提示词已内嵌,此处为总纲)

1. 独立 `git worktree` + 独立分支,base=master;一任务一提交带任务号,TDD。
2. **config.py / nightly_cron.py 本批多线都要碰,不设红区,改用「前缀+块」纪律**:
   config 只在 Settings 类尾追加自己前缀的 flag 块(S2-1=`POLIS_OFFICE_`、S1-3=`POLIS_OPINION_`),
   不改他人行;nightly 只新增自己的独立 try/except 块,不改不挪既有块。
3. **alembic 现链头已不是规格里写的 040**——port 重编号后为 `045_residents_creator_nullable`
   (以 `alembic heads` 实测为准)。新迁移文件名用 NNN 占位、down_revision 接实测链头,
   报告里登记,收口时统一线性化重排 + `alembic heads` 单头校验(硬门)。
4. 时间语义:任何"天/周/任期"一律经 `app/world_clock.py`(唯一换算入口),禁止直接 utcnow 比对世界节律。
5. 测试口径:改动范围内 pytest 全绿;全量 pytest **相对 master 基线零新增失败**
   (本机含 lab-v2 需真 redis/testcontainers 的预存失败集,硬门=零回归,S0 已确立此口径)。
   碰前端则 tsc/eslint/vitest 绿(vite8 已修 rolldown,Node 25 可构建)。
6. 门控纪律:新机制独立 bool 开关默认 False;规则做骨架、LLM 做血肉,零新增 LLM 边际成本。
7. 各线进展写 `docs/reports/<分支名>-report.md`,不碰 `docs/PROGRESS.md`(收口统一记)。

## 开工总览(三线并行,文件集经规格 §8 冲突声明核验互不相撞)

| # | 工作线 | 分支(base=master) | 依据 | 规模 |
|---|---|---|---|---|
| 1 | **S2-1 职位实体化(offices)**——阶段 2 地基,S2-2/S2-4/S3-1/S5-8 全押在它上面 | `feat/s2-1-offices` | `docs/kickoffs/KICKOFF_S2-1_offices.md` | 大 |
| 2 | **S1-3 议题立场与舆论动力学**——与 S2-1 文件交集仅 config/nightly 追加位 | `feat/s1-3-opinion` | `docs/kickoffs/KICKOFF_S1-3_opinion.md` | 大 |
| 3 | **生产修缮三件套**:sbti backfill 工具 + heat_cron tz 修复 + 熔断静默失效告警 | `fix/prod-hygiene-batch` | Roadmap 问题 #11/#12/#6 | 中 |

## 暂缓清单(想开也别开,会撞车)

| 工作项 | 撞谁 |
|---|---|
| S1-5 财政、S2-5 政策、S1-1 声誉 | 规格 §8 明确串行:S2-1 先落 `_pay_wage` 加成语义 / `_execute_outcome` mayor 分支 / offices-backed `current_mayor`,它们再叠。S2-1 收口后:S1-5 与 S2-5 可并行(文件集不相交),S1-1 最后(同时碰 civic/election/coin) |
| 夜间任务补跑机制(#10) | 正面改 `run_nightly_jobs` 调度骨架,与本批全部 nightly 追加块相撞,等本批收口后单独做 |
| 一切部署 / 生产库操作 / burn-in 结账 | 服务器故障,见下方恢复清单 |

## 服务器恢复后第一批动作(按序,另开会话)

1. **后端小版本部署** master→vm212:带上 vision 修复、townhall 路由、修缮线的 heat 修复与告警;冒烟 /townhall 出数、带图对话。
2. **burn-in 结账**:首 24h 真实账单 vs $10 预算、六探针真实出数、测试账号清理。
3. **sbti backfill 生产执行**:修缮线工具 `--dry-run` 审阅 → 真跑 → 复验 NPC 投票分布不再垄断 option 0。

---

## 提示词 1 · S2-1 职位实体化(offices)

```
任务:实现社会扩展 S2-1 职位实体化。严格按 docs/kickoffs/KICKOFF_S2-1_offices.md 执行——
规格已含任务切分/表结构/接口签名/测试用例名/探针定义/不碰区域;本提示词只补充环境事实与纪律,
两者冲突时以规格为准、偏差记报告。

准备:
- git worktree add ../sv-s2-offices master && 新建分支 feat/s2-1-offices
- 先读:规格全文、docs/SOCIETY_EXPANSION_PLAN.md §2/§3 相关行、
  services/{election,civic,duty}_service.py、tasks/nightly_cron.py 现状。
  规格的 file:line anchors 先逐条校验(master 已前进,可能行号漂移),漂移以代码为准记偏差。

环境事实(规格写作后发生的变化,以此为准):
- base=master(已含 M1–M6+世界时钟+拟真开闸);测试服务器故障,本线纯本地,禁止任何生产操作。
- alembic 实际链头=045_residents_creator_nullable(规格写 040 已被 port 重编号),
  新迁移 NNN 占位接实测链头,报告登记,收口时重排。
- 任期/换届的"天/周"语义一律经 app/world_clock.py 世界时,禁止直接 utcnow。

要求:
1. 按规格逐任务 TDD,一任务一提交带任务号;POLIS_OFFICE_* 独立开关默认 False。
2. config.py 只在 Settings 类尾追加 POLIS_OFFICE_ 前缀块;nightly_cron 只新增独立
   try/except 块(term_check),追加在既有治理块之后,不改不挪他人块。
3. 三道回归门锁死(规格 gotchas):duty_service._pay_wage 镇长工资加成语义不变;
   civic._execute_outcome mayor 分支经 install_mayor 双写落表;
   election.current_mayor 改 offices-backed 后既有消费者行为逐一不变。
4. 只留接口面不实装下游:perms_json / fill_strategy 字段就位即可,
   不做 S2-2 裁量点、S3-1 抽签(规格边界)。
5. 测试:规格列出的单测+集成用例全实现;全量 pytest 相对 master 基线零新增失败;
   规格探针节指标用 seeded fixture 演示出数。
红线:不合并不 push 不部署;不碰 app/lab 与 agent 主链路;不改他人前缀 config 块。
产出:docs/reports/feat-s2-1-offices-report.md——任务状态表、全部偏差、迁移占位登记、
收口时需进 .env.example 的配置清单、探针数字、给 S1-5/S2-5/S1-1 的接口冻结声明
(tax/disburse 无关,本线冻结的是 install_mayor/current_mayor/_pay_wage 语义)。
```

## 提示词 2 · S1-3 议题立场与舆论动力学

```
任务:实现社会扩展 S1-3 议题立场与舆论动力学。严格按 docs/kickoffs/KICKOFF_S1-3_opinion.md
执行;本提示词只补环境事实与并行纪律,冲突以规格为准、偏差记报告。

准备:
- git worktree add ../sv-s1-opinion master && 新建分支 feat/s1-3-opinion
- 先读:规格全文、services/{debate,digest}_service.py、memory/service.py、
  tasks/nightly_cron.py、agent/chat.py wrapup 路径现状;anchors 逐条校验,漂移记偏差。

环境事实:
- base=master;服务器故障,纯本地开发。
- alembic 实际链头=045_residents_creator_nullable;迁移 NNN_add_issue_stances 接实测链头,
  绝不硬编码 041(S2-5 规格也占 041,撞号)。
- SBTI 软依赖要当真:生产 26 位居民 0/26 有 A2 维(见 PROGRESS S0 节遗留 (a)),
  缺 SBTI 回落路径不是边角而是生产主路径,规格的 fallback 测试必须实测覆盖;
  修缮线正并行做 backfill 工具,本线不依赖它。

要求:
1. polis_opinion_enabled 独立开关默认 False;零新增 LLM 调用(只消费互聊 wrapup、
   辩论既有输出);数值参数 POLIS_OPINION_ 前缀进 config 类尾自己的块。
2. nightly 的 drift 块必须插在 digest 之前,单独成段并注释 "MUST run before digest",
   不动他人块。
3. 与 S2-1 并行纪律(规格 §8 声明):civic/election/duty/coin 四个 service 只读不写;
   若实现中发现必须写,停下记报告等收口协调,不得擅自动。
4. 测试:规格用例名全实现,重点含缺 SBTI 回落、relations 开关关时均匀权重两条;
   全量 pytest 相对 master 基线零新增失败;规格探针出数(seeded fixture)。
红线:不合并不 push 不部署;不碰 admin 路由注册(可选端点本批不做,按规格)。
产出:docs/reports/feat-s1-3-opinion-report.md(任务状态、偏差、迁移占位登记、
收口 config 清单、探针数字)。
```

## 提示词 3 · 生产修缮三件套

```
任务:三件独立小修,全部纯本地开发+测试;生产执行步骤写进报告,等服务器恢复后另行执行。

A. 生产居民 SBTI backfill 工具(Roadmap #11):
   新建 backend/scripts/sbti_backfill.py——扫描 residents.meta_json.sbti,对缺维/部分维居民
   补全 15 维 dimensions:优先纯规则(从已有 type 按 match_type 反推,参照 agent-S 在
   seed/preset_characters.py 的做法);LLM 从 persona 重算做成可选 --llm 模式,接 llm_usage
   计量与预算熔断。默认 --dry-run 只出差异报告不写库;幂等可重跑;--slug 可指定单人。
   单测:seeded sqlite 覆盖 缺 sbti / 部分维(有 type 无 A2)/ 已齐全 三态 + dry-run 不落库断言。
B. heat_cron tz 混比修复(Roadmap #12,既有 bug,8a0449c 即有):
   heat_service.py 约 :64 offset-naive vs aware 比较崩——按仓库既有惯例
   (civic_service.close_due_polls 的 replace(tzinfo=UTC) 分支)统一 aware 比较,
   兼容存量脏数据;回归测试:aware+naive 混合数据不再抛、重算结果正确。
C. 预算熔断静默失效告警(Roadmap #6):
   计量读数异常时(spend 查询抛错、AGENT_ENABLED=true 且 loop 在跑但 llm_usage 连续
   N 分钟零新增)发 Sentry event + WARN 日志。阈值走环境变量 os.environ 读取,
   不改 config.py(收口时统一登记进 .env.example);默认保守、可一键关。

准备:
- git worktree add ../sv-hygiene master && 新建分支 fix/prod-hygiene-batch
- 先读:heat_service.py、agent/budget.py、agent/loop.py、seed/preset_characters.py 的
  sbti 结构、docs/PROGRESS.md 的 S0 节(26 居民部分维事实与「遗留跟进 (a)(b)」)。

红线:不碰 config.py / nightly_cron.py / civic / election / duty(与另两线零交集);
不连任何远程库,一切只对本地/测试库;不合并不 push 不部署。
测试:三件各带单测;全量 pytest 相对 master 基线零新增失败。
产出:docs/reports/fix-prod-hygiene-batch-report.md,末尾附「服务器恢复后的生产执行
步骤」:backfill dry-run→人工审阅→真跑→复验投票分布;B/C 随下一次后端部署上线。
```

---

## 收口顺序(本批完成后,主会话统一执行)

1. **修缮线**(零迁移、接触面最小)→ 2. **S2-1**(迁移定 046)→ 3. **S1-3**(迁移重排为 047)
   → 每合一条跑全量 pytest,最后 `alembic heads` 单头校验 + 统一补 config/.env.example + PROGRESS 一条。
2. 收口完成即解锁第二波:**S1-5 与 S2-5 并行**(文件集不相交),之后 **S1-1** 收尾第一批。
3. 服务器恢复随时插队执行「恢复清单」,与本批开发互不阻塞(部署基线永远=master 收口后状态)。
