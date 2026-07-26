# 线 F1 · 声誉语义修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让公共声誉轴有真正的双向信号——八卦语气由「传话人对当事人的 affinity」决定而非常量负值；声誉只影响得票不再决定谁能参选；并交付一个用真实分布标定 `rep_credit_min_score` 的只读脚本。

**Architecture:** `reputation_service.recompute` 的打分公式从「常量 tone」改为「关系 affinity 驱动的 tone」——每条 gossip 记忆的两个当事人（`Memory.resident_id` = 持有者/传话人，`Memory.related_resident_id` = 被议论者）恰好构成 `resident_relations` 的一个 canonical pair，一次批量读取即可拿到全部 affinity，零 LLM、零迁移。打分逻辑抽成 `_score_all()`，`recompute()`（写）与新的 `project()`（只读）共用同一份代码，因此标定脚本永远不会和夜间任务漂移。`election_service` 删掉「按声誉排序再截断」的候选筛选，声誉唯一的入票通道收敛到 `civic_service._npc_choice` 里的 `vote_trust_delta()`。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy 2 async / pytest + anyio / sqlite(测试) + PostgreSQL(生产)。无新增依赖、无迁移、无 LLM 调用。

## Global Constraints

- 本线**不改** `backend/app/config.py`、不改 `backend/.env.example`、不改 `backend/app/tasks/nightly_cron.py`（共享文件统一延到收口接线，spec §1 决策 6 / §8）。新旋钮先落成模块常量，收口时提升为 `REP_*` 配置。
- 本线**不碰** `backend/scripts/burnin_report.py`（F2 的探针改动落在同一文件，避免线间冲突）。
- 独占文件：`app/services/reputation_service.py`、`app/services/election_service.py`（仅 `:53-60` 候选排序区）、`app/services/civic_service.py`（仅 `:366-371` vote-trust 区）、对应测试。越出该范围即为越界。
- 第 4 项「开 `REP_ENABLED` 闸」**不在本线**，属收口。本线所有改动在 `rep_enabled=False`（生产现状）下必须是零行为变化。
- 硬门：开闸前后对比，候选集不得因「被议论多」而缩小；重标定后拒绝面必须非空（用真实分布验证，不是构造数据）。
- 硬门 = **相对基线零新增失败**。本机有 `51 failed / 17 errors` 的预存 lab-v2 失败集（需 redis/testcontainers），不是 literal 0 failed。判定用失败集的**双向差集**（`comm -13` / `comm -23`），不是数量比较——数量相同不等于集合相同。
- TDD：严格红→绿，一 step 一 commit，commit 末尾带**真实** `Verified-by:` 输出。禁 `--no-verify` / `amend` / `squash` / 编造测试数据。
- 不要在 worktree 内创建 `backend/.env`（会破坏 conftest 的测试隔离）。
- 完成的定义：build/lint/单测绿不等于完成——Task 10 必须在真实进程上跑一遍标定脚本并贴运行时证据。

---

## 关键设计决策（执行前必读，不要重新讨论）

### D1. 八卦记忆 ↔ 关系 的关联走**主方案**，不走备选

spec §3 允许「若 affinity 与八卦记忆无法可靠关联」就退到「显式善行/越轨事件加减分」的备选方案。**核实结论：关联是可靠的，因此走主方案。** 证据：

- `app/models/memory.py:57,64` — `Memory.resident_id`（NOT NULL，FK→residents）是记忆持有者；`Memory.related_resident_id`（FK→residents）是被议论者。
- `app/services/gossip_service.py:123-127` — `maybe_gossip` 写新记忆时 `resident_id=listener.id`、`related_resident_id=origin.related_resident_id`，两个字段都是 `residents.id`。
- `app/services/reputation_service.py:80-86`（现状）已经用 `Memory.related_resident_id.in_(ids)` 过滤，**凡是进入打分的记忆，两个当事人 id 必然非空**（`related_resident_id` 为 NULL 的 P2-6 event-class 记忆天然被 IN 过滤掉）。
- `app/models/resident_relation.py:29-34` + `app/services/relation_service.py:50-57` — 关系按 canonical undirected pair 唯一存储，`canonical_pair(resident_id, related_resident_id)` 是一个**全函数**（任意两个 id 都能算出唯一键），查不到行 = 二人无往来 → affinity 视作 0.0 → tone 退化为现行常量 `rep_gossip_base_tone`，**与今天逐字节相同**。

语义读法：`tone` = 「传话人带着什么感情在传这条话」。affinity 是规则驱动、零 LLM、值域 `[-1,1]` 的现成量（`realism_rel_affinity_chat=±0.03` / `realism_rel_affinity_gift=0.1`）。

### D2. tone 公式与新旋钮取值

```
tone = clamp(rep_gossip_base_tone + GOSSIP_AFFINITY_WEIGHT * affinity + [distorted ? rep_distortion_penalty : 0],
             rep_min, rep_max)
```

- `affinity = 0`（无关系行）→ `tone == rep_gossip_base_tone`，即今天的常量 → 现有回归测试 `test_recompute_uses_gossip_distortion_hops_and_mood` 不用改也仍然绿。`rep_gossip_base_tone` 由此**退化为偏置项**。
- `GOSSIP_AFFINITY_WEIGHT = 3.0`（模块常量，本线不进 config）。取 3.0 的依据：在冻结的 `base_tone = -0.3` 下符号翻转点落在 `affinity = +0.1`，恰等于一次送礼/投资的增量（`realism_rel_affinity_gift = 0.1`，`app/config.py:502`）或约 4 次正向闲聊（`realism_rel_affinity_chat = 0.03`，`app/config.py:500`）。
- 读法用 `getattr(settings, "rep_gossip_affinity_weight", GOSSIP_AFFINITY_WEIGHT)`，**已实测** `settings` 对未定义字段的 `getattr` 走 default 分支（pydantic-settings 抛 AttributeError）。收口时只需在 `config.py` 加一行 `rep_gossip_affinity_weight: float = 3.0`，代码零 diff。

### D3. 候选集：删排序、保留 `[:4]`

`git blame` 核实：`candidates = candidates[:4]`（`election_service.py:61`）出自 `dde187c5`（2026-07-24），**早于**声誉功能 `8f3ef8b7`（2026-07-26）。因此删掉 `:53-60` 的声誉排序块 = 候选集回到 S1-1 之前逐字节的 SBTI/heat 口径，`[:4]` 是原有口径的一部分，保留。

已知遗留问题（**本线不修**，写进收口备注）：SBTI 分支的候选顺序是裸 SELECT 的行序，`[:4]` 因此在候选 >4 时依赖 DB 行序。这是 S1-1 之前就存在的抖动，与声誉无关；要修得先定「按什么排序截断」的口径，超出本线范围。

### D4. `reputation_service.py:74` 的人口口径由 **F1** 修，不留给 F2

spec §4.4 把这行（裸 `Resident.resident_type == "npc"` → 应为 `is_autonomous`）挂在 F2 名下，但 §3 又把 `reputation_service.py` 划为 F1 独占。**由 F1 改**：F1 本来就要重写这个函数体，F2 再去改同一行必然文本冲突；F1 改完后 F2 的「全仓 `resident_type` 字面量分类」扫到这里是已归类状态，无需再动。交接时须明说（Task 10）。

安全性：`recompute` 在 `rep_enabled=False` 时第一行就 return 0，本线又不开闸，所以这行改动对生产是零行为变化。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/app/services/reputation_service.py` | Modify | 核心。新增 `gossip_tone` / `evidence_weight` / `vote_trust_delta` / `ScoreRow` / `project` / `describe` / `recommend_credit_min_score` / `CalibrationError`；`recompute` 改为 affinity 驱动 + 人口口径 `is_autonomous` |
| `backend/app/services/election_service.py` | Modify `:53-60` | 删除声誉排序块，候选集回到 SBTI/heat 口径 |
| `backend/app/services/civic_service.py` | Modify `:366-371` | vote-trust 项改调 `reputation_service.vote_trust_delta()` |
| `backend/scripts/rep_calibrate.py` | Create | 只读标定脚本：跑真实分布 → 输出分位数 + 建议 `REP_CREDIT_MIN_SCORE` + 退出码 |
| `backend/tests/test_reputation_service.py` | Modify | 新增 tone/人口口径/project/候选集/vote-trust 用例；删除一条已被推翻的旧用例 |
| `backend/tests/test_rep_calibration.py` | Create | 标定纯函数单测 + 脚本渲染单测 + **真实机制驱动的分布 harness** |

---

## Task 0: worktree 与基线

**Files:** 无（环境准备，不提交）

**Interfaces:**
- Consumes: master `fc60ac2` 之后的当前 master
- Produces: `/tmp/f1-base.txt`（基线失败集，Task 10 做双向差集用）

- [ ] **Step 1: 建 worktree**

```bash
cd /Volumes/data/dev/simverse-world
git worktree add -b feat/f1-reputation-semantics .worktrees/f1-reputation master
```

- [ ] **Step 2: 路径守卫（逐字照抄，不通过就停）**

worktree 里**不建 `.venv`、不建软链**，直接用主仓 venv 的绝对路径解释器；`import app` 由 **cwd** 决定（`python -c` / `python -m` 的 `sys.path[0]` 就是 cwd，已实测），所以只要 cwd 在 worktree 的 `backend/` 下，导入的就是 worktree 的代码。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f1-reputation/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -c "import app; p=app.__file__; assert '.worktrees/' in p, f'WRONG: {p}'; print('OK',p)"
```

Expected: `OK /Volumes/data/dev/simverse-world/.worktrees/f1-reputation/backend/app/__init__.py`

