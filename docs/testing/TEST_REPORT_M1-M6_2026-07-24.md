# 小镇扩展 M1–M6 功能验收测试报告（本地）

> 执行时间：2026-07-24（Asia/Shanghai）
> 被测对象：工作树中未提交的「小镇扩展 M1–M6」批次（含 2026-07-23 审计补齐）
> 环境：本机 Mac / 独立 sqlite 文件库 / fakeredis / dummy LLM key（零 LLM 成本）
> 执行方式：全量+定向 pytest、接线 grep 审计、一次性端到端 harness（直调 `run_nightly_jobs()` 等生产入口）、API/service 边界抽查
> 角色：独立验收测试员，未改业务源码（新增文件仅 `tmp/` 与本报告）

## 1. 结论

**通过。** 阶段0 基线达标；接线审计 12 项全部有生产调用点且 nightly 顺序正确；端到端 T1–T10 **P0 零缺陷、P1 零缺陷**；阶段3 边界抽查 4/4 通过。

发现 **1 处 P2**（内置居民缺 `sbti.dimensions` → SBTI 类启发式在真实种子上回落默认值，机制不崩）+ 复述若干已知遗留。fail-open 语义按要求验证：缺 LLM 时日报/梦境任务报 403 但**主流程照常完成**、不崩。

M1–M6 可以进入「提交→合并→部署」，P2 非阻断（属既有数据画像问题，非本批机制缺陷）。

## 2. 阶段0 — 基线

| 项 | 命令 | 结果 | 判定 |
|---|---|---|---|
| 全量套件 | `uv run pytest -q` | **1311 passed / 1 skipped / 11 deselected / 0 failed**（168.97s） | ✅ |
| M 系定向 | 6 个 `test_m*.py` | 39 passed（9+7+9+2+5+7），+ `test_duty_service.py` 15 passed = **54 全绿 / 0 failed** | ✅ |

- 与 KICKOFF 预期「1310 passed / 12 deselected」的 **+1 pass / −1 deselect** 差异 = 文档预告的 `test_agent_worker::test_worker_crashing_task_is_fatal` 在本机 Mac **通过**（沙盒里被 deselect），非本批缺陷。
- 11 个 `lab_oci` 用例按惯例 deselect。
- 计数注：6 个 M 文件实收 **39**（KICKOFF 记 40，−1 为口径差；全部 PASS，0 fail）。

## 3. 阶段1 — 接线审计（历史病灶：实现了没接电）

**12/12 全部有真实生产调用点**：

| 入口 | 生产调用点 `file:line` | 状态 |
|---|---|---|
| `duty_service.on_work` | `agent/phases/execute/basic.py:177-179`（WORK 分支，带 `market_day=_is_market_day`） | ✅ |
| `prompt_hint` + wallet 压力提示 | `agent/prompts.py:81-82` / `:86-93` | ✅ |
| `_charge_meal` | `execute/basic.py:197`（EAT 分支，经济开关门控 `:196`） | ✅ |
| `arc_service.evaluate_arcs` | `tasks/nightly_cron.py:78-80` | ✅ |
| `civic_service.close_due_polls` / `run_npc_voting` | `nightly_cron.py:88-90` / `:120-122` | ✅ |
| `civic_service.seed_civic_agenda`（补齐件） | `nightly_cron.py:99-101` | ✅ |
| `election_service.maybe_open_seasonal_election`（补齐件） | `nightly_cron.py:110-112` | ✅ |
| `civic_service.maybe_spawn_lecture_debate` | `tasks/event_cron.py:45-46`（`phase == "end"`） | ✅ |
| `shop_effects` 的 `resident_work` handler | `shop_effects.py:267` `@register("resident_work")` | ✅ |
| 集市日折扣 `_market_discount` | `shop_service.py:84`（purchase 结算前） | ✅ |
| duty perks（gossip/encounter/quest_magnet/chat_uplift） | `gossip:55-56` / `encounter:85-86` / `daily_quest:42-46` / `chat:95-105,256` | ✅ |
| 提案人加权 `_proposer_slug` | `civic_service.py:56`（propose 写入）+ `:211-222`（`_npc_choice` 消费） | ✅ |

