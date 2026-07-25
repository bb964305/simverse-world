# S1-3 议题立场与舆论动力学 — 交付报告

分支 `feat/s1-3-opinion`(worktree `/Volumes/data/dev/sv-s1-opinion`,base = master `d27fce7`)。
规格:`docs/kickoffs/KICKOFF_S1-3_opinion.md`。纯本地开发,未合并、未 push、未部署。

## 1. 任务状态(全部 done,无 blocked)

| 任务 | 状态 | commit |
|---|---|---|
| 1. `issue_stances` 表 + 迁移 | done | `4015bf3` s1-3-1 |
| 2. `OpinionService` + config 块 | done | `02dc29f` s1-3-2(补充 `1da3df6` s1-3-2b,见偏差 D6) |
| 3. 三条信号接线 + nightly drift | done | `47914aa` s1-3-3 |
| 4. 日报 `opinion_line` | done | `f55d4de` s1-3-4 |
| 5. 测试 + §6 探针出数 | done | `c15f755` s1-3-5 |

与 S2-1 并行纪律(规格 §8):`civic_service` / `election_service` / `duty_service` /
`coin_service` **四个文件零改动**(见 `git diff master..HEAD --name-only`),无需收口协调,无 blocked 项。
`relation_service` 仅只读调用(`relations_for`),不在禁改集内。admin 路由注册未碰(可选端点按规格本批不做);
`docs/PROGRESS.md` 未碰。

## 2. 全量 pytest:基线 vs 收尾

| 轮次 | commit | 结果 |
|---|---|---|
| 基线(起点) | `d27fce7` | **51 failed, 1642 passed, 25 skipped, 11 deselected, 17 errors**(exit 1) |
| 收尾(HEAD) | `1da3df6` | 见正文末尾"收尾对比结论"(本报告提交前回填) |

- 基线失败/错误全部为 lab-v2 / postgres / testcontainers 预存失败(`test_lab_*`、
  `tests/integration/*_postgres.py`、`test_env_example_consistency::test_every_example_key_is_a_settings_field`
  的陈旧 `lab_*` 键),与"生产线预存 55 失败"记忆口径一致。硬门 = 相对基线**零新增失败**。
- 基线存档 `/tmp/s13_baseline_pytest.keep.log`,收尾 `/tmp/s13_final2_pytest.log`。
- 基线运行说明:基线启动后本线开始写任务 1 文件,但所有 app 模块在 pytest 会话启动时已按
  `d27fce7` 内容 import(Python 模块缓存),`test_opinion_service.py` 未被收集(log 中 0 命中),
  基线口径有效。
- `/Volumes/data` 挂载陷阱按纪律处理:两轮全量均把 `PYTEST_EXIT` 显式写入 /tmp 日志尾部核实。

## 3. 迁移占位登记(收口必读)

- 文件:`backend/alembic/versions/046_add_issue_stances.py`
- `revision = "046_add_issue_stances"`,`down_revision = "045_residents_creator_nullable"`
  (实测链头,**未**硬编码 041;S2-5 撞号问题不存在于本线)。
- **046 号是本 worktree 内的临时占位**:S2-1 等并行线各自也接 045,收口时主会话统一线性化
  重排(只需改文件名/revision 串,表结构无耦合),merge 后跑 `alembic heads` 单头硬门。
- 本线核验:`uv run --no-sync alembic heads` → `046_add_issue_stances (head)`(单头,exit 0);
  测试 `test_integration_migration_single_head` 断言单头 + down_revision 锚点。

## 4. 收口 config 清单

`backend/app/config.py` Settings 类尾追加单一注释块(S1-3 标识,未动他人行),
`backend/.env.example` 同步追加同名注释块。键全部 `POLIS_OPINION_` 前缀:

| 键 | 默认 | 语义 |
|---|---|---|
| `polis_opinion_enabled` | `False` | 主开关;关 = 字节级回落(零写入、无 opinion_line) |
| `polis_opinion_epsilon` | `0.4` | 有界信任阈值 ε |
| `polis_opinion_chat_rate` | `0.08` | from_chat Deffuant 步长 |
| `polis_opinion_drift_rate` | `0.05` | nightly 漂移步长 |
| `polis_opinion_seed_mag` | `0.3` | 辩论开场对立幅度(缺 SBTI 时的精确 ± 值) |
| `polis_opinion_active_window_days` | `14` | 活跃议题窗口(**世界日**,经 world_clock 换算) |
| `polis_opinion_min_participants` | `3` | 活跃议题最少表态人数 |
| `polis_opinion_neg_repel` | `False` | negative mood 是否轻微远离 |
| `polis_opinion_digest_issues` | `2` | opinion_line 最多点名议题数(规格外新增,见 D4) |
| `polis_opinion_variance_split` | `0.15` | opinion_line 措辞阈值(规格外新增,见 D4) |

