# NPC 投票 option-0 结构性偏向修复报告 · 2026-07-25

分支 `fix/npc-choice-option0`,base `d6ed5b6`(master,S1-5 财政 048 / S2-5 政策 049 /
工程健康批合并之后)。**无迁移**。未 push / 未合并 / 未部署。

| commit | 标题 |
|---|---|
| `0601789` | `test(civic)`: 复现 NPC 投票 option-0 结构性偏向(红) |
| `c407832` | `fix(civic)`: 修掉 `_npc_choice` 的 option-0 结构性偏向(三条根因全修) |
| `3d70ed6` | `feat(probe)`: burnin_report 加 NPC 投票分布探针(只读、零 LLM) |

---

## 1. 结论(先行)

现网 3 张 civic poll 的 NPC 票 **14/14 全压 option 0**(`H/lnK = 0`)不是 SBTI 数据缺失,
是 `civic_service._npc_choice` 的**算法结构性偏向**——本报告逐条给出代码证据、修复方式、
以及在**生产形态 fixture** 上的实测数字:

| 形态 | 修复前 | 修复后 |
|---|---|---|
| 选举型(4 选项**全带 effect**) | `[14, 0, 0, 0]` = **100.0%**,`H/lnK = 0.000`,1/4 选项有票 | `[5, 2, 4, 3]` = **35.7%**,`H/lnK = 0.962`,**4/4 选项有票** |
| 建设型(effect vs 维持现状) | `[13, 1]` = **92.9%**,`H/lnK = 0.371` | `[9, 5]` = **64.3%**,`H/lnK = 0.940` |

判据(动实现之前写死,未事后挪门)全部达标:

- option-0 占比 ≤ 45% → **35.7%** ✅
- 至少 3 个选项拿到票 → **4 个** ✅
- 同一 fixture 连跑两次逐票一致 → ✅(`test_production_shape_is_deterministic`)
- 零新增 LLM 调用 → ✅(纯规则,新增依赖只有 `hashlib`)
- 既有 civic/election 测试零改动通过 → ✅(**没有任何既有测试被迫修改**)

建设型的 92.9% → 64.3% 不在判据里(判据只针对 4 选项形态),但它同样脱离了垄断:
K=2 时无偏基线是 50%,9:5 属于正常分化,且**没有换成 option-1 垄断**——这一点由
`test_building_shape_is_not_a_mirror_monopoly` 硬盯着(`min(tally) >= 3`)。

---

## 2. 三条根因的代码证据

以下行号为 base `d6ed5b6` 的 `backend/app/services/civic_service.py:180-227`
(修复后逐字保留在 `_npc_choice_legacy`,`civic_service.py:394-436`)。

### 根因 1 — `A2 == "M"` 是零信号,而生产 71% 的人是 M

```python
if a2 == "H" and not eff:
    scores[i] += 1.0
if a2 == "L" and eff:
    scores[i] += 0.5
```

只有 `H`(且该选项**无** effect)与 `L`(且该选项**有** effect)会加分。
`ops-audit-2026-07-25B` §A.4 实测生产 14 个 NPC 的 A2 分布是 **M=10 / L=3 / H=1**
—— **10 个人在所有选项上恒 0.0 分**。

同一段里另外两个加分支在生产同样恒不触发,这是审计 §A.5 已核实、我复核确认的:

- `duty` 加分要求 `duty.key ∈ (shop_keeper, tavern_hub, cafe_host)`,而生产 14 个 NPC 的
  `meta_json.duty` **全为 NULL**;
- 关系加分要求 proposer 能在 `by_slug` 里查到,而 `by_slug` 只装 `resident_type='npc'`,
  提案人 `jiang-lin` / `zhou-dahe` **不是 NPC**(镇长选举那张 poll 干脆没有 proposer)。

⇒ 生产环境下 14 个 NPC 里有 10 个在每一张 poll 上得到的分数向量是全 0。

### 根因 2 — 平局兜底恒偏 index 0

```python
# deterministic tie-break: index order
best = max(range(len(opts)), key=lambda i: (scores[i], -i))
```

`-i` 让全 0 向量必然落在 `i = 0`。这不是"随机兜底恰好偏了",是**写死的方向性偏置**:
凡是打平就投第一个选项。而 civic poll 的 option 0 按约定就是提案人的主张选项,
于是"没意见的人"= 自动支持提案。

### 根因 3 — 选项全带 effect 时两个 SBTI 分支同时失效