> **不要"顺手"在 worktree 里建 `.venv` 软链。** `.gitignore:22` 的 `.venv/` 带尾斜杠 = **只匹配目录**，而 git 把符号链接记为 blob 不是目录，所以软链**不会**被忽略，`git status --short` 会冒出 `?? .venv`，Step 3 与 Task 10 Step 4 的"必须为空"硬门会直接卡死（git 2.50.1 实测）。反过来把 `.venv` 建成真目录、里面软链 `bin`，git 是干净了，但 python 找不到 `pyvenv.cfg`、`sys.prefix` 掉回系统解释器、`import fastapi` 直接 ModuleNotFoundError（同样实测）。绝对路径是唯一两头都干净的写法——本计划所有 Run 命令因此都写全路径。

- [ ] **Step 3: 确认没有 .env（有就删，它会破坏 conftest 隔离）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f1-reputation/backend
ls -la .env 2>/dev/null && echo "FATAL: 删掉它" || echo "OK: no .env"
git status --short      # 必须为空
```

Expected: `OK: no .env`，`git status --short` 无输出。

- [ ] **Step 4: 捕获基线失败集**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f1-reputation/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/ -q -p no:randomly 2>&1 \
  | grep -E "^(FAILED|ERROR) " | sed 's/\[.*//' | sort -u > /tmp/f1-base.txt
wc -l /tmp/f1-base.txt
```

Expected: 约 68 行（51 failed + 17 errors 的并集，去掉参数化后缀）。数字不必精确匹配，这份文件本身就是基线。**后续所有 Run 命令都在 `<worktree>/backend` 下执行**——解释器写的是主仓 venv 的绝对路径，导入哪份 `app` 完全由 cwd 决定，cwd 走错就会静默测到 master 的代码。

---

## Task 1: tone 纯函数（affinity 驱动）

**Files:**
- Modify: `backend/app/services/reputation_service.py`（在 `credit_allowed` 之后插入）
- Test: `backend/tests/test_reputation_service.py`

**Interfaces:**
- Consumes: `settings.rep_gossip_base_tone` / `rep_distortion_penalty` / `rep_min` / `rep_max`
- Produces:
  - `GOSSIP_AFFINITY_WEIGHT: float`（模块常量 = 3.0）
  - `gossip_tone(affinity: float | None, *, distorted: bool = False) -> float`
  - `evidence_weight(importance: float | None, hops: int, tone: float) -> float`

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_reputation_service.py` 末尾（并把顶部 import 块改成下面这份）：

```python
"""S1-1 public reputation regression tests."""
import pytest

from app.config import settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import election_service
from app.services.reputation_service import (
    credit_allowed,
    evidence_weight,
    get_many,
    gossip_tone,
    recompute,
    score_from_meta,
)
```

```python
# ── F1 第 1 项：tone 由关系 affinity 决定 ──────────────────────────────


def test_gossip_tone_follows_affinity_sign():
    assert gossip_tone(0.2) > 0
    assert gossip_tone(-0.2) < 0
    assert gossip_tone(0.5) > gossip_tone(0.1) > gossip_tone(-0.1)


def test_gossip_tone_without_relation_keeps_the_legacy_constant():
    # 无关系行 / affinity=0 → 与修复前逐字节相同，base_tone 退化为偏置项
    assert gossip_tone(None) == settings.rep_gossip_base_tone
    assert gossip_tone(0.0) == settings.rep_gossip_base_tone
    assert gossip_tone("nonsense") == settings.rep_gossip_base_tone


def test_gossip_tone_applies_distortion_penalty_and_clamps():
    assert gossip_tone(0.0, distorted=True) == pytest.approx(
        settings.rep_gossip_base_tone + settings.rep_distortion_penalty
    )
    assert gossip_tone(1.0) == settings.rep_max
    assert gossip_tone(-1.0, distorted=True) == settings.rep_min


def test_evidence_weight_damps_by_hops_and_floors_importance():
    assert evidence_weight(0.6, 0, -0.5) == pytest.approx(-0.3)
    assert evidence_weight(0.6, 3, -0.5) == pytest.approx(-0.075)
    assert evidence_weight(0.6, 0, 0.5) == pytest.approx(0.3)
    assert evidence_weight(-1.0, 0, 0.5) == 0.0
    assert evidence_weight(None, 0, 0.5) == 0.0
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly`

Expected: FAIL，收集阶段就报 `ImportError: cannot import name 'evidence_weight' from 'app.services.reputation_service'`

- [ ] **Step 3: 写最小实现**

在 `backend/app/services/reputation_service.py` 的 `credit_allowed` 函数之后插入：

```python
#: 八卦语气对关系 affinity 的权重。本线不改 config.py（批次规则 §1-6），先落成
#: 模块常量；``getattr`` 间接读使收口时「加一行 rep_gossip_affinity_weight」成为
#: 纯配置改动、代码零 diff。取 3.0 的依据：在冻结的 base_tone=-0.3 下，符号翻转
#: 点落在 affinity=+0.1 —— 恰是一次送礼/投资（realism_rel_affinity_gift=0.1）或
#: 约 4 次正向闲聊（realism_rel_affinity_chat=0.03）的增量。
GOSSIP_AFFINITY_WEIGHT = 3.0


def _affinity_weight() -> float:
    return float(getattr(settings, "rep_gossip_affinity_weight", GOSSIP_AFFINITY_WEIGHT))


def gossip_tone(affinity: float | None, *, distorted: bool = False) -> float:
    """一条传闻的语气 = 传话人对当事人的态度。

    ``affinity`` 取 ``resident_relations`` 上「记忆持有者 × 被议论者」这一对的
    质量轴（``[-1, 1]``，规则驱动零 LLM）。二人无往来（无关系行）时退化为
    ``rep_gossip_base_tone`` —— 修复前那个恒定负值现在只是**偏置项**。
    """
    try:
        value = 0.0 if affinity is None else float(affinity)
    except (TypeError, ValueError):
        value = 0.0
    value = max(-1.0, min(1.0, value))
    tone = settings.rep_gossip_base_tone + _affinity_weight() * value
    if distorted:
        tone += settings.rep_distortion_penalty
    return max(settings.rep_min, min(settings.rep_max, tone))


def evidence_weight(importance: float | None, hops: int, tone: float) -> float:
    """单条传闻的贡献：重要性加权的语气，按传播跳数衰减。"""
    try:
        weight = max(0.0, float(importance or 0.0))
    except (TypeError, ValueError):
        weight = 0.0
    try:
        damping = 1.0 + max(0, int(hops))
    except (TypeError, ValueError):
        damping = 1.0
    return weight * tone / damping
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly`

Expected: PASS（8 passed —— 原有 4 条 + 新增 4 条）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reputation_service.py backend/tests/test_reputation_service.py
git commit -m "$(cat <<'EOF'
feat(reputation): tone 由关系 affinity 决定,base_tone 退为偏置项

F1 第 1 项的纯函数层。gossip_tone(affinity) 在 affinity=0 时逐字节回落到
rep_gossip_base_tone,所以现存回归用例不用改;affinity>0 首次能产出正向语气。
权重先落模块常量 GOSSIP_AFFINITY_WEIGHT=3.0(本线不改 config.py),getattr 间
接读使收口提升为 REP_GOSSIP_AFFINITY_WEIGHT 时代码零 diff。

Verified-by: <粘贴 Step 4 的真实输出>
EOF
)"
```

---

## Task 2: 把 tone 接进 recompute（批量读关系）

**Files:**
- Modify: `backend/app/services/reputation_service.py`（`recompute` 整个函数替换；原 `:68-127`，Task 1 插入后行号已下移，按函数名定位）
- Test: `backend/tests/test_reputation_service.py`

**Interfaces:**
- Consumes: `gossip_tone(...)`、`evidence_weight(...)`（Task 1）；`relation_service.canonical_pair(id1, id2, type1="resident", type2="resident") -> tuple[str, str, str, str]`（返回 `(party_a, party_a_type, party_b, party_b_type)`，已核实 `app/services/relation_service.py:50-57`）；`ResidentRelation.party_a / party_b / affinity`（`app/models/resident_relation.py:37-42`）
- Produces:
  - `ScoreRow`（frozen dataclass：`resident_id: str`、`slug: str`、`previous: float`、`score: float`、`samples: int`）
  - `_scored_residents(db) -> list[Resident]`
  - `_affinity_lookup(db, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], float]`
  - `_score_all(db, residents: list[Resident]) -> list[ScoreRow]`
  - `recompute(db) -> int`（签名不变）

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_reputation_service.py`（并在顶部 import 块加一行 `from app.services import relation_service`）：

```python
# ── F1 第 1 项：接进 recompute ─────────────────────────────────────────