nightly_cron 收口注意:S1-3 drift 块**紧贴 digest 块上方、单独成段**,注释
`MUST run before digest`(nightly_cron.py `run_nightly_jobs` 开头)。S2-1 线往同文件追加
自己的块时不得插到 digest 之前;merge 时以该注释为界人工核对。

## 5. 偏差清单(相对规格,行号漂移以代码为准)

- **D1 · anchors 行号漂移**:规格称 nightly digest 在 `nightly_cron.py:32`,实际在
  `nightly_cron.py:66-70`(realism/lab 块使文件增长)。drift 块已按语义要求插在 digest 块
  之前(现 `nightly_cron.py:65-79`)。其余 anchors(debate_service 51-56/202-246/262-278、
  memory/service 521-582、digest_service 39-114/117-134/137-151、civic `_npc_choice` 180-227)逐条核实无漂移。
- **D2 · 迁移链头**:规格正文写"现链头 040";按主会话实测环境事实改接 **045**(见 §3)。
- **D3 · `_bump_stance` 签名扩展**:在规格签名(`target, rate, source`)上增加 keyword-only
  `insert_stance`(新行初值,辩论 seed 用)与 `epsilon`(结构性更新绕过有界门,如 settle
  赢家增强/输家回归、neg_repel)。原子性形态不变:单条 `INSERT .. ON CONFLICT DO UPDATE`,
  新值全部 SQL 内计算(有界 CASE + 可移植 clamp CASE,同 relation_service 范式),按方言分支
  `sqlite`/`postgresql` 的 `on_conflict_do_update`。
- **D4 · 两个规格外 config 键**:`polis_opinion_digest_issues`、`polis_opinion_variance_split`
  ——opinion_line 的呈现参数,按"数值参数不硬编码"纪律进 config(同前缀同块)。
- **D5 · update_from_chat 的议题口径**:规格方法注释写"共同**活跃**议题",测试口径写
  "双方都已表态的同一 issue_key"。取测试口径(共同表态即可,不叠加活跃窗过滤);positive 且
  |Δ|>ε 时完全不写(连 interact_count 都不动,避免把死议题刷成活跃)。
- **D6 · `.env.example` 追加**(不在规格 §8.2 will-modify 集):反向一致门
  `test_every_settings_field_is_documented_or_allowlisted` 要求每个新 Settings 字段有 example 行,
  不加 = 相对基线新增失败。纯追加块(commit `1da3df6`)。
- **D7 · settle 增强速率**:规格未定义,复用 `polis_opinion_chat_rate`(不新增键);
  赢家极向 = 其现有 stance 符号,无 stance(seed 钩子当时关闭)时按辩位 a→+/b→−。幂等性由
  settle 自身的 already-settled 早退保证(aftermath 只在首次 settle 跑)。
- **D8 · seed 符号规则落地**:a 侧极向由其 SBTI A1 定(H→+,L→−,缺→+),b 侧**结构性取反**
  (即使两辩手 A1 同值也保证对立);幅度由各自 A2 调制(H→×0.75,L→×1.25,缺→×1.0 即精确
  ±seed_mag)。缺 SBTI 回落被 `test_update_from_debate_seed_missing_sbti_fallback` +
  集成测试实测覆盖(生产 0/26 有 A2,回落即主路径)。
- **D9 · nightly 顺序断言的实现方式**:`test_integration_nightly_drift_before_digest` 用
  源码顺序守卫(`inspect.getsource`,仓内 `test_m5_space.py:58` 先例)+ 功能半边
  (drift 后方差下降);不真跑 `run_nightly_jobs`(其经全局 engine 连带十余个无关 job,
  在单测环境不可靠)。opinion_line 消费漂移后数值另由任务 4 测试覆盖。
- **D10 · confidence 列**:建表含 `confidence`(默认 0.5)但本批不参与步长调制(规格标注
  "可选,默认 0.5"),留给后续。