镇长选举 poll 的 4 个选项都是 `{"type": "mayor", "slug": …}`(`election_service.py:57-60`),
即**每个选项都有 effect**:

- `a2 == "H" and not eff` → 永远不触发(没有"无 effect"的选项);
- `a2 == "L" and eff` → 对**每一个**选项都 +0.5,是个常数,不产生任何区分度。

⇒ 又回到全平局 → 根因 2 → index 0。这就是选举 poll 拿到 14/14 = 100% 的机制。

**旧算法只看"有没有 effect",从不看 effect 是什么。** 这是根因 3 的本质。

---

## 3. 修复方式与设计理由

改动集中在 `backend/app/services/civic_service.py`(新增 `_stable_unit` / `_option_key` /
`_effect_tags` / 重写 `_npc_choice` / 保留 `_npc_choice_legacy`)。
**没有碰** `_close_one` 的阈值逻辑、`_execute_outcome` 的效果分派,以及 S1-5 / S2-5 /
工程健康批刚落地的任何代码。

### 3.1 根因 2 的修复:确定性个人口味哈希取代 index 兜底

```python
_TASTE_MAG = 0.25   # civic_service.py:199

def _stable_unit(*parts) -> float:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") / 2 ** 64

scores[i] += _TASTE_MAG * _stable_unit(resident.slug, poll_key, _option_key(o, i))
```

设计理由:

- **确定性**是硬要求。用 `blake2b` 而不是内建 `hash()`(PYTHONHASHSEED 加盐,跨进程会变)、
  更不用 `random`。`test_stable_unit_is_reproducible_across_processes` 把一个具体摘要值
  钉死(`0.260063755221`),任何回退到 `hash()` / `random` 的改动都会立刻红。
- **幅度 0.25 严格小于最小特质步长 0.30**,所以口味只能裁真正的平局,永远盖不过真实倾向。
  例:既有测试 `test_npc_vote_leans_toward_liked_proposer` 里"守序但与提案人亲密"的居民
  是 1.35 vs 1.0,余量 0.35 > 0.25,行为不变。
- **hash key 用 `poll.question` 而不是 `poll.id`**。poll.id 是每次运行新生成的 uuid,
  掺进哈希会让同一 fixture 每次跑出不同结果(判据里的"连跑两次一致"就废了)。
  question 是这张议案的稳定身份。选项侧用 `_option_key(o, i)` = 位置 + label + effect 的
  slug/key/type,所以同一个反复开的选举(question 逐字相同)在候选人变了以后会重新洗牌,
  不会有人永远锁死投第 3 个位置。

### 3.2 根因 1 的修复:A2=M 拿到信号——但**不是**一个统一方向的加分

```python
else:                        # "M" — 务实中间派
    if "reversible" in tags:
        scores[i] += 0.35
```

`reversible` = effect 的 `type ∈ {system_config, policy, narrative}`(能再调回来的旋钮),
不含 `dynamic_location` / `mayor`(盖了就在那儿、选上就是四年)。

设计理由,这一条我踩过一次坑,值得写清楚:

我第一版给 M 加了 `if "build" in tags: += 0.15`(务实的人偏好看得见摸得着的东西)。
结果建设型 poll 变成 `[12, 2]` —— **10 个 M 里 9 个同时倒向 option 0**,option-0 占比
从 92.9% 只降到 85.7%。原因很直白:**M 占人口 71%,任何给 M 的统一方向性加分都会立刻
变成一个新的结构性偏向**,只是把 option-0 垄断换成 option-1 垄断而已。

所以 M 的信号必须是"**取决于选项内容**"的,不能是"取决于选项有没有 effect"或"是不是建设"。
务实中间派的真实判据是**可回退性**:能走回头的改动他们支持,一锤子买卖他们不感冒。
当一张 poll 里没有任何可回退选项(建设、选举)时,M 群体就由**其余维度 + 个人口味**决定,
自然分化,而不是整块倒过去。

同时新增了跨维度的话题契合表(`_TRAIT_AFFINITY`,`civic_service.py:225-236`),
让 A2 之外的维度也参与:

| 维度 | 取值 | 话题标签 | Δ |
|---|---|---|---|
| A1 世界观乐观度 | H / L | `change` / `status_quo` | +0.30 |
| So1 社交能量 | H / L | `social` | +0.30 / −0.30 |
| Ac1 成就动机 | H | `office` / `economy` | +0.30 |
| Ac1 成就动机 | L | `office` | −0.30 |
| E1 表达欲 | H | `culture` | +0.30 |