@pytest.mark.anyio
async def test_recompute_tone_follows_relation_affinity(db_session, monkeypatch):
    """同一个传话人、同样的 importance/hops,只有 affinity 不同 → 分数异号。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    teller = _resident("teller")
    liked = _resident("liked")
    disliked = _resident("disliked")
    db_session.add_all([teller, liked, disliked])
    await db_session.flush()
    db_session.add_all([
        Memory(
            resident_id=teller.id, type="event", content="about liked",
            importance=0.7, source="gossip", related_resident_id=liked.id,
            metadata_json={"hops": 1, "distorted": False},
        ),
        Memory(
            resident_id=teller.id, type="event", content="about disliked",
            importance=0.7, source="gossip", related_resident_id=disliked.id,
            metadata_json={"hops": 1, "distorted": False},
        ),
    ])
    await db_session.commit()
    await relation_service.bump(db_session, teller.id, liked.id, d_affinity=0.4)
    await relation_service.bump(db_session, teller.id, disliked.id, d_affinity=-0.4)

    assert await recompute(db_session) == 3
    await db_session.refresh(liked)
    await db_session.refresh(disliked)
    assert score_from_meta(liked.meta_json) > 0      # 正面互动 → 正分
    assert score_from_meta(disliked.meta_json) < 0   # 负面互动 → 负分


@pytest.mark.anyio
async def test_recompute_reads_relations_in_one_batch(db_session, monkeypatch):
    """性能红线:关系读取必须是批量的,不能每条记忆一次查询。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    teller = _resident("batch_teller")
    subjects = [_resident(f"batch_sub{i}") for i in range(5)]
    db_session.add_all([teller, *subjects])
    await db_session.flush()
    for subject in subjects:
        db_session.add(Memory(
            resident_id=teller.id, type="event", content="x",
            importance=0.7, source="gossip", related_resident_id=subject.id,
            metadata_json={"hops": 1, "distorted": False},
        ))
    await db_session.commit()
    for subject in subjects:
        await relation_service.bump(db_session, teller.id, subject.id, d_affinity=0.4)

    calls = {"n": 0}
    original = db_session.execute

    async def counting_execute(statement, *args, **kwargs):
        # 用编译后的 SQL 文本判定,不碰 Select.froms(1.4.23 起 deprecated)
        if "resident_relations" in str(statement):
            calls["n"] += 1
        return await original(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", counting_execute)
    assert await recompute(db_session) == 6
    assert calls["n"] == 1, f"关系查询 {calls['n']} 次,应为 1 次批量读"
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly -k "affinity or batch"`

Expected: FAIL —— `test_recompute_tone_follows_relation_affinity` 断言 `score_from_meta(liked.meta_json) > 0` 失败（现状 tone 恒为 -0.3，两人都是负分）；`test_recompute_reads_relations_in_one_batch` 断言 `calls['n'] == 1` 失败（现状为 0，根本不读关系）。

- [ ] **Step 3: 写最小实现**

3a. 在 `backend/app/services/reputation_service.py` 顶部 import 区补两行（放在现有 `from app.models.resident import Resident` 之后；`relation_service` 只 import `app.config` 与模型，无循环导入风险）：

```python
from app.models.resident_relation import ResidentRelation
from app.services.relation_service import canonical_pair
```

以及文件最上方 `from datetime import UTC, datetime` 之后加：

```python
from dataclasses import dataclass
```

3b. 把 `recompute`（现 `:68-127` 整个函数）替换为下面这一段（`ScoreRow` 与三个 helper 放在 `recompute` 之前）：

```python
@dataclass(frozen=True)
class ScoreRow:
    """一个居民的一次声誉投影结果（不含写入）。"""

    resident_id: str
    slug: str
    previous: float
    score: float
    samples: int


async def _scored_residents(db: AsyncSession) -> list[Resident]:
    """声誉是社会属性不是政治权利 → 人口口径 ``is_autonomous``（spec §4.4）。"""
    return list((await db.execute(
        select(Resident).where(Resident.is_autonomous)
    )).scalars().all())


async def _affinity_lookup(
    db: AsyncSession, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], float]:
    """一次批量读，把 canonical pair 映射到 affinity。

    ``ids`` 的规模上界是小镇人口（传话人与被议论者都是 residents 行），Postgres
    的绑定参数上限 65535 远在其上；测试用的 sqlite 只有几十行。
    """
    if not pairs:
        return {}
    ids = sorted({party for pair in pairs for party in pair})
    rows = (await db.execute(
        select(ResidentRelation).where(
            ResidentRelation.party_a.in_(ids),
            ResidentRelation.party_b.in_(ids),
        )
    )).scalars().all()
    return {(row.party_a, row.party_b): float(row.affinity or 0.0) for row in rows}


async def _score_all(db: AsyncSession, residents: list[Resident]) -> list[ScoreRow]:
    """三次批量读（居民已由调用方读入 / 记忆 / 关系），零 LLM，纯规则。"""
    ids = [resident.id for resident in residents]
    memories = (await db.execute(
        select(Memory).where(
            Memory.source == "gossip",
            Memory.related_resident_id.in_(ids),
            Memory.archived_at.is_(None),
        )
    )).scalars().all()

    pairs: set[tuple[str, str]] = set()
    for memory in memories:
        if memory.resident_id and memory.related_resident_id:
            party_a, _, party_b, _ = canonical_pair(
                memory.resident_id, memory.related_resident_id
            )
            pairs.add((party_a, party_b))
    affinity_by_pair = await _affinity_lookup(db, pairs)

    evidence: dict[str, list[float]] = {resident_id: [] for resident_id in ids}
    for memory in memories:
        metadata = memory.metadata_json or {}
        try:
            hops = max(0, int(metadata.get("hops", 0)))
        except (TypeError, ValueError):
            hops = 0
        affinity = 0.0
        if memory.resident_id:
            party_a, _, party_b, _ = canonical_pair(
                memory.resident_id, memory.related_resident_id
            )
            affinity = affinity_by_pair.get((party_a, party_b), 0.0)
        tone = gossip_tone(affinity, distorted=metadata.get("distorted") is True)
        evidence[memory.related_resident_id].append(
            evidence_weight(memory.importance, hops, tone)
        )

    alpha = max(0.0, min(1.0, settings.rep_ema_alpha))
    rows: list[ScoreRow] = []
    for resident in residents:
        samples = evidence[resident.id]
        mood = resident.mood_json or {}
        try:
            mood_valence = float(mood.get("valence", 0.0))
        except (TypeError, ValueError):
            mood_valence = 0.0
        gossip_signal = sum(samples) / len(samples) if samples else 0.0
        raw = settings.rep_mood_weight * mood_valence + gossip_signal
        previous = score_from_meta(resident.meta_json)
        rows.append(ScoreRow(
            resident_id=resident.id,
            slug=resident.slug,
            previous=previous,
            score=_clamp((1.0 - alpha) * previous + alpha * raw),
            samples=len(samples),
        ))
    return rows


async def recompute(db: AsyncSession) -> int:
    """Recompute every simulated resident's slow reputation projection."""
    if not settings.rep_enabled:
        return 0

    residents = await _scored_residents(db)
    if not residents:
        return 0

    rows = {row.resident_id: row for row in await _score_all(db, residents)}
    now = datetime.now(UTC).isoformat()
    for resident in residents:
        row = rows[resident.id]
        meta = dict(resident.meta_json or {})
        meta["reputation"] = {
            "score": row.score,
            "updated_at": now,
            "samples": row.samples,
        }
        resident.meta_json = meta
        flag_modified(resident, "meta_json")

    await db.commit()
    return len(residents)
```

注意：`_scored_residents` 里的 `is_autonomous` 就是 Task 3 要验收的人口口径改动——这里一次写对，Task 3 只补测试与断言（见 Task 3 Step 2 的说明）。

3c. 更新模块 docstring 第一段末尾（原文「V1 stores the projection ...」段落之后）追加一句：

```
Tone is not a constant: each rumor is read through the relation affinity between
the memory's holder and its subject, so a well-liked resident accrues positive
evidence. ``rep_gossip_base_tone`` is only the bias for an unknown pair.
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly`

Expected: PASS（10 passed）。特别确认旧用例 `test_recompute_uses_gossip_distortion_hops_and_mood` 仍绿——它的三个居民之间没有关系行，affinity=0，tone 与修复前逐字节相同。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reputation_service.py backend/tests/test_reputation_service.py
git commit -m "$(cat <<'EOF'
feat(reputation): recompute 用关系 affinity 打分,一次批量读

每条 gossip 记忆的持有者与被议论者恰好构成 resident_relations 的一个 canonical
pair,批量取出后喂给 gossip_tone。正向关系首次能产出正分,「没人议论得 0 分反而
全镇最高」的单边性消失。打分逻辑抽成 _score_all(),为只读标定路径复用做准备。

Verified-by: <粘贴 Step 4 的真实输出>
EOF
)"
```

---

## Task 3: 人口口径 `is_autonomous`（spec §4.4 第 11 处读点）

**Files:**
- Modify: `backend/app/services/reputation_service.py`（`_scored_residents`，Task 2 已写成 `is_autonomous`）
- Test: `backend/tests/test_reputation_service.py`

**Interfaces:**
- Consumes: `Resident.is_autonomous`（hybrid，`app/models/resident.py:92-111`，表达式形式 `resident_type.in_(SIM_RESIDENT_TYPES)`）；`app.services.civic_membership.UGC_RESIDENT_TYPE == "resident"`
- Produces: 无新接口，只锁定行为

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_reputation_service.py`：

```python
@pytest.mark.anyio
async def test_recompute_covers_ugc_residents_but_never_the_player_avatar(
    db_session, monkeypatch
):
    """spec §4.4 第 11 处读点:声誉是社会属性,人口口径不是政治口径。

    不改的后果是被降级者退出夜间重算、分数永久冻结在降级前那一刻。
    """
    from app.services.civic_membership import UGC_RESIDENT_TYPE

    monkeypatch.setattr(settings, "rep_enabled", True)
    builtin = _resident("builtin")
    ugc = _resident("ugc")
    ugc.resident_type = UGC_RESIDENT_TYPE
    avatar = _resident("avatar")
    avatar.resident_type = "player"
    db_session.add_all([builtin, ugc, avatar])
    await db_session.commit()

    assert await recompute(db_session) == 2
    await db_session.refresh(ugc)
    await db_session.refresh(avatar)
    assert "reputation" in (ugc.meta_json or {})
    assert "reputation" not in (avatar.meta_json or {})
```

- [ ] **Step 2: 跑测试确认它失败**

Task 2 已经把口径一次写对（`is_autonomous`），所以这条用例现在是绿的。为保证红→绿链条真实，**临时把口径改回裸字面量**，确认用例确实锁住了这个行为：

```bash
/Volumes/data/dev/simverse-world/backend/.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("app/services/reputation_service.py")
s = p.read_text(encoding="utf-8")
p.write_text(s.replace("select(Resident).where(Resident.is_autonomous)",
                       'select(Resident).where(Resident.resident_type == "npc")'), encoding="utf-8")