- **D11 · 探针"时间序列"口径**:`issue_stances` 无历史表,报告单次运行输出当前方差+双峰指标;
  时间序列 = 连续夜(drift 后)运行报告采样(render 文案已注明)。seeded fixture 的 5 夜序列
  见 §6,由 `test_probe_seeded_variance_series_not_white_noise` 固定为回归断言。
- **D12 · 规格用例名对照**:19 单测 + 4 门控 + 5 集成用例名全部实现;其中
  `test_integration_migration_single_head` 在任务 1、digest 两条在任务 4。另增规格外
  `test_issue_stances_table_created`、`test_integration_settle_hook_reinforces_via_aftermath`、
  `test_enabled_digest_opinion_line_present`、探针 3 条。

## 6. 探针出数(§6,seeded fixture;ε=0.4,drift_rate=0.05)

复现:`backend/tests/test_opinion_service.py::test_probe_seeded_variance_series_not_white_noise`
(断言固定);演示脚本单跑 6 夜输出:

- 收敛议题(5 人,初始 stance 0.0/0.1/0.2/0.3/0.4,全部互在 ε 内):
  方差序列 `0.02 → 0.01758 → 0.01545 → 0.01358 → 0.01193 → 0.01049`
  ——严格单调下降(收敛形态,非白噪声)。
- 极化议题(6 人,两簇 −0.85/−0.75/−0.7 与 0.7/0.75/0.85,簇间距 > ε):
  方差序列 `0.59167 → 0.59111 → 0.59062 → 0.59021 → 0.58986 → 0.58956`,
  ε-簇数序列恒 `[2,2,2,2,2,2]`——极化保留、簇内缓收敛、永不合并。
- 探针快照(`render_probes_s13`):
  `「极化议题」 n=6 mean=0.0 var=0.5893 双峰系数=0.99(>0.556≈双峰) ε-簇数=2`;
  `「收敛议题」 n=5 mean=0.2 var=0.0092 双峰系数=0.588 ε-簇数=1`。
  注:均匀分布的 Sarle 系数天然 ≈5/9,故双峰判定以 **ε-簇数 ≥2 为主、系数为辅**(两口径都输出)。
- 对照组(开关关):`issue_stances` 零写入,报告输出固定"对照组「无动力学」"行
  (`test_probe_render_empty_is_control_group`、三条 `test_disabled_*_noop` 覆盖)。
- 规格要求"首轮数值记入 PROGRESS.md"与红线"不碰 docs/PROGRESS.md"冲突 → 数值记于本报告,
  收口时由主会话决定是否誊入 PROGRESS。

## 7. 硬纪律自查

- **零新增 LLM**:drift/from_chat/from_debate 纯规则;`test_integration_chat_wrapup_moves_stance`
  断言 wrapup LLM await_count==1;`test_integration_digest_opinion_line_zero_new_llm` 断言
  compose_digest create await_count==1。
- **prompt 隔离**:stance/方差只进村日报素材(`opinion_line`,与 circle_line 同级),不进任何
  NPC decide/chat prompt;grep 核对 `opinion` 仅出现在 digest/nightly/debate/memory 接线点。
- **tick +1 红线**:未触碰 perceive/decide 循环,写入只在 chat wrapup(有界事件)与 nightly(离线)。
- **时间语义**:活跃窗口经 `world_clock.now_world`/`world_to_real` 换算为真实 UTC 截点,
  无 utcnow 直接比对世界节律。
- **Lab 不变量**:`app/lab/`、`app/forge/`、审批门、WorldGuard、模型计价零接触。

## 8. 收尾对比结论(全量 pytest @ HEAD `1da3df6`)

- 收尾:**51 failed, 1676 passed, 25 skipped, 11 deselected, 17 errors**(PYTEST_EXIT=1,
  exit code 已写盘核实,`/tmp/s13_final2_pytest.log`)。
- 失败/错误集合与基线 `diff` **逐项一致**(51+17,全部 lab-v2/postgres 预存):唯一文本差异是
  `test_lab_runtime_v2_store_auth` 一个参数化用例 ID 内嵌的 JWT nbf/exp 时间戳(同一用例、
  同一 `token_not_yet_valid` 参数,两轮都失败)。**新增失败 = 0**。
- passed 1642 → 1676(**+34 = 本模块新增测试数,精确对账**:`test_opinion_service.py` 34 个
  test 函数)。硬门通过。