只有显式的 `H` / `L` 才出信号,缺维度的居民行为与今天一致(既有测试因此零改动通过)。

### 3.3 根因 3 的修复:按 effect 的内容打分

```python
def _effect_tags(eff) -> set[str]:          # civic_service.py:263
    if not eff:
        return {"status_quo"}
    tags = {"change"}
    ... tags.add(f"type:{etype}"); "reversible" if etype in _REVERSIBLE_TYPES
    ... 关键词命中 social / economy / order / build / office / culture
```

再加上"**选项即人**"这一类 effect 的专门打分(`civic_service.py:352-371`):

```python
if eff.get("type") in _PERSON_TYPES:        # mayor / office / duty
    if eff["slug"] == resident.slug:
        scores[i] += 2.0                    # 自己参选投自己
    else:
        pair = await relation_service.get_pair(db, resident.id, other.id)
        scores[i] += 1.5 * pair.affinity    # 投跟自己关系好的候选人
```

设计理由:镇长选举里 4 个选项的 effect **类型完全相同**,唯一的差别是 `slug` 指向谁。
任何"按话题打分"的规则在这里都无能为力——差异只可能来自**投票人与候选人的关系**。
这条既修了根因 3,又顺手让选举第一次有了政治学意义(现网 relations 表是有数据的;
关系为空时退回口味哈希,不炸)。DB 开销:每张 poll 每个人 ≤ K 次 `get_pair`,K ≤ 5,
每晚一次,零 LLM。

### 3.4 其余保持不变

`H` 分支保留 `status_quo +1.0`(另加 `order +0.4`:守序的人至少认规则层面的改动),
`L` 保留 `change +0.5`,duty 兴趣保留 `+0.8`,提案人关系保留 `+2.0 / +1.5×affinity`。
这些是既有测试直接断言的行为,一个都没动。

---

## 4. 验收判据的实测数字

测量脚本是仓库内的真实代码路径(`civic_service.propose` → `run_npc_voting`),
fixture 与生产逐项对齐:14 个 NPC(slug 抄自审计 §A.4)、A2 = M10/L3/H1、
`duty` 全 NULL、proposer `jiang-lin` 建成 `resident_type='player'`(所以 `by_slug` 恒 miss)。

```
LEGACY(旧算法)  选举型 4 全带 effect      tally=[14, 0, 0, 0] option0=100.0% H/lnK=-0.000 非零选项=1
LEGACY(旧算法)  建设型 2 (effect vs 现状) tally=[13, 1]       option0= 92.9% H/lnK= 0.371 非零选项=2
FIXED (新算法)  选举型 4 全带 effect      tally=[5, 2, 4, 3]  option0= 35.7% H/lnK= 0.962 非零选项=4
FIXED (新算法)  建设型 2 (effect vs 现状) tally=[9, 5]        option0= 64.3% H/lnK= 0.940 非零选项=2
```

LEGACY 两行与审计 §A.5 的静态推演(100% / 92.9%)**逐位吻合**,说明 fixture 确实复现了现网形态。

| 判据 | 门槛 | 实测 | 结论 |
|---|---|---|---|
| option-0 得票占比(4 选项形态) | ≤ 45% | **35.7%** | ✅ |
| 拿到票的选项数 | ≥ 3 | **4** | ✅ |
| 归一化熵 `H/lnK` | (审计口径,未设硬门) | **0.962**(修复前 0.000) | ✅ |
| 逐票确定性 | 两次一致 | 一致 | ✅ |
| 新增 LLM 调用 | 0 | 0(纯规则) | ✅ |
| 既有测试被迫改动 | 0 | **0** | ✅ |

红→绿的过程有据可查:`0601789` 的 commit body 抄了当时的真实失败输出
(`AssertionError: option-0 still dominant: [14, 0, 0, 0] → 100.0%`)。

---

## 5. 既有测试是否有被迫改动

**没有,一处都没有。** 这是我特意盯的一条,因为"顺手把断言改了"是最容易掩盖问题的地方。

四个直接相关的既有套件全部零改动通过:

| 套件 | 关键断言 | 修复后 |
|---|---|---|
| `tests/test_m3_civic.py::test_npc_voting_is_rule_based_and_idempotent` | A2=H 投无 effect 的 option 1 | 通过(H 分支 +1.0 未动) |
| `tests/test_m3_civic.py::test_npc_vote_leans_toward_liked_proposer` | 精确 2:1 分票 | 通过(1.35 vs 1.0,余量 0.35 > 口味 0.25) |
| `tests/test_preset_sbti.py::test_npc_votes_no_longer_monopolise_option_0` | `v0 < 11` 且 `v1 >= 5` | 通过 |
| `tests/test_m6_election.py`(全 7 条) | 选举开/关/装镇长/工资加成 | 通过 |