PY
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly -k ugc
```

Expected: FAIL，`assert 1 == 2`（UGC 居民被排除在夜间重算之外）。

- [ ] **Step 3: 恢复实现**

```bash
/Volumes/data/dev/simverse-world/backend/.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("app/services/reputation_service.py")
s = p.read_text(encoding="utf-8")
p.write_text(s.replace('select(Resident).where(Resident.resident_type == "npc")',
                       "select(Resident).where(Resident.is_autonomous)"), encoding="utf-8")
PY
git diff --stat            # 必须只剩测试文件的新增,服务文件应无残留 diff
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly`

Expected: PASS（11 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_reputation_service.py
git commit -m "$(cat <<'EOF'
test(reputation): 锁住人口口径 is_autonomous(spec §4.4 第 11 处读点)

裸 resident_type == "npc" 会让被降级者退出夜间声誉重算、分数永久冻结在降级前
那一刻,而候选排序读的正是这个冻结值。该行由 F1 修(文件归 F1 独占,F2 再改同
一行必然冲突);F2 的 resident_type 字面量分类扫到这里应为已归类状态。

Verified-by: <粘贴 Step 2 的红 + Step 4 的绿>
EOF
)"
```

---

## Task 4: `project()` 只读投影

**Files:**
- Modify: `backend/app/services/reputation_service.py`（在 `_score_all` 之后、`recompute` 之前插入）
- Test: `backend/tests/test_reputation_service.py`