**nightly 顺序核对**：`evaluate_arcs(78) → close_due_polls(88) → seed_civic_agenda(99) → maybe_open_seasonal_election(110) → run_npc_voting(120)`。要求的 `close→seed→election→npc_vote` 顺序满足 ✅ —— 当晚新开的议案/选举当晚就有 NPC 票。

## 4. 阶段2 — 端到端动态验证（T1–T10）

Harness：`backend/tmp/m_harness.py`（独立 sqlite、seed.seed_residents 种子、直调生产入口、断言落终态）。

| 用例 | 结果 | 关键证据 |
|---|---|---|
| **T1 世界冷启动** | PASS | 11 居民全带 duty；arc goals=5/5；relations=15/15；邮差骆小舟就位 |
| **T2 第一晚** | PASS | 2 建设议案开出（提案人 `jiang-lin`/`zhou-dahe`）+ 署名公告「本案由」×2；镇长选举开出（选项 effect 全 `type=mayor`）；NPC 投票 33 票 / 11 voters；**再跑一次幂等**：open_polls 3→3、npc_votes 33→33 不变 |
| **T3 议案到期→建成** | PASS | poll `closed`，`赞成兴建` 17 票胜出；`dynamic_locations` 出现 `post_office` 行且 active；`get_location_by_id("post_office")` reload 后可解析；文书发结果公告 |
| **T4 选举到期→就任** | PASS | 林晚秋当选，`meta_json.mayor=True` 且**唯一**（mayor_count=1），`system_config.current_mayor=lin-wanqiu`；镇长工资 ×1.2 在发薪 duty 上实测：陈铁生 base=8 → `round(8×1.2)=10` 入账 ✅（当选者恰为无发薪 handler 的 cafe_host，故 ×1.2 在发薪 duty 上单独坐实） |
| **T5 经济日常** | PASS | WORK：treasury 0→10、wallet 缓存同步=10；钱压阈值下 decision prompt 含「手头很紧」；EAT 有钱扣 2、没钱赊账（记忆含「赊」） |
| **T6 集市日** | PASS | 周六 `ensure_scheduled_events(today=周六)` 生成 `market_day` 事件；`_market_discount=0.9`；购物 price 80→结算 72（9折）；on_work 冷却 market_ttl=36000 / 普通=72000（减半，fakeredis TTL 断言） |
| **T7 故事弧** | PASS | zhao-qiwen arc：只满足里程碑2 时**两里程碑都不推进**（第二不跳过第一）；满足里程碑1 后**一晚只推进一格**；再一晚→`status=achieved`、progress=1.0、署名公告、日报 `arc_lines` 出现 arc 素材 |
| **T8 提案人加权** | PASS | 注入 A2=H（种子缺 sbti，见 P2-1）：与提案人 affinity 0.9 的守序者→投首选项(opt0)；无交情守序者→投维持现状(opt1) |
| **T9 全关对照** | PASS | 4 开关全 false + 独立 DB 快进两晚：polls=0 / dynamic_locations=0 / treasury_sum=0，M 写入零发生 |
| **T10 M4 召回评估** | PASS | `python -m scripts.memory_recall_eval`：keyword recall@5=1.000、vector recall@5=0.900，两策略 ∈[0,1]、n=20（无 embedding 后端→确定式离线 fallback，符合零 LLM 约束） |

**fail-open 复核**：T2 快进夜晚时 `generate_village_digest` / `run_nightly_dreams` 因 dummy key 报 `403 invalid api-key`，但均被各自 try/except 吞成 error 日志、**后续 M 任务照常执行、议案/选举/投票全部落表** —— 「缺 LLM 不崩、主流程照常完成」成立。

## 5. 阶段3 — 边界与恶意路径（抽查）

Harness：`backend/tmp/m_boundary.py`。