顺带说明:`test_preset_sbti` 和 `scripts/sbti_backfill.py` 的 docstring 都把 option-0 偏向
归因为"SBTI 数据缺失"。那个归因不完整(数据是根因之一,但不是全部),不过它们的**断言**
是行为断言、没有断言"NPC 都投 option 0"这类被修掉的行为,所以我一个字没动。
要不要回头修正那两处 docstring 的叙述,交给你决定。

---

## 6. Kill switch 用法

```bash
# 默认:修复生效(不需要配任何东西)
CIVIC_NPC_CHOICE_LEGACY=false

# 回落到修复前的评分器(逐字保留在 _npc_choice_legacy)
CIVIC_NPC_CHOICE_LEGACY=true
```

纪律照 2026-07-25B 批:

- **env 是运行时源头**;
- `Settings.civic_npc_choice_legacy: bool = False`(`backend/app/config.py:516`)登记同名字段提供默认值;
- 写进 `backend/.env.example`(`CIVIC_NPC_CHOICE_LEGACY=false`,带注释),
  `tests/test_env_example_consistency.py` 不变量 1/2 都过。

**默认 False = 修复默认生效**,理由写在配置注释里:这是 bug fix 不是新机制,
默认关掉等于生产继续坏着。

两条路径都有测试:

- `test_legacy_kill_switch_restores_the_old_scorer` —— 开关打开后必须**逐位**回到 `[14, 0, 0, 0]`;
- `test_civic_npc_choice_legacy_defaults_off` —— 钉死默认值;
- 探针侧另有 `test_probe_flags_the_legacy_monopoly`,确认开关回落时探针如实报红。

生效范围只有 `_npc_choice`。`_close_one` / `_execute_outcome` / S2-5 的 `policy` 效果类型
与阈值门控完全不受影响。

---

## 7. 探针出数样例

`backend/scripts/burnin_report.py` 新增 `fetch_poll_vote_snapshot` /
`npc_vote_distribution` / `render_probes_npc_vote`,形状照既有 `render_probes_*`,
纯只读、零 LLM,表不存在或无票时 fail-open。CLI 新增 `--polls`(默认最近 10 张)。

```bash
docker compose exec api python scripts/burnin_report.py --days 2 --polls 10
```

实跑输出(sqlite 生产形态 fixture,2 张 poll / 28 票):

```
== 治理探针（NPC 投票分布 · 最近 10 张 poll）==
  样本:2 张 poll / 28 张 NPC 票
  option-0 超额偏向指数 mean(占比-1/K) = +0.1250（门槛 ≤ 0.15）✅；原始 option-0 总占比 = 50.0%
  归一化熵 H/lnK 均值 = 0.9512（门槛 ≥ 0.6）✅
  全票压单一选项的 poll = 0 / 2（✅ 无垄断）
  逐张明细（tally / option-0 占比 vs 无偏基线 1/K / H·lnK⁻¹）：
    [open  ] 镇长选举:谁来当下一任镇长?               K=4 [5, 2, 4, 3] → 35.7% vs 25.0% / 0.9621 (非零选项 4/4)
    [open  ] 在南苑空地兴建一座邮局                  K=2 [9, 5] → 64.3% vs 50.0% / 0.9403 (非零选项 2/2)
    （口径同 ops-audit-2026-07-25B §A：NPC 票取自 options_json[i].npc_votes,不含 votes 表的玩家票）
```

同一套 fixture 在 `CIVIC_NPC_CHOICE_LEGACY=true` 下重新投一轮(即现网 2026-07-25 的形态),
**实跑**输出——这同时也是 kill switch 走真实 env(不是 monkeypatch)的运行时证据:

```
== 治理探针（NPC 投票分布 · 最近 10 张 poll）==
  样本:2 张 poll / 28 张 NPC 票
  option-0 超额偏向指数 mean(占比-1/K) = +0.5893（门槛 ≤ 0.15）🔴 option-0 结构性偏向；原始 option-0 总占比 = 96.4%
  归一化熵 H/lnK 均值 = 0.1856（门槛 ≥ 0.6）🔴 分布过于集中
  全票压单一选项的 poll = 1 / 2（🔴 垄断仍在）
  逐张明细（tally / option-0 占比 vs 无偏基线 1/K / H·lnK⁻¹）：
    [open  ] 镇长选举:谁来当下一任镇长?               K=4 [14, 0, 0, 0] → 100.0% vs 25.0% / 0.0 (非零选项 1/4) 🔴
    [open  ] 在南苑空地兴建一座邮局                  K=2 [13, 1] → 92.9% vs 50.0% / 0.3712 (非零选项 2/2)
    ⚠️ CIVIC_NPC_CHOICE_LEGACY=true —— 跑的是修复前的旧评分器，option-0 占比预期回到 ~100%,本探针的门槛不适用
```