**Interfaces:**
- Consumes: `_scored_residents(db)`、`_score_all(db, residents)`、`ScoreRow`（Task 2）
- Produces: `project(db: AsyncSession, *, force: bool = False) -> list[ScoreRow]` —— 只读；`force=True` 绕过 `rep_enabled` 闸门（标定脚本用，开闸前也能读到真实分布）

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_reputation_service.py`（顶部 import 块加 `project`）：

```python
@pytest.mark.anyio
async def test_project_is_read_only_and_matches_recompute(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", False)
    subject = _resident("proj_subject")
    teller = _resident("proj_teller")
    db_session.add_all([subject, teller])
    await db_session.flush()
    db_session.add(Memory(
        resident_id=teller.id, type="event", content="x",
        importance=0.7, source="gossip", related_resident_id=subject.id,
        metadata_json={"hops": 1, "distorted": False},
    ))
    await db_session.commit()

    assert await project(db_session) == []            # 闸门关且未 force → 空
    rows = await project(db_session, force=True)      # 标定路径:开闸前也能读
    assert {row.slug for row in rows} == {"proj_subject", "proj_teller"}
    await db_session.refresh(subject)
    assert "reputation" not in (subject.meta_json or {})   # 只读,零写入

    monkeypatch.setattr(settings, "rep_enabled", True)
    projected = {row.resident_id: row.score for row in await project(db_session)}
    assert await recompute(db_session) == 2
    await db_session.refresh(subject)
    assert score_from_meta(subject.meta_json) == pytest.approx(projected[subject.id])
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly -k project`

Expected: FAIL，`ImportError: cannot import name 'project' from 'app.services.reputation_service'`

- [ ] **Step 3: 写最小实现**

```python
async def project(db: AsyncSession, *, force: bool = False) -> list[ScoreRow]:
    """只读投影：算出「今晚会写成什么」但一个字节都不落库。

    ``force=True`` 绕过 ``rep_enabled``，让开闸前的标定（``scripts/rep_calibrate.py``）
    能读到真实分布。与 ``recompute`` 共用 ``_score_all``，因此标定口径和夜间任务
    永远不会漂移。
    """
    if not (force or settings.rep_enabled):
        return []
    residents = await _scored_residents(db)
    if not residents:
        return []
    return await _score_all(db, residents)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly`

Expected: PASS（12 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reputation_service.py backend/tests/test_reputation_service.py
git commit -m "$(cat <<'EOF'
feat(reputation): 加 project() 只读投影,供开闸前标定

与 recompute 共用 _score_all,标定口径不可能和夜间任务漂移;force=True 让
REP_ENABLED=false 的生产库也能被读出真实分布(第 3 项重标定的前提)。

Verified-by: <粘贴 Step 4 的真实输出>
EOF
)"
```

---

## Task 5: 候选集去截断（election_service）

**Files:**
- Modify: `backend/app/services/election_service.py:53-61`
- Test: `backend/tests/test_reputation_service.py`（删 1 条旧用例、加 2 条新用例）

**Interfaces:**
- Consumes: `election_service.open_election(db, *, candidate_slugs: list[str] | None = None, days: int | None = None) -> Poll | None`（已核实签名，`election_service.py:32`）
- Produces: 候选集与 `settings.rep_enabled` 无关的 `open_election`

- [ ] **Step 1: 删掉被推翻的旧用例，写新的失败测试**

删除 `backend/tests/test_reputation_service.py` 里整条 `test_open_election_ranks_reputation_when_enabled`（它断言的正是本线要废除的行为：按声誉排序决定谁在 ballot 上）。追加：

```python
# ── F1 第 2 项：候选集由截断改为加权 ────────────────────────────────────


@pytest.mark.anyio
async def test_open_election_keeps_low_reputation_candidates_on_the_ballot(
    db_session, monkeypatch
):
    """硬门:候选集不得因「被议论多」而缩小。被动选举权不因名声受损而剥夺。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    worst = _resident("worst", reputation=-0.9)
    others = [_resident(f"cand{i}", reputation=0.5) for i in range(4)]
    db_session.add_all([worst, *others])
    await db_session.commit()

    # 显式候选名单:顺序由调用方决定,不依赖 DB 行序
    poll = await election_service.open_election(
        db_session,
        candidate_slugs=["worst", "cand0", "cand1", "cand2", "cand3"],
    )
    slugs = [option["effect"]["slug"] for option in poll.options_json]
    assert slugs == ["worst", "cand0", "cand1", "cand2"]   # [:4] 保留,顺序即入参顺序


@pytest.mark.anyio
async def test_open_election_candidate_set_is_reputation_blind(db_session, monkeypatch):
    """开闸前后同一个世界,候选集必须逐项相同。"""
    db_session.add_all([
        _resident("blind_low", reputation=-0.9),
        _resident("blind_mid", reputation=0.0),
        _resident("blind_high", reputation=0.9),
    ])
    await db_session.commit()

    monkeypatch.setattr(settings, "rep_enabled", True)
    on = [o["effect"]["slug"] for o in (await election_service.open_election(db_session)).options_json]
    monkeypatch.setattr(settings, "rep_enabled", False)
    off = [o["effect"]["slug"] for o in (await election_service.open_election(db_session)).options_json]
    assert on == off
    assert set(on) == {"blind_low", "blind_mid", "blind_high"}
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly -k election`

Expected: FAIL —— `test_open_election_keeps_low_reputation_candidates_on_the_ballot` 得到 `['cand0','cand1','cand2','cand3']`（`worst` 被声誉排序挤出 `[:4]`）；`test_open_election_candidate_set_is_reputation_blind` 的 `on` 为 `['blind_high','blind_mid','blind_low']` 而 `off` 为插入顺序 → 不等。

- [ ] **Step 3: 写最小实现**

`backend/app/services/election_service.py` 中删除这 8 行（现 `:53-60`）：

```python
    if settings.rep_enabled:
        from app.services.reputation_service import score_from_meta
        # Stable sort: equal reputation preserves the existing SBTI/heat order.
        candidates = sorted(
            candidates,
            key=lambda resident: score_from_meta(resident.meta_json),
            reverse=True,
        )
```

替换为注释（`candidates = candidates[:4]` 保持原样紧随其后）：

```python
    # F1 第 2 项:声誉**不参与**候选集选取。此处曾按声誉排序再 [:4],等于把「被
    # 议论最多、叙事最中心」的居民系统性挤出候选(tone 恒为负 → 被议论就扣分)。
    # 被动选举权不因名声受损而剥夺;声誉的唯一入票通道是 civic_service._npc_choice
    # 里的 vote_trust_delta(),影响得票而不决定谁能参选。
    # 候选集口径回到 S1-1 之前:SBTI(Ac1/So1=H)优先,不足 2 人回落 heat 前三。
    candidates = candidates[:4]
```

本文件不需要其它改动：`score_from_meta` 的 import 是那个块的局部 import，随块一起消失；`settings` 仍被 `election_enabled` 等使用，不要动 import 区。

- [ ] **Step 4: 跑测试确认通过**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py tests/test_m6_election.py tests/test_m3_civic.py -q -p no:randomly`

Expected: PASS（`test_m6_election.py` / `test_m3_civic.py` 全绿——它们不设 `rep_enabled`，默认 False，本改动对它们是零行为变化）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/election_service.py backend/tests/test_reputation_service.py
git commit -m "$(cat <<'EOF'
fix(election): 候选集不再按声誉截断,被动选举权与名声解耦

election_service.py:53-60 原来「按声誉排序后 [:4]」,叠加恒负的八卦 tone,后果
是被议论最多的居民被系统性挤出候选、路人当选。删掉排序块后候选集逐字节回到
S1-1 之前的 SBTI/heat 口径([:4] 出自 dde187c5,早于声誉功能)。删除被推翻的旧
用例 test_open_election_ranks_reputation_when_enabled。

Verified-by: <粘贴 Step 4 的真实输出>
EOF
)"
```

---

## Task 6: 声誉的唯一入票通道 `vote_trust_delta`

**Files:**
- Modify: `backend/app/services/reputation_service.py`（在 `credit_allowed` 之后、`gossip_tone` 之前插入）
- Modify: `backend/app/services/civic_service.py:366-371`
- Test: `backend/tests/test_reputation_service.py`

**Interfaces:**
- Consumes: `score_from_meta(meta_json: dict | None) -> float`；`settings.rep_vote_trust_weight`；`civic_service._npc_choice(db, resident, poll, opts, relation_service, by_slug=None) -> int`（已核实签名，`civic_service.py:280`）
- Produces: `vote_trust_delta(meta_json: dict | None) -> float`

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_reputation_service.py`（顶部 import 块加 `vote_trust_delta`）：

```python
# ── F1 第 2 项：声誉只影响得票 ──────────────────────────────────────────


def test_vote_trust_delta_is_gated_and_weighted(monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", False)
    assert vote_trust_delta({"reputation": {"score": 0.8}}) == 0.0
    monkeypatch.setattr(settings, "rep_enabled", True)
    monkeypatch.setattr(settings, "rep_vote_trust_weight", 2.0)
    assert vote_trust_delta({"reputation": {"score": 0.4}}) == pytest.approx(0.8)
    assert vote_trust_delta(None) == pytest.approx(2.0 * settings.rep_neutral)


@pytest.mark.anyio
async def test_reputation_moves_votes_not_candidacy(db_session, monkeypatch):
    """回归锁:声誉在候选集上失效之后,得票权重是它唯一的政治通道。"""
    from types import SimpleNamespace

    from app.services import civic_service, relation_service

    voter = _resident("vote_caster")
    good = _resident("vote_good", reputation=0.9)
    bad = _resident("vote_bad", reputation=-0.9)
    db_session.add_all([voter, good, bad])
    await db_session.commit()

    poll = SimpleNamespace(question="镇长选举:谁来当下一任镇长?", id=1)
    opts = [
        {"label": "bad", "effect": {"type": "mayor", "slug": "vote_bad"}},
        {"label": "good", "effect": {"type": "mayor", "slug": "vote_good"}},
    ]
    by_slug = {"vote_caster": voter, "vote_good": good, "vote_bad": bad}

    monkeypatch.setattr(settings, "rep_enabled", True)
    idx = await civic_service._npc_choice(
        db_session, voter, poll, opts, relation_service, by_slug
    )
    assert opts[idx]["effect"]["slug"] == "vote_good"

    monkeypatch.setattr(settings, "rep_enabled", False)
    idx_off = await civic_service._npc_choice(
        db_session, voter, poll, opts, relation_service, by_slug
    )
    good.meta_json = {**good.meta_json, "reputation": {"score": -0.9, "samples": 1}}
    bad.meta_json = {**bad.meta_json, "reputation": {"score": 0.9, "samples": 1}}
    idx_off_swapped = await civic_service._npc_choice(
        db_session, voter, poll, opts, relation_service, by_slug
    )
    assert idx_off_swapped == idx_off   # 闸门关 → 声誉一个字节都到不了选票
```

诚实标注：第二条用例在改动前后都应为绿，它锁的是「声誉唯一入票通道」这一语义；本 Task 真正的红来自第一条（`vote_trust_delta` 尚不存在）。

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py -q -p no:randomly -k "vote"`

Expected: FAIL，`ImportError: cannot import name 'vote_trust_delta' from 'app.services.reputation_service'`

- [ ] **Step 3: 写最小实现**

3a. `backend/app/services/reputation_service.py`，在 `credit_allowed` 之后插入：

```python
def vote_trust_delta(meta_json: dict | None) -> float:
    """声誉进入一张选票的**唯一**通道（F1 第 2 项）。

    候选集选取已与声誉解耦（``election_service.open_election``），名声只在
    ``civic_service._npc_choice`` 的打分里当一项权重。闸门关时返回 0.0，加到
    分数上是逐字节无影响。
    """
    if not settings.rep_enabled:
        return 0.0
    return settings.rep_vote_trust_weight * score_from_meta(meta_json)
```

3b. `backend/app/services/civic_service.py:366-371`，把

```python
        if settings.rep_enabled:
            from app.services.reputation_service import score_from_meta
            scores[i] += (
                settings.rep_vote_trust_weight
                * score_from_meta(other.meta_json)
            )
```

替换为

```python
        # F1 第 2 项:声誉只在这里影响选票。候选集选取已与声誉解耦
        # (election_service.open_election),被动选举权不因名声受损而剥夺。
        # 闸门关时 vote_trust_delta 返回 0.0 → 逐字节等价于改动前。
        from app.services.reputation_service import vote_trust_delta
        scores[i] += vote_trust_delta(other.meta_json)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_reputation_service.py tests/test_m3_civic.py tests/test_burnin_report_npc_vote.py -q -p no:randomly`

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reputation_service.py backend/app/services/civic_service.py backend/tests/test_reputation_service.py
git commit -m "$(cat <<'EOF'
refactor(civic): 声誉入票收敛到 vote_trust_delta(),闸门关时零影响

候选集去截断之后,得票权重成为声誉唯一的政治通道;把权重与读分收进一个函数,
收口重调 REP_VOTE_TRUST_WEIGHT 时只有一处要看。

Verified-by: <粘贴 Step 4 的真实输出>
EOF
)"
```

---

## Task 7: 标定纯函数（分布 + 阈值建议）

**Files:**
- Modify: `backend/app/services/reputation_service.py`（追加到文件末尾）
- Test: `backend/tests/test_rep_calibration.py`（新建）

**Interfaces:**
- Consumes: 无（纯函数）
- Produces:
  - `class CalibrationError(RuntimeError)`
  - `describe(scores: Sequence[float]) -> dict[str, float]` —— 键：`n` / `min` / `p10` / `p25` / `median` / `p75` / `p90` / `max` / `mean` / `negative_share`
  - `recommend_credit_min_score(scores: Sequence[float], reject_fraction: float = 0.15) -> float`

- [ ] **Step 1: 写失败的测试**

新建 `backend/tests/test_rep_calibration.py`：

```python
"""F1 第 3 项：rep_credit_min_score 重标定。

纯函数单测在此；用真实机制跑出分布的 harness 在本文件下半部分（Task 9）。
"""
import pytest

from app.services.reputation_service import (
    CalibrationError,
    describe,
    recommend_credit_min_score,
)


def test_describe_reports_the_shape_of_the_distribution():
    stats = describe([-0.2, -0.1, 0.0, 0.1, 0.2])
    assert stats["n"] == 5
    assert stats["min"] == pytest.approx(-0.2)
    assert stats["max"] == pytest.approx(0.2)
    assert stats["median"] == pytest.approx(0.0)
    assert stats["p25"] == pytest.approx(-0.1)
    assert stats["p75"] == pytest.approx(0.1)
    assert stats["mean"] == pytest.approx(0.0)
    assert stats["negative_share"] == pytest.approx(0.4)


def test_describe_of_an_empty_sample_is_all_zero():
    stats = describe([])
    assert stats["n"] == 0
    assert stats["median"] == 0.0
    assert stats["negative_share"] == 0.0


def test_recommend_cuts_close_to_the_target_reject_fraction():
    scores = [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    threshold = recommend_credit_min_score(scores, 0.2)
    assert threshold == pytest.approx(0.5)
    assert sum(1 for s in scores if s < threshold) == 2


def test_recommend_never_rejects_everyone_or_no_one():
    scores = [-0.31, -0.12, -0.05, 0.0, 0.0, 0.02, 0.09, 0.21]
    for fraction in (0.01, 0.15, 0.5, 0.99):
        threshold = recommend_credit_min_score(scores, fraction)
        rejected = sum(1 for s in scores if s < threshold)
        assert 0 < rejected < len(scores), (fraction, threshold, rejected)


def test_recommend_refuses_degenerate_samples():
    with pytest.raises(CalibrationError, match="degenerate"):
        recommend_credit_min_score([0.0] * 10)
    with pytest.raises(CalibrationError, match="at least 2"):
        recommend_credit_min_score([0.3])
    with pytest.raises(CalibrationError, match="reject_fraction"):
        recommend_credit_min_score([0.0, 1.0], 1.5)
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_rep_calibration.py -q -p no:randomly`

Expected: FAIL，`ImportError: cannot import name 'CalibrationError' from 'app.services.reputation_service'`

- [ ] **Step 3: 写最小实现**

`backend/app/services/reputation_service.py` 顶部 import 区加：

```python
import math
from collections.abc import Sequence
```

文件末尾追加：

```python
# ── F1 第 3 项：用真实分布标定信用阈值 ──────────────────────────────────


class CalibrationError(RuntimeError):
    """样本无法给出一个可用的信用阈值（太少 / 退化）。"""


def _percentile(values_sorted: list[float], q: float) -> float:
    """最近秩分位数（无 numpy 依赖）。``q`` 取 [0, 1]。"""
    if not values_sorted:
        return 0.0
    n = len(values_sorted)
    index = math.ceil(max(0.0, min(1.0, q)) * n) - 1
    return values_sorted[max(0, min(n - 1, index))]


def describe(scores: Sequence[float]) -> dict[str, float]:
    """声誉分分布的形状——标定与探针共用的读数。"""
    values = sorted(float(score) for score in scores)
    if not values:
        return {
            "n": 0, "min": 0.0, "p10": 0.0, "p25": 0.0, "median": 0.0,
            "p75": 0.0, "p90": 0.0, "max": 0.0, "mean": 0.0,
            "negative_share": 0.0,
        }
    return {
        "n": len(values),
        "min": values[0],
        "p10": _percentile(values, 0.10),
        "p25": _percentile(values, 0.25),
        "median": _percentile(values, 0.50),
        "p75": _percentile(values, 0.75),
        "p90": _percentile(values, 0.90),
        "max": values[-1],
        "mean": sum(values) / len(values),
        "negative_share": sum(1 for v in values if v < 0) / len(values),
    }


def recommend_credit_min_score(
    scores: Sequence[float], reject_fraction: float = 0.15
) -> float:
    """给出使拒绝面**非空且非全量**的阈值，尽量贴近 ``reject_fraction``。

    只在相邻的两个**实际出现过的**分值之间取中点，因此返回值必然满足
    ``0 < |{s < T}| < n``——「拒绝面非空」是构造保证，不靠事后断言。
    """
    values = sorted(float(score) for score in scores)
    if len(values) < 2:
        raise CalibrationError(f"need at least 2 scores to calibrate, got {len(values)}")
    if not 0.0 < float(reject_fraction) < 1.0:
        raise CalibrationError(
            f"reject_fraction must be in (0, 1), got {reject_fraction!r}"
        )
    distinct = sorted(set(values))
    if len(distinct) < 2:
        raise CalibrationError(
            f"degenerate distribution: every score == {distinct[0]!r}"
        )
    target = float(reject_fraction) * len(values)
    best_gap: float | None = None
    best_threshold = 0.0
    for low, high in zip(distinct, distinct[1:]):
        threshold = (low + high) / 2.0
        rejected = sum(1 for value in values if value < threshold)
        gap = abs(rejected - target)
        if best_gap is None or gap < best_gap:
            best_gap, best_threshold = gap, threshold
    return best_threshold
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_rep_calibration.py -q -p no:randomly`

Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reputation_service.py backend/tests/test_rep_calibration.py
git commit -m "$(cat <<'EOF'
feat(reputation): 加分布描述与信用阈值建议纯函数

阈值只在实际出现过的相邻分值之间取中点,「拒绝面非空且非全量」是构造保证。
rep_credit_min_score=-0.3 之所以变成装饰性闸门,正是因为它是拍出来的。

Verified-by: <粘贴 Step 4 的真实输出>
EOF
)"
```

---

## Task 8: `scripts/rep_calibrate.py` 只读标定脚本

**Files:**
- Create: `backend/scripts/rep_calibrate.py`
- Test: `backend/tests/test_rep_calibration.py`（追加）

**Interfaces:**
- Consumes: `project(db, force=True) -> list[ScoreRow]`、`describe`、`recommend_credit_min_score`、`CalibrationError`、`ScoreRow`（字段 `resident_id/slug/previous/score/samples`）
- Produces:
  - `build_report(rows: list[ScoreRow], reject_fraction: float, current_threshold: float) -> dict`
  - `render(report: dict) -> str`
  - `main(argv: list[str] | None = None) -> int` —— 退出码 0=可标定且拒绝面非空，2=分布退化/样本不足，3=建议阈值拒绝面为空（不应发生，防御性）

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_rep_calibration.py`：

```python
from app.services.reputation_service import ScoreRow
from scripts.rep_calibrate import build_report, render


def _rows(scores):
    return [
        ScoreRow(resident_id=f"r{i}", slug=f"s{i}", previous=0.0, score=score, samples=i)
        for i, score in enumerate(scores)
    ]


def test_build_report_flags_the_decorative_gate():
    report = build_report(_rows([-0.20, -0.12, -0.05, 0.0, 0.0, 0.03, 0.08, 0.15]),
                          0.25, -0.3)
    assert report["n"] == 8
    assert report["current_rejected"] == 0            # -0.3 谁也拒绝不了
    assert report["recommended"] == pytest.approx(-0.085)
    assert report["recommended_rejected"] == 2
    assert [entry["slug"] for entry in report["lowest"]][0] == "s0"
    text = render(report)
    assert "装饰性闸门" in text
    assert "建议 REP_CREDIT_MIN_SCORE" in text


def test_build_report_handles_a_degenerate_world():
    report = build_report(_rows([0.0, 0.0]), 0.15, -0.3)
    assert report["recommended"] is None
    assert "degenerate" in report["error"]
    assert "无法标定" in render(report)


def test_build_report_handles_an_empty_world():
    report = build_report([], 0.15, -0.3)
    assert report["n"] == 0
    assert report["recommended"] is None
    assert render(report)   # 不炸
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_rep_calibration.py -q -p no:randomly -k report`

Expected: FAIL，`ModuleNotFoundError: No module named 'scripts.rep_calibrate'`

- [ ] **Step 3: 写最小实现**

新建 `backend/scripts/rep_calibrate.py`：

```python
#!/usr/bin/env python3
"""REP_* 信用阈值标定（纯只读，零写入）。

用法（vm212 api 容器内跑，DATABASE_URL 由 deploy compose 注入）::

    docker compose exec api python scripts/rep_calibrate.py --reject-fraction 0.15

本地 / 沙盒（任意 DATABASE_URL）::

    DATABASE_URL=sqlite+aiosqlite:////tmp/f1-calib.db DEBUG=true LLM_API_KEY=x \\
        python scripts/rep_calibrate.py

口径注意：

- 分数走 ``reputation_service.project(db, force=True)``——与夜间 ``recompute``
  共用 ``_score_all``，因此标定值和实际写入的值不可能漂移；``force`` 使
  ``REP_ENABLED=false`` 的生产库在开闸前也能被读出真实分布。
- 本脚本**不写库**：没有 commit，没有 UPDATE。
- ``project`` 算的是**一步 EMA**（从库里现存的 ``previous`` 出发）。开闸前
  ``previous`` 恒为 0，读数因此是稳态值的 ``rep_ema_alpha`` 倍；要看稳态，配合
  ``REP_GOSSIP_BASE_TONE`` 等 env 覆盖多跑几晚 ``recompute`` 后再读。
- 退出码：0=可标定且拒绝面非空；2=样本不足/分布退化；3=建议阈值拒绝面为空。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# `python scripts/rep_calibrate.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.services.reputation_service import (  # noqa: E402
    CalibrationError,
    ScoreRow,
    describe,
    project,
    recommend_credit_min_score,
)

TOP_N = 5


def build_report(
    rows: list[ScoreRow], reject_fraction: float, current_threshold: float
) -> dict:
    """把一组投影结果整理成可打印/可 JSON 的读数（纯函数，供测试直接调用）。"""
    scores = [row.score for row in rows]
    by_score = sorted(rows, key=lambda row: row.score)
    report: dict = {
        "n": len(scores),
        "distribution": describe(scores),
        "current_threshold": float(current_threshold),
        "current_rejected": sum(1 for s in scores if s < current_threshold),
        "reject_fraction": float(reject_fraction),
        "lowest": [
            {"slug": row.slug, "score": row.score, "samples": row.samples}
            for row in by_score[:TOP_N]
        ],
        "highest": [
            {"slug": row.slug, "score": row.score, "samples": row.samples}
            for row in by_score[::-1][:TOP_N]
        ],
    }
    try:
        threshold = recommend_credit_min_score(scores, reject_fraction)
    except CalibrationError as exc:
        report["recommended"] = None
        report["recommended_rejected"] = 0
        report["error"] = str(exc)
        return report
    report["recommended"] = threshold
    report["recommended_rejected"] = sum(1 for s in scores if s < threshold)
    return report


def render(report: dict) -> str:
    d = report["distribution"]
    lines = [
        "== REP 信用阈值标定（只读）==",
        f"样本 n={report['n']}  min={d['min']:+.4f}  p10={d['p10']:+.4f}  "
        f"p25={d['p25']:+.4f}  median={d['median']:+.4f}  p75={d['p75']:+.4f}  "
        f"p90={d['p90']:+.4f}  max={d['max']:+.4f}  mean={d['mean']:+.4f}",
        f"负分占比 {d['negative_share'] * 100:.1f}%",
        f"当前 REP_CREDIT_MIN_SCORE={report['current_threshold']:+.4f} → 拒绝 "
        f"{report['current_rejected']}/{report['n']} 人"
        + ("  ← 装饰性闸门（拒绝面为空）" if report["current_rejected"] == 0 else ""),
    ]
    if report.get("recommended") is None:
        lines.append(f"建议值：无法标定 — {report.get('error', '样本为空')}")
    else:
        lines.append(
            f"建议 REP_CREDIT_MIN_SCORE={report['recommended']:+.4f}"
            f"（目标拒绝面 {report['reject_fraction'] * 100:.0f}%）→ 拒绝 "
            f"{report['recommended_rejected']}/{report['n']} 人"
        )
    if report["lowest"]:
        lines.append("最低 %d 人: " % len(report["lowest"]) + ", ".join(
            f"{e['slug']}={e['score']:+.4f}(n={e['samples']})" for e in report["lowest"]
        ))
        lines.append("最高 %d 人: " % len(report["highest"]) + ", ".join(
            f"{e['slug']}={e['score']:+.4f}(n={e['samples']})" for e in report["highest"]
        ))
    return "\n".join(lines)


async def _run(reject_fraction: float) -> dict:
    from app.database import async_session

    async with async_session() as db:
        rows = await project(db, force=True)
    return build_report(rows, reject_fraction, settings.rep_credit_min_score)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="REP 信用阈值标定（只读）")
    parser.add_argument("--reject-fraction", type=float, default=0.15,
                        help="目标拒绝面占比（默认 0.15）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = parser.parse_args(argv)
    report = asyncio.run(_run(args.reject_fraction))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    if report.get("recommended") is None:
        return 2
    return 0 if report["recommended_rejected"] > 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_rep_calibration.py -q -p no:randomly`

Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/scripts/rep_calibrate.py backend/tests/test_rep_calibration.py
git commit -m "$(cat <<'EOF'
feat(scripts): 加 rep_calibrate.py —— 用真实分布标定信用阈值(只读)

复用 project()/_score_all,标定口径与夜间 recompute 同源;退出码把「拒绝面为空」
变成可被 CI/运维捕获的失败,而不是一句人眼判断。不写库、不改 config。

Verified-by: <粘贴 Step 4 的真实输出>
EOF
)"
```

---

## Task 9: 真实机制驱动的分布 harness（第 3 项验收）

**Files:**
- Modify: `backend/tests/test_rep_calibration.py`（追加 harness 与验收用例）

**Interfaces:**
- Consumes:
  - `relation_service.bump(db, id1, id2, d_familiarity=0.0, d_affinity=0.0, *, type1="resident", type2="resident", now=None) -> None`（已核实 `relation_service.py:78-88`）
  - `gossip_service.maybe_gossip(db, speaker: Resident, listener: Resident, rng=random) -> Memory | None`（已核实 `gossip_service.py:49`）；模块级 `GOSSIP_PROBABILITY`、`_distort(content) -> str`
  - `recompute(db) -> int`、`score_from_meta`、`credit_allowed`、`describe`、`recommend_credit_min_score`
  - 生产常量：`settings.realism_rel_familiarity_chat=0.05`、`settings.realism_rel_affinity_chat=0.03`（`app/config.py:499-500`）
- Produces: `simulate_world(db, *, cast=CAST, rounds=ROUNDS, seed=SEED) -> list[Resident]`（Task 10 的真实进程验证会 import 它）

- [ ] **Step 1: 写验收 harness**

先把 `backend/tests/test_rep_calibration.py` 的顶部 import 块**整体替换**为下面这份合并版，并**删除 Task 8 留在文件中段的那两行 import**（`from app.services.reputation_service import ScoreRow` 与 `from scripts.rep_calibrate import build_report, render`）——它们已并入下面这份：

```python
"""F1 第 3 项：rep_credit_min_score 重标定。

纯函数单测在上半部分；用真实机制跑出分布的 harness 在下半部分。
"""
import random

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import gossip_service, relation_service
from app.services.reputation_service import (
    CalibrationError,
    ScoreRow,
    credit_allowed,
    describe,
    recompute,
    recommend_credit_min_score,
    score_from_meta,
)
from scripts.rep_calibrate import build_report, render
```

然后追加到文件末尾：

```python
CAST = 12
ROUNDS = 12
SEED = 20260727
#: 收口建议值。本线不改 config.py，测试用 monkeypatch 复现收口取值。
RECOMMENDED_BASE_TONE = -0.05


def _sim_resident(index: int) -> Resident:
    # creator_id 必须是 None:Resident.creator_id 是 ForeignKey("users.id")
    # (app/models/resident.py:27-29,nullable),而 harness 从不建 users 行。
    # sqlite 默认不校验外键所以填什么都"能过",但 Task 10 Step 2 允许把
    # DATABASE_URL 指向 Postgres,填字符串就是 ForeignKeyViolation。
    # 与本文件既有的 _resident 助手(tests/test_reputation_service.py)一致。
    return Resident(
        slug=f"sim{index:02d}", name=f"居民{index:02d}", district="central_plaza",
        status="idle", resident_type="npc", creator_id=None,
        tile_x=70, tile_y=56,
        mood_json={"valence": 0.0, "arousal": 0.2, "label": "calm"},
        meta_json={"sbti": {"dimensions": {"Ac1": "H"}}},
    )


async def simulate_world(db, *, cast: int = CAST, rounds: int = ROUNDS,
                         seed: int = SEED) -> list[Resident]:
    """用**真实机制**跑出一个小镇：关系走 relation_service.bump，传闻走
    gossip_service.maybe_gossip。所有数值都是生产常量（闲聊 familiarity +0.05 /
    affinity ±0.03，app/agent/chat.py:64-68），没有一个分数是手写的。

    调用方须先：settings.realism_relations_enabled=True、
    gossip_service.GOSSIP_PROBABILITY=1.0、stub 掉 gossip_service._distort
    （唯一被替换的是那次 LLM 改写调用，测试不联网）。
    """
    residents = [_sim_resident(i) for i in range(cast)]
    db.add_all(residents)
    await db.commit()

    rng = random.Random(seed)
    random.seed(seed)   # maybe_gossip 直接用模块级 random

    # 一手见闻：每个人手里有一条关于别人的高重要性事件记忆（传闻链的源头）
    for index, resident in enumerate(residents):
        subject = residents[(index + 1) % cast]
        db.add(Memory(
            resident_id=resident.id, type="event",
            content=f"{subject.name}在广场上做了件事",
            importance=0.8, source="observation",
            related_resident_id=subject.id,
        ))
    await db.commit()

    pairs = [(a, b) for a in range(cast) for b in range(a + 1, cast)]
    for _ in range(rounds):
        for a, b in pairs:
            if rng.random() >= 0.5:      # 这轮这两人没碰上
                continue
            positive = rng.random() < 0.65
            await relation_service.bump(
                db, residents[a].id, residents[b].id,
                d_familiarity=settings.realism_rel_familiarity_chat,
                d_affinity=(settings.realism_rel_affinity_chat if positive
                            else -settings.realism_rel_affinity_chat),
            )
            await gossip_service.maybe_gossip(db, residents[a], residents[b], rng)
            await gossip_service.maybe_gossip(db, residents[b], residents[a], rng)
    return residents


async def _steady_state(db, residents, nights: int = 3) -> list[float]:
    for _ in range(nights):
        await recompute(db)
    for resident in residents:
        await db.refresh(resident)
    return [score_from_meta(resident.meta_json) for resident in residents]


async def _clear_scores(db, residents) -> None:
    for resident in residents:
        meta = dict(resident.meta_json or {})
        meta.pop("reputation", None)
        resident.meta_json = meta
        flag_modified(resident, "meta_json")
    await db.commit()


@pytest.mark.anyio
async def test_emergent_distribution_is_two_sided_and_has_a_reject_face(
    db_session, monkeypatch
):
    """第 3 项验收：阈值必须由**跑出来的**分布决定，不是构造数据凑。

    注意:这条用例要跑 ~500 次真实的 bump/maybe_gossip,耗时以十秒计,是本仓最慢
    的单测之一。迭代时用 ``-k emergent`` 单独跑。
    """
    monkeypatch.setattr(settings, "rep_enabled", True)
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    monkeypatch.setattr(gossip_service, "GOSSIP_PROBABILITY", 1.0)

    async def _fake_distort(content: str) -> str:
        return f"据说{content}"

    monkeypatch.setattr(gossip_service, "_distort", _fake_distort)

    residents = await simulate_world(db_session)

    # ① 冻结常量（rep_gossip_base_tone=-0.3）下的稳态分布
    frozen = await _steady_state(db_session, residents)
    frozen_stats = describe(frozen)
    assert all(score > -0.3 for score in frozen), (
        f"-0.3 竟然拒绝到了人，spec 的判断需要重新核对: {sorted(frozen)}")

    # ② 收口建议常量下的稳态分布（同一个世界，清空分数重算）
    await _clear_scores(db_session, residents)
    monkeypatch.setattr(settings, "rep_gossip_base_tone", RECOMMENDED_BASE_TONE)
    fixed = await _steady_state(db_session, residents)
    fixed_stats = describe(fixed)

    print("\n[frozen  base=-0.3 ]", frozen_stats)
    print("[fixed   base=%.2f]" % RECOMMENDED_BASE_TONE, fixed_stats)

    assert min(fixed) < 0.0 < max(fixed), f"分布仍是单边: {sorted(fixed)}"
    assert fixed_stats["negative_share"] < frozen_stats["negative_share"]

    # ③ 用②的真实分布标定阈值，拒绝面必须非空且非全量
    threshold = recommend_credit_min_score(fixed, 0.15)
    print("[recommended REP_CREDIT_MIN_SCORE] %+.4f" % threshold)
    monkeypatch.setattr(settings, "rep_credit_min_score", threshold)
    rejected = [score for score in fixed if not credit_allowed(score)]
    assert 0 < len(rejected) < len(fixed)

    # ④ 现行 -0.3 在同一分布上仍然谁也拒绝不了 —— 装饰性闸门
    monkeypatch.setattr(settings, "rep_credit_min_score", -0.3)
    assert all(credit_allowed(score) for score in fixed)
```

**执行者注意**：按计划原样的参数（`CAST=12` / `ROUNDS=12` / `SEED=20260727` / `RECOMMENDED_BASE_TONE=-0.05` / `GOSSIP_AFFINITY_WEIGHT=3.0`）这条用例**是能过的**（实测 `negative_share ≈ 0.42`，耗时约 6s），先别急着调参。

若 ② 的 `min(fixed) < 0 < max(fixed)` 失败，**不要放宽断言**，也**不要加大 `ROUNDS`**——方向是反的：每次闲聊的 affinity 期望是 `+0.009 = 0.03 × (0.65 - 0.35)`，均值随轮数**线性**累积而标准差只按 `sqrt` 增长，轮数越多分布越往正侧平移、负分越少。实测 `ROUNDS = 12 / 16 / 20` 对应 `negative_share = 0.4167 / 0.1667 / 0.0`，即 `ROUNDS=20` 时 `min<0<max` 直接 False——照"加大轮数"改会把一个本来能过的用例改挂。

正确处置顺序：

- (a) **加宽负侧**：把 `ROUNDS` 往**下**调（12 → 10 → 8），或把 `positive = rng.random() < 0.65` 的 `0.65` 降到 `0.55`（每次闲聊期望降到 `+0.003`）。两者都是让 affinity 分布的中位数更靠近 0。
- (b) 若失败方向相反（`max(fixed) <= 0`，全负），说明偏置项压得太狠，调 `RECOMMENDED_BASE_TONE` 让符号翻转点 `affinity = -base_tone / 3.0` 落在实测 affinity 的中位数附近（用 ① 打印的读数反推）。
- (c) 上面都试过仍不过，则记录实测分布并停下报告——那意味着 `GOSSIP_AFFINITY_WEIGHT=3.0` / `RECOMMENDED_BASE_TONE=-0.05` 这组取值不足以翻转符号，需要在计划外重新定值（属于设计问题，不是测试问题）。

- [ ] **Step 2: 确认它锁的是新语义（真实的红）**

本 Task 不产出新的产品代码——第 1/2 项的实现已落地，这里验收的是它们在真实机制下的**涌现效果**。为证明这条用例不是摆设，临时把 affinity 权重清零（等价于回到「tone 是常量」的修复前语义），它必须变红：

```bash
/Volumes/data/dev/simverse-world/backend/.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("app/services/reputation_service.py")
s = p.read_text(encoding="utf-8")
p.write_text(s.replace("GOSSIP_AFFINITY_WEIGHT = 3.0",
                       "GOSSIP_AFFINITY_WEIGHT = 0.0"), encoding="utf-8")
PY
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_rep_calibration.py -q -p no:randomly -k emergent
```

Expected: FAIL，`AssertionError: 分布仍是单边: [...]`（权重为 0 时 tone 恒等于偏置项，所有分数同号）。

- [ ] **Step 3: 恢复权重**

```bash
/Volumes/data/dev/simverse-world/backend/.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("app/services/reputation_service.py")
s = p.read_text(encoding="utf-8")
p.write_text(s.replace("GOSSIP_AFFINITY_WEIGHT = 0.0",
                       "GOSSIP_AFFINITY_WEIGHT = 3.0"), encoding="utf-8")
PY
git diff --stat        # 服务文件必须无残留 diff
```

- [ ] **Step 4: 跑测试确认通过并留存读数**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_rep_calibration.py -q -p no:randomly -s 2>&1 | tail -20`

Expected: PASS，且 stdout 里有三行读数（`[frozen ...]` / `[fixed ...]` / `[recommended REP_CREDIT_MIN_SCORE] ...`）。**把这三行原样抄进 commit message 的 `Verified-by:`**，它们是收口定值的证据。

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_rep_calibration.py
git commit -m "$(cat <<'EOF'
test(reputation): 用真实机制跑出的分布验收信用阈值重标定

harness 走 relation_service.bump + gossip_service.maybe_gossip 真实路径(只 stub
掉那一次 LLM 改写),分数是涌现的不是手写的。对比冻结常量与收口建议常量两组稳态
分布:前者拒绝面为空(装饰性闸门),后者双边且拒绝面非空。

Verified-by: <粘贴 Step 4 的三行读数 + pytest 汇总行>
EOF
)"
```

---

## Task 10: 全量回归 + 真实进程验证 + 交接

**Files:** 无代码改动（除非回归暴露问题）

**Interfaces:**
- Consumes: `/tmp/f1-base.txt`（Task 0）、`scripts/rep_calibrate.py`（Task 8）、`tests.test_rep_calibration.simulate_world`（Task 9）
- Produces: 交接读数（收口用），无新代码接口

- [ ] **Step 1: 全量回归 + 双向差集**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f1-reputation/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/ -q -p no:randomly 2>&1 \
  | grep -E "^(FAILED|ERROR) " | sed 's/\[.*//' | sort -u > /tmp/f1-final.txt
echo "=== 新增失败（必须为空）==="; comm -13 /tmp/f1-base.txt /tmp/f1-final.txt
echo "=== 顺带修好的（信息项）==="; comm -23 /tmp/f1-base.txt /tmp/f1-final.txt
```

Expected: 「新增失败」段为空。非空则停下逐条修，不得进入 Step 2。

- [ ] **Step 2: 真实进程验证 —— 在文件库上跑标定脚本**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f1-reputation/backend
rm -f /tmp/f1-calib.db
export DEBUG=true LLM_API_KEY=x REP_ENABLED=true REP_GOSSIP_BASE_TONE=-0.05
export DATABASE_URL="sqlite+aiosqlite:////tmp/f1-calib.db"

/Volumes/data/dev/simverse-world/backend/.venv/bin/python - <<'PY'
import asyncio

from app.database import Base, async_session, engine
from app.services import gossip_service
from app.services.reputation_service import recompute
from tests.test_rep_calibration import simulate_world


async def _fake_distort(content: str) -> str:
    return f"据说{content}"


async def main() -> None:
    gossip_service.GOSSIP_PROBABILITY = 1.0
    gossip_service._distort = _fake_distort
    from app.config import settings
    settings.realism_relations_enabled = True
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as db:
        residents = await simulate_world(db)
        for _ in range(3):
            await recompute(db)
    print("seeded residents:", len(residents))


asyncio.run(main())
PY

/Volumes/data/dev/simverse-world/backend/.venv/bin/python scripts/rep_calibrate.py --reject-fraction 0.15
echo "exit=$?"
```

Expected: 打印分布表 + `建议 REP_CREDIT_MIN_SCORE=...`，`exit=0`。**把整段输出留作运行时证据**（这是 verify-before-done 的硬要求：单测绿不等于完成）。

- [ ] **Step 3: 确认脚本对空世界的行为（防御性退出码）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f1-reputation/backend
rm -f /tmp/f1-empty.db
DEBUG=true LLM_API_KEY=x DATABASE_URL="sqlite+aiosqlite:////tmp/f1-empty.db" \
  /Volumes/data/dev/simverse-world/backend/.venv/bin/python - <<'PY'
import asyncio

from app.database import Base, engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


asyncio.run(main())
PY
DEBUG=true LLM_API_KEY=x DATABASE_URL="sqlite+aiosqlite:////tmp/f1-empty.db" \
  /Volumes/data/dev/simverse-world/backend/.venv/bin/python scripts/rep_calibrate.py; echo "exit=$?"
```

Expected: `建议值：无法标定 — need at least 2 scores to calibrate, got 0`，`exit=2`。

- [ ] **Step 4: 清理临时库**

```bash
rm -f /tmp/f1-calib.db /tmp/f1-empty.db
git status --short     # 必须干净：无未跟踪文件、无未提交改动
```

- [ ] **Step 5: 提交交接说明并汇报**

本线不新增文档文件（收口统一更新 `docs/ROADMAP.md`）。把下列内容写进**最终汇报**（返回给编排者），不要新建 md：

1. Task 9 / Task 10 Step 2 的真实读数（冻结分布、修复后分布、建议 `REP_CREDIT_MIN_SCORE`），**含 `max(|score|)`**（由分布表的 min/max 取绝对值较大者）。
2. 收口清单（下节表格）逐行确认。其中 `rep_vote_trust_weight` 一行**必须用 ① 的 `max(|score|)` 代入表里的公式算出来再写**——表里没有可照抄的数字，任何预填值都视为未完成。
3. 与 F2 的交接一句话：`reputation_service.py` 的人口口径（spec §4.4 第 11 处读点）**已由 F1 改为 `is_autonomous`**，F2 的全仓 `resident_type` 字面量分类扫到这里应为已归类状态，不要重复改（会文本冲突）。
4. 遗留项（本线明确不做）：`open_election` 的 SBTI 分支在候选 >4 时按裸 SELECT 行序截断（S1-1 之前就存在，与声誉无关）；`_npc_choice` 对每个人物型选项各发一次 `get_pair` 查询。

若 Step 1-4 全绿而没有任何代码改动，则本 Task 无 commit；有修复则按「一 step 一 commit」补交。

---

## 收口清单（本线不执行，交接给收口步骤）

| 项 | 现值 | 收口建议值 | 依据 |
|---|---|---|---|
| `config.py` 新增 `rep_gossip_affinity_weight: float = 3.0` | 无（模块常量 `GOSSIP_AFFINITY_WEIGHT`） | 3.0 | 符号翻转点落在 affinity=+0.1 = 一次送礼（`realism_rel_affinity_gift`）或约 4 次正向闲聊（`realism_rel_affinity_chat=0.03`）。代码已用 `getattr` 读，加字段即生效、零代码 diff |
| `rep_gossip_base_tone` | -0.3 | **-0.05** | Task 9 harness 实测：-0.3 下分布单边且 `-0.3` 阈值拒绝面为空；-0.05 下双边。以 Task 10 Step 2 的真实读数为准 |
| `rep_credit_min_score` | -0.3（装饰性） | `scripts/rep_calibrate.py` 的输出，**禁止预填** | **必须在 T1/T3 之后在 vm212 上重跑复标**；本线读数来自本机 harness，按 F2 同样的纪律标记「待用生产分布复标」。预期阈值落在 **1e-3 量级**（不是 -0.3 那种十分位数），看到「建议值只有 0.00x」是正常的，不要当成脚本坏了 |
| `rep_vote_trust_weight` | 1.0 | **待 Task 10 Step 2 实测后按公式反推，禁止预填数字** | 量程以 Task 10 Step 2 / Task 9 的真实读数为准（`max(\|score\|)`），**不要照搬任何预估值**。定值公式：`rep_vote_trust_weight ≈ 0.5 * _TASTE_MAG / max(\|score\|)`，即让声誉的入票贡献约为口味噪声的一半（`_TASTE_MAG = 0.25`，`app/services/civic_service.py:199`；入票项是 `rep_vote_trust_weight * score_from_meta(...)`，`:366-371`）。**注意分数量程比直觉小两个数量级**：`gossip_signal` 是**均值**不是求和，再经 `importance ≤ 0.7`（`gossip_service.py:25`）× `1/(1+hops)` × 3 夜 EMA（`alpha=0.3` → `1-0.7³=0.657`）三重压缩——本机 harness 实测稳态分数只有 **±0.02** 量级，按公式即 `≈ 6~10`。也就是说 1.0 和 2.0 都远不够，声誉在选票上仍近似不可见 |
| `rep_enabled` | false | true | spec §3 第 4 项：前三项验收通过后才开闸，属收口 |
| `.env.example` | 已有 10 行 `REP_*` | 新增 `REP_GOSSIP_AFFINITY_WEIGHT=3.0`，并同步改动上面三行的值 | `tests/test_env_example_consistency.py` 强制「每个 Settings 字段都要有 example 行」 |
| `nightly_cron.py` | 已在 `:387-395` 调 `recompute` | 无需改 | 本线未改 `recompute` 签名与返回语义 |