| 用例 | 结果 | 证据 |
|---|---|---|
| **B1 `/polls/propose`** | PASS | 无 token→401；<2 选项→400；非管理员带 effect→200 且 effect 被剥离（advisory，DB 核实 options effect 全 None） |
| **B2 全 None-effect 零票关闭** | PASS | 不崩、`status=closed`、胜出项标记正常 |
| **B3 naive(无时区) closes_at** | PASS | `closes_at` 无 tzinfo 也正确判到期并关闭（`close_due_polls` 的 `replace(tzinfo=UTC)` 分支坐实） |
| **B4 邮差 fail-open** | PASS（代码+单测） | `on_work` 全程 try/except 包裹（`duty_service.py:107-122`），`deliver_due_capsules` 异常不阻断 WORK 完成；`test_m5_space::test_postman_work_runs_delivery` 覆盖投递主路径 |
| **B5 resident_work** | PASS | 首次上架成功、已有在售拒重复上架（item 行=1）；库存减到 0→`active=False`；再买→`ShopError` 明确失败而非超卖 |

## 6. 缺陷与发现

### P2-1：内置居民缺 `sbti.dimensions`，SBTI 类启发式在真实种子上回落默认值

**现象**：`seed.preset_characters.PRESET_CHARACTERS` 的 11 位内置居民 `meta_json` 只含 `{origin, is_preset, duty, ...}`，**无 `sbti.dimensions`**（harness 实测 A2 分布 = 11×MISSING）。

**影响**：以下启发式在真实内置居民上取默认值、失去区分度（机制不崩，fail-safe）：
- `civic_service._npc_choice` 守序倾向 `a2 = dims.get("A2","M")` → 恒 "M"，守序/叛逆分支从不触发；叠加确定式 tie-break `(score, -i)`，无交情/无 effect 差异时 NPC 票**系统性偏向 option 0**（提案首选项），使建设议案默认易过、选举首位候选默认易胜。
- `election_service.open_election` 候选按 `Ac1==H or So1==H` 选取 → 无匹配 → 回落 heat/顺序。
- `civic_service.maybe_spawn_lecture_debate` 的 A1/So1 对比取人 → 回落 pool[0]。

**不影响**：M3 补齐的头牌特性——**提案人 affinity 加权**（基于 `resident_relations`，不依赖 SBTI）——T8 实测生效。

**定级理由**：机制均 fail-safe（投票/选举/辩论照常发生），属**数据画像/保真度**问题且为既有（M 批次只是消费从未被种子填充的 SBTI），非本批机制缺陷。**建议**（非阻断）：给内置居民补 `sbti.dimensions`（或从 persona 派生），否则治理/选举的多样性长期偏低。

### 已知遗留（复述，不计新缺陷）

- 批次仍未 git 提交，工作树同时躺 0723 生产修复批改动，需拍板提交切分。
- 集市日 9 折只在 purchase 结算生效，商店目录页仍显示原价（前端增强项）。
- `_npc_choice` 的 duty 经济倾向用 `str(effect)` 关键词匹配，粗但够用。
- 6 个 M 文件实收 39 用例（KICKOFF 记 40，口径差，全绿）。

## 7. 复跑命令

```bash
cd backend
# 环境隔离（匹配云端锁定基线：dummy key + 独立 sqlite）
export DATABASE_URL="sqlite+aiosqlite:////tmp/simverse_m_accept.db" LLM_API_KEY="test-dummy-key" DEBUG=true

# 阶段0
uv run pytest -q
uv run pytest tests/test_m1_economy.py tests/test_m2_arcs.py tests/test_m3_civic.py \
  tests/test_m4_recall_eval.py tests/test_m5_space.py tests/test_m6_election.py -q

# 阶段2（T1–T8 + T7/T6）
M_MODE=full  uv run python tmp/m_harness.py     # → 8/8 PASS，结果写 tmp/m_harness_full.json
# 阶段2（T9 全关对照，独立进程 env 关四开关）
M_MODE=alloff uv run python tmp/m_harness.py     # → 1/1 PASS，tmp/m_harness_alloff.json
# 阶段2（T10 M4 召回）
uv run python -m scripts.memory_recall_eval

# 阶段3 边界
uv run python tmp/m_boundary.py                  # → 4/4 PASS，tmp/m_boundary.json
```

证据 JSON：`backend/tmp/m_harness_full.json`、`m_harness_alloff.json`、`m_boundary.json`。Harness 脚本：`backend/tmp/m_harness.py`、`backend/tmp/m_boundary.py`（`tmp/` 已 gitignore）。