新旧对照一眼可见:超额偏向指数 **+0.5893 → +0.1250**,熵均值 **0.1856 → 0.9512**,
垄断 poll **1 → 0**。

**聚合口径为什么不是直接看原始 option-0 占比**:跨不同 K 的 poll 直接比原始占比不成立
——K=2 的无偏基线本来就是 50%,拿 45% 去卡它会把正常的 9:5 判成红。所以聚合门用
**超额偏向指数** `mean(占比 − 1/K)`(无偏世界 ≈ 0,修复前现网 = 0.5~0.75),
原始占比和逐张"占比 vs 1/K"仍照实打印,单张 K≥4 的 poll 超 45% 会单独标红。

---

## 8. 全量测试与"新增失败 = 空"证据

```
$ cd backend && .venv/bin/python -m pytest tests/ -q
51 failed, 1906 passed, 25 skipped, 11 deselected, 219 warnings, 17 errors in 280.00s (0:04:40)
```

对比 base `d6ed5b6` 的主会话基线 `51 failed, 1896 passed, 25 skipped, 17 errors`:
failed / errors **完全持平**,passed **+10**(= 本线新增的 7 条 `test_npc_choice_bias`
+ 3 条 `test_burnin_report_npc_vote`)。

归一化失败集比对:

```
$ grep -E "^(FAILED|ERROR)" /tmp/npcfix-full.txt | sed 's/\[.*\]//' | sort > /tmp/npcfix-fails.txt
$ wc -l /tmp/npcfix-fails.txt /tmp/closeout-25B-fails.txt
      68 /tmp/npcfix-fails.txt
      68 /tmp/closeout-25B-fails.txt
$ comm -13 /tmp/closeout-25B-fails.txt /tmp/npcfix-fails.txt   # 新增
（空）
$ comm -23 /tmp/closeout-25B-fails.txt /tmp/npcfix-fails.txt   # 消失
（空）
```

**新增失败 = 空。** 两个集合逐行相同(68 行),即本线既没引入新失败,也没顺手"修好"
任何预存失败(那会是掩盖问题的信号)。

预存失败里与本线最容易混淆的一条是
`tests/test_env_example_consistency.py::test_every_example_key_is_a_settings_field`
—— 它在 base 就红,原因是 `.env.example` 里 39 个 `lab_egress_*` / `lab_artifact_scanner_*`
键在 `Settings` 里没有对应字段。**与本线新增的 `CIVIC_NPC_CHOICE_LEGACY` 无关**
(该键在 `Settings` 中已登记,不在失败列表里);不变量 2(每个 Settings 字段都要有 example 行)
本线通过。这条预存红属于工程健康债,不在本线 scope,未动。

---

## 9. 需要你知道的事

1. **本线不含任何迁移。** 迁移链头仍是 `049_add_policies`,纯代码改动。
2. **现网那 3 张 poll 不会自动重投。** `run_npc_voting` 用 `options_json[0]['_npc_voters']`
   做幂等,14 个 slug 已全部落库。部署本修复**不会**改变已有的 14/14 结果——要拿到可判样本,
   要么等下一次自动选举(≈ 2026-08-21),要么人工开一张新 poll 让当晚 nightly 投一轮。
   这是审计 §A.6 的结论,本修复不改变它。
3. **`test_preset_sbti.py` / `scripts/sbti_backfill.py` 的 docstring 把偏向归因为"SBTI 数据缺失"**,
   这个叙述现在不完整了(数据只是根因之一)。断言没问题,我一个字没改,要不要回头改叙述由你定。
4. **建设型 poll 修复后是 64.3%**,高于 45%,但那个门是给 4 选项形态写的,K=2 的无偏基线是 50%。
   如果你希望建设型也压到某个数,那是"要不要给 M 群体一个方向性倾向"的产品判断,
   不是 bug——按 §3.2 的经验,任何统一方向的加分都会立刻变成新的结构性偏向,得换别的做法(比如
   让 effect 的具体收益进入打分),建议单开一线。
