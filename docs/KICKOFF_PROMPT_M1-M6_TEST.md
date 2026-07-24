# Kickoff — 小镇扩展 M1–M6 功能验收测试(本地)

你在 simverse-world 仓库。**角色:独立验收测试员。目标:对工作树中未提交的「小镇扩展 M1–M6」批次(含 2026-07-23 审计补齐)做一轮不留情面的功能验收**——不要只看单测绿不绿,重点验证机制在真实运行链路里会不会发生、发生得对不对。此前这批功能曾自称"全部实现完成"但被审计出三处"实现了却没接电"的缺口(详见 `docs/PROGRESS.md` 末节「小镇扩展 M1–M6 — 完成度审计与补齐」),你的任务就是防止这种事再次漏网。

## 被测范围(六里程碑 + 补齐件)

- **M1 经济**:职务工资(`duty_service.on_work` → treasury + `meta_json.wallet` 写穿缓存)、吃饭扣费与赊账(`execute/basic._charge_meal`,赊账生成记忆+与店主关系)、手头紧提示(decision prompt)、居民自产商品(`resident_work` 商店条目,限量、售出给创作者入账+记忆)、集市日(周六全天事件、全店 9 折、WORK 冷却减半)。开关 `NPC_ECONOMY_ENABLED`。
- **M2 故事弧**:`arc_service` 规则里程碑引擎(relation / co_location / count 三种触发器,严格顺序推进),nightly 驱动,finale 三件套(关系跃升+署名公告+人格跃迁记忆),日报消费 arc 素材。开关 `ARC_ENGINE_ENABLED`。
- **M3 镇务自治**:`civic_service` propose(文书公告、提案人署名)→ NPC 规则投票(守序倾向 + **提案人关系加权**:自投 +2.0、好友 +1.5×affinity)+ 玩家投票(`POST /polls/propose`、既有 vote 端点)→ 到期关闭 → 胜出 effect 落地(system_config / dynamic_location / narrative / mayor)、公开课结束触发居民辩论。开关 `CIVIC_POLLS_ENABLED`。
- **M4 记忆召回评估**:`scripts/memory_recall_eval.py` 离线 harness(keyword vs vector,20 条固定用例,recall@k)。
- **M5 空间扩展**:骆小舟(邮差,第 11 位预设居民,WORK=投递到期时间胶囊)、常设建设议案(邮局/剧院,`seed_civic_agenda`,**nightly 接线**)、议案通过 → dynamic_locations 落表 + world reload。
- **M6 镇长选举**:`election_service` 开选(野心家候选)→ 投票 → 就任(`meta_json.mayor` + system_config `current_mayor`)→ 全镇工资 ×1.2 → 换届摘旗;**nightly 触发器 `maybe_open_seasonal_election`**(有赛季一季一场,无赛季每 `ELECTION_INTERVAL_DAYS`=28 天一场,选举开着不重开)。开关 `ELECTION_ENABLED`。

## 环境与纪律(硬约束)

1. **只测本地,禁碰生产**:不访问 `simverse.world` / `simverse-api.proxypool.eu.org`,不部署。
2. **本轮是测试任务,不改业务源码**。发现缺陷记报告,不当场修(P0 例外:先报告、等拍板)。新增文件仅限 `docs/testing/` 与 `tmp/`。
3. **零 LLM 成本**:M1–M6 全部是规则机制,验证不需要真实 LLM key。用 dummy key 跑(`tests/conftest.py` 会注入),LLM 分支(arc finale 人格跃迁、日报生成)按 fail-open 语义验证"缺 LLM 不崩、主流程照常完成"即可,不要为它们配真 key。
4. **可复现**:随机路径(`npc_work_item_prob`、集市日 news 概率等)一律 monkeypatch/seed 固定,不许"跑几次看大概"。所有等待设有限超时。
5. **假成功要抓**:断言落到终态数据(表行、meta_json、system_config、公告内容),不要停在"函数没抛异常"。fail-open 是这批代码的统一风格——它会把真缺陷吞成 warning 日志,测试时开 DEBUG 日志并检查 `exc_info` 告警。

## 阶段 0 — 基线(先跑,不过不往下走)

```bash
cd backend && uv sync --frozen --all-extras
uv run pytest -q
```

预期基线:**1310 passed / 1 skipped / 12 deselected / 0 failed**(2026-07-23 云端锁定版本实测)。已知环境现象,不算本批缺陷,但要在报告里复核并注明:
- `tests/test_agent_worker.py::test_worker_crashing_task_is_fatal` 在部分沙盒环境超时(HEAD 上同样失败,与 M 批次无关;本机 Mac 应通过——如果本机也挂,升级为独立问题记录);
- 11 个 `lab_oci` 用例按惯例 deselect;
- `test_deploy_compose` / `test_lab_adapter_gate` / `test_world_geometry` / `test_env_example_consistency` 依赖仓库完整结构(deploy/、docs/、frontend/、.env.example),必须在完整 checkout 里跑。

然后定向跑 M 系(应 **40 全绿**):

```bash
uv run pytest tests/test_m1_economy.py tests/test_m2_arcs.py tests/test_m3_civic.py \
  tests/test_m4_recall_eval.py tests/test_m5_space.py tests/test_m6_election.py -q
```

## 阶段 1 — 接线审计(这批的历史病灶,逐条复核)

用 grep/inspect 确认每个入口在生产链路里真的有调用方,输出"入口 → 调用点"清单进报告:

| 入口 | 应有调用点 |
|---|---|
| `duty_service.on_work` | `agent/phases/execute/basic.py` WORK 分支(带 market_day) |
| `duty_service.prompt_hint` + wallet 压力提示 | `agent/prompts.py` |
| `_charge_meal` | execute EAT 分支(经济开关门控) |
| `arc_service.evaluate_arcs` | `tasks/nightly_cron.py` |
| `civic_service.close_due_polls` / `run_npc_voting` | nightly_cron |
| `civic_service.seed_civic_agenda` | **nightly_cron(补齐件)** |
| `election_service.maybe_open_seasonal_election` | **nightly_cron(补齐件)** |
| `civic_service.maybe_spawn_lecture_debate` | `tasks/event_cron.py` phase=end |
| `shop_effects` 的 `resident_work` handler | `@register` 注册表 |
| 集市日折扣 `_market_discount` | `shop_service.purchase` |
| duty perks(gossip/encounter/quest_magnet/chat_uplift) | gossip / encounter / daily_quest / agent chat 各消费点 |
| 提案人加权 `_proposer_slug` | `propose()` 写入 + `_npc_choice` 消费 |

顺带核对 nightly 顺序:**close_due_polls → seed_civic_agenda → maybe_open_seasonal_election → run_npc_voting**(新开议案当晚要有 NPC 票,顺序错了就是缺陷)。

## 阶段 2 — 端到端动态验证(核心工作量)

写一个一次性 harness(放 `tmp/`,用 `async_session` + 独立 sqlite 文件库,种子数据走 `seed.seed_residents`),**直接调 `run_nightly_jobs()` 当"快进一晚"**,连续快进多晚,验证机制链条真的发生:

**T1 世界冷启动**:seed 后 11 位居民就位、各带 duty;`PRESET_ARCS` 5 条 arc goal、`PRESET_RELATIONS` 关系与镜像记忆落表。
**T2 第一晚**:两个建设议案(邮局/剧院)开出 + 文书公告带"本案由 XX 提议";镇长选举 poll 开出(候选为 Ac1=H/So1=H 居民);所有 NPC 已投票(`_npc_voters` 幂等);**再跑一次 nightly 验证幂等**——议案不重复开、选举不重复开、NPC 不重复投。
**T3 议案到期**:把 poll `closes_at` 拨到过去 → 快进 → poll closed、胜出项 `won=true`;若"赞成兴建"胜出:`dynamic_locations` 出现 post_office 行、`get_location_by_id("post_office")` 在 reload 后可解析;文书发结果公告。
**T4 选举到期**:选举 poll 关闭 → 得票最高者 `meta_json.mayor=True`、`system_config.current_mayor` 正确、就任记忆存在;旧镇长(如预置)旗子被摘;镇长下次 `on_work` 工资 = round(基础×1.2)。
**T5 经济日常**:居民 WORK → treasury 入账、wallet 缓存同步;钱扣到阈值下 → decision prompt 含"手头很紧";EAT 有钱扣费/没钱赊账(记忆含"赊"、与店主 familiarity>0)。
**T6 集市日**:构造周六 → `ensure_scheduled_events` 生成集市日事件(payload.market_day=true)+ 文书公告;事件激活时购物 `total_sc` 打 9 折;on_work 冷却减半(用 fakeredis TTL 断言)。
**T7 故事弧**:选一条 relation 触发的 arc,人为 bump 关系跨过阈值 → 快进 → 里程碑顺序推进(第二里程碑不得跳过第一)、双方记忆落表;推到 finale → status=achieved、署名公告、关系跃升;日报素材 `gather_material` 出现 arc_lines。
**T8 提案人加权**:守序居民(A2=H)+ 与提案人 affinity 0.9 → 投首选项;无交情守序者 → 投维持现状(对照)。
**T9 全关对照**:四个开关全 false → 快进一晚,以上所有写入均不发生(polls/dynamic_locations/treasury/公告零新增),既有行为与主线一致。
**T10 M4 harness**:`uv run python scripts/memory_recall_eval.py`(或按其入口)离线出数,两种策略 recall@k ∈ [0,1]、n=20。

## 阶段 3 — 边界与恶意路径(抽查)

- `POST /polls/propose`:无 token 401;非管理员带 effect → effect 被剥离(advisory);少于 2 选项 400。
- 选项全零票/全 None effect 的 poll 正常关闭不崩。
- `closes_at` 无时区(naive)也能正确判到期(代码有 replace(tzinfo) 分支,验一下)。
- 邮差投递:无到期胶囊时记忆为"没有迟到的信"而非报错;`deliver_due_capsules` 异常时 on_work 仍完成(fail-open)。
- 商店 `resident_work`:库存减到 0 → 条目 inactive;再买 → 明确失败而非超卖;同一创作者已有在售条目不重复上架。

## 交付物与通过标准

产出 `docs/testing/TEST_REPORT_M1-M6_<日期>.md`:逐用例 PASS/FAIL/SKIP + 证据(关键表行、日志摘录、断言值),缺陷分级 P0(机制不发生/数据损坏)/ P1(行为错误)/ P2(体验瑕疵),并附"接线审计清单"结果。harness 脚本留在 `tmp/`,报告里给出复跑命令。

**通过标准**:阶段 0 基线达标;接线审计 12 项全有调用点且 nightly 顺序正确;T1–T10 中 P0 零缺陷、P1 缺陷有明确复现步骤。**已知遗留不算新缺陷**(报告里复述即可):批次未提交 git、集市日折扣不显示在目录页(仅结算生效)、`_npc_choice` 的 duty 经济倾向用字符串匹配。测试数据不污染工作树:harness 用独立 sqlite 文件,结束后留在 `tmp/` 并在报告注明路径。
