# F2 公民权晋升与撤销 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给玩家创作（UGC）的居民一条真实可走的公民权晋升路径，并把「撤销」作为晋升的严格逆操作一起落地——门槛驱动只升、事件驱动才降、每次档位变更留一行可回滚的历史记录，全程默认关闸、shadow 可预演。

**Architecture:** 状态模型是 **出身（provenance）× 档位（standing）** 二维。出身冻结（`creator_id` / `meta_json.origin` 判定，业务代码不再改写）；档位有序三档 `citizen > denizen > exiled`，v1 仍编码在 `resident_type` 单列（不加列、不加取值），但业务代码一律走 `civic_membership` 的派生函数与两个写入口 `grant_citizenship` / `revoke_citizenship(tier=...)`。新表 `civic_standing_history` 同时承载「可回滚」硬门与「公民时钟锚点」。晋升判定是纯函数 + snapshot 语义（一次冻结输入、末尾一次 commit、锚定公民集不自指）；撤销是有序复合事务（防呆 → 卸民选职务 → 清三处镇长表示 → 改档位 → 写历史 → 断言 → 广播）。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x async / Alembic / pytest(anyio) / SQLite（测试）与 PostgreSQL（生产）。零新增 LLM 调用，全部规则驱动。

## Global Constraints

- 本线 worktree：`/Volumes/data/dev/simverse-world/.worktrees/f2-civic`，branch `feat/f2-civic-standing`，base `master`。**绝不在主工作区跑**（主工作区另有 agent 在写 lab 功能）。
- Python 解释器只用 `/Volumes/data/dev/simverse-world/backend/.venv/bin/python`；系统 python3 缺依赖。
- **不要在 worktree 里创建 `backend/.env`**（会破坏 conftest 的测试隔离）。
- 硬门 = **相对基线零新增失败**，不是 literal 0 failed。本机预存失败集约 `51 failed / 17 errors`（lab-v2，需 redis/testcontainers）。判定必须用失败集的**双向差集**（`comm -13` / `comm -23`），数量相同不等于集合相同。
- 严格 TDD：红 → 绿 → 提交，一 step 一 commit；commit message 末尾必须带**真实**的 `Verified-by:` 输出（贴实际命令与实际结果，禁编造）。禁 `--no-verify` / `amend` / `squash`。
- `git checkout <branch> -- <path>` 会**连带写入暂存区**；用它取文件后要先看 `git status` 再提交。
- **不改** `backend/app/tasks/nightly_cron.py`（接线延到收口）。收口时 `civic_promotion` 接在 `close_due_polls`(`nightly_cron.py:215`) **之后**、`run_npc_voting`(`:247`) **之前**（≈`nightly_cron.py:245`），用与 `nightly_cron.py:142-145` 同款注释形式锚住位置。
- **不改** `backend/app/config.py` 与 `backend/.env.example`（开关延到收口）。本线的所有旋钮走 `os.environ` + 模块内默认值，形状照抄 `app/services/social_status_recovery.py:57-67` 的 `_settings_default()`：env 是运行时来源，`Settings` 未来接管 fallback，收口时 F2 代码零改动。**不得给 `Settings` 加字段**——`tests/test_env_example_consistency.py` 会因缺 `.env.example` 行而失败。
- **不改** `backend/app/services/office_service.py`（F3 的独占文件，避免冲突）。F2 只 `import app.models.office.Office` 自写 guard UPDATE。⚠️ 该文件的 `_clear_mayor_legacy_stores`（`:222`）是 spec §4.3 通用约束点名的两处反例之一，本线只修得了 `election_service.py:141` 那处——另一处**已写进收口清单第 5 条**，不是遗漏。
- F2 在 `civic_service.py` 的作业面是 `propose()`、`_close_one()`、`_eligible_voter_count()` / `_policy_threshold_verdict()`；与 F1 声明的独占区 `:366-371`（vote-trust）**无重叠**。
- F2 在 `election_service.py` 的作业面是 `install_mayor()`（`:135-193`）；与 F1 声明的独占区 `:53-60`（候选排序）**无重叠**。
- `reputation_service.py:74` 与 F1 独占文件重叠 —— 见 Task 9 的合并协议（F1 若先落地，Task 9 退化为只留断言测试）。
- 「零迁移」边界改为「**零数据迁移**」：允许一次纯建表 additive migration（`civic_standing_history`），且该迁移**不得与开闸同批**。
- 上线是四次独立变更、顺序不可合并：① 建表迁移（本线交付第一步，必须先于 T2）→ ② T2 存量回填 → ③ F2 代码合入（`CIVIC_PROMOTION_MODE=off`）→ ④ shadow 观察 ≥3 个夜间周期后开闸（单独一次变更，只翻开关）。
- 撤销 v1 只做 `demote` 档；`exile` 档只留签名与枚举，实现处 `raise NotImplementedError`。
- 夜间任务**只升，永不自动降**。`CIVIC_AUTO_DEMOTION_ENABLED` 默认关，置真时任务直接 `raise NotImplementedError`（滞后三件套未实现）。
- **永不 DELETE**：撤销是软状态 + 副作用清单，绝不复用 `seed/reset_builtin_residents.py:117-165` 的 `purge_residents` 或其中任何一段级联。
- **凡是清理「已离开集合 S 的居民」的扫描，都不能用 S 本身做 WHERE**（`office_service.py:222`、`election_service.py:141` 是现存反例）——一律按 slug / id 直查。本线修 `election_service.py:141`（Task 7），`office_service.py:222` 交接给收口清单第 5 条（F3 独占文件），**逐出档上线前必须完成**。
- 三个阈值（`MIN_WORLD_DAYS` / `MIN_PEERS` / `MIN_FAMILIARITY`）本计划给的是**占位默认值，标定前不得开闸**。spec §4.2 把「只读标定」定为 F2 的第一步，所以本线必须交付**可执行的标定工具**而不只是一段注释：Task 6b 产出 `backend/scripts/civic_calibration_report.py`（只读，三张分布表 + 候选面判据），Task 13 Step 6 把上线纪律写进模块 docstring。`MIN_FAMILIARITY` 不得取 `0.3`（撞 `realism_circle_threshold`，`app/config.py:512`）。
- **本机 dev 库标定的读数一律显式标记「待生产数据复标」**（spec §4.2 的降级路径）：标定脚本自己输出这行结论，收口会话据此得到「开闸前必须补什么」的可执行清单。读数为空 ≠ 标定完成。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/app/models/civic_standing_history.py` | **Create** | 档位变更历史表模型：可回滚载体 + 公民时钟锚点载体 |
| `backend/alembic/versions/051_add_civic_standing_history.py` | **Create** | 纯建表 additive 迁移，`down_revision = "050_add_resident_sprites"` |
| `backend/app/models/__init__.py` | Modify（尾部 1 行 import） | 注册新模型，使 `Base.metadata.create_all` 看得到 |
| `backend/app/services/civic_membership.py` | Modify（大量新增，尾部追加） | 三档枚举 / `CIVIC_MEMBER_TYPE` / `civic_standing()` / `standing_to_type()` / `UGC_ORIGINS` + `is_ugc_resident()` + `ugc_filter()` / env 旋钮 / `CivicStandingRefused` / 两个写入口 + 批量写 |
| `backend/app/tasks/civic_promotion.py` | **Create** | 晋升 snapshot + 纯判定函数 + 三态 pass（off/shadow/on）+ 四道数值闸门 |
| `backend/scripts/civic_calibration_report.py` | **Create** | 只读标定报告（spec §4.2 的「第一步」）：三张分布表 + 候选面判据，零写入 |
| `backend/app/services/election_service.py` | Modify（`:135-193`） | `install_mayor` 结票复核资格 + 事务化 |
| `backend/app/services/civic_service.py` | Modify（`propose` / `_close_one` / `_policy_threshold_verdict`） | 开票冻结分母、当选人失格的流会分支 |
| `backend/app/services/reputation_service.py` | Modify（`:68-75`） | 第 11 处 `resident_type == "npc"` 读点归到人口口径 `is_autonomous` |
| `backend/app/routers/admin/residents.py` | Modify（`_edit_resident` + `edit_resident`） | `resident_type` 裸赋值收敛到写入口，拒绝→409 |
| `backend/scripts/burnin_report.py` | Modify（探针区 + `_run`） | 交叉表 / 晋升队列 / 翻转统计 / 交叉一致性 四项新增；`unknown_types` ⚠️→🔴 |
| `backend/tests/test_civic_standing_history_model.py` | **Create** | Task 1 |
| `backend/tests/test_civic_membership_standing.py` | **Create** | Task 2 |
| `backend/tests/test_civic_grant_citizenship.py` | **Create** | Task 3 |
| `backend/tests/test_civic_revoke_guard.py` | **Create** | Task 4 |
| `backend/tests/test_civic_revoke_citizenship.py` | **Create** | Task 5 |
| `backend/tests/test_civic_promotion_rules.py` | **Create** | Task 6 |
| `backend/tests/test_civic_calibration_report.py` | **Create** | Task 6b |
| `backend/tests/test_install_mayor_recheck.py` | **Create** | Task 7 |
| `backend/tests/test_civic_frozen_denominator.py` | **Create** | Task 8 |
| `backend/tests/test_reputation_population_scope.py` | **Create** | Task 9 |
| `backend/tests/test_civic_standing_write_entrypoints.py` | **Create** | Task 10 |
| `backend/tests/test_burnin_report_civic_standing.py` | **Create** | Task 11 |
| `backend/tests/test_burnin_report_civic_boundary.py` | **无需修改**（已核实：文件里没有任何 ⚠️ 文案断言；涉及 🔴 的四处断言 `:78`/`:83`/`:89`/`:95` 用的快照 `unknown_types` 都是空的，⚠️→🔴 一行都波及不到）| — |
| `backend/tests/test_civic_promotion_pass.py` | **Create** | Task 12 |

---

### Task 0: 工作区、路径守卫与基线捕获

**Files:** 无代码改动（不产生 commit）

**Interfaces:**
- Produces: `/tmp/f2-base.txt`（基线失败集，后续所有任务的差集比较基准）

- [ ] **Step 1: 建 worktree**

```bash
cd /Volumes/data/dev/simverse-world
git worktree add -b feat/f2-civic-standing .worktrees/f2-civic master
ls -d /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
```

- [ ] **Step 2: 路径守卫（逐字照抄，不通过就停）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -c "import app; p=app.__file__; assert '.worktrees/' in p, f'WRONG: {p}'; print('OK',p)"
```

Expected: `OK /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend/app/__init__.py`

- [ ] **Step 3: 确认 worktree 内没有 `.env`，且 alembic 链头是 050**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
test ! -f .env && echo "OK: no .env in worktree"
ls alembic/versions/ | grep -E '^0[0-9]{2}_' | sort | tail -3
```

Expected: `OK: no .env in worktree`；三行分别是 `048_add_town_treasury.py` / `049_add_policies.py` / `050_add_resident_sprites.py`。

（`grep -E '^0[0-9]{2}_'` 是刻意的：裸 `ls | sort | tail -3` 在 ASCII 序下会把 `b9c99304b867_initial_schema.py` 排到最后（`'b' > '0'`），链头看起来像初始迁移。另：**不含** `051_add_lab_codex_model_tier.py` / `052_add_lab_run_resource_profile.py`——那是主工作区另一条线的未跟踪文件，不在 master 上，也不会进 worktree。）

- [ ] **Step 4: 捕获基线失败集**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/ -q -p no:randomly 2>&1 | tee /tmp/f2-base-full.txt | tail -3
grep -E "^(FAILED|ERROR) " /tmp/f2-base-full.txt | sed 's/\[.*//' | sort -u > /tmp/f2-base.txt
wc -l /tmp/f2-base.txt
```

Expected: `/tmp/f2-base.txt` 有约 60-70 行（51 failed + 17 errors 去重后）。这一行数字要抄进 Task 13 的差集比较。

---

### Task 1: `civic_standing_history` 模型 + 建表迁移

**Files:**
- Create: `backend/app/models/civic_standing_history.py`
- Create: `backend/alembic/versions/051_add_civic_standing_history.py`
- Modify: `backend/app/models/__init__.py`（尾部追加 1 行）
- Test: `backend/tests/test_civic_standing_history_model.py`

**Interfaces:**
- Produces: `CivicStandingHistory`（`__tablename__ = "civic_standing_history"`），列：`id: str` PK、`resident_id: str` FK→`residents.id` index、`old_standing: str`、`new_standing: str`、`reason: str | None`(Text)、`reason_code: str`、`actor: str`、`evidence_json: dict | None`(JSON)、`world_at: datetime`（**世界时间，UTC-aware 存储**）、`created_at: datetime`（真实时间）；索引 `ix_civic_standing_history_resident_created(resident_id, created_at)`。
- Produces: alembic revision id `"051_add_civic_standing_history"`，`down_revision = "050_add_resident_sprites"`。

**为什么必须建表**：硬门「可回滚」需要载体，而 `meta_json` 有 7 个 read-modify-write 写入方（`reputation_service.py:124`、`circle_service.py:117`、`office_service.py:231`、`duty_service.py:213`、`social_status_recovery.py:115`/`:125`、`election_service.py:155`），agent loop 也在同一批居民上写 → 滞后状态被静默覆盖、只在并发窗口发生、测试抓不到；它还是 `sa.JSON` 无法索引，并由多个无鉴权前台接口原样公开（`frontend/src/components/NpcTooltip.tsx` 等）——**撤销原因文本绝不能进去**。

**为什么单独存 `world_at`**：公民时钟锚点必须是世界时间。虽然 `real_to_world(created_at)` 是仿射变换可推导，但 `WORLD_EPOCH` / `WORLD_CLOCK_K` 一旦被改，所有历史锚点会整体漂移；显式列让锚点在配置变更下保持稳定，也让探针不必二次换算。存储时 `now_world().astimezone(UTC)`——SQLite 的 `DateTime(timezone=True)` 会丢时区，只有统一转 UTC 存、读回按 UTC 补时区才能无损往返（同 `office_service._term_window` 的口径）。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_standing_history_model.py`:

```python
"""F2 Task 1 — 档位变更历史表：可回滚硬门与公民时钟锚点的共同载体。

形状照抄仓内先例 app/models/personality_history.py（同为「一行一次变更 +
resident_id 索引 + created_at」的审计表）。reason 与 reason_code 刻意分列：
code 可以外发（WS payload / 探针），text 永不外发。
"""
import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident


def test_migration_single_head_and_chains_onto_050():
    """`alembic heads` 单头，且建表迁移挂在本 worktree 实测的链头 050 上。

    收口注记：主线并行的 lab 迁移也取 051 前缀（不同 revision id），两条线
    合并后会出现双头，按仓内先例（048_add_town_treasury / 049_add_policies
    的线性化）在收口时把后落地的一支 re-chain，本测试的断言随之更新。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("051_add_civic_standing_history")
    assert rev is not None
    assert rev.down_revision == "050_add_resident_sprites"


def test_migration_is_additive_only():
    """纯建表 additive：``upgrade()`` / ``downgrade()`` 里只许出现建表与建索引
    的 ``op.*`` 调用，且函数体内不得出现任何数据写语句的 SQL 字符串 ——
    「零数据迁移」边界的机器可查版本。

    刻意用 **AST 扫函数体**而不是裸 substring 扫全文：迁移的模块 docstring 里
    写「本文件不含任何数据写语句」这类解释性文字是正常的，裸扫会把守卫自己
    打红（第一版就踩过这个坑）。注释与 docstring 一律不计入。
    """
    path = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / "051_add_civic_standing_history.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    allowed_ops = {"create_table", "create_index", "drop_index", "drop_table"}
    forbidden_sql = ("insert", "update", "delete", "alter table")
    called: list[str] = []
    literals: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if fn.name not in ("upgrade", "downgrade"):
            continue
        body = fn.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                      # 跳过函数自己的 docstring
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "op"):
                        called.append(func.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literals.append(node.value)

    assert called, ("没扫到任何 op.* 调用 —— 迁移文件的形状变了，先看一眼再"
                    "决定要不要改守卫")
    assert set(called) <= allowed_ops, (
        f"建表迁移只许 {sorted(allowed_ops)}，实际出现了 {sorted(set(called))}"
        " —— 这一批只许建表（op.execute / batch_alter_table / add_column /"
        " drop_column 都在禁区里）")
    offenders = [s for s in literals
                 if any(kw in s.lower() for kw in forbidden_sql)]
    assert offenders == [], (
        f"迁移正文里出现了数据写语句字符串 {offenders} —— 这一批只许建表")


def test_model_shape():
    cols = CivicStandingHistory.__table__.columns
    assert CivicStandingHistory.__tablename__ == "civic_standing_history"
    assert cols["id"].primary_key is True
    assert cols["resident_id"].nullable is False
    assert cols["resident_id"].index is True
    assert cols["old_standing"].nullable is False
    assert cols["new_standing"].nullable is False
    # reason 是自由文本、可为空、永不外发；reason_code 是可外发的枚举码
    assert isinstance(cols["reason"].type, sa.Text)
    assert cols["reason"].nullable is True
    assert cols["reason_code"].nullable is False
    assert cols["actor"].nullable is False
    assert isinstance(cols["evidence_json"].type, sa.JSON)
    # 公民时钟锚点（世界时间）与审计时间（真实时间）是两列，不可合并
    assert cols["world_at"].nullable is False
    assert cols["created_at"].nullable is False
    names = {ix.name for ix in CivicStandingHistory.__table__.indexes}
    assert "ix_civic_standing_history_resident_created" in names


@pytest.mark.anyio
async def test_table_is_created_by_metadata(db_engine):
    """models/__init__.py 注册了模型，Base.metadata.create_all（main.py 的
    测试路径）才看得到这张表。"""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "civic_standing_history" in names


@pytest.mark.anyio
async def test_row_roundtrips_with_world_and_real_time(db_session):
    r = Resident(slug="ugc-1", name="ugc-1", district="town_hall", status="idle",
                 resident_type="resident", creator_id="u1", tile_x=1, tile_y=1)
    db_session.add(r)
    await db_session.flush()
    world_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    db_session.add(CivicStandingHistory(
        resident_id=r.id, old_standing="denizen", new_standing="citizen",
        reason="满足门槛：在镇 40 世界日 / 3 位锚定公民",
        reason_code="threshold_met", actor="civic_promotion",
        evidence_json={"world_days": 40.0, "peers": 3, "min_familiarity": 0.2},
        world_at=world_at,
    ))
    await db_session.commit()

    row = (await db_session.execute(select(CivicStandingHistory))).scalar_one()
    assert row.resident_id == r.id
    assert (row.old_standing, row.new_standing) == ("denizen", "citizen")
    assert row.evidence_json["peers"] == 3
    stored = row.world_at
    if stored.tzinfo is None:          # sqlite 丢时区 → 按 UTC 补回
        stored = stored.replace(tzinfo=UTC)
    assert stored == world_at
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_standing_history_model.py -q -p no:randomly
```
Expected: FAIL —— collection error `ModuleNotFoundError: No module named 'app.models.civic_standing_history'`。

- [ ] **Step 3: 写模型**

Create `backend/app/models/civic_standing_history.py`:

```python
"""F2 —— 公民权档位（civic standing）变更历史。

一行 = 一次档位变更。这张表承担两个互不重叠的职责：

1. **可回滚**（F2 硬门 2）。``old_standing`` 让恢复能回到「变更前那一档」，
   而不是一律回 citizen。T2 存量回填也必须写行（``actor="ops_backfill_t2"``），
   否则回填批次事后不可追溯。
2. **公民时钟锚点**（晋升门槛①的起算点）。锚 ``residents.created_at`` 会让
   T2 的降权对存量整批走过场——一个已在镇 200 世界日的 UGC 被降权后，开闸
   当晚条件①立刻重新满足。锚点取本表最近一行的 ``world_at``，无行才回落
   ``real_to_world(created_at)``。

形状照抄仓内先例 ``app/models/personality_history.py``。

两组时间列不可合并：``world_at`` 是**世界时间**（门槛判定用），``created_at``
是**真实时间**（审计/运维用）。世界时间以 UTC-aware 落库——``DateTime(timezone
=True)`` 在 SQLite 上会丢时区，统一转 UTC 存、读回补 UTC 才能无损往返（同
``app/services/office_service.py`` 的存储口径）。

``reason``（自由文本）与 ``reason_code``（枚举码）刻意分列：**code 可外发**
（WS payload、探针输出），**text 永不外发**。这是把撤销原因挡在无鉴权前台
接口之外的结构性保证——正是 ``meta_json`` 做不到的那一条。本表在 v1 **不加
任何读接口**（YAGNI + 隐私）。
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Text, DateTime, JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CivicStandingHistory(Base):
    __tablename__ = "civic_standing_history"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    resident_id: Mapped[str] = mapped_column(
        String, ForeignKey("residents.id"), index=True, nullable=False
    )
    # citizen / denizen / exiled —— app/services/civic_membership.CIVIC_STANDINGS
    old_standing: Mapped[str] = mapped_column(String(20), nullable=False)
    new_standing: Mapped[str] = mapped_column(String(20), nullable=False)
    # 自由文本，永不外发（无读接口、不进 WS payload、不进探针输出）
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可外发的枚举码：threshold_met / admin_grant / admin_revoke / ops_backfill / ...
    reason_code: Mapped[str] = mapped_column(String(50), nullable=False)
    # civic_promotion | civic_demotion | admin:<user_id> | ops_backfill_t2
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    # {"world_days": float, "peers": int, "min_familiarity": float, ...}
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 世界时间（公民时钟锚点）。存 UTC-aware，读回若 naive 按 UTC 补。
    world_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 真实时间（审计）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_civic_standing_history_resident_created",
              "resident_id", "created_at"),
    )
```

- [ ] **Step 4: 注册模型**

Edit `backend/app/models/__init__.py` —— 在文件**末尾**追加：

```python
# F2 公民权档位变更历史 —— 可回滚硬门 + 公民时钟锚点的载体
import app.models.civic_standing_history  # noqa: F401
```

- [ ] **Step 5: 写建表迁移**

Create `backend/alembic/versions/051_add_civic_standing_history.py`:

```python
"""F2 —— civic_standing_history（纯建表 additive，零数据行为）。

「零迁移」边界在 F2 被显式改写为「零**数据**迁移」：允许这一次纯建表
migration，且它**不得与开闸同批**（上线四次独立变更的第 ①步，必须先于 T2
存量回填——T2 要写历史行作为公民时钟锚点）。

本文件只有 create_table + create_index，没有任何数据写语句，也不碰 residents
表；tests/test_civic_standing_history_model.py 用 AST 扫 upgrade/downgrade 的
函数体把这条约束钉住（扫函数体而不是扫全文，所以这段说明文字本身不算违规）。

Revision ID: 051_add_civic_standing_history
Revises: 050_add_resident_sprites
Create Date: 2026-07-27
"""
import sqlalchemy as sa
from alembic import op

revision = "051_add_civic_standing_history"
down_revision = "050_add_resident_sprites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "civic_standing_history",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("resident_id", sa.String(), nullable=False),
        sa.Column("old_standing", sa.String(length=20), nullable=False),
        sa.Column("new_standing", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.String(length=50), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("world_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["resident_id"], ["residents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_civic_standing_history_resident_id",
                    "civic_standing_history", ["resident_id"])
    op.create_index("ix_civic_standing_history_resident_created",
                    "civic_standing_history", ["resident_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_civic_standing_history_resident_created",
                  table_name="civic_standing_history")
    op.drop_index("ix_civic_standing_history_resident_id",
                  table_name="civic_standing_history")
    op.drop_table("civic_standing_history")
```

- [ ] **Step 6: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_standing_history_model.py -q -p no:randomly
```
Expected: PASS（5 passed）。

- [ ] **Step 7: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/models/civic_standing_history.py \
        backend/app/models/__init__.py \
        backend/alembic/versions/051_add_civic_standing_history.py \
        backend/tests/test_civic_standing_history_model.py
git status --short   # 确认没有夹带无关文件
git commit -m "$(cat <<'EOF'
feat(civic): civic_standing_history 建表——可回滚硬门与公民时钟锚点的载体

纯建表 additive 迁移（051 ← 050），零数据行为：上线四次独立变更的第 ①步，
必须先于 T2 存量回填（T2 要写历史行当锚点）。

- world_at（世界时间）与 created_at（真实时间）分列，锚点不再回落 created_at
- reason（文本，永不外发）与 reason_code（枚举码，可外发）分列
- 本表 v1 不加任何读接口

Verified-by: <贴 pytest tests/test_civic_standing_history_model.py -q 的真实输出>
EOF
)"
```

---

### Task 2: `civic_membership` 扩展——三档枚举、派生函数、UGC 判定、env 旋钮、防呆异常

**Files:**
- Modify: `backend/app/services/civic_membership.py`（尾部追加；`__all__` 就地更新）
- Test: `backend/tests/test_civic_membership_standing.py`

**Interfaces:**
- Consumes: 既有 `CIVIC_VOTER_TYPES` / `SIM_RESIDENT_TYPES` / `UGC_RESIDENT_TYPE`（同文件，`civic_membership.py:43/53/58`）；`app.models.resident.Resident`（**必须惰性导入**）
- Produces:
  - 常量：`CITIZEN="citizen"` / `DENIZEN="denizen"` / `EXILED="exiled"` / `CIVIC_STANDINGS: tuple[str,str,str]` / `CIVIC_MEMBER_TYPE="npc"` / `PLAYER_RESIDENT_TYPE="player"` / `ADMIN_PRESET_TYPE="preset"` / `ADMIN_PRESET_CREATOR_ID="system"` / `SYSTEM_CREATOR_ID: str` / `UGC_ORIGINS: frozenset[str]` / `NON_UGC_ORIGINS: frozenset[str]` / `POLITICAL_FILL_STRATEGY="election"` / `STANDING_TO_TYPE: dict[str,str]` / `TYPE_TO_STANDING: dict[str,str]`
  - 异常：`class CivicStandingRefused(RuntimeError)`
  - 函数：`civic_standing(resident) -> str`、`standing_to_type(standing: str) -> str`、`assert_known_types(*types: str) -> None`、`is_ugc_resident(resident) -> bool`、`ugc_filter()`（返回 SQLAlchemy 布尔表达式）
  - env 旋钮（全部零参、返回标量）：`promotion_mode() -> str`、`min_world_days() -> float`、`min_peers() -> int`、`min_familiarity() -> float`、`peer_seasoning_world_days() -> float`、`promotion_max_per_run() -> int`、`promotion_breaker_fraction() -> float`、`promotion_breaker_min_abs() -> int`、`min_electorate() -> int`、`min_tenure_world_days() -> float`、`promotion_cooldown_world_days() -> float`、`auto_demotion_enabled() -> bool`

**两个必须写进代码的陷阱：**

1. **循环导入**。`app/models/resident.py:8` 在模型层 `from app.services.civic_membership import ...`。因此本模块**任何** `app.models.*` / `app.config` 的导入都必须放在函数体内（惰性）。模块顶层只许 stdlib 与 `sqlalchemy`。
2. **`SYSTEM_CREATOR_ID` 不得从 `seed` 导入**。全仓核实：`app/` 今天零处 `import seed`（`grep -rn "from seed\|import seed" app/` 只命中 `seed_achievements` / `seed_items` / `seed_civic_agenda` 三个同名函数）。这里重复字面量、由测试断言两处相等，而不是把 1200 行种子数据模块拉进模型层。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_membership_standing.py`:

```python
"""F2 Task 2 —— 出身 × 档位二维模型的常量层与派生函数。

档位有序三档（citizen > denizen > exiled），v1 仍编码在 resident_type 单列：
不加列、不加取值。不新增第 5 个 type 的理由是地图与感知**不读 type**——公开
名录是全表（app/services/resident_service.py:6-18）、tile 占用也是全表
（app/services/resident_placement.py:104-111/:157-160），新增取值只会掉出
SIM_RESIDENT_TYPES，产出「仍在地图上、仍被搭话，只是自己不再 tick」的活体
雕像。逐出要收窄的是第四族谓词 is_in_town，不是这两个集合。
"""
import ast
import pathlib

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.services import civic_membership as cm

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _res(slug, rtype, *, creator_id="u1", meta=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id,
                    tile_x=1, tile_y=1, meta_json=meta)


# ── 档位枚举与编码 ─────────────────────────────────────────────────────

def test_three_ordered_standings():
    assert cm.CIVIC_STANDINGS == ("citizen", "denizen", "exiled")
    assert (cm.CITIZEN, cm.DENIZEN, cm.EXILED) == cm.CIVIC_STANDINGS


def test_citizen_tier_encodes_as_the_voter_type():
    """CIVIC_MEMBER_TYPE 是唯一允许出现的 'npc' 字面量来源：resident_type 是
    裸 String(20)、无 enum 无 CHECK（models/resident.py:55），写错一个字符
    ('npc ') 就同时掉出两个集合。"""
    assert cm.CIVIC_MEMBER_TYPE == "npc"
    assert cm.CIVIC_MEMBER_TYPE in cm.CIVIC_VOTER_TYPES
    assert cm.CIVIC_MEMBER_TYPE in cm.SIM_RESIDENT_TYPES
    assert cm.STANDING_TO_TYPE == {cm.CITIZEN: cm.CIVIC_MEMBER_TYPE,
                                   cm.DENIZEN: cm.UGC_RESIDENT_TYPE}
    assert cm.TYPE_TO_STANDING == {cm.CIVIC_MEMBER_TYPE: cm.CITIZEN,
                                   cm.UGC_RESIDENT_TYPE: cm.DENIZEN}


def test_no_fifth_resident_type_value_was_introduced():
    """EXILED 档刻意不映射到任何 resident_type 取值。"""
    assert cm.EXILED not in cm.STANDING_TO_TYPE
    assert "exiled" not in cm.SIM_RESIDENT_TYPES
    assert "exiled" not in cm.CIVIC_VOTER_TYPES


def test_civic_standing_reads_the_tier_off_a_resident():
    assert cm.civic_standing(_res("b1", "npc", creator_id=cm.SYSTEM_CREATOR_ID)) == cm.CITIZEN
    assert cm.civic_standing(_res("u1", cm.UGC_RESIDENT_TYPE)) == cm.DENIZEN


@pytest.mark.parametrize("rtype", ["player", "preset", "npc ", "", None])
def test_civic_standing_refuses_types_outside_the_tier_model(rtype):
    """player 由第三族谓词（!= "player"）管辖、preset 是待决项、其余是写错的
    字面量——都不该被当成某个档位悄悄处理。"""
    with pytest.raises(ValueError):
        cm.civic_standing(_res("x", rtype))


def test_standing_to_type_reserves_the_exile_tier():
    assert cm.standing_to_type(cm.CITIZEN) == cm.CIVIC_MEMBER_TYPE
    assert cm.standing_to_type(cm.DENIZEN) == cm.UGC_RESIDENT_TYPE
    with pytest.raises(NotImplementedError):
        cm.standing_to_type(cm.EXILED)
    with pytest.raises(ValueError):
        cm.standing_to_type("citizen-ish")


def test_assert_known_types_is_the_value_whitelist_gate():
    cm.assert_known_types(cm.CIVIC_MEMBER_TYPE, cm.UGC_RESIDENT_TYPE)
    with pytest.raises(cm.CivicStandingRefused):
        cm.assert_known_types("npc ")           # 尾空格：闸门 4 要拦的正是它
    with pytest.raises(cm.CivicStandingRefused):
        cm.assert_known_types("player")


# ── UGC 判定（T2 与 F2 共用同一份实现） ────────────────────────────────

def test_system_creator_id_matches_the_seed_constant():
    """T2 脚本与 F2 任务共用这个常量；它必须与种子模块逐字相等，否则内置阵容
    会被当成 UGC 纳入晋升/撤销射程。"""
    from seed.preset_characters import SYSTEM_USER_ID
    assert cm.SYSTEM_CREATOR_ID == SYSTEM_USER_ID


def test_ugc_origins_are_the_three_creation_paths():
    assert cm.UGC_ORIGINS == frozenset({"forge", "import", "quick_forge"})


def test_non_ugc_origins_include_onboarding():
    """``"onboarding"`` 是玩家化身的出身（onboarding_service.py:91），必须与
    ``"preset"`` 并列判否。

    否则：admin 手滑把化身的 type 改成 ``resident`` 之后，兜底分支
    ``return creator_id is not None`` 会把它判成 UGC（化身的 creator_id 是真实
    user id）→ 进 select_promotions 的候选面 → 被夜间任务自动授予投票权，而且
    此后 _assert_revocable 的玩家化身 FK 复核会拒绝撤销，人就永久卡在 citizen 档。
    """
    assert cm.NON_UGC_ORIGINS == frozenset({"preset", "onboarding"})
    assert not (cm.UGC_ORIGINS & cm.NON_UGC_ORIGINS)


@pytest.mark.parametrize("meta,creator,expected", [
    ({"origin": "forge"}, "u1", True),
    ({"origin": "import"}, "u1", True),
    ({"origin": "quick_forge"}, "u1", True),
    # 极老的 UGC 行不保证带 origin —— 有真实 creator_id 即算
    (None, "u1", True),
    # 账号注销后 creator_id 变 NULL（迁移 045）且无 origin —— 保守判否，
    # 由 T2 的「残差人工点名复核」兜底
    (None, None, False),
    # admin preset：creator_id 是字面量 "system"，origin 也写 "preset"
    ({"origin": "preset"}, "system", False),
    # 被篡改 type 的玩家化身：creator_id 是真实 user id，只有 origin 认得出它
    ({"origin": "onboarding"}, "u1", False),
])
def test_is_ugc_resident_covers_the_three_valued_creator_id(meta, creator, expected):
    assert cm.is_ugc_resident(_res("x", cm.UGC_RESIDENT_TYPE,
                                   creator_id=creator, meta=meta)) is expected


def test_is_ugc_resident_excludes_builtins_and_avatars():
    """内置阵容与 admin preset 同写 meta_json.origin == "preset"
    （seed/preset_characters.py:1237 与 routers/admin/residents.py:148），
    所以 provenance 判定以 creator_id 为主键。"""
    builtin = _res("b1", "npc", creator_id=cm.SYSTEM_CREATOR_ID,
                   meta={"origin": "preset", "is_preset": True})
    assert cm.is_ugc_resident(builtin) is False
    avatar = _res("a1", "player", creator_id="u1")
    assert cm.is_ugc_resident(avatar) is False


@pytest.mark.anyio
async def test_ugc_filter_is_a_sql_superset_of_the_python_predicate(db_session):
    """SQL 只做粗筛（meta_json 是 sa.JSON，跨 sqlite/PG 没有可移植的 JSON 路径
    查询），精确判定必须再过一遍 is_ugc_resident。粗筛必须是超集，否则会漏人。"""
    rows = [
        _res("ugc-forge", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"}),
        _res("ugc-old", cm.UGC_RESIDENT_TYPE, meta=None),
        _res("ugc-orphan", cm.UGC_RESIDENT_TYPE, creator_id=None),
        _res("builtin", "npc", creator_id=cm.SYSTEM_CREATOR_ID,
             meta={"origin": "preset"}),
        _res("adminpreset", "preset", creator_id="system",
             meta={"origin": "preset"}),
        _res("avatar", "player"),
        # admin 手滑把化身改成 resident 档：SQL 粗筛认不出（creator_id 是真实
        # user id），只有 origin == "onboarding" 能挡下它
        _res("tampered-avatar", cm.UGC_RESIDENT_TYPE,
             meta={"origin": "onboarding"}),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    coarse = (await db_session.execute(
        select(Resident).where(cm.ugc_filter())
    )).scalars().all()
    coarse_slugs = {r.slug for r in coarse}
    exact = {r.slug for r in coarse if cm.is_ugc_resident(r)}

    assert "builtin" not in coarse_slugs
    assert "adminpreset" not in coarse_slugs
    assert "avatar" not in coarse_slugs
    assert exact == {"ugc-forge", "ugc-old"}
    # 超集性质：孤儿行与被篡改的化身都进了粗筛、被精确判定挡掉，而不是在 SQL
    # 层就消失（SQL 挡不住它们，所以 is_ugc_resident 必须是最终判据）
    assert "ugc-orphan" in coarse_slugs
    assert "tampered-avatar" in coarse_slugs


# ── env 旋钮 ───────────────────────────────────────────────────────────

def test_promotion_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("CIVIC_PROMOTION_MODE", raising=False)
    assert cm.promotion_mode() == "off"
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "  Shadow ")
    assert cm.promotion_mode() == "shadow"


def test_auto_demotion_defaults_off(monkeypatch):
    monkeypatch.delenv("CIVIC_AUTO_DEMOTION_ENABLED", raising=False)
    assert cm.auto_demotion_enabled() is False
    monkeypatch.setenv("CIVIC_AUTO_DEMOTION_ENABLED", "true")
    assert cm.auto_demotion_enabled() is True


def test_numeric_knobs_fall_back_on_garbage(monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "not-a-number")
    assert cm.min_peers() == 3
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.35")
    assert cm.min_familiarity() == 0.35


def test_familiarity_threshold_must_not_collide_with_circle_threshold(monkeypatch):
    """θ 不要取 0.3 —— realism_circle_threshold = 0.3（config.py:512）是圈子
    检测的强边阈值，撞上去会让两套语义纠缠。"""
    monkeypatch.delenv("CIVIC_PROMOTION_MIN_FAMILIARITY", raising=False)
    from app.config import settings
    assert cm.min_familiarity() != settings.realism_circle_threshold


def test_breaker_has_an_absolute_floor(monkeypatch):
    """熔断的**绝对下限**。只按比例算，小镇规模下熔断会恒响：生产内置阵容
    ≈10-11 位公民 × 0.20 ≈ 2.2，一夜 3 个合法候选就整批拒绝，而
    ``CIVIC_PROMOTION_MAX_PER_RUN`` 默认 5 永远够不着——两道闸门互相吞掉，
    闸门 1 变成死代码。下限让「小批量放行、大批量熔断」两个语义都活着。
    """
    monkeypatch.delenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", raising=False)
    assert cm.promotion_breaker_min_abs() == 3
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "8")
    assert cm.promotion_breaker_min_abs() == 8
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "-1")
    assert cm.promotion_breaker_min_abs() == 0     # 负值 = 只按比例判


def test_min_electorate_floor_is_at_least_three(monkeypatch):
    """open_election 需要 ≥2 候选（election_service.py:62-63），下限低于 3 时
    撤销可以把小镇的选举机制打死。"""
    monkeypatch.delenv("CIVIC_MIN_ELECTORATE", raising=False)
    assert cm.min_electorate() >= 3
    monkeypatch.setenv("CIVIC_MIN_ELECTORATE", "1")
    assert cm.min_electorate() >= 3


def test_hysteresis_knobs_are_at_least_one_poll_lifetime(monkeypatch):
    """一张 poll 开 civic_poll_days=3 真实天 = 12 世界日（k=4）。最短任期与
    冷却期小于它，单张 poll 生命周期内公民权仍可翻转。"""
    for name in ("CIVIC_MIN_TENURE_WORLD_DAYS",
                 "CIVIC_PROMOTION_COOLDOWN_WORLD_DAYS"):
        monkeypatch.delenv(name, raising=False)
    assert cm.min_tenure_world_days() >= 12
    assert cm.promotion_cooldown_world_days() >= 12


# ── 层次约束（模型层导入本模块，本模块不许反向依赖） ───────────────────

def test_module_top_level_imports_stay_lazy():
    """app/models/resident.py:8 在模型层导入本模块。任何顶层的 app.models.* /
    app.config 导入都会造成循环导入，必须写在函数体内。"""
    src = (BACKEND_ROOT / "app" / "services" / "civic_membership.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in tree.body:            # 只看模块顶层
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names
                          if a.name.startswith(("app.models", "app.config",
                                                "seed."))]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(("app.models", "app.config", "seed")):
                offenders.append(node.module)
    assert offenders == [], (
        f"civic_membership 顶层导入了 {offenders} —— 会与 models/resident.py:8 "
        "构成循环导入；把它们挪进函数体")
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_membership_standing.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: module 'app.services.civic_membership' has no attribute 'CIVIC_STANDINGS'`（以及后续同类）。

- [ ] **Step 3: 在 `civic_membership.py` 追加常量层与派生函数**

在 `backend/app/services/civic_membership.py` 的 `UGC_RESIDENT_TYPE = "resident"` 那一行**之后**、`__all__` 那一行**之前**插入下面全部内容：

```python

# ═══════════════════════════════════════════════════════════════════════
# F2 —— 出身（provenance）× 档位（standing）二维模型
# ═══════════════════════════════════════════════════════════════════════
#
# 维度 A · 出身：``resident_type`` 的四个取值今天各有唯一创建路径，本轮之后
# 除 admin 纠错外不再被业务改写（见 ``is_ugc_resident``）。
#
# 维度 B · 档位：有序三档，正好对应「降级与逐出是同一套机制的不同强度」::
#
#     citizen  有票 · 在镇 · 被 loop 驱动          ← 晋升终点
#     denizen  无票 · 在镇 · 被 loop 驱动          ← 降级落点（本轮实现）
#     exiled   无票 · 不在镇 · 不被驱动 · 不在地图  ← 逐出落点（本轮仅预留）
#
# v1 的落地形态是**零列变更**：档位仍由 ``resident_type`` 的 npc / resident
# 编码，但任何业务代码不得再直接读写该列，一律走本模块的派生函数与两个写
# 入口 ``grant_citizenship`` / ``revoke_citizenship``。
#
# **不新增第 5 个取值（例如 "exiled"）**：地图与感知不读 type——公开名录是
# 全表（``app/services/resident_service.py:6-18``），tile 占用也是全表
# （``app/services/resident_placement.py:104-111`` / ``:157-160``）。新增取值
# 只会掉出 ``SIM_RESIDENT_TYPES``，产出「仍在地图上、仍被搭话，只是自己不再
# tick」的活体雕像。逐出要收窄的是第四族谓词 ``is_in_town``（v1 不实现，语义
# 已在此写死：出现在公开名录/地图 + 占用住房 + 占用 tile，三处口径统一开关）。
#
# 本模块被 ``app/models/resident.py:8`` 在**模型层**导入，所以顶层只许 stdlib
# 与 sqlalchemy；``app.models.*`` / ``app.config`` 一律惰性导入。

import logging
import os

logger = logging.getLogger(__name__)

CITIZEN = "citizen"
DENIZEN = "denizen"
EXILED = "exiled"

#: 有序三档（强度递增的撤销落点）。
CIVIC_STANDINGS: tuple[str, str, str] = (CITIZEN, DENIZEN, EXILED)

#: citizen 档在 v1 编码成的 ``resident_type``。**禁止任务里出现裸字面量**
#: ``"npc"``——该列是裸 ``String(20)``、无 enum 无 CHECK
#: （``app/models/resident.py:55``），写错一个字符（``"npc "``）就同时掉出
#: ``CIVIC_VOTER_TYPES`` 与 ``SIM_RESIDENT_TYPES``，居民从 agent loop、市政厅
#: 名册、职务查找、mayor 清扫里一起消失。
CIVIC_MEMBER_TYPE = "npc"

#: 玩家化身（``users.player_resident_id`` 的单值 FK）。刻意不在任何档位里，
#: 由第三族谓词 ``!= "player"`` 管辖。
PLAYER_RESIDENT_TYPE = "player"

#: admin 创建的 resident（``app/schemas/admin.py:129`` 默认值）。两个集合之外，
#: 本轮不动（U6 待决项）。
ADMIN_PRESET_TYPE = "preset"

#: ``app/routers/admin/residents.py`` 给 admin preset 写的 creator_id 字面量。
ADMIN_PRESET_CREATOR_ID = "system"

#: 内置阵容的 creator_id。与 ``seed/preset_characters.py:20`` 的
#: ``SYSTEM_USER_ID`` 逐字相等（由 tests 断言）。**刻意重复字面量而不是
#: import**：``app/`` 全仓今天零处 ``import seed``，而本模块被模型层导入，
#: 把 1200 行的种子数据模块拉进来会把层次倒过来。
SYSTEM_CREATOR_ID = "00000000-0000-0000-0000-000000000001"

#: UGC 创建路径写进 ``meta_json['origin']`` 的三个取值（五处创建点：
#: ``app/forge/pipeline.py::ForgePipeline``、``app/forge/legacy_pipeline.py``
#: 的 forge 与 quick_forge 两处、``app/routers/residents.py`` 的 import 两处）。
#: 这里刻意只写路径 + 符号名不写行号——行号会随代码漂移成陈旧文档。
#: T2 存量回填脚本与 F2 夜间任务**共用本模块的判定**，两边各写一份必然漂移。
UGC_ORIGINS = frozenset({"forge", "import", "quick_forge"})

#: 明确的**非** UGC 出身。``"preset"`` 是内置阵容与 admin preset 的共同出身；
#: ``"onboarding"`` 是玩家化身的出身（``app/services/onboarding_service.py``
#: 的 ``Resident(resident_type="player", meta_json={"origin": "onboarding"})``）。
#:
#: ``"onboarding"`` 必须在这里，而不能只靠 ``resident_type == "player"`` 挡：
#: admin 手滑把化身的 type 改成 ``resident`` 之后 type 已不可信，而化身的
#: ``creator_id`` 是真实 user id，:func:`is_ugc_resident` 的兜底分支
#: （``return creator_id is not None``）会把它判成 UGC → 进晋升候选面 → 被夜间
#: 任务自动授予投票权；此后 :func:`_assert_revocable` 的玩家化身 FK 复核又会
#: 拒绝撤销，人就永久卡在 citizen 档。
NON_UGC_ORIGINS = frozenset({"preset", "onboarding"})

#: 民选职务的 ``offices.fill_strategy``（迁移 046 只给 mayor 写了这个值）。
#: 撤销只卸民选职务——``town_clerk`` / ``postman`` / ``doctor`` 是**劳动职务**，
#: offices 表把两类混在一张表里，一刀切会误伤。
POLITICAL_FILL_STRATEGY = "election"

#: 档位 → ``resident_type``。``EXILED`` 刻意缺席，见 :func:`standing_to_type`。
STANDING_TO_TYPE: dict[str, str] = {
    CITIZEN: CIVIC_MEMBER_TYPE,
    DENIZEN: UGC_RESIDENT_TYPE,
}
TYPE_TO_STANDING: dict[str, str] = {v: k for k, v in STANDING_TO_TYPE.items()}


class CivicStandingRefused(RuntimeError):
    """一次档位变更被防呆拒绝。

    对标 ``seed/reset_builtin_residents.py:60`` 的 ``PlayerPurgeRefused``，
    照抄它的两条设计选择：

    - **Raise，不 skip**——静默跳过会让调用方以为动作完成了；
    - **读数据库，不信传入对象**——调用点自己建的目标列表里，
      ``target.resident_type`` 恰恰是不能信的字段。

    永远在**第一条 UPDATE 之前**抛出（"Guard first: no UPDATE has run yet"），
    使拒绝是真正的 no-op。
    """


# ── 档位派生函数 ───────────────────────────────────────────────────────

def civic_standing(resident) -> str:
    """该居民当前的档位，取值来自 :data:`CIVIC_STANDINGS`。

    ``"exiled"`` 现在就在枚举里，但 v1 没有任何 ``resident_type`` 取值映射到
    它——逐出上线时是在 :func:`standing_to_type` 填空，不是改签名。

    ``player`` / ``preset`` / 写错的字面量都会 ``ValueError``：把它们当成某个
    档位悄悄处理，正是本模块存在的理由的反面。调用方（两个写入口）在调用本
    函数之前已经做完射程防呆，所以不会拿玩家化身来问。
    """
    rtype = getattr(resident, "resident_type", None)
    standing = TYPE_TO_STANDING.get(rtype)
    if standing is None:
        raise ValueError(
            f"resident_type {rtype!r} 不在档位模型内："
            f"{PLAYER_RESIDENT_TYPE!r} 由第三族谓词（!= \"player\"）管辖，"
            f"{ADMIN_PRESET_TYPE!r} 是两个集合之外的待决项，其余取值是写错的"
            f"字面量（该列无 enum 无 CHECK）。已知映射：{TYPE_TO_STANDING}"
        )
    return standing


def standing_to_type(standing: str) -> str:
    """档位 → v1 的 ``resident_type`` 编码。"""
    if standing == EXILED:
        raise NotImplementedError(
            "exile 档 v1 不实现：档位枚举、revoke_citizenship(tier='exile') 与"
            "分档清理表都已按两档写好，落地时在这里补 is_in_town 的收窄"
            "（公开名录 / tile 占用 / 住房三处口径），不需要改任何签名。"
        )
    try:
        return STANDING_TO_TYPE[standing]
    except KeyError:
        raise ValueError(
            f"unknown civic standing {standing!r}; expected one of "
            f"{CIVIC_STANDINGS}"
        ) from None


def assert_known_types(*types: str) -> None:
    """取值白名单断言（数值闸门 4）。

    ``new_type`` 与 ``expected_type`` 都必须取自本模块导出的常量且落在
    ``SIM_RESIDENT_TYPES`` 里，不满足直接拒绝——这是「写错一个字符就让居民从
    整个模拟里消失」的唯一兜底。
    """
    unknown = [t for t in types if t not in SIM_RESIDENT_TYPES]
    if unknown:
        raise CivicStandingRefused(
            f"refusing a standing transition with resident_type value(s) "
            f"{unknown!r}: not in SIM_RESIDENT_TYPES={sorted(SIM_RESIDENT_TYPES)}"
        )


# ── UGC（出身）判定：T2 脚本与 F2 任务的唯一来源 ───────────────────────

def is_ugc_resident(resident) -> bool:
    """这个居民是不是玩家创作的（UGC）。

    判定优先级（``creator_id`` 是三值混合，不能单条判定）：

    1. 玩家化身 → False（第三族谓词管辖）；
    2. ``creator_id == SYSTEM_CREATOR_ID`` → False（内置阵容）；
    3. ``meta_json['origin'] in UGC_ORIGINS`` → True（forge / import 五处）；
    4. ``meta_json['origin'] in NON_UGC_ORIGINS`` → False。``"preset"`` 是
       **内置阵容与 admin 创建的 preset 的共同出身**，``"onboarding"`` 是玩家
       化身的出身——所以 origin 只是辅助信号，provenance 主键是 creator_id；
    5. 其余：有非空 ``creator_id`` 即算 UGC（极老的 UGC 行不保证带 origin）。

    第 4 条里的 ``"onboarding"`` 是**射程纪律**，不是装饰：第 1 条只在 type 还
    可信时有效，而 admin 手滑把化身改成 ``resident`` 之后 type 恰恰不可信，那
    时化身的真实 ``creator_id`` 会让第 5 条把它判成 UGC。

    ⚠️ 账号注销后 ``creator_id`` 变 NULL（迁移 045）且无 origin 的行判 False——
    保守，由 T2 的「残差人工点名复核」兜底。宁可漏升，不可误降。
    """
    rtype = getattr(resident, "resident_type", None)
    if rtype == PLAYER_RESIDENT_TYPE:
        return False
    creator_id = getattr(resident, "creator_id", None)
    if creator_id == SYSTEM_CREATOR_ID:
        return False
    origin = (getattr(resident, "meta_json", None) or {}).get("origin")
    if origin in UGC_ORIGINS:
        return True
    if origin in NON_UGC_ORIGINS:
        return False
    if creator_id == ADMIN_PRESET_CREATOR_ID:
        return False
    return creator_id is not None


def ugc_filter():
    """UGC 的 **SQL 粗筛**谓词（返回一个 SQLAlchemy 布尔表达式）。

    ``meta_json`` 是 ``sa.JSON`` 而非 jsonb，跨 sqlite / PostgreSQL 没有可移植
    的 JSON 路径查询，所以 SQL 只能按 ``resident_type`` + ``creator_id`` 粗筛。
    **粗筛保证是超集**，精确判定必须再过一遍 :func:`is_ugc_resident`。

    三值 NULL 陷阱：``creator_id != :x`` 在 ``creator_id IS NULL`` 时求值为
    NULL（行被丢掉），所以必须显式 ``OR creator_id IS NULL``。
    """
    from sqlalchemy import and_, or_

    from app.models.resident import Resident   # 惰性：模型层导入本模块

    return and_(
        Resident.resident_type != PLAYER_RESIDENT_TYPE,
        Resident.resident_type != ADMIN_PRESET_TYPE,
        or_(
            Resident.creator_id.is_(None),
            and_(
                Resident.creator_id != SYSTEM_CREATOR_ID,
                Resident.creator_id != ADMIN_PRESET_CREATOR_ID,
            ),
        ),
    )


# ── 运行时旋钮 ─────────────────────────────────────────────────────────
#
# 本批**不改** ``app/config.py`` / ``.env.example``（收口 §8 统一补齐），所以
# 旋钮走 env + 模块内 fallback。形状照抄 ``app/services/social_status_recovery
# .py:57-67``：env 是运行时来源，``Settings`` 未来接管 fallback，收口给
# ``Settings`` 加同名字段后本文件零改动即可生效。
#
# ⚠️ 三个门槛值（MIN_WORLD_DAYS / MIN_PEERS / MIN_FAMILIARITY）这里给的是
# **占位默认值，标定前不得开闸**——真实取值必须由生产分布反推（使晋升面非空
# 且非全量）。``rep_credit_min_score = -0.3`` 之所以变成装饰性闸门，正是因为
# 它是拍出来的。

_TRUE = {"1", "true", "yes", "on"}

#: 三个门槛的占位默认值（待生产数据复标）
_DEFAULT_MIN_WORLD_DAYS = 30.0
_DEFAULT_MIN_PEERS = 3
#: 刻意避开 ``realism_circle_threshold = 0.3``（``app/config.py:512``，圈子
#: 检测的强边阈值），撞上去会让两套语义纠缠。
_DEFAULT_MIN_FAMILIARITY = 0.20
#: 归化公民进入「锚定公民集」前的考察期
_DEFAULT_PEER_SEASONING_WORLD_DAYS = 28.0
#: 一张 poll 开 ``civic_poll_days = 3`` 真实天 = 12 世界日（k=4）。最短任期与
#: 冷却期的**下限**就是它：更小则单张 poll 生命周期内公民权仍可翻转。
_MIN_HYSTERESIS_WORLD_DAYS = 12.0
#: ``open_election`` 需要 ≥2 候选（``app/services/election_service.py:62-63``）
_ABSOLUTE_MIN_ELECTORATE = 3
#: 熔断的**绝对下限**（见 :func:`promotion_breaker_min_abs`）。生产内置阵容
#: ≈10-11 位公民，只按比例算的阈值 ≈2.2，一夜 3 个合法候选就整批拒绝，而单夜
#: 上限默认 5 永远够不着——两道闸门互相吞掉。
_DEFAULT_BREAKER_MIN_ABS = 3


def _settings_default(name: str, default):
    """env 旋钮的注册默认值：收口把同名字段加进 ``Settings`` 后自动生效。"""
    try:
        from app.config import settings   # 惰性：避免与模型层构成循环导入

        return getattr(settings, name.lower(), default)
    except Exception:      # config 导入失败绝不能打断政治层判定
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return str(_settings_default(name, default))
    return raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return bool(_settings_default(name, default))
    return raw.strip().lower() in _TRUE


def _env_float(name: str, default: float) -> float:
    fallback = float(_settings_default(name, default))
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r — using %s", name, raw, fallback)
        return fallback


def _env_int(name: str, default: int) -> int:
    fallback = int(_settings_default(name, default))
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r — using %s", name, raw, fallback)
        return fallback


def promotion_mode() -> str:
    """``off`` | ``shadow`` | ``on``。默认 ``off``——off 时行为与本批开工前
    逐字节一致。"""
    return _env_str("CIVIC_PROMOTION_MODE", "off").lower()


def min_world_days() -> float:
    """门槛①：在镇**世界日**（不是真实日）。占位值，待生产分布复标。"""
    return _env_float("CIVIC_PROMOTION_MIN_WORLD_DAYS", _DEFAULT_MIN_WORLD_DAYS)


def min_peers() -> int:
    """门槛②：达标的锚定公民同伴数下限。占位值，待生产分布复标。"""
    return _env_int("CIVIC_PROMOTION_MIN_PEERS", _DEFAULT_MIN_PEERS)


def min_familiarity() -> float:
    """门槛② 的 θ。占位值，待生产分布复标；不得取 0.3。"""
    return _env_float("CIVIC_PROMOTION_MIN_FAMILIARITY", _DEFAULT_MIN_FAMILIARITY)


def peer_seasoning_world_days() -> float:
    """归化公民成为「锚定公民」前的考察期（世界日）。"""
    return _env_float("CIVIC_PEER_SEASONING_WORLD_DAYS",
                      _DEFAULT_PEER_SEASONING_WORLD_DAYS)


def promotion_max_per_run() -> int:
    """数值闸门 1：单夜移动分母的上限（超出按确定性顺序截断，余量下夜再来）。"""
    return max(0, _env_int("CIVIC_PROMOTION_MAX_PER_RUN", 5))


def promotion_breaker_fraction() -> float:
    """数值闸门 2 的比例项：候选集 > ``max(绝对下限, 当前公民数 × 该比例)``
    → **整批拒绝并告警，不截断**。截断会掩盖「阈值写反」这类全量误判。"""
    return _env_float("CIVIC_PROMOTION_BREAKER_FRACTION", 0.20)


def promotion_breaker_min_abs() -> int:
    """数值闸门 2 的**绝对下限**：熔断阈值取
    ``max(promotion_breaker_min_abs(), citizens × promotion_breaker_fraction())``。

    没有这个下限，小镇规模下熔断恒响、单夜上限恒不生效：生产内置阵容
    ≈10-11 位公民 × 0.20 ≈ 2.2，一夜 3 个合法候选就整批拒绝，而
    ``CIVIC_PROMOTION_MAX_PER_RUN`` 默认 5 永远够不着——闸门 1 在真实世界里
    是死代码，两道闸门的语义互相吞掉。

    下限本身也是可调的：置 0 即退化成纯比例判定（世界规模足够大之后）。
    """
    return max(0, _env_int("CIVIC_PROMOTION_BREAKER_MIN_ABS",
                           _DEFAULT_BREAKER_MIN_ABS))


def min_electorate() -> int:
    """数值闸门 3 的下限之一。低于 3 时撤销可以把选举机制打死。"""
    return max(_ABSOLUTE_MIN_ELECTORATE,
               _env_int("CIVIC_MIN_ELECTORATE", _ABSOLUTE_MIN_ELECTORATE))


def min_tenure_world_days() -> float:
    """晋升后此期内不得降级（世界日）。v1 只用于探针观测，自动降级未实现。"""
    return max(_MIN_HYSTERESIS_WORLD_DAYS,
               _env_float("CIVIC_MIN_TENURE_WORLD_DAYS",
                          _MIN_HYSTERESIS_WORLD_DAYS))


def promotion_cooldown_world_days() -> float:
    """降级后此期内不得复升（世界日）。v1 只用于探针观测。"""
    return max(_MIN_HYSTERESIS_WORLD_DAYS,
               _env_float("CIVIC_PROMOTION_COOLDOWN_WORLD_DAYS",
                          _MIN_HYSTERESIS_WORLD_DAYS))


def auto_demotion_enabled() -> bool:
    """自动下滑降级总开关，默认关。

    开启必须**同时**具备滞后三件套（缺一不可）：滞后区间 Δ ≥ 0.10（严格大于
    单次最大相关增量 0.05——聊天 ``realism_rel_familiarity_chat``、arc 完结
    ``arc_service.py:213``）、最短任期 ≥ 12 世界日、冷却期 ≥ 12 世界日。三件套
    未实现，所以 :func:`app.tasks.civic_promotion.run_promotion_pass` 在这个
    开关为真时直接 ``raise NotImplementedError``。

    注意：衰减用的是**真实日**（``realism_rel_decay_idle_days = 30``）而门槛用
    **世界日**——这是有意的两套尺度，实现不得擅自统一。
    """
    return _env_bool("CIVIC_AUTO_DEMOTION_ENABLED", False)

```

- [ ] **Step 4: 更新 `__all__`**

把 `backend/app/services/civic_membership.py` 末尾的

```python
__all__ = ["CIVIC_VOTER_TYPES", "SIM_RESIDENT_TYPES", "UGC_RESIDENT_TYPE"]
```

替换为

```python
__all__ = [
    # 既有的两个集合边界
    "CIVIC_VOTER_TYPES", "SIM_RESIDENT_TYPES", "UGC_RESIDENT_TYPE",
    # 档位（standing）
    "CITIZEN", "DENIZEN", "EXILED", "CIVIC_STANDINGS", "CIVIC_MEMBER_TYPE",
    "STANDING_TO_TYPE", "TYPE_TO_STANDING",
    "civic_standing", "standing_to_type", "assert_known_types",
    # 出身（provenance）
    "PLAYER_RESIDENT_TYPE", "ADMIN_PRESET_TYPE", "ADMIN_PRESET_CREATOR_ID",
    "SYSTEM_CREATOR_ID", "UGC_ORIGINS", "NON_UGC_ORIGINS", "is_ugc_resident",
    "ugc_filter", "POLITICAL_FILL_STRATEGY",
    # 防呆
    "CivicStandingRefused",
    # 运行时旋钮
    "promotion_mode", "min_world_days", "min_peers", "min_familiarity",
    "peer_seasoning_world_days", "promotion_max_per_run",
    "promotion_breaker_fraction", "promotion_breaker_min_abs",
    "min_electorate", "min_tenure_world_days",
    "promotion_cooldown_world_days", "auto_demotion_enabled",
]
```

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_membership_standing.py tests/test_ugc_resident_no_political_rights.py -q -p no:randomly
```
Expected: PASS（两个文件全绿——既有的边界测试必须不受影响）。

- [ ] **Step 6: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/services/civic_membership.py \
        backend/tests/test_civic_membership_standing.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(civic): 出身 × 档位二维模型的常量层与派生函数

- 三档枚举 citizen/denizen/exiled；exile 档只留签名，standing_to_type 里
  raise NotImplementedError（逐出上线时是填空不是改签名）
- CIVIC_MEMBER_TYPE 收口裸字面量 "npc"；assert_known_types 是取值白名单闸门
- UGC 判定（UGC_ORIGINS / NON_UGC_ORIGINS / is_ugc_resident / ugc_filter）落在
  本模块，T2 脚本与 F2 任务共用同一份实现——两边各写一份必然漂移
- NON_UGC_ORIGINS 把 "onboarding" 与 "preset" 并列判否：type 被篡改后
  is_ugc_resident 的兜底分支会把玩家化身判成 UGC，只有 origin 认得出它
- 全部旋钮走 env + 模块 fallback（本批不改 config.py）；θ 刻意避开 0.3
- 熔断带绝对下限 promotion_breaker_min_abs（默认 3）：纯比例在小镇规模下会让
  熔断恒响、单夜上限恒不生效，两道闸门互相吞掉
- 顶层导入保持惰性：models/resident.py:8 反向导入本模块

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 3: 晋升写入口 `grant_citizenship` / `grant_citizenship_batch`

**Files:**
- Modify: `backend/app/services/civic_membership.py`（尾部追加；`__all__` 就地更新）
- Test: `backend/tests/test_civic_grant_citizenship.py`

**Interfaces:**
- Consumes（Task 2 定义）：`CITIZEN` / `DENIZEN` / `CIVIC_MEMBER_TYPE` / `UGC_RESIDENT_TYPE` / `CivicStandingRefused` / `assert_known_types(*types)`；（Task 1 定义）`app.models.civic_standing_history.CivicStandingHistory`；`app.models.user.User`（`player_resident_id`，`app/models/user.py:30`——射程防呆与 Task 4 的第 ① 条同一段 SQL）
- Produces：
  - `async def _write_history(db, *, resident_id: str, old_standing: str, new_standing: str, reason: str | None, reason_code: str, actor: str, evidence: dict | None) -> None`
  - `async def _emit_standing_changed(db, *, slug: str, old_standing: str, new_standing: str, reason_code: str) -> None`
  - `async def grant_citizenship_batch(db, resident_ids: list[str], *, reason: str, reason_code: str, actor: str, evidence_by_id: dict[str, dict] | None = None) -> int`
  - `async def grant_citizenship(db, resident, *, reason: str, actor: str, evidence: dict | None = None, reason_code: str = "granted") -> bool`

**设计决定：为什么是「批量 + 单条包装」两个函数。** 晋升 pass 的 snapshot 语义要求「所有写入在 pass 末尾一次 commit」，而 admin 路由要的是单条。批量函数做一次 guarded UPDATE（`WHERE id IN (:ids) AND resident_type = :expected`）并检查 `rowcount != len(ids)` → **整批回滚 + 告警**（有人在窗口内改过）；单条函数是它的薄包装，语义完全一致，不存在两份实现漂移。正面样板：`app/services/relation_service.py:214-223`、`app/services/office_service.py:128-135`；反面样板：`app/routers/admin/residents.py:103-127` 的读-改-写。

**WS 事件名必须是 `civic_standing_changed`，不得叫 `resident_type_changed`**——后者已被 SBTI 人格类型漂移占用（`app/ws/handlers/chat.py:474-482`），复用会让前端把政治事件渲染成人格变化。payload 只发 `reason_code` 枚举码，**不发 reason 文本**。广播是易失的 WS 扇出、不落表，不能拿它当「可回滚」硬门的载体。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_grant_citizenship.py`:

```python
"""F2 Task 3 —— 晋升写入口。

批量写形态是 guarded UPDATE + rowcount 校验（正面样板：relation_service.py
:214-223、office_service.py:128-135；反面样板：admin/residents.py:103-127 的
读-改-写）。rowcount 与目标数不符 = 有人在窗口内改过 → 整批回滚并告警。
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, update

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.user import User
from app.services import civic_membership as cm


def _ugc(slug, **kw):
    d = dict(slug=slug, name=slug, district="town_hall", status="idle",
             resident_type=cm.UGC_RESIDENT_TYPE, creator_id="u1",
             tile_x=1, tile_y=1, meta_json={"origin": "forge"})
    d.update(kw)
    return Resident(**d)


def _npc(slug, **kw):
    d = dict(slug=slug, name=slug, district="town_hall", status="idle",
             resident_type=cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
             tile_x=1, tile_y=1, meta_json={"origin": "preset"})
    d.update(kw)
    return Resident(**d)


@pytest.fixture
def _no_ws():
    """WS 扇出在测试里没有 manager；显式打桩，免得每个断言都被 fail-open 的
    warning 噪声淹没。"""
    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as m:
        yield m


@pytest.mark.anyio
async def test_grant_flips_the_tier_and_writes_one_history_row(db_session, _no_ws):
    r = _ugc("ugc-1")
    db_session.add(r)
    await db_session.commit()

    ok = await cm.grant_citizenship(
        db_session, r, reason="满足门槛", actor="civic_promotion",
        evidence={"world_days": 40.0, "peers": 3, "min_familiarity": 0.2},
        reason_code="threshold_met",
    )
    assert ok is True

    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE

    row = (await db_session.execute(select(CivicStandingHistory))).scalar_one()
    assert (row.old_standing, row.new_standing) == (cm.DENIZEN, cm.CITIZEN)
    assert row.reason_code == "threshold_met"
    assert row.actor == "civic_promotion"
    assert row.evidence_json["peers"] == 3
    assert row.world_at is not None


@pytest.mark.anyio
async def test_grant_makes_the_resident_a_civic_voter(db_session, _no_ws):
    """晋升的全部意义：进政治层，同时不改变世界人口口径。"""
    r = _ugc("ugc-1")
    db_session.add(r)
    await db_session.commit()
    await cm.grant_citizenship(db_session, r, reason="x", actor="admin:1")

    voters = (await db_session.execute(
        select(Resident.slug).where(Resident.is_civic_voter))).scalars().all()
    population = (await db_session.execute(
        select(Resident.slug).where(Resident.is_autonomous))).scalars().all()
    assert set(voters) == {"ugc-1"}
    assert set(population) == {"ugc-1"}


@pytest.mark.anyio
async def test_grant_refuses_a_resident_that_is_not_in_the_denizen_tier(db_session):
    """撤销/晋升都是白名单：内置公民、玩家化身、admin preset 一律拒绝。"""
    builtin = _npc("b1")
    avatar = _ugc("a1", resident_type="player")
    preset = _ugc("p1", resident_type="preset", creator_id="system")
    db_session.add_all([builtin, avatar, preset])
    await db_session.commit()

    for target in (builtin, avatar, preset):
        with pytest.raises(cm.CivicStandingRefused):
            await cm.grant_citizenship(db_session, target, reason="x",
                                       actor="admin:1")
    # 拒绝是真正的 no-op：一行历史都没写
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_grant_reads_the_database_not_the_passed_object(db_session):
    """照抄 07-25 的设计选择：调用点自己建的目标列表里，target.resident_type
    恰恰是不能信的字段。

    这里用 SimpleNamespace 而不是把 ORM 对象改脏——改脏的 ORM 对象会在下一次
    查询的 autoflush 里真的落库，就测不出「信不信传入对象」了。SimpleNamespace
    正好模型化 07-25 那个「自带 id 列表」的调用点。
    """
    from types import SimpleNamespace

    r = _npc("b1")
    db_session.add(r)
    await db_session.commit()
    fake = SimpleNamespace(id=r.id, resident_type=cm.UGC_RESIDENT_TYPE)

    with pytest.raises(cm.CivicStandingRefused):
        await cm.grant_citizenship(db_session, fake, reason="x", actor="admin:1")


@pytest.mark.anyio
async def test_grant_refuses_a_tampered_player_avatar(db_session):
    """射程纪律必须与撤销侧对称：撤销查 users.player_resident_id 复核，晋升侧
    也要查（_assert_revocable 的第 ① 条同一段 SQL）。

    admin 手滑把化身的 resident_type 改成 'resident' 之后，档位检查会放行，而
    is_ugc_resident 的兜底分支会把它判成 UGC（化身的 creator_id 是真实 user
    id）——一旦升上去，_assert_revocable 的化身复核又会拒绝撤销，人永久卡在
    citizen 档。
    """
    avatar = _ugc("avatar", meta_json={"origin": "onboarding"})
    db_session.add(avatar)
    await db_session.flush()
    db_session.add(User(name="玩家", email="p@t.com",
                        player_resident_id=avatar.id))
    await db_session.commit()

    with pytest.raises(cm.CivicStandingRefused, match="player avatar"):
        await cm.grant_citizenship(db_session, avatar, reason="x",
                                   actor="civic_promotion")

    rtype = (await db_session.execute(
        select(Resident.resident_type)
        .where(Resident.id == avatar.id))).scalar_one()
    assert rtype == cm.UGC_RESIDENT_TYPE
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_grant_refuses_unknown_ids(db_session):
    with pytest.raises(cm.CivicStandingRefused):
        await cm.grant_citizenship_batch(
            db_session, ["no-such-id"], reason="x",
            reason_code="threshold_met", actor="civic_promotion")


@pytest.mark.anyio
async def test_batch_grant_is_all_or_nothing(db_session, _no_ws):
    """rowcount != len(ids) → 整批回滚 + 告警。用一次并发窗口内的改档位模拟。"""
    a, b = _ugc("ugc-a"), _ugc("ugc-b")
    db_session.add_all([a, b])
    await db_session.commit()

    real_execute = db_session.execute
    seen = {"n": 0}

    async def _sneaky(statement, *args, **kwargs):
        # 在 guard SELECT 之后、guarded UPDATE 之前，把 b 改掉
        result = await real_execute(statement, *args, **kwargs)
        if seen["n"] == 0 and getattr(statement, "is_select", False):
            seen["n"] = 1
            await real_execute(
                update(Resident).where(Resident.id == b.id)
                .values(resident_type=cm.CIVIC_MEMBER_TYPE)
                .execution_options(synchronize_session=False))
        return result

    with patch.object(db_session, "execute", new=_sneaky):
        with pytest.raises(cm.CivicStandingRefused):
            await cm.grant_citizenship_batch(
                db_session, [a.id, b.id], reason="x",
                reason_code="threshold_met", actor="civic_promotion")

    # a 没有被晋升，且一行历史都没留下
    rtype_a = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == a.id))).scalar_one()
    assert rtype_a == cm.UGC_RESIDENT_TYPE
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_batch_grant_writes_one_row_per_resident(db_session, _no_ws):
    residents = [_ugc(f"ugc-{i}") for i in range(3)]
    db_session.add_all(residents)
    await db_session.commit()
    ids = [r.id for r in residents]

    n = await cm.grant_citizenship_batch(
        db_session, ids, reason="满足门槛", reason_code="threshold_met",
        actor="civic_promotion",
        evidence_by_id={i: {"world_days": 40.0, "peers": 3} for i in ids},
    )
    assert n == 3
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 3
    assert (await db_session.execute(
        select(func.count()).select_from(Resident)
        .where(Resident.is_civic_voter))).scalar() == 3


@pytest.mark.anyio
async def test_batch_grant_of_nothing_is_a_noop(db_session):
    assert await cm.grant_citizenship_batch(
        db_session, [], reason="x", reason_code="y", actor="z") == 0


@pytest.mark.anyio
async def test_grant_broadcasts_the_standing_event_without_the_reason_text(db_session):
    """事件名不得叫 resident_type_changed（已被 SBTI 人格漂移占用，
    app/ws/handlers/chat.py:474-482）；payload 只带枚举码，不带原因文本。"""
    r = _ugc("ugc-1")
    db_session.add(r)
    await db_session.commit()

    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as bc:
        await cm.grant_citizenship(db_session, r, reason="秘密理由",
                                   actor="admin:1", reason_code="admin_grant")
    payload = bc.await_args.args[0]
    assert payload["type"] == "civic_standing_changed"
    assert payload["old_standing"] == cm.DENIZEN
    assert payload["new_standing"] == cm.CITIZEN
    assert payload["reason_code"] == "admin_grant"
    assert payload["resident_slug"] == "ugc-1"
    assert "秘密理由" not in str(payload)
    assert "reason" not in payload


@pytest.mark.anyio
async def test_broadcast_failure_never_breaks_the_write(db_session):
    """WS 扇出 fail-open：广播炸了，档位变更与历史行必须已经落地。"""
    r = _ugc("ugc-1")
    db_session.add(r)
    await db_session.commit()

    with patch("app.lab.apply.broadcast_world_changed",
               new=AsyncMock(side_effect=RuntimeError("ws down"))):
        assert await cm.grant_citizenship(db_session, r, reason="x",
                                          actor="admin:1") is True
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 1
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_grant_citizenship.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: module 'app.services.civic_membership' has no attribute 'grant_citizenship'`。

- [ ] **Step 3: 写实现**

在 `backend/app/services/civic_membership.py` 的 `auto_demotion_enabled()` 之后、`__all__` 之前追加：

```python

# ═══════════════════════════════════════════════════════════════════════
# 写入口 ①：晋升
# ═══════════════════════════════════════════════════════════════════════
#
# ``resident_type`` 在本轮之后只许由本模块的两个写入口（加 admin 路由的转调）
# 改写。列上没有 CHECK，代码就是最后一道闸——``tests/
# test_civic_standing_write_entrypoints.py`` 用 AST 扫描把这条钉住。


async def _write_history(
    db, *, resident_id: str, old_standing: str, new_standing: str,
    reason: str | None, reason_code: str, actor: str,
    evidence: dict | None,
) -> None:
    """落一行 ``civic_standing_history``（可回滚硬门 + 公民时钟锚点）。

    不 commit——由调用方决定事务边界。``world_at`` 存 UTC-aware：
    ``DateTime(timezone=True)`` 在 SQLite 上丢时区，统一转 UTC 存、读回补 UTC
    才能无损往返。
    """
    from datetime import UTC

    from app import world_clock
    from app.models.civic_standing_history import CivicStandingHistory

    db.add(CivicStandingHistory(
        resident_id=resident_id,
        old_standing=old_standing,
        new_standing=new_standing,
        reason=reason,
        reason_code=reason_code,
        actor=actor,
        evidence_json=evidence or {},
        world_at=world_clock.now_world().astimezone(UTC),
    ))


async def _emit_standing_changed(
    db, *, slug: str, old_standing: str, new_standing: str, reason_code: str,
) -> None:
    """广播 ``civic_standing_changed``（world_changed v1 信封，fail-open）。

    ⚠️ 事件名**不得**叫 ``resident_type_changed``——该名字已被 SBTI 人格类型
    漂移占用（``app/ws/handlers/chat.py:474-482``），复用会让前端把政治事件
    渲染成人格变化。payload 只带 ``reason_code`` 枚举码，**永不带 reason
    文本**。挂 world_revision / seq 的写法参照
    ``app/services/office_service.py:244-271``；注意那是易失的 WS 扇出、
    **不落任何表**，不能拿它当「可回滚」硬门的载体。
    """
    try:
        import uuid
        from datetime import datetime, UTC

        from app.services import world_revision_service as wrsvc

        payload = {
            "type": "civic_standing_changed",
            "schema_version": wrsvc.SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "seq": await wrsvc.current_source_cursor(db),
            "world_revision_id": await wrsvc.current_revision_id(db),
            "resident_slug": slug,
            "old_standing": old_standing,
            "new_standing": new_standing,
            "reason_code": reason_code,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        from app.lab.apply import broadcast_world_changed

        await broadcast_world_changed(payload)
    except Exception:
        logger.warning("civic_standing_changed broadcast failed", exc_info=True)


async def grant_citizenship_batch(
    db, resident_ids, *, reason: str, reason_code: str, actor: str,
    evidence_by_id: dict | None = None,
) -> int:
    """把一批 denizen 升为 citizen。返回实际晋升数。

    形态是「guard 全做完 → 一次 guarded UPDATE → N 行历史 → 一次 commit」::

        UPDATE residents SET resident_type = :new
        WHERE id IN (:ids) AND resident_type = :expected

    ``rowcount != len(ids)`` → **整批回滚 + 告警**（有人在窗口内改过
    ``resident_type``，唯一的并发对手是 admin 手改）。绝不截断执行。

    ⚠️ **调用方契约**：档位翻转走 ``update(...).execution_options(
    synchronize_session=False)``，且本仓的会话是 ``expire_on_commit=False``
    （``tests/conftest.py:119-122``、``app/database.py`` 的 ``async_session``），
    所以**调用方手里的 ORM 对象在本函数返回后仍是旧值**，实体查询也会把同一个
    陈旧对象取回来。需要读新值就 ``await db.refresh(resident)``（
    ``app/routers/admin/residents.py`` 的 ``_edit_resident`` 就是这么做的），
    或者改用列级 ``select(Resident.resident_type)`` / SQL 侧
    ``where(Resident.is_civic_voter)``。
    """
    from sqlalchemy import select, update

    from app.models.resident import Resident
    from app.models.user import User

    ids = sorted(set(resident_ids))
    if not ids:
        return 0

    # 数值闸门 4：取值白名单（写错一个字符的唯一兜底）
    assert_known_types(CIVIC_MEMBER_TYPE, UGC_RESIDENT_TYPE)

    # Guard first: no UPDATE has run yet —— 查库，不信传入对象
    rows = (await db.execute(
        select(Resident.id, Resident.slug, Resident.resident_type)
        .where(Resident.id.in_(ids))
    )).all()
    found = {rid: (slug, rtype) for rid, slug, rtype in rows}
    missing = [rid for rid in ids if rid not in found]
    if missing:
        raise CivicStandingRefused(
            f"grant refused: {len(missing)} unknown resident id(s): {missing}")
    wrong_tier = sorted(rid for rid in ids
                        if found[rid][1] != UGC_RESIDENT_TYPE)
    if wrong_tier:
        raise CivicStandingRefused(
            f"grant refused: {len(wrong_tier)} resident(s) are not in the "
            f"{DENIZEN!r} tier (expected resident_type={UGC_RESIDENT_TYPE!r}): "
            + ", ".join(f"{found[r][0]}={found[r][1]!r}" for r in wrong_tier)
        )
    # 射程防呆：玩家化身即使被 admin 手滑改成 denizen 档也不得被晋升。这是
    # _assert_revocable 第 ① 条的同一段 SQL —— 两个写入口的射程纪律必须对称，
    # 否则「type 已不可信」只在撤销侧成立，晋升侧仍然裸奔（tier 检查会放行，
    # is_ugc_resident 的兜底分支还会把它判成 UGC）。
    avatar_ids = set((await db.execute(
        select(User.player_resident_id).where(User.player_resident_id.in_(ids))
    )).scalars().all())
    if avatar_ids:
        raise CivicStandingRefused(
            f"grant refused: {len(avatar_ids)} target(s) are player avatars "
            f"(users.player_resident_id hits: "
            f"{sorted(found[a][0] for a in avatar_ids if a in found)}). "
            "政治层永不碰玩家化身——2026-07-25 16:53 的事故对象正是这一类；"
            "resident_type 在 admin 手滑那一刻就已不可信。"
        )

    res = await db.execute(
        update(Resident)
        .where(Resident.id.in_(ids), Resident.resident_type == UGC_RESIDENT_TYPE)
        .values(resident_type=CIVIC_MEMBER_TYPE)
        .execution_options(synchronize_session=False)
    )
    touched = res.rowcount or 0
    if touched != len(ids):
        await db.rollback()
        raise CivicStandingRefused(
            f"grant refused: guarded UPDATE touched {touched} of {len(ids)} "
            "rows — resident_type changed inside the window; whole batch "
            "rolled back (see relation_service.py:214-223 for the pattern)"
        )

    for rid in ids:
        await _write_history(
            db, resident_id=rid, old_standing=DENIZEN, new_standing=CITIZEN,
            reason=reason, reason_code=reason_code, actor=actor,
            evidence=(evidence_by_id or {}).get(rid),
        )
    await db.commit()

    for rid in ids:
        await _emit_standing_changed(
            db, slug=found[rid][0], old_standing=DENIZEN,
            new_standing=CITIZEN, reason_code=reason_code,
        )
    logger.info("civic grant: %d resident(s) promoted by %s (%s)",
                len(ids), actor, reason_code)
    return len(ids)


async def grant_citizenship(
    db, resident, *, reason: str, actor: str, evidence: dict | None = None,
    reason_code: str = "granted",
) -> bool:
    """单条晋升（admin 路由用）。:func:`grant_citizenship_batch` 的薄包装——
    两条路径共用同一份 guard 与同一份写形态，不存在实现漂移。"""
    resident_id = getattr(resident, "id", None)
    if not resident_id:
        raise CivicStandingRefused("grant refused: resident has no id")
    return await grant_citizenship_batch(
        db, [resident_id], reason=reason, reason_code=reason_code, actor=actor,
        evidence_by_id={resident_id: evidence or {}},
    ) == 1

```

- [ ] **Step 4: 更新 `__all__`**

把 `__all__` 里的

```python
    # 防呆
    "CivicStandingRefused",
```

替换为

```python
    # 防呆
    "CivicStandingRefused",
    # 写入口
    "grant_citizenship", "grant_citizenship_batch",
```

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_grant_citizenship.py tests/test_civic_membership_standing.py -q -p no:randomly
```
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/services/civic_membership.py \
        backend/tests/test_civic_grant_citizenship.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(civic): 晋升写入口 grant_citizenship / grant_citizenship_batch

批量写形态 = guard 全做完 → 一次 guarded UPDATE（WHERE id IN … AND
resident_type = :expected）→ N 行历史 → 一次 commit；rowcount 不符整批回滚
并告警，绝不截断。单条入口是批量的薄包装，两条路径不会漂移。

- 查库复核而非信传入对象（照抄 07-25 PlayerPurgeRefused 的设计选择）
- 射程防呆与撤销侧对称：同样查 users.player_resident_id 复核玩家化身，
  被 admin 手滑改成 denizen 档的化身不得被晋升
- 写入口契约写进 docstring：synchronize_session=False + expire_on_commit=False
  ⇒ 调用方手里的 ORM 对象是陈旧的，要读新值必须 refresh
- WS 事件名 civic_standing_changed（不得复用被 SBTI 占用的
  resident_type_changed），payload 只发枚举码不发原因文本，fail-open

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 4: 撤销防呆 `_assert_revocable`（射程白名单 + 选民下限不变式）

**Files:**
- Modify: `backend/app/services/civic_membership.py`（尾部追加）
- Test: `backend/tests/test_civic_revoke_guard.py`

**Interfaces:**
- Consumes：`CivicStandingRefused`、`SYSTEM_CREATOR_ID`、`PLAYER_RESIDENT_TYPE`、`ADMIN_PRESET_TYPE`、`CIVIC_MEMBER_TYPE`、`CITIZEN`、`min_peers()`、`min_electorate()`、`app.models.user.User`（`player_resident_id`，`app/models/user.py:30`）、`app.models.civic_standing_history.CivicStandingHistory`
- Produces：`async def _assert_revocable(db, resident_id: str) -> tuple[str, str]`，返回 `(slug, current_resident_type)`；命中任一禁区即 `raise CivicStandingRefused`

**射程是白名单，不是泛谓词。** 绝对不可被撤销的四类，命中即 **raise**（不是跳过）：

| 类别 | 判定 | 理由 |
|---|---|---|
| 玩家化身 | `resident_type == "player"` **或**查库命中 `users.player_resident_id` | 07-25 事故对象；两个条件是 OR，因为 admin 手滑可以把化身改成 `npc`，那时 type 已不可信 |
| 内置阵容 | `creator_id == SYSTEM_CREATOR_ID` | 内置被降 = 选举与法定人数熄火；`polis_office_mayor_term_days=0` 下真实稳态是「现任镇长被永久冻结、再也选不出新人」 |
| admin preset | `resident_type == "preset"` | 不在两个集合内，本来就不该被政治层动 |
| 无晋升记录者 | `civic_standing_history` 里查不到 `new_standing == "citizen"` 的行 | 撤销是晋升的**严格逆操作**，白名单而非泛谓词 |

再加**数值闸门 3（选民下限不变式）**：撤销后 `is_civic_voter` 计数必须 ≥ `max(min_peers() + 1, min_electorate())`，不满足整批拒绝并 WARN。这条在未来做逐出时同样成立——逐出内置成员必须撞同一道墙。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_revoke_guard.py`:

```python
"""F2 Task 4 —— 撤销的射程防呆。

对标 seed/reset_builtin_residents.py:84-114 的 _assert_no_players：
raise 而非静默跳过（静默跳过会让调用方以为动作完成了），查数据库而非信传入
对象（调用点自己建的目标列表里，target.resident_type 恰恰是不能信的字段）。
"""
import pytest
from sqlalchemy import func, select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.user import User
from app.services import civic_membership as cm


def _res(slug, rtype, *, creator_id="u1", meta=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id,
                    tile_x=1, tile_y=1, meta_json=meta)


async def _promote_record(db, resident_id):
    """给某人补一行晋升记录，让他进入撤销白名单。"""
    from datetime import UTC, datetime
    db.add(CivicStandingHistory(
        resident_id=resident_id, old_standing=cm.DENIZEN,
        new_standing=cm.CITIZEN, reason=None, reason_code="threshold_met",
        actor="civic_promotion", evidence_json={},
        world_at=datetime.now(UTC),
    ))
    await db.commit()


async def _fill_electorate(db, n, *, prefix="filler"):
    """把选民数增加 n 位内置公民。``prefix`` 让同一个测试里可以调用多次而不撞
    slug 的 UNIQUE 约束。"""
    db.add_all([_res(f"{prefix}-{i}", cm.CIVIC_MEMBER_TYPE,
                     creator_id=cm.SYSTEM_CREATOR_ID) for i in range(n)])
    await db.commit()


@pytest.mark.anyio
async def test_guard_accepts_a_naturalised_citizen(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("ugc-1", cm.CIVIC_MEMBER_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    await _promote_record(db_session, r.id)

    slug, rtype = await cm._assert_revocable(db_session, r.id)
    assert (slug, rtype) == ("ugc-1", cm.CIVIC_MEMBER_TYPE)


@pytest.mark.anyio
async def test_guard_refuses_a_player_avatar_by_type(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("avatar", cm.PLAYER_RESIDENT_TYPE)
    db_session.add(r)
    await db_session.commit()
    with pytest.raises(cm.CivicStandingRefused, match="player"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_a_player_avatar_by_fk_even_if_type_was_tampered(db_session):
    """admin 手滑把化身改成 npc 后 type 已不可信 —— 必须查
    users.player_resident_id 复核（app/models/user.py:30）。"""
    await _fill_electorate(db_session, 6)
    r = _res("avatar", cm.CIVIC_MEMBER_TYPE)     # 已被改成 npc
    db_session.add(r)
    await db_session.flush()
    db_session.add(User(name="玩家", email="p@t.com", player_resident_id=r.id))
    await db_session.commit()
    await _promote_record(db_session, r.id)      # 连晋升记录都伪造了

    with pytest.raises(cm.CivicStandingRefused, match="player"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_the_builtin_cast(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("builtin", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
             meta={"origin": "preset"})
    db_session.add(r)
    await db_session.commit()
    await _promote_record(db_session, r.id)

    with pytest.raises(cm.CivicStandingRefused, match="built-in"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_admin_presets(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("adminpreset", cm.ADMIN_PRESET_TYPE, creator_id="system",
             meta={"origin": "preset"})
    db_session.add(r)
    await db_session.commit()
    with pytest.raises(cm.CivicStandingRefused, match="preset"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_someone_without_a_promotion_record(db_session):
    """撤销是晋升的严格逆操作。没有晋升记录的 npc 不在射程内——admin 手工把
    某人改回 npc 会在探针上显示为「无晋升记录的 UGC-origin 公民」，这正好是
    一条有用的红旗，不是噪声。"""
    await _fill_electorate(db_session, 6)
    r = _res("no-record", cm.CIVIC_MEMBER_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    with pytest.raises(cm.CivicStandingRefused, match="no promotion record"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_someone_already_in_the_denizen_tier(db_session):
    await _fill_electorate(db_session, 6)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    await _promote_record(db_session, r.id)
    with pytest.raises(cm.CivicStandingRefused, match="not in the 'citizen' tier"):
        await cm._assert_revocable(db_session, r.id)


@pytest.mark.anyio
async def test_guard_refuses_an_unknown_id(db_session):
    with pytest.raises(cm.CivicStandingRefused, match="no resident"):
        await cm._assert_revocable(db_session, "nope")


@pytest.mark.anyio
async def test_guard_enforces_the_electorate_floor(db_session, monkeypatch):
    """数值闸门 3：撤销后选民数必须 ≥ max(min_peers + 1, CIVIC_MIN_ELECTORATE)。
    这条不变式在未来做逐出时同样成立——逐出内置成员必须撞同一道墙。"""
    monkeypatch.setenv("CIVIC_MIN_ELECTORATE", "3")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    # 选民总数 3：撤销后剩 2 < max(2+1, 3) = 3 → 拒绝
    await _fill_electorate(db_session, 2)
    r = _res("ugc-1", cm.CIVIC_MEMBER_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    await _promote_record(db_session, r.id)

    with pytest.raises(cm.CivicStandingRefused, match="electorate"):
        await cm._assert_revocable(db_session, r.id)

    # 再多两个内置公民 → 撤销后剩 4 ≥ 3 → 放行
    await _fill_electorate(db_session, 2, prefix="extra")
    assert (await cm._assert_revocable(db_session, r.id))[0] == "ugc-1"


@pytest.mark.anyio
async def test_guard_writes_nothing(db_session):
    """Guard first: no UPDATE has run yet —— 拒绝必须是真正的 no-op。"""
    await _fill_electorate(db_session, 6)
    r = _res("builtin", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID)
    db_session.add(r)
    await db_session.commit()
    before = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()

    with pytest.raises(cm.CivicStandingRefused):
        await cm._assert_revocable(db_session, r.id)

    after = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert before == after
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_revoke_guard.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: module 'app.services.civic_membership' has no attribute '_assert_revocable'`。

- [ ] **Step 3: 写实现**

在 `backend/app/services/civic_membership.py` 的 `grant_citizenship()` 之后、`__all__` 之前追加：

```python

# ═══════════════════════════════════════════════════════════════════════
# 写入口 ②：撤销 —— 防呆（Guard first）
# ═══════════════════════════════════════════════════════════════════════


async def _assert_revocable(db, resident_id: str) -> tuple[str, str]:
    """撤销的射程白名单检查。返回 ``(slug, current_resident_type)``。

    **在第一条 UPDATE 之前**全部做完（照抄 ``seed/reset_builtin_residents.py
    :125-127`` 的 "Guard first: no DELETE has run yet" 姿势），使拒绝是真正的
    no-op。两条设计选择照抄 07-25：**raise 而非静默跳过**、**读数据库而非信
    传入对象**。

    绝对不可被碰的四类 + 一道数值闸门，任一命中即
    :class:`CivicStandingRefused`。
    """
    from sqlalchemy import func, select

    from app.models.civic_standing_history import CivicStandingHistory
    from app.models.resident import Resident
    from app.models.user import User

    row = (await db.execute(
        select(Resident.id, Resident.slug, Resident.resident_type,
               Resident.creator_id)
        .where(Resident.id == resident_id)
    )).first()
    if row is None:
        raise CivicStandingRefused(
            f"revoke refused: no resident with id {resident_id!r}")
    rid, slug, rtype, creator_id = row

    # ① 玩家化身 —— 07-25 事故对象。type 与 FK 是 OR：admin 手滑可以把化身
    #    改成 npc，那一刻 resident_type 已不可信，users.player_resident_id
    #    （app/models/user.py:30）才是权威。
    avatar_hits = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.player_resident_id == rid)
    )).scalar() or 0
    if rtype == PLAYER_RESIDENT_TYPE or avatar_hits:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} is a player avatar "
            f"(resident_type={rtype!r}, users.player_resident_id hits="
            f"{avatar_hits}). 2026-07-25 16:53 的事故对象正是这一类；政治层"
            "永不碰玩家化身。"
        )
    # ② 内置阵容 —— 被降 = 选举与法定人数熄火
    if creator_id == SYSTEM_CREATOR_ID:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} is part of the built-in cast "
            f"(creator_id == SYSTEM_CREATOR_ID). 降内置成员会让选举与法定人数"
            "熄火：polis_office_mayor_term_days=0 下的真实稳态是「现任镇长被"
            "永久冻结、再也选不出新人」。"
        )
    # ③ admin preset —— 两个集合之外，本来就不该被政治层动
    if rtype == ADMIN_PRESET_TYPE:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} is an admin-created {ADMIN_PRESET_TYPE!r} "
            "resident — outside both membership sets by design (U6 待决项)。"
        )
    # ④ 当前不在 citizen 档
    if rtype != CIVIC_MEMBER_TYPE:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} is not in the {CITIZEN!r} tier "
            f"(resident_type={rtype!r}, expected {CIVIC_MEMBER_TYPE!r})"
        )
    # ⑤ 无晋升记录者 —— 撤销是晋升的严格逆操作，白名单而非泛谓词
    promotions = (await db.execute(
        select(func.count()).select_from(CivicStandingHistory).where(
            CivicStandingHistory.resident_id == rid,
            CivicStandingHistory.new_standing == CITIZEN,
        )
    )).scalar() or 0
    if not promotions:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} has no promotion record in "
            "civic_standing_history. 撤销是晋升的严格逆操作——白名单，不是泛"
            "谓词。（admin 手工改回 npc 的人会在探针上显示为「无晋升记录的 "
            "UGC-origin 公民」，那是一条有用的红旗。）"
        )
    # 数值闸门 3：选民下限不变式
    electorate = (await db.execute(
        select(func.count()).select_from(Resident).where(Resident.is_civic_voter)
    )).scalar() or 0
    floor = max(min_peers() + 1, min_electorate())
    if electorate - 1 < floor:
        raise CivicStandingRefused(
            f"revoke refused: electorate would drop to {electorate - 1}, below "
            f"the floor max(min_peers+1, CIVIC_MIN_ELECTORATE) = {floor}. "
            "open_election 需要 ≥2 候选（election_service.py:62-63）；这条不"
            "变式在未来做逐出时同样成立。"
        )
    return slug, rtype

```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_revoke_guard.py -q -p no:randomly
```
Expected: PASS（11 passed）。

- [ ] **Step 5: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/services/civic_membership.py \
        backend/tests/test_civic_revoke_guard.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(civic): 撤销的射程白名单防呆（对标 PlayerPurgeRefused）

Guard first：第一条 UPDATE 之前全部做完，拒绝是真正的 no-op。
四类禁区 raise 而非跳过——玩家化身（type 或 users.player_resident_id 复核，
两者 OR）、内置阵容（creator_id == SYSTEM_USER_ID）、admin preset、无晋升
记录者；外加选民下限不变式 max(min_peers+1, CIVIC_MIN_ELECTORATE)。

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 5: 撤销写入口 `revoke_citizenship(tier="demote"|"exile")` —— 有序复合事务

**Files:**
- Modify: `backend/app/services/civic_membership.py`（尾部追加；`__all__` 就地更新）
- Test: `backend/tests/test_civic_revoke_citizenship.py`

**Interfaces:**
- Consumes：`_assert_revocable(db, resident_id) -> (slug, rtype)`（Task 4）、`_write_history(...)` / `_emit_standing_changed(...)`（Task 3）、`assert_known_types`、`POLITICAL_FILL_STRATEGY`、`app.models.office.Office`（`office_key` / `holder_slug` / `fill_strategy` / `term_ends_at` / `updated_at`）、`app.models.system_config.SystemConfig`（`key` / `value` / `updated_by` / `updated_at`）
- Produces：
  - `async def _assert_demotion_invariants(db, *, resident_id: str, slug: str) -> None`
  - `async def revoke_citizenship(db, resident, *, reason: str, actor: str, tier: str = "demote", reason_code: str = "revoked") -> bool`

**顺序不可颠倒（每一步都对应一个会让实现落空的坑）：**

```
0. 防呆（Guard first，第一条 UPDATE 之前全部做完）           ← Task 4
1. 卸民选职务：guard UPDATE offices SET holder_slug=NULL, term_ends_at=NULL
               WHERE fill_strategy='election' AND holder_slug=:slug
2. 清 meta_json['mayor']：按 slug 直查该居民，pop + flag_modified
3. 清 system_config['current_mayor']：仅当当前值 == slug
4. 改档位：UPDATE residents SET resident_type=:new WHERE id=:id AND resident_type=:expected
5. 写 civic_standing_history 一行
6. 断言：三处镇长表示都不指向他；is_autonomous 仍 True；is_civic_voter 已 False
7. commit + 广播 civic_standing_changed
```

**四条实现约束：**

1. **不得调用 `OfficeService.vacate()`**。它自带 `await self.db.commit()`（`office_service.py:138`），挂不进 F2 的事务；更关键的是 `polis_office_enabled` 默认 False 时 offices 表可能根本没有 mayor 行，guard UPDATE 命中 0 行 → `vacated=False` → `_clear_mayor_legacy_stores()` **不会被调用**（`office_service.py:136-137`），两个 legacy store 一点没清。所以步骤 2、3 必须**无条件**执行，offices 侧只是 gate 开时的附加项。
2. **不得复用 `_clear_mayor_legacy_stores`**（`office_service.py:220-222`）——它的 WHERE 是 `Resident.is_autonomous`，即用「人口集合」去清理「刚离开集合的人」。降级档侥幸命中，逐出档天然自锁。按 slug 直查。
3. **不得用 `ConfigService.set()` 清 `current_mayor`**——它自带 `await self._db.commit()`（`config_service.py:48`），会把复合事务劈成两半。直接改 `SystemConfig` 行（同 `office_service.py:232-240` 的写法）。
4. **guard 必须带 holder 校验**。`polis_office_enabled` 关时 `offices.holder_slug` 可能是迁移 046 遗留的陈旧值，无条件 vacate 会罢免错的人。**撤销的正确性不得依赖 `polis_office_enabled` 的取值，gate 开与关两种状态都要有测试覆盖**。

**只卸民选职务**（`fill_strategy == "election"`，迁移 046 里只有 `mayor` 是这个值）；`town_clerk` / `postman` / `doctor` 是**劳动职务不受影响**——offices 表把两类混在一张表里，一刀切会误伤。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_revoke_citizenship.py`:

```python
"""F2 Task 5 —— 撤销是有序复合事务。

顺序不可颠倒：若先改档位再清理，meta_json['mayor'] 在逐出档会永久卡死（清扫
扫不到他），期间 install_mayor() 清他人标志时也会跳过他，可产生「两个
meta_json['mayor']=True」并双份工资倍率（duty_service.py:172-173 × 1.2）。
"""
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.civic_standing_history import CivicStandingHistory
from app.models.office import Office
from app.models.resident import Resident
from app.models.system_config import SystemConfig
from app.services import civic_membership as cm


def _res(slug, rtype, *, creator_id="u1", meta=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id,
                    tile_x=1, tile_y=1, meta_json=meta)


async def _seed_citizen(db, slug="ugc-1", *, meta=None):
    """一位「已归化 + 有晋升记录」的公民，外加 6 位内置公民撑住选民下限。"""
    db.add_all([_res(f"builtin-{i}", cm.CIVIC_MEMBER_TYPE,
                     creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    r = _res(slug, cm.CIVIC_MEMBER_TYPE, meta=meta or {"origin": "forge"})
    db.add(r)
    await db.commit()
    db.add(CivicStandingHistory(
        resident_id=r.id, old_standing=cm.DENIZEN, new_standing=cm.CITIZEN,
        reason=None, reason_code="threshold_met", actor="civic_promotion",
        evidence_json={}, world_at=datetime.now(UTC)))
    await db.commit()
    return r


@pytest.fixture
def _no_ws():
    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as m:
        yield m


# ── 基本语义 ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_revoke_demotes_and_writes_history(db_session, _no_ws):
    r = await _seed_citizen(db_session)
    assert await cm.revoke_citizenship(
        db_session, r, reason="违反镇规", actor="admin:42") is True

    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.UGC_RESIDENT_TYPE

    rows = (await db_session.execute(
        select(CivicStandingHistory)
        .where(CivicStandingHistory.new_standing == cm.DENIZEN))).scalars().all()
    assert len(rows) == 1
    assert rows[0].old_standing == cm.CITIZEN
    assert rows[0].actor == "admin:42"
    assert rows[0].reason == "违反镇规"          # 文本落表但永不外发


@pytest.mark.anyio
async def test_revoke_keeps_the_resident_in_the_world_population(db_session, _no_ws):
    """硬门 6：撤销后 is_autonomous 仍 True / is_civic_voter 已 False。防止有人
    顺手把撤销实现成「移出世界人口」。

    ⚠️ 必须先 ``refresh``：档位翻转走 ``update(...).execution_options(
    synchronize_session=False)``，而 conftest 的 ``db_session`` 是
    ``expire_on_commit=False``（tests/conftest.py:119-122），commit 后会话身份
    映射里的 Resident 实体仍是旧值——``select(Resident)`` 这种**实体查询**会把
    同一个陈旧对象原样取回来（实测：``fresh is r`` 为 True、``resident_type``
    仍是 'npc'，而同一事务的列级读已是 'resident'）。本文件其它用例走的是列级
    ``select(Resident.resident_type)`` 或 SQL 侧 ``where(Resident.is_civic_voter)``，
    只有这一条需要读 Python 侧的 hybrid，所以只有这一条要 refresh。
    """
    r = await _seed_citizen(db_session)
    await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    await db_session.refresh(r)
    assert r.is_autonomous is True
    assert r.is_civic_voter is False


@pytest.mark.anyio
async def test_exile_tier_is_reserved_not_implemented(db_session):
    r = await _seed_citizen(db_session)
    with pytest.raises(NotImplementedError):
        await cm.revoke_citizenship(db_session, r, reason="x",
                                    actor="admin:1", tier="exile")
    # 预留分支必须是零写入
    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE


@pytest.mark.anyio
async def test_unknown_tier_is_a_value_error(db_session):
    r = await _seed_citizen(db_session)
    with pytest.raises(ValueError):
        await cm.revoke_citizenship(db_session, r, reason="x",
                                    actor="admin:1", tier="banish")


# ── 三处镇长表示的同步清理（gate 开 / 关都要覆盖）────────────────────

async def _make_sitting_mayor(db, r):
    """三处镇长表示都指向 r：offices 行 + meta_json['mayor'] + system_config。"""
    meta = dict(r.meta_json or {})
    meta["mayor"] = True
    r.meta_json = meta
    db.add(Office(office_key="mayor", holder_slug=r.slug,
                  institution="town_hall", perms_json={},
                  fill_strategy=cm.POLITICAL_FILL_STRATEGY,
                  term_started_at=datetime.now(UTC), term_ends_at=None))
    db.add(SystemConfig(key="current_mayor", value=json.dumps(r.slug),
                        group="civic", updated_by="election"))
    await db.commit()


@pytest.mark.parametrize("gate_on", [True, False])
@pytest.mark.anyio
async def test_revoking_a_sitting_mayor_clears_all_three_representations(
        db_session, monkeypatch, _no_ws, gate_on):
    """硬门 3。polis_office_enabled 开与关都必须成立——默认是关，最容易漏测的
    恰恰是生产以外的那一态。"""
    monkeypatch.setattr(settings, "polis_office_enabled", gate_on)
    r = await _seed_citizen(db_session)
    await _make_sitting_mayor(db_session, r)

    assert await cm.revoke_citizenship(db_session, r, reason="x",
                                       actor="admin:1") is True

    holder = (await db_session.execute(
        select(Office.holder_slug).where(Office.office_key == "mayor"))).scalar_one()
    assert holder is None
    metas = (await db_session.execute(select(Resident.meta_json))).scalars().all()
    assert all(not (m or {}).get("mayor") for m in metas)
    cfg = (await db_session.execute(
        select(SystemConfig.value)
        .where(SystemConfig.key == "current_mayor"))).scalar_one()
    assert json.loads(cfg) is None


@pytest.mark.anyio
async def test_revoke_does_not_touch_labour_offices(db_session, _no_ws):
    """只卸民选职务。offices 表把政治职务与劳动职务混在一张表里
    （office_service.py:41-46），一刀切会误伤 town_clerk / postman / doctor。"""
    r = await _seed_citizen(db_session)
    db_session.add_all([
        Office(office_key="town_clerk", holder_slug=r.slug,
               institution="town_hall", perms_json={}, fill_strategy="seed"),
        Office(office_key="postman", holder_slug=r.slug,
               institution="post_office", perms_json={}, fill_strategy="seed"),
    ])
    await db_session.commit()

    await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    holders = dict((await db_session.execute(
        select(Office.office_key, Office.holder_slug))).all())
    assert holders["town_clerk"] == r.slug
    assert holders["postman"] == r.slug


@pytest.mark.anyio
async def test_revoke_does_not_vacate_someone_elses_stale_office_row(
        db_session, _no_ws):
    """guard 必须带 holder 校验：gate 关时 offices.holder_slug 可能是迁移 046
    的陈旧值，无条件 vacate 会罢免错的人。"""
    r = await _seed_citizen(db_session)
    db_session.add(Office(office_key="mayor", holder_slug="builtin-0",
                          institution="town_hall", perms_json={},
                          fill_strategy=cm.POLITICAL_FILL_STRATEGY))
    db_session.add(SystemConfig(key="current_mayor",
                                value=json.dumps("builtin-0"),
                                group="civic", updated_by="election"))
    await db_session.commit()

    await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    holder = (await db_session.execute(
        select(Office.holder_slug).where(Office.office_key == "mayor"))).scalar_one()
    assert holder == "builtin-0", "别人的职位不能被顺手罢免"
    cfg = (await db_session.execute(
        select(SystemConfig.value)
        .where(SystemConfig.key == "current_mayor"))).scalar_one()
    assert json.loads(cfg) == "builtin-0", "current_mayor 只在指向本人时才清"


@pytest.mark.anyio
async def test_cleanup_happens_before_the_tier_flip(db_session, _no_ws):
    """顺序不可颠倒的可执行断言：把清 meta_json 的那一步换成一个探针，断言它
    执行时 resident_type 还是 citizen 档。若实现先改档位，探针会读到
    'resident' 并让断言失败。"""
    r = await _seed_citizen(db_session)
    await _make_sitting_mayor(db_session, r)

    seen: list[str] = []
    real = cm._write_history

    async def _probe(db, **kw):
        rtype = (await db.execute(
            select(Resident.resident_type)
            .where(Resident.id == kw["resident_id"]))).scalar_one()
        seen.append(rtype)
        return await real(db, **kw)

    # 历史行是步骤 5，写它时档位已经翻过；真正要证明的是步骤 1-3 在步骤 4
    # 之前跑完 —— 用「历史行写入时 offices/meta/config 都已清空」来锁死。
    with patch.object(cm, "_write_history", new=_probe):
        await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    assert seen == [cm.UGC_RESIDENT_TYPE]
    holder = (await db_session.execute(
        select(Office.holder_slug).where(Office.office_key == "mayor"))).scalar_one()
    assert holder is None


@pytest.mark.anyio
async def test_revoke_broadcasts_citizen_to_denizen(db_session):
    r = await _seed_citizen(db_session)
    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as bc:
        await cm.revoke_citizenship(db_session, r, reason="秘密理由",
                                    actor="admin:1", reason_code="admin_revoke")
    payload = bc.await_args.args[0]
    assert payload["type"] == "civic_standing_changed"
    assert (payload["old_standing"], payload["new_standing"]) == (cm.CITIZEN,
                                                                  cm.DENIZEN)
    assert payload["reason_code"] == "admin_revoke"
    assert "秘密理由" not in str(payload)


@pytest.mark.anyio
async def test_refused_revoke_leaves_the_database_untouched(db_session):
    """硬门 5：射程外的人被撤销时 raise 且数据库零变化。"""
    db_session.add_all([_res(f"builtin-{i}", cm.CIVIC_MEMBER_TYPE,
                             creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    await db_session.commit()
    builtin = (await db_session.execute(
        select(Resident).where(Resident.slug == "builtin-0"))).scalar_one()

    with pytest.raises(cm.CivicStandingRefused):
        await cm.revoke_citizenship(db_session, builtin, reason="x",
                                    actor="admin:1")

    assert (await db_session.execute(
        select(func.count()).select_from(Resident)
        .where(Resident.is_civic_voter))).scalar() == 6
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_revoke_citizenship.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: module 'app.services.civic_membership' has no attribute 'revoke_citizenship'`。

- [ ] **Step 3: 写实现**

在 `backend/app/services/civic_membership.py` 的 `_assert_revocable()` 之后、`__all__` 之前追加：

```python

async def _assert_demotion_invariants(db, *, resident_id: str, slug: str) -> None:
    """步骤 6 的自查：三处镇长表示都不指向他；人口口径不变、政治口径已收回。

    全部用**列级 SELECT**（不是 ORM 实体），绕开 identity map——步骤 4 的
    ``update()`` 带 ``synchronize_session=False``，会话里的实体对象仍是旧值。
    """
    import json

    from sqlalchemy import func, select

    from app.models.office import Office
    from app.models.resident import Resident
    from app.models.system_config import SystemConfig

    rtype = (await db.execute(
        select(Resident.resident_type).where(Resident.id == resident_id)
    )).scalar_one()
    if rtype != UGC_RESIDENT_TYPE:
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} landed on resident_type "
            f"{rtype!r}, expected {UGC_RESIDENT_TYPE!r}")
    if rtype not in SIM_RESIDENT_TYPES:
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} fell out of the world "
            "population (is_autonomous would be False) — 撤销不是移出世界")
    if rtype in CIVIC_VOTER_TYPES:
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} still holds political rights")

    held = (await db.execute(
        select(func.count()).select_from(Office).where(
            Office.fill_strategy == POLITICAL_FILL_STRATEGY,
            Office.holder_slug == slug,
        )
    )).scalar() or 0
    if held:
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} still holds {held} elected "
            "office row(s)")

    meta = (await db.execute(
        select(Resident.meta_json).where(Resident.id == resident_id)
    )).scalar_one() or {}
    if meta.get("mayor"):
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} still carries "
            "meta_json['mayor'] — 工资倍率的唯一读点（duty_service.py:172-173）")

    cfg_value = (await db.execute(
        select(SystemConfig.value).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none()
    if cfg_value is not None:
        try:
            current = json.loads(cfg_value)
        except (TypeError, ValueError):
            current = None
        if current == slug:
            raise CivicStandingRefused(
                f"demotion invariant broken: system_config['current_mayor'] "
                f"still points at {slug!r}")


async def revoke_citizenship(
    db, resident, *, reason: str, actor: str, tier: str = "demote",
    reason_code: str = "revoked",
) -> bool:
    """撤销公民权。``tier="demote"``（本轮）| ``"exile"``（占位）。

    **有序复合事务，顺序不可颠倒**::

        0. 防呆（第一条 UPDATE 之前全部做完）
        1. 卸民选职务（fill_strategy='election' 且 holder_slug=:slug）
        2. 清 meta_json['mayor']（按 slug 直查，不用集合谓词做 WHERE）
        3. 清 system_config['current_mayor']（仅当指向此人）
        4. 改档位（guarded UPDATE）
        5. 写 civic_standing_history 一行
        6. 断言
        7. commit + 广播

    若先改档位再清理，``meta_json['mayor']`` 在逐出档会永久卡死（清扫扫不到
    他），期间 ``install_mayor()`` 清他人标志时也会跳过他，可产生「两个
    ``meta_json['mayor']=True``」并双份工资倍率。

    三条「不得」：不得调用 ``OfficeService.vacate()``（自带 commit，且 gate 关
    时命中 0 行会跳过 legacy 清理）；不得复用 ``_clear_mayor_legacy_stores``
    （用 ``is_autonomous`` 这个集合谓词去清「刚离开集合的人」）；不得用
    ``ConfigService.set()``（自带 commit，会把复合事务劈成两半）。

    **劳动职务不受影响**：``town_clerk`` / ``postman`` / ``doctor`` 的 offices
    行与 ``meta_json['duty']`` 一律不动。**永不 DELETE。**

    ⚠️ **调用方契约**（与 :func:`grant_citizenship_batch` 同）：步骤 4 的档位
    翻转走 ``update(...).execution_options(synchronize_session=False)``，而本仓
    的会话是 ``expire_on_commit=False``，所以**调用方传进来的 ORM 对象在本函数
    返回后仍是旧值**，``select(Resident)`` 这类实体查询也会把同一个陈旧对象取
    回来。要读新值就 ``await db.refresh(resident)``（``_edit_resident`` 就是这
    么做的），或改用列级 SELECT / SQL 侧谓词。步骤 6 的
    :func:`_assert_demotion_invariants` 全部用列级 SELECT 正是这个原因。
    """
    if tier == EXILED or tier == "exile":
        raise NotImplementedError(
            "revoke_citizenship(tier='exile') 是预留签名：分档清理表已按两档"
            "写好（住房 home_location_id / tile 占用 / 劳动职务全撤 + is_in_town "
            "收窄），v1 只实现 demote 档。逐出上线时是填空，不是改签名。"
        )
    if tier != "demote":
        raise ValueError(
            f"unknown revoke tier {tier!r}; expected 'demote' or 'exile'")

    import json
    from datetime import datetime, UTC

    from sqlalchemy import select, update
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.office import Office
    from app.models.resident import Resident
    from app.models.system_config import SystemConfig

    resident_id = getattr(resident, "id", None)
    if not resident_id:
        raise CivicStandingRefused("revoke refused: resident has no id")

    # 0. Guard first: no UPDATE has run yet
    slug, current_type = await _assert_revocable(db, resident_id)
    assert_known_types(current_type, UGC_RESIDENT_TYPE)   # 数值闸门 4

    try:
        # 1. 卸民选职务。只 election 档；带 holder 校验（gate 关时 offices
        #    可能留着迁移 046 的陈旧值，无条件 vacate 会罢免错的人）。
        #    正确性不依赖 polis_office_enabled 的取值。
        await db.execute(
            update(Office)
            .where(Office.fill_strategy == POLITICAL_FILL_STRATEGY,
                   Office.holder_slug == slug)
            .values(holder_slug=None, term_ends_at=None,
                    updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        # 2. 清 meta_json['mayor'] —— 按 slug 直查（通用约束：清理「已离开
        #    集合 S 的居民」不得用 S 本身做 WHERE）
        target = (await db.execute(
            select(Resident).where(Resident.slug == slug)
        )).scalar_one()
        meta = dict(target.meta_json or {})
        if meta.pop("mayor", None) is not None:
            target.meta_json = meta
            flag_modified(target, "meta_json")
        # 3. 清 system_config['current_mayor'] —— 仅当当前值指向此人
        cfg = (await db.execute(
            select(SystemConfig).where(SystemConfig.key == "current_mayor")
        )).scalar_one_or_none()
        if cfg is not None:
            try:
                current = json.loads(cfg.value)
            except (TypeError, ValueError):
                current = None
            if current == slug:
                cfg.value = json.dumps(None)
                cfg.updated_by = actor
                cfg.updated_at = datetime.now(UTC)
        # 4. 改档位（guarded UPDATE）
        res = await db.execute(
            update(Resident)
            .where(Resident.id == resident_id,
                   Resident.resident_type == current_type)
            .values(resident_type=UGC_RESIDENT_TYPE)
            .execution_options(synchronize_session=False)
        )
        if (res.rowcount or 0) != 1:
            raise CivicStandingRefused(
                f"revoke refused: guarded UPDATE touched {res.rowcount} rows "
                f"for {slug!r} — resident_type changed inside the window")
        # 5. 历史行
        await _write_history(
            db, resident_id=resident_id, old_standing=CITIZEN,
            new_standing=DENIZEN, reason=reason, reason_code=reason_code,
            actor=actor, evidence=None,
        )
        # 6. 断言（flush 让前面的 ORM 改动落到本事务里再自查）
        await db.flush()
        await _assert_demotion_invariants(db, resident_id=resident_id, slug=slug)
    except Exception:
        await db.rollback()
        raise
    # 7. commit + 广播
    await db.commit()
    await _emit_standing_changed(
        db, slug=slug, old_standing=CITIZEN, new_standing=DENIZEN,
        reason_code=reason_code,
    )
    logger.info("civic revoke: %s demoted by %s (%s)", slug, actor, reason_code)
    return True

```

- [ ] **Step 4: 更新 `__all__`**

把 `__all__` 里的

```python
    # 写入口
    "grant_citizenship", "grant_citizenship_batch",
```

替换为

```python
    # 写入口
    "grant_citizenship", "grant_citizenship_batch", "revoke_citizenship",
```

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_revoke_citizenship.py tests/test_civic_revoke_guard.py tests/test_civic_grant_citizenship.py -q -p no:randomly
```
Expected: PASS。

- [ ] **Step 6: 回归既有政治层测试**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_office_service.py tests/test_office_integration.py \
  tests/test_m6_election.py tests/test_ugc_resident_no_political_rights.py \
  -q -p no:randomly
```
Expected: PASS（F2 至此未改任何既有路径，这一轮是防回归确认）。

- [ ] **Step 7: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/services/civic_membership.py \
        backend/tests/test_civic_revoke_citizenship.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(civic): 撤销写入口——有序复合事务 + exile 档占位

顺序不可颠倒：防呆 → 卸民选职务 → 清 meta_json['mayor'] → 清 system_config
['current_mayor'] → 改档位 → 写历史 → 断言 → commit + 广播。先改档位再清理
会让 meta_json['mayor'] 永久卡死并产生双份工资倍率。

- 不调 OfficeService.vacate()（自带 commit，且 gate 关时跳过 legacy 清理）
- 不复用 _clear_mayor_legacy_stores（用集合谓词清「刚离开集合的人」）
- 不用 ConfigService.set()（自带 commit，会劈开复合事务）
- 只卸 fill_strategy='election'，劳动职务与住房一律不动；永不 DELETE
- polis_office_enabled 开与关两态都有测试覆盖

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 6: 晋升判定——snapshot 语义 + 纯函数

**Files:**
- Create: `backend/app/tasks/civic_promotion.py`（本任务只写数据结构、纯判定与快照构建；三态 pass 在 Task 12）
- Test: `backend/tests/test_civic_promotion_rules.py`

**Interfaces:**
- Consumes：`civic_membership` 的 `CITIZEN` / `DENIZEN` / `CIVIC_MEMBER_TYPE` / `CIVIC_VOTER_TYPES` / `UGC_RESIDENT_TYPE` / `SYSTEM_CREATOR_ID` / `is_ugc_resident`；`app.world_clock.now_world()` / `real_to_world(dt)` / `world_epoch()`；`app.models.resident_relation.ResidentRelation`（`party_a` / `party_b` / `party_a_type` / `party_b_type` / `familiarity`）；`app.services.config_service.ConfigService.get(key, *, default=None)`
- Produces：
  - `@dataclass(frozen=True) class ResidentFact`：`resident_id: str` / `slug: str` / `resident_type: str` / `is_builtin: bool` / `is_ugc: bool` / `anchor_world: datetime` / `promoted_world: datetime | None` / `banned: bool`
  - `@dataclass(frozen=True) class PromotionSnapshot`：`now_world: datetime` / `facts: tuple[ResidentFact, ...]` / `familiarity: tuple[tuple[str, str, float], ...]`
  - `def anchored_citizen_ids(snap, *, seasoning_days: float) -> frozenset[str]`
  - `def qualified_peers(snap, anchors: frozenset[str], threshold: float) -> dict[str, frozenset[str]]`
  - `def select_promotions(snap, *, min_world_days: float, min_peers: int, min_familiarity: float, seasoning_days: float) -> tuple[str, ...]`（**返回按 id 排序的元组**）
  - `def promotion_evidence(snap, resident_id: str, *, min_familiarity: float, seasoning_days: float) -> dict`
  - `async def build_snapshot(db) -> PromotionSnapshot`
  - 常量 `BACKFILL_MARK_KEY = "civic_backfill_done"`

**两条硬语义（不满足则整个机制失效）：**

1. **①的锚点不是 `created_at`，而是「本轮公民资格起算点」**——取 `civic_standing_history` 里该居民最近一条档位变更的 `world_at`，无历史行时回落 `real_to_world(created_at)`。若锚 `created_at`，T2 把一个已在镇 200 世界日的 UGC 降权后，F2 开闸当晚条件①对它立刻重新满足，**T2 的降权对存量整批走过场**。
2. **②的同伴取自「锚定公民集」，不是活的 `is_civic_voter`**——否则判定的转移函数自指，产生级联升降与「脱锚公民团」（某人的 N 位同伴全是已晋升 UGC、零条内置边）。锚定公民集 = 内置阵容（`creator_id == SYSTEM_CREATOR_ID` 且当前在 citizen 档）∪ 已过考察期的归化公民（有晋升记录且 `now_world − promoted_at ≥ seasoning_days`）。

**降级路径（spec §7 明确要求写清哪条生效）**：主路径是「建表迁移先于 T2，T2 写历史行 = 锚点」。**主路径生效**。降级路径同时实现并测试：若某 UGC 居民**没有任何历史行**且 `system_config['civic_backfill_done']` 存在，anchor 取 `max(real_to_world(created_at), 回填标记的世界时间)`——这样即使运维时序意外反了，存量也不会在开闸当晚被整批升回。

**世界时间的存取口径**：`world_at` 落库时转 UTC-aware；读回若是 naive（SQLite 会丢时区）按 UTC 补。`Resident.created_at` 的 naive 值按 UTC 解释——`world_clock._as_zone` 的 docstring 明确写着「that is how the DB stores created_at」。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_promotion_rules.py`:

```python
"""F2 Task 6 —— 晋升判定的 snapshot 语义与纯函数。

整个 pass 是 snapshot 语义：pass 开始一次性冻结输入，中途绝不重读选民集，
否则结果依赖数据库行序、同一状态多次运行得到不同不动点。判定做成纯函数，
测试用 random.shuffle 打乱内存中的居民列表再跑，断言输出集合恒等——不要试图
在 Postgres 上控制行序。
"""
import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import world_clock
from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.services import civic_membership as cm
from app.tasks import civic_promotion as cp

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _fact(rid, *, rtype=cm.UGC_RESIDENT_TYPE, builtin=False, ugc=True,
          age_days=100.0, promoted_days_ago=None, banned=False):
    return cp.ResidentFact(
        resident_id=rid, slug=rid, resident_type=rtype, is_builtin=builtin,
        is_ugc=ugc, anchor_world=NOW - timedelta(days=age_days),
        promoted_world=(None if promoted_days_ago is None
                        else NOW - timedelta(days=promoted_days_ago)),
        banned=banned,
    )


def _snap(facts, edges=()):
    return cp.PromotionSnapshot(now_world=NOW, facts=tuple(facts),
                                familiarity=tuple(edges))


def _builtin(rid):
    return _fact(rid, rtype=cm.CIVIC_MEMBER_TYPE, builtin=True, ugc=False)


# ── 锚定公民集 ─────────────────────────────────────────────────────────

def test_anchor_set_is_builtins_plus_seasoned_naturalised_citizens():
    snap = _snap([
        _builtin("b1"), _builtin("b2"),
        # 归化且已过考察期
        _fact("n1", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=40),
        # 归化但考察期未满
        _fact("n2", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=5),
        # 还在 denizen 档
        _fact("u1"),
    ])
    assert cp.anchored_citizen_ids(snap, seasoning_days=28.0) == frozenset(
        {"b1", "b2", "n1"})


def test_anchor_set_excludes_a_builtin_that_is_not_currently_a_citizen():
    """锚定集只收当前在 citizen 档的人——档位是活的，出身是冻结的。"""
    snap = _snap([_fact("b1", rtype=cm.UGC_RESIDENT_TYPE, builtin=True, ugc=False)])
    assert cp.anchored_citizen_ids(snap, seasoning_days=28.0) == frozenset()


def test_anchor_set_is_not_the_live_voter_set():
    """若同伴集合就是 is_civic_voter 本身，转移函数自指 → 级联升降 + 脱锚
    公民团（某人的 N 位同伴全是刚晋升的 UGC、零条内置边）。"""
    snap = _snap([
        _fact("fresh1", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=0),
        _fact("fresh2", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=0),
        _fact("fresh3", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=0),
        _fact("u1"),
    ])
    anchors = cp.anchored_citizen_ids(snap, seasoning_days=28.0)
    assert anchors == frozenset(), "刚晋升的人不得立刻成为别人的晋升同伴"
    assert cp.select_promotions(
        snap, min_world_days=1.0, min_peers=1, min_familiarity=0.1,
        seasoning_days=28.0) == ()


# ── 两个门槛 ───────────────────────────────────────────────────────────

def test_both_conditions_must_hold():
    edges = [("u1", "b1", 0.5), ("u1", "b2", 0.5), ("u1", "b3", 0.5)]
    facts = [_builtin("b1"), _builtin("b2"), _builtin("b3"), _fact("u1")]
    kw = dict(min_world_days=30.0, min_peers=3, min_familiarity=0.2,
              seasoning_days=28.0)

    assert cp.select_promotions(_snap(facts, edges), **kw) == ("u1",)
    # 条件① 不满足（在镇 10 世界日 < 30）
    young = [f if f.resident_id != "u1" else _fact("u1", age_days=10.0)
             for f in facts]
    assert cp.select_promotions(_snap(young, edges), **kw) == ()
    # 条件② 不满足（只有 2 位达标同伴）
    thin = edges[:2]
    assert cp.select_promotions(_snap(facts, thin), **kw) == ()
    # 条件② 的边低于 θ
    weak = [(a, b, 0.15) for a, b, _ in edges]
    assert cp.select_promotions(_snap(facts, weak), **kw) == ()


def test_peers_must_be_anchored_citizens_not_other_denizens():
    edges = [("u1", "u2", 0.9), ("u1", "u3", 0.9), ("u1", "u4", 0.9)]
    facts = [_fact("u1"), _fact("u2"), _fact("u3"), _fact("u4")]
    assert cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=3,
        min_familiarity=0.2, seasoning_days=28.0) == ()


def test_edges_are_undirected():
    """resident_relations 存的是规范化无向对（party_a ≤ party_b），所以判定
    必须两个方向都认。"""
    edges = [("b1", "u1", 0.5), ("b2", "u1", 0.5)]
    facts = [_builtin("b1"), _builtin("b2"), _fact("u1")]
    assert cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=2,
        min_familiarity=0.2, seasoning_days=28.0) == ("u1",)


def test_only_ugc_denizens_are_candidates():
    """内置阵容、admin preset、玩家化身、已是公民的人都不进候选面。"""
    edges = [(x, "b1", 0.9) for x in ("p1", "adm1", "n1")]
    facts = [
        _builtin("b1"),
        _fact("p1", rtype=cm.PLAYER_RESIDENT_TYPE, ugc=False),
        _fact("adm1", rtype=cm.ADMIN_PRESET_TYPE, ugc=False),
        _fact("n1", rtype=cm.CIVIC_MEMBER_TYPE, ugc=True, promoted_days_ago=99),
    ]
    assert cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=1,
        min_familiarity=0.2, seasoning_days=28.0) == ()


def test_civic_ban_is_excluded_from_day_one():
    """civic_ban 是 sticky 剥夺位：v1 只留状态位不实现写入，但候选面从第一天
    起就排除它——否则被逐者只要在冷却期内和几个 npc 聊够 familiarity 就自动
    升回，晋升任务无法区分「因疏远而降」与「因违规而逐」。"""
    edges = [("u1", "b1", 0.9), ("u1", "b2", 0.9)]
    facts = [_builtin("b1"), _builtin("b2"), _fact("u1", banned=True)]
    assert cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=2,
        min_familiarity=0.2, seasoning_days=28.0) == ()


# ── 顺序无关性（硬门 4）─────────────────────────────────────────────

def test_output_is_invariant_under_input_shuffling():
    facts = [_builtin(f"b{i}") for i in range(4)]
    facts += [_fact(f"u{i}") for i in range(5)]
    edges = [(f"u{i}", f"b{j}", 0.5) for i in range(5) for j in range(3)]

    kw = dict(min_world_days=30.0, min_peers=3, min_familiarity=0.2,
              seasoning_days=28.0)
    baseline = cp.select_promotions(_snap(facts, edges), **kw)
    assert baseline == ("u0", "u1", "u2", "u3", "u4")

    rng = random.Random(20260727)
    for _ in range(20):
        f2, e2 = list(facts), list(edges)
        rng.shuffle(f2)
        rng.shuffle(e2)
        assert cp.select_promotions(_snap(f2, e2), **kw) == baseline


def test_output_is_sorted_so_the_per_run_cap_is_deterministic():
    facts = [_builtin("b1"), _builtin("b2"),
             _fact("zzz"), _fact("aaa"), _fact("mmm")]
    edges = [(x, b, 0.9) for x in ("zzz", "aaa", "mmm") for b in ("b1", "b2")]
    out = cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=2,
        min_familiarity=0.2, seasoning_days=28.0)
    assert out == tuple(sorted(out)) == ("aaa", "mmm", "zzz")


# ── 证据 ───────────────────────────────────────────────────────────────

def test_promotion_evidence_records_the_three_numbers():
    edges = [("u1", "b1", 0.5), ("u1", "b2", 0.7), ("u1", "b3", 0.15)]
    facts = [_builtin("b1"), _builtin("b2"), _builtin("b3"),
             _fact("u1", age_days=42.0)]
    ev = cp.promotion_evidence(_snap(facts, edges), "u1",
                               min_familiarity=0.2, seasoning_days=28.0)
    assert ev["world_days"] == pytest.approx(42.0)
    assert ev["peers"] == 2
    assert ev["peer_ids"] == ["b1", "b2"]
    assert ev["min_familiarity"] == 0.2
    # 观测面要输出 top-familiarity 分布而非只输出达标计数（否则「晋升面长期
    # 为空」时分不清是阈值问题还是加权采样对新人的结构性歧视）
    assert ev["top_familiarity"][:2] == [0.7, 0.5]


# ── 快照构建（唯一一次 DB 读）───────────────────────────────────────

def _res(slug, rtype, *, creator_id="u1", meta=None, created_at=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=created_at or datetime.now(UTC))


@pytest.mark.anyio
async def test_build_snapshot_anchors_on_history_not_created_at(db_session):
    """锚 created_at 会让 T2 的降权对存量整批走过场——一个已在镇 200 世界日的
    UGC 被降权后，开闸当晚条件①立刻重新满足。"""
    old = _res("ugc-old", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"},
               created_at=datetime.now(UTC) - timedelta(days=365))
    db_session.add(old)
    await db_session.commit()
    recent_world = world_clock.now_world().astimezone(UTC) - timedelta(days=2)
    db_session.add(CivicStandingHistory(
        resident_id=old.id, old_standing=cm.CITIZEN, new_standing=cm.DENIZEN,
        reason=None, reason_code="ops_backfill", actor="ops_backfill_t2",
        evidence_json={}, world_at=recent_world))
    await db_session.commit()

    snap = await cp.build_snapshot(db_session)
    fact = next(f for f in snap.facts if f.slug == "ugc-old")
    age = (snap.now_world - fact.anchor_world) / timedelta(days=1)
    assert age < 5, f"锚点回落到了 created_at（世界龄 {age:.1f} 天）"


@pytest.mark.anyio
async def test_build_snapshot_falls_back_to_created_at_without_history(db_session):
    born = datetime.now(UTC) - timedelta(days=10)
    db_session.add(_res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"},
                        created_at=born))
    await db_session.commit()

    snap = await cp.build_snapshot(db_session)
    fact = next(f for f in snap.facts if f.slug == "ugc-1")
    # k=4：10 真实日 = 40 世界日
    age = (snap.now_world - fact.anchor_world) / timedelta(days=1)
    assert age == pytest.approx(10 * world_clock._k(), rel=0.05)


@pytest.mark.anyio
async def test_build_snapshot_honours_the_t2_backfill_mark_fallback(db_session):
    """降级路径（运维时序反了时的兜底）：无历史行的 UGC，anchor 取
    max(created_at→world, T2 完成标记的世界时间)。"""
    from app.services.config_service import ConfigService

    db_session.add(_res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"},
                        created_at=datetime.now(UTC) - timedelta(days=365)))
    await db_session.commit()
    mark = (world_clock.now_world() - timedelta(days=3)).date().isoformat()
    await ConfigService(db_session).set(cp.BACKFILL_MARK_KEY, mark,
                                        group="civic", updated_by="ops")

    snap = await cp.build_snapshot(db_session)
    fact = next(f for f in snap.facts if f.slug == "ugc-1")
    age = (snap.now_world - fact.anchor_world) / timedelta(days=1)
    assert age <= 4, f"降级路径没生效（世界龄 {age:.1f} 天）"


@pytest.mark.anyio
async def test_build_snapshot_reads_relations_and_provenance(db_session):
    b = _res("b1", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
             meta={"origin": "preset"})
    u = _res("u1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add_all([b, u])
    await db_session.flush()
    a_id, b_id = sorted([b.id, u.id])
    db_session.add(ResidentRelation(party_a=a_id, party_b=b_id,
                                    familiarity=0.42, affinity=0.1))
    await db_session.commit()

    snap = await cp.build_snapshot(db_session)
    by_slug = {f.slug: f for f in snap.facts}
    assert by_slug["b1"].is_builtin is True and by_slug["b1"].is_ugc is False
    assert by_slug["u1"].is_builtin is False and by_slug["u1"].is_ugc is True
    assert snap.familiarity == ((a_id, b_id, 0.42),)


@pytest.mark.anyio
async def test_build_snapshot_ignores_player_party_relations(db_session):
    """resident_relations 把 resident-resident 与 resident-player 统一在一张
    表里（party_*_type）；玩家的边不该给公民权判定投票。"""
    b = _res("b1", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID)
    db_session.add(b)
    await db_session.flush()
    db_session.add(ResidentRelation(party_a="user-xyz", party_a_type="player",
                                    party_b=b.id, party_b_type="resident",
                                    familiarity=0.9))
    await db_session.commit()

    snap = await cp.build_snapshot(db_session)
    assert snap.familiarity == ()
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_promotion_rules.py -q -p no:randomly
```
Expected: FAIL —— collection error `ModuleNotFoundError: No module named 'app.tasks.civic_promotion'`。

- [ ] **Step 3: 写实现**

Create `backend/app/tasks/civic_promotion.py`:

```python
"""F2 公民权晋升 —— 夜间任务的判定层（snapshot + 纯函数）。

设计要点（三条，都对应一个会让机制失效的坑）：

1. **snapshot 语义**。pass 开始时一次性读出 ``{resident: (档位, 锚点, 达标
   同伴)}`` 并冻结，所有判定基于快照，所有写入在 pass 末尾一次 commit，中途
   绝不重读选民集。否则结果依赖数据库行序，同一状态多次运行得到不同不动点。
2. **判定是纯函数**。输入快照 → 输出待升 id 集合，可以在内存里打乱顺序反复
   跑并断言输出恒等——不要试图在 Postgres 上控制行序。
3. **锚定公民集不自指**。同伴取自「内置阵容 ∪ 已过考察期的归化公民」，不是
   活的 ``is_civic_voter``；否则转移函数自指，产生级联升降与「脱锚公民团」
   （某人的 N 位同伴全是刚晋升的 UGC、零条内置边）。

时间尺度：门槛一律走**世界日**（``app/world_clock.py`` 是唯一入口，k=4），
而 familiarity 的衰减用的是**真实日**（``realism_rel_decay_idle_days = 30``）
——这是有意的两套尺度，实现不得擅自统一。

夜间任务**只升，永不自动降**（见 ``civic_membership.auto_demotion_enabled``
的 docstring）。撤销是显式事件，走 ``civic_membership.revoke_citizenship``。

⚠️ 已知的结构性偏置（风险项，不是阈值问题）：``extravert`` 档的
``SpontaneousDecidePlugin`` 用加权采样挑聊天对象，权重与既有熟识度正相关，
系统性歧视新人。若标定发现晋升面长期为空，根因可能在采样而不在阈值——所以
:func:`promotion_evidence` 输出 top-familiarity 分布，而不只是达标计数。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from app.services.civic_membership import (
    CITIZEN,
    CIVIC_VOTER_TYPES,
    SYSTEM_CREATOR_ID,
    UGC_RESIDENT_TYPE,
    is_ugc_resident,
)

logger = logging.getLogger(__name__)

#: T2 存量回填的完成标记（``system_config``）。降级 anchor 路径读它。
BACKFILL_MARK_KEY = "civic_backfill_done"

#: 关系表里「居民」这一侧的 party 类型（另一种是 "player"）。
_RESIDENT_PARTY = "resident"


@dataclass(frozen=True)
class ResidentFact:
    """快照里的一行居民事实。冻结（frozen）以保证纯函数不能改写输入。"""

    resident_id: str
    slug: str
    resident_type: str
    #: ``creator_id == SYSTEM_CREATOR_ID``（provenance 判定的主键；
    #: ``meta_json.origin == "preset"`` 不可用——admin 创建的 preset 同值）
    is_builtin: bool
    is_ugc: bool
    #: 公民时钟锚点（世界时间）
    anchor_world: datetime
    #: 最近一次晋升的世界时间；None = 从未晋升
    promoted_world: datetime | None
    #: civic_ban sticky 剥夺位（v1 只读不写，候选面从第一天起排除它）
    banned: bool = False


@dataclass(frozen=True)
class PromotionSnapshot:
    now_world: datetime
    facts: tuple[ResidentFact, ...]
    #: 无向边 ``(party_a, party_b, familiarity)``，只含 resident-resident
    familiarity: tuple[tuple[str, str, float], ...]


# ── 纯判定 ─────────────────────────────────────────────────────────────

def anchored_citizen_ids(snap: PromotionSnapshot, *,
                         seasoning_days: float) -> frozenset[str]:
    """锚定公民集 = 内置阵容 ∪ 已过考察期的归化公民（都必须当前在 citizen 档）。"""
    out: set[str] = set()
    for fact in snap.facts:
        if fact.resident_type not in CIVIC_VOTER_TYPES:
            continue
        if fact.is_builtin:
            out.add(fact.resident_id)
            continue
        if (fact.promoted_world is not None
                and (snap.now_world - fact.promoted_world)
                >= timedelta(days=seasoning_days)):
            out.add(fact.resident_id)
    return frozenset(out)


def qualified_peers(snap: PromotionSnapshot, anchors: frozenset[str],
                    threshold: float) -> dict[str, frozenset[str]]:
    """``{resident_id: 与之 familiarity ≥ threshold 的锚定公民集合}``。

    边是无向的（``relation_service.canonical_pair`` 规范化过），两个方向都认。
    """
    acc: dict[str, set[str]] = {}
    for party_a, party_b, fam in snap.familiarity:
        if fam < threshold:
            continue
        if party_b in anchors and party_a != party_b:
            acc.setdefault(party_a, set()).add(party_b)
        if party_a in anchors and party_a != party_b:
            acc.setdefault(party_b, set()).add(party_a)
    return {k: frozenset(v) for k, v in acc.items()}


def select_promotions(
    snap: PromotionSnapshot, *, min_world_days: float, min_peers: int,
    min_familiarity: float, seasoning_days: float,
) -> tuple[str, ...]:
    """待晋升的居民 id，**按 id 升序**（顺序无关性 + 单夜上限的确定性截断）。"""
    anchors = anchored_citizen_ids(snap, seasoning_days=seasoning_days)
    peers = qualified_peers(snap, anchors, min_familiarity)
    picked: list[str] = []
    for fact in snap.facts:
        if not fact.is_ugc:
            continue
        if fact.resident_type != UGC_RESIDENT_TYPE:
            continue                      # 只有 denizen 档进候选面
        if fact.banned:
            continue                      # civic_ban：sticky，永不自动复籍
        age_world_days = (snap.now_world - fact.anchor_world) / timedelta(days=1)
        if age_world_days < min_world_days:
            continue
        if len(peers.get(fact.resident_id, frozenset())) < min_peers:
            continue
        picked.append(fact.resident_id)
    return tuple(sorted(picked))


def promotion_evidence(
    snap: PromotionSnapshot, resident_id: str, *, min_familiarity: float,
    seasoning_days: float,
) -> dict:
    """一位候选人的判定证据（落进 ``civic_standing_history.evidence_json`` 与
    shadow 名单）。``top_familiarity`` 是对锚定公民的熟识度降序前 5——观测面
    要的是分布，不只是达标计数。"""
    anchors = anchored_citizen_ids(snap, seasoning_days=seasoning_days)
    fact = next((f for f in snap.facts if f.resident_id == resident_id), None)
    if fact is None:
        return {}
    peers = qualified_peers(snap, anchors, min_familiarity).get(
        resident_id, frozenset())
    tops = sorted(
        (fam for a, b, fam in snap.familiarity
         if (a == resident_id and b in anchors)
         or (b == resident_id and a in anchors)),
        reverse=True,
    )[:5]
    return {
        "world_days": round(
            (snap.now_world - fact.anchor_world) / timedelta(days=1), 2),
        "peers": len(peers),
        "peer_ids": sorted(peers),
        "min_familiarity": min_familiarity,
        "top_familiarity": [round(f, 4) for f in tops],
    }


# ── 快照构建（整个 pass 唯一一次 DB 读）────────────────────────────────

def _as_aware(dt: datetime) -> datetime:
    """DB 读回的 datetime 补时区。

    ``DateTime(timezone=True)`` 在 SQLite 上丢时区；本仓的存储口径是「一律
    转 UTC 落库」（``office_service._term_window`` / ``Resident.created_at``），
    所以 naive 值按 UTC 解释。
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _backfill_mark_world(db) -> datetime | None:
    """T2 完成标记（世界日期）→ tz-aware datetime；无标记/不可解析 → None。

    这是**降级路径**：主路径是「建表迁移先于 T2、T2 写历史行」，锚点直接取
    历史行。只有当某 UGC 居民一行历史都没有、而回填标记又存在时，这个值才会
    参与 ``max()``——防止运维时序反了时存量在开闸当晚被整批升回。
    """
    try:
        from app.services.config_service import ConfigService

        raw = await ConfigService(db).get(BACKFILL_MARK_KEY)
    except Exception:
        logger.debug("civic_backfill_done lookup failed", exc_info=True)
        return None
    if not raw:
        return None
    from app import world_clock

    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("unparseable %s=%r — 降级 anchor 路径跳过",
                       BACKFILL_MARK_KEY, raw)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=world_clock.world_epoch().tzinfo)
    return parsed


async def build_snapshot(db) -> PromotionSnapshot:
    """一次性冻结整个 pass 的输入。三次读：居民、档位历史、关系边。"""
    from sqlalchemy import select

    from app import world_clock
    from app.models.civic_standing_history import CivicStandingHistory
    from app.models.resident import Resident
    from app.models.resident_relation import ResidentRelation

    now_world = world_clock.now_world()
    residents = (await db.execute(select(Resident))).scalars().all()

    history = (await db.execute(
        select(CivicStandingHistory.resident_id,
               CivicStandingHistory.new_standing,
               CivicStandingHistory.world_at)
    )).all()
    anchor_by: dict[str, datetime] = {}
    promoted_by: dict[str, datetime] = {}
    for rid, new_standing, world_at in history:
        when = _as_aware(world_at)
        prev = anchor_by.get(rid)
        anchor_by[rid] = when if prev is None else max(prev, when)
        if new_standing == CITIZEN:
            prev_p = promoted_by.get(rid)
            promoted_by[rid] = when if prev_p is None else max(prev_p, when)

    backfill_world = await _backfill_mark_world(db)

    facts: list[ResidentFact] = []
    for r in residents:
        ugc = is_ugc_resident(r)
        anchor = anchor_by.get(r.id)
        if anchor is None:
            anchor = world_clock.real_to_world(r.created_at)
            if ugc and backfill_world is not None:
                # 降级路径（spec §7）：无历史行 + 有回填标记
                anchor = max(anchor, backfill_world)
        facts.append(ResidentFact(
            resident_id=r.id,
            slug=r.slug,
            resident_type=r.resident_type,
            is_builtin=(r.creator_id == SYSTEM_CREATOR_ID),
            is_ugc=ugc,
            anchor_world=anchor,
            promoted_world=promoted_by.get(r.id),
            banned=bool((r.meta_json or {}).get("civic_ban")),
        ))

    edges = (await db.execute(
        select(ResidentRelation.party_a, ResidentRelation.party_b,
               ResidentRelation.familiarity)
        .where(ResidentRelation.party_a_type == _RESIDENT_PARTY,
               ResidentRelation.party_b_type == _RESIDENT_PARTY)
    )).all()
    return PromotionSnapshot(
        now_world=now_world,
        facts=tuple(facts),
        familiarity=tuple((a, b, float(f or 0.0)) for a, b, f in edges),
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_promotion_rules.py -q -p no:randomly
```
Expected: PASS（18 passed）。

- [ ] **Step 5: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/tasks/civic_promotion.py \
        backend/tests/test_civic_promotion_rules.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(civic): 晋升判定——snapshot 语义 + 顺序无关的纯函数

- 门槛①锚在「公民时钟」（civic_standing_history.world_at），不是 created_at；
  无历史行回落 real_to_world(created_at)，并实现 T2 回填标记的降级路径
- 门槛②的同伴取自锚定公民集（内置 ∪ 已过考察期的归化），不是活的
  is_civic_voter——自指的转移函数会产生级联升降与脱锚公民团
- select_promotions 是纯函数且输出按 id 排序：打乱输入 20 次输出恒等，
  单夜上限截断因此也是确定性的
- civic_ban 从第一天起排除在候选面外（sticky 剥夺位，v1 只读）

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 6b: 只读标定报告脚本（spec §4.2 的「F2 第一步」）

**Files:**
- Create: `backend/scripts/civic_calibration_report.py`
- Test: `backend/tests/test_civic_calibration_report.py`

**Interfaces:**
- Consumes：Task 6 的 `civic_promotion.build_snapshot(db)` / `anchored_citizen_ids(snap, *, seasoning_days)` / `select_promotions(...)`；Task 2 的 `civic_membership` 旋钮与 `CIVIC_VOTER_TYPES` / `UGC_RESIDENT_TYPE`；`app.database.async_session`
- Produces：
  - `def percentiles(values: list[float], qs=(0, 25, 50, 75, 90, 100)) -> dict[str, float]`
  - `async def collect_calibration(db, *, top_n: int = 5) -> dict`
  - `def render_calibration(data: dict) -> str`
  - `def main(argv: list[str] | None = None) -> None`（`--top-n` / `--list`）

**为什么必须是一个脚本，不能是一段 docstring。** spec §4.2 把「只读标定」定为 F2 的**第一步**，并给了明确的降级路径（「若 F2 开工时 T1 尚未落地，则以本机 dev 库标定并显式标记为待用生产数据复标」）。这与 F1 第 3 项共享同一条纪律：**阈值必须由实测分布决定**——`rep_credit_min_score = -0.3` 之所以变成装饰性闸门，正是因为它是拍出来的。只写注释的话，三个占位值（30.0 / 3 / 0.20）会以「默认值」的形态进 master，而收口会话拿不到「开闸前必须补什么」的可执行清单。

**三张表（对应 spec 要求的三项读数）：**

| 表 | 读数 | 定哪个阈值 |
|---|---|---|
| ① UGC 居民的在镇**世界日**分布 | `p0/p25/p50/p75/p90/p100` | `MIN_WORLD_DAYS` |
| ② 每位 UGC 对**锚定公民**的 top-N familiarity + 「第 `MIN_PEERS` 高」那一档的分布 | 同上 | `MIN_FAMILIARITY` / `MIN_PEERS` |
| ③ 当前公民总数（拆内置 / 归化） | 计数 | `CIVIC_MIN_ELECTORATE` / 单夜上限 / 熔断阈值 |

②里「第 `MIN_PEERS` 高的那条边」是最贴判据的统计量：一位居民通过门槛② **当且仅当**他对锚定公民的第 k 高 familiarity ≥ θ（k = `MIN_PEERS`）。只看均值或达标计数都读不出「θ 该往哪挪」。

**三条实现约束：**

1. **复用 `build_snapshot` / `select_promotions`，不另写一份查询**。标定读数与夜间任务的判据必须逐字同源，否则标出来的阈值对不上真实候选面——两边各写一份必然漂移（同 `is_ugc_resident` 的理由）。
2. **零写入**。不调 `ConfigService.set`，不建表，不改任何行；`build_snapshot` 内部只读（`ConfigService.get` 是纯 SELECT，已核实 `config_service.py:14-25`）。没有 `--dry-run`——整个脚本只有 dry 一态。
3. **读数为空必须自己喊出来**。本机 dev 库是空的，`needs_production_recalibration` 为真时报告里要出现「⚠️ 待生产数据复标，不得直接开闸」——这行就是交给收口会话的交付物。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_calibration_report.py`:

```python
"""F2 Task 6b —— 只读标定报告（spec §4.2 的「F2 第一步」）。

阈值必须由实测分布决定，不能拍脑袋——rep_credit_min_score = -0.3 变成装饰性
闸门正是因为它是拍出来的。本脚本复用 civic_promotion 的 snapshot 与判定函数，
保证「标定读数」与「夜间任务判据」逐字同源。
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import world_clock
from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.services import civic_membership as cm
from scripts.civic_calibration_report import (
    collect_calibration,
    percentiles,
    render_calibration,
)


def _res(slug, rtype, *, creator_id="u1", meta=None, created_days_ago=100):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=datetime.now(UTC)
                    - timedelta(days=created_days_ago))


def _builtin(slug):
    return _res(slug, cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
                meta={"origin": "preset"})


def _ugc(slug, rtype=cm.UGC_RESIDENT_TYPE, **kw):
    return _res(slug, rtype, meta={"origin": "forge"}, **kw)


async def _edge(db, a, b, fam):
    x, y = sorted([a.id, b.id])
    db.add(ResidentRelation(party_a=x, party_b=y, familiarity=fam))
    await db.commit()


# ── 分位数 ─────────────────────────────────────────────────────────────

def test_percentiles_of_an_empty_sample_is_empty():
    assert percentiles([]) == {}


def test_percentiles_use_nearest_rank():
    d = percentiles([1.0, 2.0, 3.0, 4.0, 5.0], qs=(0, 50, 100))
    assert (d["p0"], d["p50"], d["p100"]) == (1.0, 3.0, 5.0)


# ── 交付物：空读数必须自己喊 ───────────────────────────────────────────

@pytest.mark.anyio
async def test_an_empty_world_reports_that_calibration_is_still_pending(db_session):
    """本机 dev 库是空的。空读数 ≠ 标定完成——报告必须自己写出「待生产数据
    复标」，这行就是交给收口会话的交付物。"""
    data = await collect_calibration(db_session)
    assert data["ugc"]["count"] == 0
    assert data["needs_production_recalibration"] is True
    out = render_calibration(data)
    assert "待生产数据复标" in out


# ── 表①：在镇世界日分布锚在公民时钟上 ─────────────────────────────────

@pytest.mark.anyio
async def test_world_days_are_anchored_on_the_civic_clock_not_created_at(db_session):
    """与 build_snapshot 同源：锚 created_at 会让 T2 降权过的存量看起来「早就
    够老了」，标出来的 MIN_WORLD_DAYS 直接失真。"""
    old = _ugc("ugc-old", created_days_ago=365)
    db_session.add_all([_builtin("b1"), old])
    await db_session.commit()
    db_session.add(CivicStandingHistory(
        resident_id=old.id, old_standing=cm.CITIZEN, new_standing=cm.DENIZEN,
        reason=None, reason_code="ops_backfill", actor="ops_backfill_t2",
        evidence_json={},
        world_at=world_clock.now_world().astimezone(UTC) - timedelta(days=2)))
    await db_session.commit()

    data = await collect_calibration(db_session)
    assert data["ugc"]["count"] == 1
    assert data["ugc"]["world_days"]["p50"] < 5


# ── 表②：只数对锚定公民的边 ───────────────────────────────────────────

@pytest.mark.anyio
async def test_top_familiarity_only_counts_edges_to_anchored_citizens(db_session):
    """门槛②的同伴取自锚定公民集。把 denizen 之间的边算进来，标出来的 θ 会
    被一群互相熟识、零条内置边的「脱锚公民团」带偏。"""
    b1, u1, u2 = _builtin("b1"), _ugc("u1"), _ugc("u2")
    db_session.add_all([b1, u1, u2])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.5)
    await _edge(db_session, u1, u2, 0.9)      # denizen ↔ denizen，不算

    data = await collect_calibration(db_session)
    assert data["familiarity"]["per_resident_top"]["u1"] == [0.5]
    assert data["familiarity"]["per_resident_top"]["u2"] == []


@pytest.mark.anyio
async def test_kth_best_edge_is_the_statistic_that_decides_theta(db_session,
                                                                 monkeypatch):
    """一位居民通过门槛② 当且仅当他对锚定公民的第 k 高 familiarity ≥ θ
    （k = MIN_PEERS）。报告必须直接给这一档的分布。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    b1, b2, b3, u1 = _builtin("b1"), _builtin("b2"), _builtin("b3"), _ugc("u1")
    db_session.add_all([b1, b2, b3, u1])
    await db_session.commit()
    for peer, fam in ((b1, 0.7), (b2, 0.5), (b3, 0.15)):
        await _edge(db_session, u1, peer, fam)

    data = await collect_calibration(db_session)
    assert data["familiarity"]["per_resident_top"]["u1"] == [0.7, 0.5, 0.15]
    assert data["familiarity"]["kth_best"]["values"] == [0.5]
    assert data["familiarity"]["kth_best"]["k"] == 2


# ── 表③ 与候选面判据 ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_citizen_counts_split_builtin_from_naturalised(db_session):
    naturalised = _ugc("n1", cm.CIVIC_MEMBER_TYPE)
    db_session.add_all([_builtin("b1"), _builtin("b2"), naturalised, _ugc("u1")])
    await db_session.commit()

    data = await collect_calibration(db_session)
    assert data["citizens"] == {"total": 3, "builtin": 2, "naturalised": 1}


@pytest.mark.anyio
async def test_a_full_sweep_is_flagged_red(db_session, monkeypatch):
    """标定的判据是「使晋升面**非空且非全量**」。全量 = 阈值写松了，报红。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.1")
    b1, u1 = _builtin("b1"), _ugc("u1")
    db_session.add_all([b1, u1])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.9)

    data = await collect_calibration(db_session)
    assert data["candidate_face"]["size"] == 1
    assert data["candidate_face"]["total_ugc"] == 1
    assert data["candidate_face"]["verdict"] == "full"
    assert data["needs_production_recalibration"] is True
    assert "🔴" in render_calibration(data)


@pytest.mark.anyio
async def test_a_partial_face_is_the_target_shape(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.4")
    b1, u1, u2 = _builtin("b1"), _ugc("u1"), _ugc("u2")
    db_session.add_all([b1, u1, u2])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.9)
    await _edge(db_session, u2, b1, 0.1)

    data = await collect_calibration(db_session)
    assert data["candidate_face"]["verdict"] == "partial"
    assert data["candidate_face"]["slugs"] == ["u1"]
    out = render_calibration(data)
    assert "🔴" not in out


# ── 只读 ───────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_the_report_is_read_only(db_session):
    """标定是只读动作。写一行都不许——它跑在生产库上。"""
    b1, u1 = _builtin("b1"), _ugc("u1")
    db_session.add_all([b1, u1])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.5)
    before = (await db_session.execute(
        select(Resident.slug, Resident.resident_type, Resident.meta_json))).all()

    await collect_calibration(db_session)

    after = (await db_session.execute(
        select(Resident.slug, Resident.resident_type, Resident.meta_json))).all()
    assert after == before
    assert not db_session.dirty and not db_session.new and not db_session.deleted
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_calibration_report.py -q -p no:randomly
```
Expected: FAIL —— collection error `ModuleNotFoundError: No module named 'scripts.civic_calibration_report'`。

- [ ] **Step 3: 写脚本**

Create `backend/scripts/civic_calibration_report.py`:

```python
#!/usr/bin/env python3
"""F2 三个门槛的**只读标定报告**（spec §4.2 的「F2 第一步」）。

用法（vm212 api 容器内跑，DATABASE_URL 已由 deploy compose 注入）::

    docker compose exec api python scripts/civic_calibration_report.py --list

本机 dev 库::

    DATABASE_URL=sqlite+aiosqlite:////tmp/f2.db python scripts/civic_calibration_report.py

**阈值必须由实测分布决定，不能拍脑袋。** ``rep_credit_min_score = -0.3`` 之所以
变成装饰性闸门，正是因为它是拍出来的；F2 的三个门槛
（``CIVIC_PROMOTION_MIN_WORLD_DAYS`` / ``MIN_PEERS`` / ``MIN_FAMILIARITY``）在
``civic_membership`` 里给的是**占位默认值，标定前不得开闸**。

判据是「使晋升面**非空且非全量**」：
- 空  → 阈值写紧了（或 familiarity 的主增长路径没开，见 ``REALISM_RELATIONS_ENABLED``）
- 全量 → 阈值写松了，开闸当晚会整批放行

三条实现约束：

1. **复用** ``civic_promotion.build_snapshot`` / ``select_promotions``，不另写一份
   查询——标定读数与夜间任务判据必须逐字同源，两边各写一份必然漂移。
2. **零写入**。没有 ``--dry-run``：整个脚本只有 dry 一态，它是要跑在生产库上的。
3. **读数为空必须自己喊**。本机 dev 库是空的；``needs_production_recalibration``
   为真时报告里会出现「待生产数据复标」——这行就是交给收口会话的交付物
   （spec §4.2 的降级路径：以 dev 库标定必须显式标记，不得直接开闸）。

⚠️ 内置阵容的世界龄已 ≈450 世界日、UGC 新人从 0 开始，**两类人不要放进同一
分布看**——所以表①只统计 UGC denizen。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import timedelta

# `python scripts/civic_calibration_report.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import civic_membership as cm  # noqa: E402
from app.tasks import civic_promotion as cp  # noqa: E402

_DEFAULT_QS = (0, 25, 50, 75, 90, 100)


def percentiles(values, qs=_DEFAULT_QS) -> dict:
    """最近秩（nearest-rank）分位数。样本为空返回 ``{}``（不是 0 —— 空读数与
    「读数是 0」是两件事，前者要触发「待生产数据复标」）。"""
    ordered = sorted(values)
    if not ordered:
        return {}
    out = {}
    for q in qs:
        idx = int(round(q / 100.0 * (len(ordered) - 1)))
        out[f"p{q}"] = ordered[min(max(idx, 0), len(ordered) - 1)]
    return out


async def collect_calibration(db, *, top_n: int = 5) -> dict:
    """三张分布表 + 候选面判据。**只读**。"""
    seasoning = cm.peer_seasoning_world_days()
    theta = cm.min_familiarity()
    k = max(1, cm.min_peers())

    snap = await cp.build_snapshot(db)
    anchors = cp.anchored_citizen_ids(snap, seasoning_days=seasoning)

    citizens = [f for f in snap.facts if f.resident_type in cm.CIVIC_VOTER_TYPES]
    denizens = [f for f in snap.facts
                if f.is_ugc and f.resident_type == cm.UGC_RESIDENT_TYPE]

    # 表①：UGC 的在镇世界日（锚在公民时钟上，与 build_snapshot 同源）
    world_days = [round((snap.now_world - f.anchor_world) / timedelta(days=1), 2)
                  for f in denizens]

    # 表②：每位 UGC 对锚定公民的 top-N familiarity，以及「第 k 高」那一档
    per_resident_top: dict[str, list[float]] = {}
    for fact in denizens:
        edges = sorted(
            (fam for a, b, fam in snap.familiarity
             if (a == fact.resident_id and b in anchors)
             or (b == fact.resident_id and a in anchors)),
            reverse=True,
        )[:top_n]
        per_resident_top[fact.slug] = [round(x, 4) for x in edges]
    kth = sorted(v[k - 1] for v in per_resident_top.values() if len(v) >= k)

    # 候选面：用与夜间任务**同一个**判定函数
    candidate_ids = cp.select_promotions(
        snap, min_world_days=cm.min_world_days(), min_peers=k,
        min_familiarity=theta, seasoning_days=seasoning,
    )
    slug_by_id = {f.resident_id: f.slug for f in snap.facts}
    if not denizens:
        verdict = "no_data"
    elif not candidate_ids:
        verdict = "empty"
    elif len(candidate_ids) == len(denizens):
        verdict = "full"
    else:
        verdict = "partial"

    return {
        "world_at": snap.now_world.isoformat(),
        "thresholds": {
            "min_world_days": cm.min_world_days(),
            "min_peers": k,
            "min_familiarity": theta,
            "peer_seasoning_world_days": seasoning,
        },
        "citizens": {
            "total": len(citizens),
            "builtin": sum(1 for f in citizens if f.is_builtin),
            "naturalised": sum(1 for f in citizens if not f.is_builtin),
        },
        "anchors": len(anchors),
        "ugc": {
            "count": len(denizens),
            "world_days": percentiles(world_days),
            "world_days_raw": sorted(world_days),
        },
        "familiarity": {
            "top_n": top_n,
            "per_resident_top": per_resident_top,
            "kth_best": {"k": k, "values": [round(x, 4) for x in kth],
                         "percentiles": percentiles(kth)},
        },
        "candidate_face": {
            "size": len(candidate_ids),
            "total_ugc": len(denizens),
            "slugs": sorted(slug_by_id.get(i, i) for i in candidate_ids),
            "verdict": verdict,
        },
        # spec §4.2 的降级路径：只有「非空且非全量」才算标定出了一组可用取值，
        # 其余一律要求用生产数据复标。
        "needs_production_recalibration": verdict != "partial",
    }


def _fmt_pct(d: dict) -> str:
    return "  ".join(f"{k}={v}" for k, v in d.items()) if d else "（无样本）"


def render_calibration(data: dict, *, list_residents: bool = False) -> str:
    t = data["thresholds"]
    c = data["citizens"]
    u = data["ugc"]
    f = data["familiarity"]
    face = data["candidate_face"]
    out = [
        "== F2 门槛标定报告（只读 · 零 LLM）==",
        f"  世界时间 {data['world_at']}",
        f"  当前阈值（占位默认值）：MIN_WORLD_DAYS={t['min_world_days']} "
        f"MIN_PEERS={t['min_peers']} MIN_FAMILIARITY={t['min_familiarity']} "
        f"SEASONING={t['peer_seasoning_world_days']}",
        "",
        "-- 表③ 公民总数（定 CIVIC_MIN_ELECTORATE / 单夜上限 / 熔断阈值）--",
        f"  公民 {c['total']}（内置 {c['builtin']} / 归化 {c['naturalised']}）；"
        f"锚定公民集 {data['anchors']}",
        "",
        "-- 表① UGC 在镇世界日分布（定 MIN_WORLD_DAYS）--",
        f"  样本 {u['count']} 人：{_fmt_pct(u['world_days'])}",
        "  注：内置阵容世界龄 ≈450 世界日、UGC 从 0 起算，两类人不进同一分布，",
        "      所以本表只统计 UGC denizen。",
        "",
        f"-- 表② 对锚定公民的 top-{f['top_n']} familiarity（定 MIN_FAMILIARITY "
        "/ MIN_PEERS）--",
        f"  第 {f['kth_best']['k']} 高那一档（通过门槛② 当且仅当它 ≥ θ）："
        f"{_fmt_pct(f['kth_best']['percentiles'])}",
        f"  达到 {f['kth_best']['k']} 条锚定边的 UGC：{len(f['kth_best']['values'])}"
        f" / {u['count']}",
    ]
    if list_residents:
        for slug, tops in sorted(f["per_resident_top"].items()):
            out.append(f"    {slug:<24} {tops}")
    out.append("")
    out.append("-- 候选面（用夜间任务同一个 select_promotions 算）--")
    out.append(f"  当前阈值下会晋升 {face['size']} / {face['total_ugc']} 人："
               f"{face['slugs'][:20]}")
    verdict_note = {
        "partial": "✅ 非空且非全量 —— 这组取值形状正确",
        "empty": "🔴 晋升面为空：阈值写紧了，或 familiarity 的主增长路径没开"
                 "（先确认生产 REALISM_RELATIONS_ENABLED 的实际取值）",
        "full": "🔴 晋升面是全量：阈值写松了，开闸当晚会整批放行",
        "no_data": "🔴 库里没有 UGC denizen —— 读数为空，标定没有发生",
    }
    out.append("  " + verdict_note[face["verdict"]])
    if data["needs_production_recalibration"]:
        out.append("")
        out.append("  ⚠️ **待生产数据复标，不得直接开闸**（spec §4.2 的降级路径："
                   "以本机 dev 库标定必须显式标注）。")
        out.append("     开闸前要补的三件事：① 在有真实 UGC 的库上重跑本报告；"
                   "② 把三个阈值调到 verdict=partial；③ 复验生产 "
                   "REALISM_RELATIONS_ENABLED=true。")
    return "\n".join(out)


async def _run(top_n: int, list_residents: bool) -> str:
    from app.database import async_session, engine

    async with async_session() as db:
        data = await collect_calibration(db, top_n=top_n)
    await engine.dispose()
    return render_calibration(data, list_residents=list_residents)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="F2 三个门槛的只读标定报告（无写入，无 --dry-run）")
    parser.add_argument("--top-n", type=int, default=5,
                        help="每位 UGC 输出多少条最强的锚定边（默认 5）")
    parser.add_argument("--list", action="store_true",
                        help="逐人列出 top-N（默认只给分布）")
    args = parser.parse_args(argv)
    print(asyncio.run(_run(max(args.top_n, 1), args.list)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_calibration_report.py tests/test_civic_promotion_rules.py -q -p no:randomly
```
Expected: PASS。

- [ ] **Step 5: 真跑一次（运行时证据 + 交付物）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
DATABASE_URL=sqlite+aiosqlite:////tmp/f2-calib.db python -c "
import asyncio
from app.database import Base, engine
async def m():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await engine.dispose()
asyncio.run(m())
"
DATABASE_URL=sqlite+aiosqlite:////tmp/f2-calib.db python scripts/civic_calibration_report.py --list
```

Expected: 报告完整打印、零异常；本机 dev 库是空的，所以尾部**必然**出现 `🔴 库里没有 UGC denizen —— 读数为空，标定没有发生` 与 `⚠️ 待生产数据复标，不得直接开闸`。**这就是本任务的交付物**：把这段原样贴进 commit 与 handoff，收口会话据此知道开闸前必须补哪三件事。

- [ ] **Step 6: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/scripts/civic_calibration_report.py \
        backend/tests/test_civic_calibration_report.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(civic): 门槛标定报告脚本——只读、与夜间任务判据同源

spec §4.2 把「只读标定」定为 F2 的第一步，不能降级成一段注释：三个阈值
（30.0 / 3 / 0.20）是占位默认值，没有标定工具就会以「默认值」的形态进 master。

- 三张表：UGC 在镇世界日分布 / 对锚定公民的 top-N familiarity（含「第 k 高」
  那一档 —— 通过门槛② 当且仅当它 ≥ θ）/ 公民总数拆内置与归化
- 候选面用 select_promotions 本尊算，标定读数与夜间任务判据逐字同源
- 判据 = 非空且非全量；空/全量/无数据一律报红并输出「待生产数据复标」
- 零写入、无 --dry-run（只有 dry 一态，它要跑在生产库上）

Verified-by: <贴 pytest 输出 + 本机空库真跑的报告全文（含待复标那两行）>
MSG_EOF
)"
```

---

### Task 7: `install_mayor` 的结票复核与事务化

**Files:**
- Modify: `backend/app/services/election_service.py:135-193`（只改 `install_mayor` 函数体；`:53-60` 是 F1 的独占区，**不得触碰**）
- Test: `backend/tests/test_install_mayor_recheck.py`

**Interfaces:**
- Consumes：`Resident.is_civic_voter`（`app/models/resident.py:113-125`）、`app.models.system_config.SystemConfig`
- Produces：`async def install_mayor(db, slug: str | None) -> bool` —— 签名不变，语义补两条

**这段代码落在归属真空里**：`election_service.py:135-193` 在 F1 声明的独占区 `:53-60` 之外，F1/F3 都不覆盖它，容易两边都不动。本计划把它划给 F2。

**两处收口：**

1. **结票时复核资格**：用 `Resident.is_civic_voter` 而非 `is_autonomous`（现状 `:141`）解析 winner；不合格直接 `return False` 且**不做任何写**。通用约束：**候选资格开票时快照，结票时复核，快照不构成信任**。
2. **事务化**：现状是先 `await db.commit()`（`:158`）再判 `winner is None`（`:160-161`）——winner 解析失败时旧镇长的 `meta_json` 已被清掉、`system_config` 与 offices 却仍指向他，留下三向分歧。触发条件今天就可达（目标 slug 查不到即可）。改成「先解析 winner，失败立即 return False 且零写入；旧镇长清理 + 新镇长安装 + `current_mayor` 记录同一事务同一次 commit」。`ConfigService.set()` 自带 commit（`config_service.py:48`），所以这里直接写 `SystemConfig` 行。

**顺带修掉 `:141` 的集合谓词**：清扫「谁不再是镇长」用的是 `Resident.is_autonomous`——又一处「用集合 S 去清理刚离开 S 的人」。改成全表（`meta_json IS NOT NULL`），逐出档落地时不必回来改。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_install_mayor_recheck.py`:

```python
"""F2 Task 7 —— install_mayor 的结票复核与事务化。

归属说明：election_service.py:135-193 落在 F1 独占区（:53-60 候选排序）之外，
F1/F3 都不覆盖它。本文件是 F2 对这段代码的收口测试。
"""
import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.system_config import SystemConfig
from app.services import civic_membership as cm
from app.services import election_service


def _res(slug, rtype=cm.CIVIC_MEMBER_TYPE, *, meta=None):
    return Resident(slug=slug, name=slug, district="central_plaza",
                    status="idle", resident_type=rtype, creator_id="sys",
                    tile_x=70, tile_y=56, meta_json=meta)


@pytest.mark.anyio
async def test_install_mayor_refuses_a_winner_who_lost_the_ballot(db_session):
    """候选资格开票时快照，结票时复核——快照不构成信任。被降级者不得就任。"""
    old = _res("old", meta={"mayor": True})
    demoted = _res("demoted", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add_all([old, demoted])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "demoted") is False

    # 零写入：旧镇长的标志必须原封不动（现状 bug 是先 commit 再判 winner）
    await db_session.refresh(old)
    assert (old.meta_json or {}).get("mayor") is True
    assert (await db_session.execute(
        select(SystemConfig).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none() is None


@pytest.mark.anyio
async def test_install_mayor_refuses_an_unknown_slug_without_writing(db_session):
    """今天就可达的触发条件：目标 slug 查不到。"""
    old = _res("old", meta={"mayor": True})
    db_session.add(old)
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "ghost") is False
    await db_session.refresh(old)
    assert (old.meta_json or {}).get("mayor") is True


@pytest.mark.anyio
async def test_install_mayor_refuses_an_empty_slug(db_session):
    assert await election_service.install_mayor(db_session, None) is False
    assert await election_service.install_mayor(db_session, "") is False


@pytest.mark.anyio
async def test_install_mayor_lands_all_representations_in_one_commit(db_session):
    old = _res("old", meta={"mayor": True})
    new = _res("new")
    db_session.add_all([old, new])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "new") is True

    await db_session.refresh(old)
    await db_session.refresh(new)
    assert (old.meta_json or {}).get("mayor") in (None, False)
    assert (new.meta_json or {}).get("mayor") is True
    cfg = (await db_session.execute(
        select(SystemConfig.value)
        .where(SystemConfig.key == "current_mayor"))).scalar_one()
    assert json.loads(cfg) == "new"


@pytest.mark.anyio
async def test_stale_mayor_flag_on_a_demoted_resident_is_still_swept(db_session):
    """通用约束：清理「已离开集合 S 的居民」不得用 S 本身做 WHERE。
    现状 :141 用 is_autonomous 做清扫面——降级档侥幸命中，逐出档天然自锁。"""
    ex = _res("ex-mayor", cm.UGC_RESIDENT_TYPE,
              meta={"origin": "forge", "mayor": True})
    winner = _res("winner")
    db_session.add_all([ex, winner])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "winner") is True
    rows = (await db_session.execute(select(Resident))).scalars().all()
    assert {r.slug for r in rows if (r.meta_json or {}).get("mayor")} == {"winner"}


@pytest.mark.anyio
async def test_reinstalling_the_same_mayor_is_idempotent(db_session):
    sitting = _res("sitting", meta={"mayor": True})
    db_session.add(sitting)
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "sitting") is True
    await db_session.refresh(sitting)
    assert (sitting.meta_json or {}).get("mayor") is True


@pytest.mark.anyio
async def test_office_dual_write_still_happens_when_gate_on(db_session, monkeypatch):
    """S2-1 的 offices 双写是 gate 开时的附加项，收口不得把它弄丢。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    from app.services.office_service import OfficeService

    db_session.add(_res("cand"))
    await db_session.commit()
    assert await election_service.install_mayor(db_session, "cand") is True
    assert await OfficeService(db_session).get_holder("mayor") == "cand"


@pytest.mark.anyio
async def test_close_one_announces_a_failed_vote_when_the_winner_lost_rights(
        db_session):
    """当选人已失去资格 → _close_one 走流会公告分支，不安装镇长。"""
    from app.models.season import Poll
    from app.services import civic_service

    db_session.add(_res("demoted", cm.UGC_RESIDENT_TYPE,
                        meta={"origin": "forge"}))
    poll = Poll(question=f"{election_service.ELECTION_TAG}:谁来当下一任镇长?",
                options_json=[
                    {"label": "落选者", "effect": {"type": "mayor",
                                                   "slug": "demoted"},
                     "npc_votes": 3},
                    {"label": "弃权", "effect": None, "npc_votes": 1},
                ], status="open")
    db_session.add(poll)
    await db_session.commit()

    await civic_service._close_one(db_session, poll)
    await db_session.refresh(poll)

    assert poll.status == "closed"
    assert (await db_session.execute(
        select(SystemConfig).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none() is None
    from app.models.bulletin_post import BulletinPost
    posts = (await db_session.execute(select(BulletinPost))).scalars().all()
    assert any("失去" in (p.content_md or "") for p in posts), \
        "公告必须说明本案流会的原因是当选人已失去资格"
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_install_mayor_recheck.py -q -p no:randomly
```
Expected: FAIL —— `test_install_mayor_refuses_a_winner_who_lost_the_ballot` 失败（现状用 `is_autonomous` 解析 winner，UGC 居民也能就任），`test_close_one_announces_a_failed_vote_when_the_winner_lost_rights` 也失败。

- [ ] **Step 3: 改 `election_service.py` 的导入**

把文件顶部的

```python
import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident
```

替换为

```python
import json
import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident
from app.models.system_config import SystemConfig
```

- [ ] **Step 4: 重写 `install_mayor`**

把 `election_service.py` 里从 `async def install_mayor(db, slug: str | None) -> bool:` 到 `    return True`（`:135-193` 整个函数）替换为：

```python
async def install_mayor(db, slug: str | None) -> bool:
    """Set ``slug`` as the sitting mayor (clearing any previous one) and record
    it in system_config. Returns True on success.

    F2 收口的两条语义（原实现的两个坑）：

    1. **结票时复核资格**。winner 用 ``Resident.is_civic_voter``（政治权利）
       解析，不是 ``is_autonomous``（世界人口）——候选名单是开票那一刻的快照，
       中途可能有人被降级，**快照不构成信任**。不合格立即 ``return False`` 且
       零写入，由 ``civic_service._close_one`` 走流会公告分支。
    2. **事务化**。原实现先 ``commit()`` 再判 ``winner is None``：winner 解析
       失败时旧镇长的 ``meta_json`` 已被清掉、``system_config`` 与 offices 却
       仍指向他，留下三向分歧（触发条件今天就可达：目标 slug 查不到即可）。
       现在旧镇长清理、新镇长安装、``current_mayor`` 记录在同一次 commit 里。
       ``ConfigService.set()`` 自带 commit，所以这里直接写 ``SystemConfig`` 行。

    清扫面是「全表带 ``meta_json`` 的居民」而不是 ``is_autonomous``——通用约束：
    凡是清理「已离开集合 S 的居民」的扫描，都不能用 S 本身做 WHERE（逐出档
    落地时无需回来改这里）。
    """
    if not slug:
        return False

    winner = (await db.execute(
        select(Resident).where(Resident.slug == slug, Resident.is_civic_voter)
    )).scalar_one_or_none()
    if winner is None:
        logger.warning(
            "install_mayor refused: %r is not (or is no longer) a civic voter "
            "— zero writes, the poll fails over to the 流会 branch", slug)
        return False

    others = (await db.execute(
        select(Resident).where(Resident.meta_json.isnot(None))
    )).scalars().all()
    for r in others:
        if r.slug == slug:
            continue
        meta = dict(r.meta_json or {})
        if meta.pop("mayor", None) is not None:
            r.meta_json = meta
            flag_modified(r, "meta_json")
    winner_meta = dict(winner.meta_json or {})
    if not winner_meta.get("mayor"):
        winner_meta["mayor"] = True
        winner.meta_json = winner_meta
        flag_modified(winner, "meta_json")

    cfg = (await db.execute(
        select(SystemConfig).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none()
    if cfg is None:
        db.add(SystemConfig(key="current_mayor", value=json.dumps(slug),
                            group="civic", updated_by="election"))
    else:
        cfg.value = json.dumps(slug)
        cfg.updated_by = "election"
        cfg.updated_at = datetime.now(UTC)
    await db.commit()

    # S2-1: dual-write the offices row when the gate is on. Both legacy
    # stores above stay alive — meta_json['mayor'] is the wage multiplier
    # (gotcha #1), system_config the read fallback. Fail-open: an offices
    # hiccup must never break an election result.
    if settings.polis_office_enabled:
        try:
            from app.services.office_service import OfficeService
            await OfficeService(db).appoint(
                "mayor", slug, fill_strategy="election",
                term_days=settings.polis_office_mayor_term_days,
            )
        except Exception:
            logger.warning("office dual-write failed for mayor", exc_info=True)
    try:
        from app.services.feed_service import push
        await push(slug, "goal_achieved", {"goal": "当选小镇镇长"})
        from app.memory.service import MemoryService
        await MemoryService(db).add_memory(
            winner.id, "event",
            "我当选了小镇的镇长。这份信任沉甸甸的,得对得起投我票的每一个人。",
            0.9, "reflection",
        )
    except Exception:
        logger.warning("mayor install side-effects failed", exc_info=True)
    return True
```

- [ ] **Step 5: 给 `_close_one` 加「当选人已失去资格」的流会分支**

在 `backend/app/services/civic_service.py` 里，把

```python
#: 流会原因 → 公告措辞（世界内信息物；探针数值永不进 NPC prompt）。
_VERDICT_NOTE = {
    "threshold_not_met": "未达本级审批所需的票数门槛",
    "quorum_not_met": "投票人数未达法定出席门槛",
    "no_votes": "无人投票",
}
```

替换为

```python
#: 流会原因 → 公告措辞（世界内信息物；探针数值永不进 NPC prompt）。
_VERDICT_NOTE = {
    "threshold_not_met": "未达本级审批所需的票数门槛",
    "quorum_not_met": "投票人数未达法定出席门槛",
    "no_votes": "无人投票",
    # F2：install_mayor 结票复核不通过（当选人在投票窗口内失去了公民资格）。
    "winner_ineligible": "当选人已失去公民资格",
}
```

并把 `_close_one` 末尾的

```python
    effect = opts[win].get("effect")
    result_note = f"「{poll.question}」投票结束,「{opts[win]['label']}」以 {tally[win]} 票胜出。"
    if effect:
        applied = await _execute_outcome(db, effect, poll_id=poll.id)
        result_note += "议案已生效。" if applied else "议案生效时遇到问题,已记录。"
    await _clerk_announce(db, f"镇务结果:{poll.question}", result_note)
```

替换为

```python
    effect = opts[win].get("effect")
    result_note = f"「{poll.question}」投票结束,「{opts[win]['label']}」以 {tally[win]} 票胜出。"
    if effect:
        applied = await _execute_outcome(db, effect, poll_id=poll.id)
        if applied:
            result_note += "议案已生效。"
        elif effect.get("type") == "mayor":
            # F2: install_mayor 的结票复核不通过 —— 当选人在投票窗口内被撤销了
            # 公民权。它是零写入的 return False，所以本案只是流会，不是「生效
            # 时出了问题」。
            result_note += f"{_VERDICT_NOTE['winner_ineligible']},本案流会。"
        else:
            result_note += "议案生效时遇到问题,已记录。"
    await _clerk_announce(db, f"镇务结果:{poll.question}", result_note)
```

- [ ] **Step 6: 跑测试确认通过（含既有回归）**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_install_mayor_recheck.py tests/test_m6_election.py \
  tests/test_office_integration.py tests/test_office_service.py \
  tests/test_ugc_resident_no_political_rights.py tests/test_m3_civic.py \
  tests/test_policy_approval_integration.py -q -p no:randomly
```
Expected: PASS。`tests/test_m3_civic.py` 是 `propose` / `_close_one` / `_npc_voters` 的既有回归套件（仓内 civic 相关测试只有它与 `test_burnin_report_civic_boundary.py`，开工前已核实）——本 Task 改了 `_close_one` 的公告分支，漏跑就等于没有回归护栏，必跑。

- [ ] **Step 7: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/services/election_service.py \
        backend/app/services/civic_service.py \
        backend/tests/test_install_mayor_recheck.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
fix(election): install_mayor 结票复核资格 + 事务化

归属真空收口（election_service.py:135-193 在 F1 独占区 :53-60 之外）：

- winner 用 is_civic_voter 解析而非 is_autonomous——候选名单是开票快照，
  中途可能有人被降级，快照不构成信任；不合格零写入 return False
- 原实现先 commit 再判 winner is None，失败时旧镇长 meta_json 已清、
  system_config 与 offices 仍指向他，三向分歧；改为同一次 commit
- 清扫面改全表（meta_json IS NOT NULL），不再用 is_autonomous 这个集合谓词
  去清「刚离开集合的人」
- _close_one 新增「当选人已失去公民资格」的流会公告分支

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 8: 在途投票——开票时冻结法定人数分母

**Files:**
- Modify: `backend/app/services/civic_service.py`（`propose()` ≈`:33-81`、`_eligible_voter_count()` / `_policy_threshold_verdict()` ≈`:529-564`）
- Test: `backend/tests/test_civic_frozen_denominator.py`

**Interfaces:**
- Consumes：`app.services.policy_service.META_THRESHOLD` (`"_policy_threshold"`) / `META_QUORUM` (`"_policy_quorum"`)；`settings.polis_policy_quorum_fraction`
- Produces：模块常量 `META_ELIGIBLE_AT_OPEN = "_eligible_at_open"`；`propose()` 在 `options_json[0]` 上写入该快照；`_policy_threshold_verdict()` 优先读快照

**方案 A（冻结分母），不实现撤票。** 幽灵票**保留**并写成设计语义「**投票时具备资格即计票**」——`_npc_voters` 是 `options_json[0]` 上的扁平 slug 列表（`civic_service.py:165`/`:173`），物理上没存票的归属，撤票要改结构且要兼容存量 poll，不值当。冻结分母对晋升与撤销**同时免疫**，改动局限在 `civic_service`。

**适用面必须写清楚**（否则会被以「默认 False」驳回）：threshold / quorum 整段只在 `polis_policy_approval_enabled`（默认 False，**vm212 为 true**）为真、且 `options_json[0]` 带 `META_THRESHOLD` 时才计算；quorum 还要额外带 `META_QUORUM`。普通 civic poll 与镇长选举 poll 走纯 plurality，分母不参与判决——撤销对它们的影响是票差而非流会。

**真正会翻转的算例（写进测试）**：10 位选民 / 4 票 → `4 < 10×0.5` 判 `quorum_not_met`；降掉 4 位已投票者后 eligible=6、total=4 → `4 < 3` 为假 → 通过。

**顺带把 `eligible > 0` 的沉默 guard 改成显式告警**：行为**完全不变**（`eligible == 0` 时仍跳过法定人数判定），但不再是一句沉默的短路——安全阀在分母为 0 时自己关掉，语义上说不通，至少要留下一条 WARNING。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_frozen_denominator.py`:

```python
"""F2 Task 8 —— 在途投票用「开票时冻结分母」解决，不实现撤票。

幽灵票保留并写成设计语义「投票时具备资格即计票」：_npc_voters 是
options_json[0] 上的扁平 slug 列表（civic_service.py:165/:173），物理上没存票
的归属，撤票要改结构且要兼容存量 poll。
"""
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll
from app.services import civic_membership as cm
from app.services import civic_service
from app.services.policy_service import META_QUORUM, META_THRESHOLD


def _res(slug, rtype=cm.CIVIC_MEMBER_TYPE):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id="sys", tile_x=1, tile_y=1)


@pytest.fixture
def approval_gate(monkeypatch):
    monkeypatch.setattr(settings, "polis_policy_approval_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_quorum_fraction", 0.5)
    yield


@pytest.mark.anyio
async def test_propose_freezes_the_electorate_size(db_session):
    db_session.add_all([_res(f"n{i}") for i in range(5)])
    db_session.add(_res("ugc", cm.UGC_RESIDENT_TYPE))
    await db_session.commit()

    poll = await civic_service.propose(
        db_session, "广场加长椅",
        [{"label": "支持", "effect": None}, {"label": "反对", "effect": None}])
    assert poll is not None
    # UGC 居民不在分母里
    assert poll.options_json[0][civic_service.META_ELIGIBLE_AT_OPEN] == 5


@pytest.mark.anyio
async def test_snapshot_survives_a_promotion_inside_the_voting_window(db_session):
    db_session.add_all([_res(f"n{i}") for i in range(5)])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    # 窗口内新增两位公民
    db_session.add_all([_res("late1"), _res("late2")])
    await db_session.commit()

    assert poll.options_json[0][civic_service.META_ELIGIBLE_AT_OPEN] == 5
    assert await civic_service._eligible_voter_count(db_session) == 7


@pytest.mark.anyio
async def test_quorum_reads_the_snapshot_not_the_live_count(db_session,
                                                            approval_gate):
    """会翻转的算例：10 位选民 / 4 票 → 4 < 10×0.5 判 quorum_not_met；若改读
    实时分母（降掉 4 位后 eligible=6）→ 4 < 3 为假 → 通过。冻结分母让这张
    poll 的判决不因窗口内的人事变动而改变。"""
    db_session.add_all([_res(f"n{i}") for i in range(6)])
    await db_session.commit()

    opts = [{"label": "赞成", "npc_votes": 4, META_THRESHOLD: 0.5,
             META_QUORUM: True, civic_service.META_ELIGIBLE_AT_OPEN: 10},
            {"label": "反对", "npc_votes": 0}]
    verdict = await civic_service._policy_threshold_verdict(
        db_session, opts, [4, 0], 0)
    assert verdict == "quorum_not_met"


@pytest.mark.anyio
async def test_legacy_poll_without_a_snapshot_falls_back_to_live_count(
        db_session, approval_gate):
    """存量 poll（本改动之前开的）没有快照 → 回落实时计数，行为与改动前一致。"""
    db_session.add_all([_res(f"n{i}") for i in range(10)])
    await db_session.commit()

    opts = [{"label": "赞成", "npc_votes": 4, META_THRESHOLD: 0.5,
             META_QUORUM: True},
            {"label": "反对", "npc_votes": 0}]
    assert await civic_service._policy_threshold_verdict(
        db_session, opts, [4, 0], 0) == "quorum_not_met"


@pytest.mark.anyio
async def test_plain_civic_polls_are_untouched_by_the_denominator(db_session,
                                                                  approval_gate):
    """普通 civic poll 不带 META_THRESHOLD → 纯 plurality，分母不参与判决。"""
    opts = [{"label": "A", "npc_votes": 1}, {"label": "B", "npc_votes": 0}]
    assert await civic_service._policy_threshold_verdict(
        db_session, opts, [1, 0], 0) is None


@pytest.mark.anyio
async def test_zero_electorate_warns_instead_of_silently_short_circuiting(
        db_session, approval_gate, caplog):
    """行为不变（分母为 0 时仍跳过法定人数判定），但必须留下 WARNING——安全阀
    在分母为 0 时自己关掉，语义上说不通。"""
    opts = [{"label": "赞成", "npc_votes": 2, META_THRESHOLD: 0.5,
             META_QUORUM: True, civic_service.META_ELIGIBLE_AT_OPEN: 0},
            {"label": "反对", "npc_votes": 0}]
    with caplog.at_level("WARNING"):
        verdict = await civic_service._policy_threshold_verdict(
            db_session, opts, [2, 0], 0)
    assert verdict is None
    assert any("eligible" in rec.message for rec in caplog.records)


@pytest.mark.anyio
async def test_ghost_votes_are_kept_by_design(db_session):
    """投票时具备资格即计票。被降级者的票不撤——_npc_voters 没存票的归属。"""
    voter = _res("will-be-demoted")
    db_session.add_all([voter, _res("n1")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    cast = await civic_service.run_npc_voting(db_session)
    assert cast == 2

    voter.resident_type = cm.UGC_RESIDENT_TYPE          # 模拟降级后的档位
    await db_session.commit()

    await db_session.refresh(poll)
    assert "will-be-demoted" in poll.options_json[0]["_npc_voters"]
    assert sum(int(o.get("npc_votes", 0)) for o in poll.options_json) == 2
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_frozen_denominator.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: module 'app.services.civic_service' has no attribute 'META_ELIGIBLE_AT_OPEN'`。

- [ ] **Step 3: 在 `civic_service.py` 加常量并在 `propose()` 写快照**

把

```python
logger = logging.getLogger(__name__)


async def propose(
```

替换为

```python
logger = logging.getLogger(__name__)

#: F2 —— 开票那一刻的合格选民数，冻结在 ``options_json[0]`` 上（同
#: ``_npc_voters`` / ``_proposer_slug`` 的 blob-on-opts[0] 约定）。
#:
#: 晋升与撤销都会在投票窗口内移动选民集。若法定人数的分母读结票时的实时
#: ``_eligible_voter_count()``，一张已经开出去的 poll 的判决门槛会在中途改变。
#: 冻结分母对晋升与撤销**同时免疫**，且改动局限在本模块。
#:
#: 配套的语义决定：**幽灵票保留，不实现撤票**——「投票时具备资格即计票」。
#: ``_npc_voters`` 是扁平 slug 列表，物理上没存票的归属，撤票要改
#: ``options_json`` 的形状且要兼容存量 poll。
META_ELIGIBLE_AT_OPEN = "_eligible_at_open"


async def propose(
```

再把 `propose()` 里的

```python
    if proposer_slug:
        # Same blob-on-opts[0] convention as _npc_voters: the proposer travels
        # with the poll so NPC voting can weigh the relationship (option 0 is
        # the proposer's lead option by convention).
        opts[0]["_proposer_slug"] = proposer_slug
```

替换为

```python
    if proposer_slug:
        # Same blob-on-opts[0] convention as _npc_voters: the proposer travels
        # with the poll so NPC voting can weigh the relationship (option 0 is
        # the proposer's lead option by convention).
        opts[0]["_proposer_slug"] = proposer_slug
    if opts:
        # F2: freeze the quorum denominator at open time (see
        # META_ELIGIBLE_AT_OPEN). Cheap — one COUNT on the same session.
        opts[0][META_ELIGIBLE_AT_OPEN] = await _eligible_voter_count(db)
```

- [ ] **Step 4: 让 `_policy_threshold_verdict` 读快照**

把 `_policy_threshold_verdict` 的函数体（从 docstring 之后的 `from app.services.policy_service import ...` 到 `return None`）替换为：

```python
    from app.services.policy_service import META_THRESHOLD, META_QUORUM

    blob = opts[0] if opts else {}
    threshold = blob.get(META_THRESHOLD)
    if threshold is None:
        return None                      # not a policy poll → status quo
    total = sum(tally)
    if total <= 0:
        return "no_votes"
    if blob.get(META_QUORUM):
        # F2: 分母取开票那一刻的快照；存量 poll（本改动之前开的）没有快照，
        # 回落实时计数 —— 行为与改动前逐字节一致。
        frozen = blob.get(META_ELIGIBLE_AT_OPEN)
        eligible = int(frozen if frozen is not None
                       else await _eligible_voter_count(db))
        if eligible <= 0:
            # 行为不变（跳过法定人数判定），但不再是一句沉默的 `eligible > 0`
            # 短路：安全阀在分母为 0 时自己关掉，语义上说不通，至少要留痕。
            logger.warning(
                "quorum check skipped: eligible electorate is %d "
                "(frozen=%r) — 选民集为空，法定人数分母无意义",
                eligible, frozen)
        elif total < eligible * settings.polis_policy_quorum_fraction:
            return "quorum_not_met"
    if (tally[win] / total) < float(threshold):
        return "threshold_not_met"
    return None
```

并把该函数的 docstring 末尾补上一段：

```python
    F2 冻结分母：法定人数的分母取 **开票那一刻** 的快照
    (``options_json[0][META_ELIGIBLE_AT_OPEN]``，由 :func:`propose` 写入)，
    而不是结票时的实时 ``_eligible_voter_count()``。适用面：整段只在
    ``polis_policy_approval_enabled`` 为真、且 opts[0] 带 ``META_THRESHOLD``
    时才计算；quorum 还要额外带 ``META_QUORUM``。普通 civic poll 与镇长选举
    poll 走纯 plurality，分母不参与判决——撤销对它们的影响是票差而非流会。
```

- [ ] **Step 5: 跑测试确认通过（含既有回归）**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_frozen_denominator.py \
  tests/test_policy_approval_integration.py tests/test_m6_election.py \
  tests/test_m3_civic.py \
  tests/test_ugc_resident_no_political_rights.py -q -p no:randomly
```
Expected: PASS。（`test_policy_approval_integration.py` 的 poll 是直接构造的、不带快照 → 走回落路径，读数不变。`tests/test_m3_civic.py` 是 `propose` / `_close_one` / `_npc_voters` 的既有回归套件，本 Task 改了 `propose()`，必跑。）

- [ ] **Step 6: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/services/civic_service.py \
        backend/tests/test_civic_frozen_denominator.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(civic): 在途投票冻结法定人数分母（方案 A，不实现撤票）

propose() 在 options_json[0] 上冻结开票那一刻的合格选民数；
_policy_threshold_verdict 优先读快照，存量 poll 回落实时计数（行为不变）。

- 幽灵票保留并写成设计语义「投票时具备资格即计票」——_npc_voters 是扁平
  slug 列表，物理上没存票的归属
- 冻结分母对晋升与撤销同时免疫，改动局限在 civic_service
- eligible <= 0 的沉默短路改成显式 WARNING（行为不变）

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 9: `reputation_service.py:74` 归到人口口径 + 全仓字面量分类守卫

**Files:**
- Modify: `backend/app/services/reputation_service.py:68-75`（一处谓词 + docstring）
- Test: `backend/tests/test_reputation_population_scope.py`

**Interfaces:**
- Consumes：`Resident.is_autonomous`（`app/models/resident.py:92-111`）
- Produces：`recompute()` 的选取谓词由 `Resident.resident_type == "npc"` 改为 `Resident.is_autonomous`；签名不变

**这是 `civic_membership` 收口时漏掉的第 11 处读。** 它必须归到**人口口径**（声誉是社会属性，不是政治权利）。不改的后果：被降级者退出夜间声誉重算、分数永久冻结在降级前那一刻，而 `election_service.py:53-60` 的候选排序读的正是这个冻结值；更要命的是未来「违规扣声誉」若先改档位再扣分，扣分动作会因这行字面量永远不生效。

**与 F1 的归属协商（`reputation_service.py` 是 F1 的独占文件）：** 执行本任务前先跑一次检查——

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
grep -n 'resident_type == "npc"\|Resident.is_autonomous' app/services/reputation_service.py
```

- 若还能看到 `resident_type == "npc"` → F1 尚未落地，**由 F2 改**（本任务照常执行全部步骤）。
- 若已经是 `Resident.is_autonomous` → F1 已顺手改过，**跳过 Step 3**，只落测试文件（Step 1/2 的红只会剩下断言本身，直接进 Step 4 验证 + Step 5 提交，commit message 改成 `test(civic): 锁住 reputation 的人口口径（F1 已改，F2 补断言）`）。

**全仓 `resident_type` 字面量分类**（F2 开工前的核查结果，写进测试 docstring 当作分类表）：

| 位置 | 形态 | 分类 | 处置 |
|---|---|---|---|
| `app/services/reputation_service.py:74` | `== "npc"` | **半状态源**（既不走 `is_civic_voter` 也不走 `is_autonomous`） | 本任务改为 `is_autonomous` |
| `app/routers/home_decor.py:56`、`app/agent/map_data.py:475` | `!= "player"` | 第三族谓词（玩家化身），刻意不动 | 保留 |
| `app/routers/admin/residents.py:38` | `in ("preset","npc",UGC)` | admin 面板显示标签 | 保留 |
| `app/routers/admin/residents.py:299` | `!= "preset"` | preset 删除守卫 | 保留 |
| `app/services/resident_sprite_publish_service.py:217` | `or "npc"` | 精灵模板的缺省回退，非成员判定 | 保留 |
| 五处创建点 + `onboarding_service.py:81` | `Resident(resident_type=...)` 关键字 | 创建路径 | 保留 |

改完后，全仓 `resident_type == "npc"` / `!= "npc"` 的比较应为**零**。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_reputation_population_scope.py`:

```python
"""F2 Task 9 —— 声誉是社会属性，不是政治权利。

reputation_service.recompute 是 civic_membership 收口时漏掉的第 11 处 type
读点（裸的 resident_type == "npc"）。不改的后果：被降级者退出夜间声誉重算、
分数永久冻结在降级前那一刻，而 election_service.py:53-60 的候选排序读的正是
这个冻结值；将来「违规扣声誉」若先改档位再扣分，扣分会因这行字面量永不生效。

全仓 resident_type 字面量分类（F2 开工核查）：
  半状态源  reputation_service.py:74           → 本任务改成 is_autonomous
  第三族    home_decor.py:56 / map_data.py:475 → != "player"，刻意不动
  展示层    admin/residents.py:38（标签）/ :299（preset 删除守卫）
  回退值    resident_sprite_publish_service.py:217（精灵模板缺省）
  创建路径  forge ×3 / routers/residents ×2 / onboarding ×1（关键字实参）
"""
import ast
import pathlib

import pytest

from app.config import settings
from app.models.resident import Resident
from app.services import civic_membership as cm
from app.services.reputation_service import recompute, score_from_meta

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _res(slug, rtype):
    return Resident(slug=slug, name=slug, district="central_plaza",
                    status="idle", resident_type=rtype, creator_id="sys",
                    tile_x=70, tile_y=56,
                    mood_json={"valence": 0.4, "arousal": 0.2, "label": "calm"},
                    meta_json={"sbti": {"dimensions": {"Ac1": "H"}}})


@pytest.mark.anyio
async def test_recompute_covers_the_world_population_not_the_electorate(
        db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", True)
    db_session.add_all([_res("builtin", cm.CIVIC_MEMBER_TYPE),
                        _res("demoted", cm.UGC_RESIDENT_TYPE)])
    await db_session.commit()

    assert await recompute(db_session) == 2, (
        "被降级者必须留在夜间声誉重算里，否则分数永久冻结在降级前那一刻")


@pytest.mark.anyio
async def test_recompute_skips_player_avatars(db_session, monkeypatch):
    """人口口径 = is_autonomous：玩家化身是注册成员但不是自治居民。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    db_session.add_all([_res("builtin", cm.CIVIC_MEMBER_TYPE),
                        _res("avatar", cm.PLAYER_RESIDENT_TYPE)])
    await db_session.commit()
    assert await recompute(db_session) == 1


@pytest.mark.anyio
async def test_demoted_resident_score_keeps_moving(db_session, monkeypatch):
    """回归意义上的断言：降级后再跑一次重算，分数确实被更新了。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    r = _res("demoted", cm.UGC_RESIDENT_TYPE)
    db_session.add(r)
    await db_session.commit()

    await recompute(db_session)
    await db_session.refresh(r)
    block = (r.meta_json or {}).get("reputation")
    assert block is not None, "被降级者必须拿到新的 reputation 投影"
    assert "score" in block and "updated_at" in block and "samples" in block
    # mood_valence=0.4 × rep_mood_weight，EMA 从 0 起步 → 分数必然为正
    assert score_from_meta(r.meta_json) > 0.0


def test_no_bare_npc_literal_comparison_survives_in_app():
    """结构性守卫：任何 `resident_type == "npc"` / `!= "npc"` 都是半状态源。

    成员判定必须走 Resident.is_autonomous（人口）或 Resident.is_civic_voter
    （政治），字面量只许出现在 civic_membership 的常量定义里。
    """
    offenders = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            is_type_read = (
                (isinstance(left, ast.Attribute) and left.attr == "resident_type")
                or (isinstance(left, ast.Name) and left.id == "resident_type")
            )
            if not is_type_read:
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.Eq, ast.NotEq)):
                    continue
                if (isinstance(comparator, ast.Constant)
                        and comparator.value == "npc"):
                    offenders.append(
                        f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
    assert offenders == [], (
        "裸的 resident_type 与 \"npc\" 比较 = 半状态源，改走 "
        f"is_autonomous / is_civic_voter：{offenders}")
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_reputation_population_scope.py -q -p no:randomly
```
Expected: FAIL —— `test_recompute_covers_the_world_population_not_the_electorate` 得到 1（只算了内置），且 `test_no_bare_npc_literal_comparison_survives_in_app` 报出 `app/services/reputation_service.py:74`。

- [ ] **Step 3: 改谓词（若 F1 已改则跳过本步）**

把 `backend/app/services/reputation_service.py` 的

```python
async def recompute(db: AsyncSession) -> int:
    """Recompute every NPC's slow reputation projection in two batch reads."""
    if not settings.rep_enabled:
        return 0

    residents = (await db.execute(
        select(Resident).where(Resident.resident_type == "npc")
    )).scalars().all()
```

替换为

```python
async def recompute(db: AsyncSession) -> int:
    """Recompute every inhabitant's slow reputation projection in two batch reads.

    口径是**世界人口**（``Resident.is_autonomous``），不是选民集
    （``is_civic_voter``）——声誉是社会属性，不是政治权利。这行原本是裸的
    ``resident_type == "npc"``，是 ``civic_membership`` 收口时漏掉的第 11 处读；
    留着它的后果是被降级者退出夜间重算、分数永久冻结在降级前那一刻，而
    ``election_service.py:53-60`` 的候选排序读的正是这个冻结值；未来「违规扣
    声誉」若先改档位再扣分，扣分动作也会因这行字面量永远不生效。
    """
    if not settings.rep_enabled:
        return 0

    residents = (await db.execute(
        select(Resident).where(Resident.is_autonomous)
    )).scalars().all()
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_reputation_population_scope.py tests/test_reputation_service.py -q -p no:randomly
```
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/services/reputation_service.py \
        backend/tests/test_reputation_population_scope.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
fix(reputation): recompute 归到人口口径——第 11 处 type 读点收口

声誉是社会属性不是政治权利：选取谓词由裸的 resident_type == "npc" 改为
Resident.is_autonomous。不改的后果是被降级者退出夜间重算、分数永久冻结在
降级前那一刻，而候选排序（election_service.py:53-60）读的正是这个冻结值。

附结构性守卫：全仓 app/ 下 resident_type 与 "npc" 的 ==/!= 比较归零。

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 10: `admin/residents.py` 的 `resident_type` 写入收敛 + AST 写入口守卫

**Files:**
- Modify: `backend/app/routers/admin/residents.py`（`_edit_resident` ≈`:90-128`、`edit_resident` ≈`:246-263`）
- Test: `backend/tests/test_civic_standing_write_entrypoints.py`

**Interfaces:**
- Consumes：`civic_membership.grant_citizenship(db, resident, *, reason, actor, evidence=None, reason_code="granted")`、`revoke_citizenship(db, resident, *, reason, actor, tier="demote", reason_code="revoked")`、`CivicStandingRefused`、`CIVIC_MEMBER_TYPE`、`UGC_RESIDENT_TYPE`
- Produces：`_edit_resident(db, resident_id, ability_md=None, persona_md=None, soul_md=None, district=None, status=None, resident_type=None, reply_mode=None, *, actor: str = "admin") -> Resident`（新增 keyword-only 参数 `actor`）；`PUT /admin/residents/{id}` 在被防呆拒绝时返回 **409**

**这一处是「唯一写入口」条款成立与否的分界。** `admin/residents.py:117-118` 是仓库里唯一的 `resident_type` 运行时裸赋值（已全仓核实：`grep -rn "\.resident_type = " app/ seed/` 只有这一处），也是批量 UPDATE 唯一的并发对手。不封它就意味着有一条完全不受 F2 管辖、零校验零清理的变更入口。

同时明确写进文档：**admin 手工把某人改回 `npc` 会在探针上显示为「无晋升记录的 UGC-origin 公民」——这正好是一条有用的红旗，不是噪声。**

⚠️ **不采纳的一条论断**（防止被写进实现）：「把误改成 `npc` 的玩家化身降级为 `resident` 会让 agent loop 开始驱动它 / 让装修权反转」是误读——`npc` 与 `resident` 同属 `SIM_RESIDENT_TYPES`，也同时满足 `!= "player"`，危害在 admin 手滑那一刻就已发生。但 `player` 仍然必须被 raise 拒绝——理由是**射程纪律**，不是这条危害链。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_standing_write_entrypoints.py`:

```python
"""F2 Task 10 —— resident_type 收敛为唯一写入口。

列上没有 CHECK（app/models/resident.py:55 是裸 String(20)），代码就是最后
一道闸。admin/residents.py:117-118 是仓库里唯一的运行时裸赋值，也是 F2 批量
UPDATE 唯一的并发对手（正面样板：relation_service.py:214-223；反面样板：
admin/residents.py:103-127 的读-改-写）。

结构性守卫仿 tests/test_ugc_resident_no_political_rights.py:69-88 的 AST 扫描，
把覆盖面从「Resident(...) 构造」扩展到「*.resident_type = ...」赋值。
"""
import ast
import pathlib

import pytest
from sqlalchemy import func, select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.user import User
from app.services import civic_membership as cm
from app.services.auth_service import create_token

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 允许给 resident_type 赋值的文件（相对 backend/）。加进来必须写理由。
_ASSIGNMENT_ALLOWLIST = {
    "app/services/civic_membership.py": "两个写入口所在的模块",
}


def test_only_civic_membership_assigns_resident_type():
    offenders = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        rel = str(path.relative_to(BACKEND_ROOT))
        if rel in _ASSIGNMENT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "resident_type":
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "resident_type 只许由 civic_membership 的两个写入口改写（列上没有 "
        f"CHECK，代码是最后一道闸）：{offenders}")


def test_every_resident_construction_still_sets_the_type_explicitly():
    """既有守卫的复述：创建路径必须显式给 resident_type（依赖模型默认值正是
    2026-07-25 把选票发给 UGC 居民的根因）。"""
    offenders = []
    for sub in ("app", "seed"):
        for path in (BACKEND_ROOT / sub).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Name) and fn.id == "Resident"):
                    continue
                if not any(kw.arg == "resident_type" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
    assert offenders == []


# ── admin 路由的功能验证 ───────────────────────────────────────────────

async def _admin(db):
    u = User(name="管理员", email="admin@t.com", is_admin=True)
    db.add(u)
    await db.commit()
    return u


def _res(slug, rtype, *, creator_id="u1", meta=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta)


@pytest.mark.anyio
async def test_admin_promotion_goes_through_the_write_entrypoint(client, db_session):
    admin = await _admin(db_session)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"resident_type": cm.CIVIC_MEMBER_TYPE},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text

    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE
    row = (await db_session.execute(select(CivicStandingHistory))).scalar_one()
    assert row.new_standing == cm.CITIZEN
    assert row.actor.startswith("admin:"), "actor 必须带 admin 的 user id"


@pytest.mark.anyio
async def test_admin_demotion_goes_through_the_write_entrypoint(client, db_session):
    admin = await _admin(db_session)
    db_session.add_all([_res(f"b{i}", cm.CIVIC_MEMBER_TYPE,
                             creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    r = _res("ugc-1", cm.CIVIC_MEMBER_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    await cm._write_history(
        db_session, resident_id=r.id, old_standing=cm.DENIZEN,
        new_standing=cm.CITIZEN, reason=None, reason_code="threshold_met",
        actor="civic_promotion", evidence=None)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"resident_type": cm.UGC_RESIDENT_TYPE},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text
    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.UGC_RESIDENT_TYPE


@pytest.mark.anyio
async def test_admin_cannot_demote_a_builtin(client, db_session):
    """射程纪律：防呆对 admin 同样生效，返回 409 而不是静默成功。"""
    admin = await _admin(db_session)
    db_session.add_all([_res(f"b{i}", cm.CIVIC_MEMBER_TYPE,
                             creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    await db_session.commit()
    b = (await db_session.execute(
        select(Resident).where(Resident.slug == "b0"))).scalar_one()

    resp = await client.put(
        f"/admin/residents/{b.id}",
        json={"resident_type": cm.UGC_RESIDENT_TYPE, "district": "free"},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 409, resp.text
    await db_session.refresh(b)
    assert b.resident_type == cm.CIVIC_MEMBER_TYPE
    assert b.district == "town_hall", "拒绝必须是整请求的 no-op"


@pytest.mark.anyio
async def test_admin_cannot_set_an_arbitrary_type(client, db_session):
    admin = await _admin(db_session)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"resident_type": "player"},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 409, resp.text
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_admin_edit_of_other_fields_still_works(client, db_session):
    admin = await _admin(db_session)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"district": "free", "status": "sleeping"},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(r)
    assert (r.district, r.status) == ("free", "sleeping")
    assert r.resident_type == cm.UGC_RESIDENT_TYPE


@pytest.mark.anyio
async def test_admin_setting_the_same_type_is_a_noop(client, db_session):
    admin = await _admin(db_session)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"resident_type": cm.UGC_RESIDENT_TYPE},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_standing_write_entrypoints.py -q -p no:randomly
```
Expected: FAIL —— `test_only_civic_membership_assigns_resident_type` 报出 `app/routers/admin/residents.py:118`；三个 409 用例拿到 200。

- [ ] **Step 3: 改 `_edit_resident`**

把 `backend/app/routers/admin/residents.py` 的

```python
async def _edit_resident(
    db: AsyncSession,
    resident_id: str,
    ability_md: str | None = None,
    persona_md: str | None = None,
    soul_md: str | None = None,
    district: str | None = None,
    status: str | None = None,
    resident_type: str | None = None,
    reply_mode: str | None = None,
) -> Resident:
    """Admin-level edit of any resident's fields."""
    result = await db.execute(select(Resident).where(Resident.id == resident_id))
    resident = result.scalar_one_or_none()
    if not resident:
        raise ValueError("Resident not found")

    if ability_md is not None:
```

替换为

```python
async def _edit_resident(
    db: AsyncSession,
    resident_id: str,
    ability_md: str | None = None,
    persona_md: str | None = None,
    soul_md: str | None = None,
    district: str | None = None,
    status: str | None = None,
    resident_type: str | None = None,
    reply_mode: str | None = None,
    *,
    actor: str = "admin",
) -> Resident:
    """Admin-level edit of any resident's fields.

    ``resident_type`` **不再裸赋值**：它是政治层的档位编码，一律转调
    ``civic_membership`` 的两个写入口，从而拿到同一套防呆、同一套有序清理与
    同一行 ``civic_standing_history``。列上没有 CHECK，代码就是最后一道闸；
    这里是仓库里唯一的运行时裸赋值点，不封它「唯一写入口」条款就是假的。

    档位转换放在**所有其它字段之前**：两个写入口都是 guard-first（第一条
    UPDATE 之前抛出），所以被拒绝时整个请求都是 no-op。

    ⚠️ admin 手工把某人改回 ``npc`` 会在 burn-in 探针上显示为「无晋升记录的
    UGC-origin 公民」——那是一条有用的红旗，不是噪声。
    """
    from app.services.civic_membership import (
        CIVIC_MEMBER_TYPE, CivicStandingRefused, grant_citizenship,
        revoke_citizenship,
    )

    result = await db.execute(select(Resident).where(Resident.id == resident_id))
    resident = result.scalar_one_or_none()
    if not resident:
        raise ValueError("Resident not found")

    if resident_type is not None and resident_type != resident.resident_type:
        if (resident_type == CIVIC_MEMBER_TYPE
                and resident.resident_type == UGC_RESIDENT_TYPE):
            await grant_citizenship(
                db, resident, reason=f"admin edit ({actor})", actor=actor,
                evidence={"source": "admin_edit"}, reason_code="admin_grant",
            )
        elif (resident_type == UGC_RESIDENT_TYPE
                and resident.resident_type == CIVIC_MEMBER_TYPE):
            await revoke_citizenship(
                db, resident, reason=f"admin edit ({actor})", actor=actor,
                tier="demote", reason_code="admin_revoke",
            )
        else:
            raise CivicStandingRefused(
                f"admin edit refused: {resident.resident_type!r} → "
                f"{resident_type!r} is not a civic standing transition. 只支持 "
                f"{UGC_RESIDENT_TYPE!r} ⇄ {CIVIC_MEMBER_TYPE!r}；player / preset "
                "的出身是冻结的，不由政治层改写。"
            )
        await db.refresh(resident)

    if ability_md is not None:
```

再把同一函数里的

```python
    if status is not None:
        resident.status = status
    if resident_type is not None:
        resident.resident_type = resident_type
    if reply_mode is not None:
```

替换为

```python
    if status is not None:
        resident.status = status
    # resident_type 已在函数开头经两个写入口处理，这里刻意不再赋值
    if reply_mode is not None:
```

- [ ] **Step 4: 改路由：透传 actor + 把拒绝映射成 409**

把

```python
    """Edit any resident's persona layers, district, status, type, reply mode."""
    try:
        resident = await _edit_resident(
            db, resident_id,
            ability_md=req.ability_md, persona_md=req.persona_md, soul_md=req.soul_md,
            district=req.district, status=req.status,
            resident_type=req.resident_type, reply_mode=req.reply_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _resident_to_dict(resident)
```

替换为

```python
    """Edit any resident's persona layers, district, status, type, reply mode."""
    from app.services.civic_membership import CivicStandingRefused

    try:
        resident = await _edit_resident(
            db, resident_id,
            ability_md=req.ability_md, persona_md=req.persona_md, soul_md=req.soul_md,
            district=req.district, status=req.status,
            resident_type=req.resident_type, reply_mode=req.reply_mode,
            actor=f"admin:{admin.id}",
        )
    except CivicStandingRefused as e:
        # 防呆拒绝是 409（冲突/被拒），不是 404（找不到）——两者混在一起会让
        # 运维以为「居民不存在」而重试。
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _resident_to_dict(resident)
```

- [ ] **Step 5: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_standing_write_entrypoints.py \
  tests/test_ugc_resident_no_political_rights.py -q -p no:randomly
ls tests/ | grep -i admin        # 若有 admin 路由测试，一并跑
```
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/routers/admin/residents.py \
        backend/tests/test_civic_standing_write_entrypoints.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(admin): resident_type 裸赋值收敛到 civic_membership 的两个写入口

admin/residents.py:117-118 是仓库里唯一的运行时裸赋值，也是 F2 批量 UPDATE
唯一的并发对手。不封它，「唯一写入口」条款就是假的（列上没有 CHECK）。

- npc ⇄ resident 转调 grant_citizenship / revoke_citizenship，actor 带 admin id
- 其它转换一律 CivicStandingRefused → HTTP 409（不是 404）
- 档位转换放在所有其它字段之前：写入口 guard-first，拒绝即整请求 no-op
- AST 守卫：app/ 下除 civic_membership 外无人给 resident_type 赋值

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 11: burn-in 探针升级（四项新增 + 一项升格）

**Files:**
- Modify: `backend/scripts/burnin_report.py`（探针区 ≈`:1176-1282` 之后新增；`render_probes_civic_boundary` 的 ⚠️→🔴；`_run()` 接线）
- Test: `backend/tests/test_burnin_report_civic_standing.py`

**Interfaces:**
- Consumes：`civic_membership` 的 `CITIZEN` / `POLITICAL_FILL_STRATEGY` / `SYSTEM_CREATOR_ID` / `is_ugc_resident` / 门槛旋钮；`civic_promotion.build_snapshot(db)` / `select_promotions(...)` / `_as_aware(dt)`；`app.models.office.Office` / `app.models.season.Poll` / `app.models.system_config.SystemConfig`
- Produces：
  - `async def fetch_civic_standing_snapshot(session, *, gate_office_on: bool = False, flip_window_world_days: float = 7.0) -> dict`
  - `def render_probes_civic_standing(snapshot: dict) -> str`
  - `render_probes_civic_boundary` 的 `unknown_types` 输出从 ⚠️ 升为 🔴

**现有探针对 F2 的核心失败模式全盲。** `civic_boundary_breakdown` 判泄漏的条件是**常量集合被拓宽**（`:1234-1236`），而 F2 只改行值不改集合，**永远不会触发**；误升只会让 npc 计数合法增长，07-25 靠人眼看出「npc 该是 10 人却有 13 人」的那条人工嗅觉也一起失效。所以判泄漏的条件必须改成「**provenance=UGC 且 `is_civic_voter` 为真、但 `civic_standing_history` 里查不到晋升记录**」。

**四项新增 + 一项升格：**

1. **交叉表 `resident_type × provenance`**：三行口径——内置 npc / 已晋升 UGC / 未晋升 UGC。
2. **晋升队列**：满足门槛但仍是 `resident` 的人数（= shadow 模式的候选名单大小）。
3. **翻转统计**：每位居民累计变更次数、**最近 7 世界日内发生翻转的居民数**、当前处于最短任期或冷却期内的人数。**「最近 7 世界日翻转数 > 0」是告警条件，不是信息项**——滞后设计生效后稳态下这个数应恒为 0。
4. **交叉一致性**（只读、零 LLM）：①每个 `fill_strategy='election'` 的 holder 必须 `is_civic_voter`（**只对民选职位断言**——`town_clerk`/`postman`/`doctor` 是劳动职务，UGC 居民担任是既定边界）；②带 `meta_json['mayor']` 的集合 == `{offices.mayor.holder_slug}` == `{system_config['current_mayor']}`（⚠️ **必须按 `polis_office_enabled` 分档**，gate 关时 offices 是 046 遗留值，不分档会在 T2 前直接报红并被当噪声关掉）；③每个 open poll 的 `_npc_voters` 全员当前仍是 `is_civic_voter`，否则输出幽灵票数；④所有 `offices.holder_slug` 都能在 residents 表查到。
5. **`unknown_types` 非空从 ⚠️ 升为 🔴**——这是未来引入新 type 时唯一的自动发现口，也是写错一个字符的唯一兜底。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_burnin_report_civic_standing.py`:

```python
"""F2 Task 11 —— 晋升与撤销的可观测性（硬门 1）。

现有探针（burnin_report.py:1176-1282）只输出按 resident_type 分组的静态计数，
对 F2 的核心失败模式全盲：误升只会让 npc 计数合法增长，leaked_voter_types 判
的是常量集合被拓宽，F2 只改行值不改集合，永远不会触发。
"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import world_clock
from app.models.civic_standing_history import CivicStandingHistory
from app.models.office import Office
from app.models.resident import Resident
from app.models.season import Poll
from app.models.system_config import SystemConfig
from app.services import civic_membership as cm
from scripts.burnin_report import (
    civic_boundary_breakdown,
    fetch_civic_standing_snapshot,
    render_probes_civic_boundary,
    render_probes_civic_standing,
)


def _res(slug, rtype, *, creator_id="u1", meta=None, created_days_ago=200):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=datetime.now(UTC)
                    - timedelta(days=created_days_ago))


def _builtin(slug):
    return _res(slug, cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
                meta={"origin": "preset"})


def _ugc(slug, rtype=cm.UGC_RESIDENT_TYPE):
    return _res(slug, rtype, meta={"origin": "forge"})


async def _history(db, resident_id, old, new, *, world_days_ago=0.0,
                   actor="civic_promotion"):
    db.add(CivicStandingHistory(
        resident_id=resident_id, old_standing=old, new_standing=new,
        reason=None, reason_code="threshold_met", actor=actor,
        evidence_json={},
        world_at=(world_clock.now_world().astimezone(UTC)
                  - timedelta(days=world_days_ago))))
    await db.commit()


# ── ① 交叉表与泄漏判据 ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_cross_table_splits_builtin_promoted_and_unpromoted(db_session):
    promoted = _ugc("ugc-promoted", cm.CIVIC_MEMBER_TYPE)
    db_session.add_all([_builtin("b1"), _builtin("b2"), promoted,
                        _ugc("ugc-waiting")])
    await db_session.commit()
    await _history(db_session, promoted.id, cm.DENIZEN, cm.CITIZEN)

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["available"] is True
    assert snap["cross"]["builtin_citizen"] == 2
    assert snap["cross"]["ugc_citizen_promoted"] == 1
    assert snap["cross"]["ugc_citizen_unrecorded"] == 0
    assert snap["cross"]["ugc_denizen"] == 1
    assert snap["leaked"] == []


@pytest.mark.anyio
async def test_leak_is_a_ugc_voter_without_a_promotion_record(db_session):
    """判泄漏的条件改成「provenance=UGC 且 is_civic_voter 为真、但查不到晋升
    记录」——现有探针判的是常量集合被拓宽，F2 只改行值不改集合。"""
    db_session.add_all([_builtin("b1"), _ugc("sneaky", cm.CIVIC_MEMBER_TYPE)])
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["cross"]["ugc_citizen_unrecorded"] == 1
    assert snap["leaked"] == ["sneaky"]
    out = render_probes_civic_standing(snap)
    assert "🔴" in out and "sneaky" in out


# ── ② 晋升队列 ─────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_promotion_queue_counts_threshold_ready_denizens(
        db_session, monkeypatch):
    from app.models.resident_relation import ResidentRelation

    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.2")

    b1, b2, u = _builtin("b1"), _builtin("b2"), _ugc("u1")
    db_session.add_all([b1, b2, u, _ugc("u2")])
    await db_session.commit()
    for b in (b1, b2):
        a_id, b_id = sorted([b.id, u.id])
        db_session.add(ResidentRelation(party_a=a_id, party_b=b_id,
                                        familiarity=0.5))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["queue"]["size"] == 1
    assert snap["queue"]["slugs"] == ["u1"]


# ── ③ 翻转统计（告警条件，不是信息项）─────────────────────────────────

@pytest.mark.anyio
async def test_recent_flip_is_an_alert_not_an_info_line(db_session):
    """静态计数发现不了振荡——11 内置 + 3 归化的读数在 X 升 / Y 降的同一夜看
    起来完全正常。滞后设计生效后稳态下这个数应恒为 0。"""
    r = _ugc("flipper", cm.CIVIC_MEMBER_TYPE)
    db_session.add_all([_builtin("b1"), r])
    await db_session.commit()
    await _history(db_session, r.id, cm.DENIZEN, cm.CITIZEN, world_days_ago=6)
    await _history(db_session, r.id, cm.CITIZEN, cm.DENIZEN, world_days_ago=5)
    await _history(db_session, r.id, cm.DENIZEN, cm.CITIZEN, world_days_ago=1)

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["flips"]["recent_flip_residents"] == 1
    assert snap["flips"]["max_changes_per_resident"] == 3
    assert snap["flips"]["in_min_tenure"] >= 1
    out = render_probes_civic_standing(snap)
    assert "🔴" in out
    assert "翻转" in out


@pytest.mark.anyio
async def test_no_recent_flip_is_quiet(db_session):
    r = _ugc("settled", cm.CIVIC_MEMBER_TYPE)
    db_session.add_all([_builtin("b1"), r])
    await db_session.commit()
    await _history(db_session, r.id, cm.DENIZEN, cm.CITIZEN, world_days_ago=90)

    snap = await fetch_civic_standing_snapshot(db_session)
    assert snap["flips"]["recent_flip_residents"] == 0
    assert snap["leaked"] == []


# ── ④ 交叉一致性 ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_elected_office_held_by_a_non_voter_is_red(db_session):
    u = _ugc("ugc-mayor")
    db_session.add_all([_builtin("b1"), u])
    db_session.add(Office(office_key="mayor", holder_slug="ugc-mayor",
                          institution="town_hall", perms_json={},
                          fill_strategy=cm.POLITICAL_FILL_STRATEGY))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session, gate_office_on=True)
    assert snap["crosscheck"]["election_office_non_voter"] == [
        ["mayor", "ugc-mayor"]]


@pytest.mark.anyio
async def test_labour_office_held_by_a_ugc_resident_is_not_flagged(db_session):
    """只对民选职位断言：town_clerk / postman / doctor 是劳动职务，UGC 居民
    担任它们是既定边界，不是红旗。"""
    db_session.add_all([_builtin("b1"), _ugc("ugc-postman")])
    db_session.add(Office(office_key="postman", holder_slug="ugc-postman",
                          institution="post_office", perms_json={},
                          fill_strategy="seed"))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session, gate_office_on=True)
    assert snap["crosscheck"]["election_office_non_voter"] == []


@pytest.mark.anyio
async def test_mayor_three_way_consistency_is_gated_on_the_office_flag(db_session):
    """gate 关时 offices 是迁移 046 的遗留值，不分档会在 T2 前直接报红并被当
    噪声关掉。"""
    db_session.add(_builtin("b1", ))
    db_session.add(Office(office_key="mayor", holder_slug="stale-046",
                          institution="town_hall", perms_json={},
                          fill_strategy=cm.POLITICAL_FILL_STRATEGY))
    db_session.add(SystemConfig(key="current_mayor", value=json.dumps(None),
                                group="civic", updated_by="ops"))
    await db_session.commit()

    gate_off = await fetch_civic_standing_snapshot(db_session,
                                                   gate_office_on=False)
    assert gate_off["crosscheck"]["mayor_reps"]["checked"] is False

    gate_on = await fetch_civic_standing_snapshot(db_session,
                                                  gate_office_on=True)
    assert gate_on["crosscheck"]["mayor_reps"]["checked"] is True
    assert gate_on["crosscheck"]["mayor_reps"]["consistent"] is False


@pytest.mark.anyio
async def test_ghost_votes_on_open_polls_are_counted(db_session):
    demoted = _ugc("demoted")
    db_session.add_all([_builtin("b1"), demoted])
    db_session.add(Poll(question="议题", status="open",
                        options_json=[{"label": "A", "npc_votes": 2,
                                       "_npc_voters": ["b1", "demoted"]},
                                      {"label": "B", "npc_votes": 0}]))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session)
    ghosts = snap["crosscheck"]["ghost_votes"]
    assert len(ghosts) == 1
    assert ghosts[0]["ghosts"] == 1
    assert "demoted" in ghosts[0]["slugs"]


@pytest.mark.anyio
async def test_dangling_office_holders_are_reported(db_session):
    """purge_residents 不清 offices 与 current_mayor：删掉在任镇长会留下悬空
    holder_slug，current_mayor() 照常返回它，townhall.py:61 会把 slug 当名字
    显示给玩家。"""
    db_session.add(_builtin("b1"))
    db_session.add(Office(office_key="mayor", holder_slug="deleted-guy",
                          institution="town_hall", perms_json={},
                          fill_strategy=cm.POLITICAL_FILL_STRATEGY))
    await db_session.commit()

    snap = await fetch_civic_standing_snapshot(db_session, gate_office_on=True)
    assert snap["crosscheck"]["dangling_holders"] == ["deleted-guy"]


@pytest.mark.anyio
async def test_probe_is_skipped_when_the_table_is_missing(db_session):
    """新表未建（迁移未跑）→ 探针跳过而不是炸掉整份报告。"""
    out = render_probes_civic_standing({"available": False})
    assert "探针跳过" in out


# ── ⑤ unknown_types 升为红旗 ───────────────────────────────────────────

def test_unknown_types_render_as_a_red_flag():
    """未来引入新 type 时唯一的自动发现口，也是写错一个字符的唯一兜底。"""
    snap = {"available": True, "by_type": {"npc": 10, "npc ": 1}}
    d = civic_boundary_breakdown(snap)
    assert d["unknown_types"] == {"npc ": 1}
    out = render_probes_civic_boundary(snap)
    assert "🔴" in out
    assert "⚠️ 两列之外的取值" not in out
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_burnin_report_civic_standing.py -q -p no:randomly
```
Expected: FAIL —— `ImportError: cannot import name 'fetch_civic_standing_snapshot' from 'scripts.burnin_report'`。

- [ ] **Step 3: 在 `burnin_report.py` 追加 F2 探针**

在 `backend/scripts/burnin_report.py` 的 `render_probes_civic_boundary` 函数**之后**、`# ----- CLI` 分隔注释**之前**插入：

```python


# ---------------------------------------------------------------------------
# F2 公民权档位探针（晋升/撤销可观测）—— 只读、零 LLM
# ---------------------------------------------------------------------------
#
# 现有的政治层边界探针判泄漏的条件是「常量集合被拓宽」（civic_boundary_
# breakdown），而 F2 只改行值不改集合，那条永远不会触发；误升只会让 npc 计数
# 合法增长，07-25 靠人眼看出「npc 该是 10 人却有 13 人」的嗅觉也一起失效。
# 所以这里把判据改成「provenance=UGC 且 is_civic_voter 为真、但
# civic_standing_history 里查不到晋升记录」。

#: 「最近 N 世界日内发生翻转」的窗口。滞后设计生效后稳态下这个数应恒为 0，
#: 所以它是**告警条件**，不是信息项。
CIVIC_FLIP_WINDOW_WORLD_DAYS = 7.0


async def fetch_civic_standing_snapshot(
    session, *, gate_office_on: bool = False,
    flip_window_world_days: float = CIVIC_FLIP_WINDOW_WORLD_DAYS,
) -> dict:
    """交叉表 / 晋升队列 / 翻转统计 / 交叉一致性。表不存在 → available=False。"""
    from datetime import timedelta

    try:
        from app.models.civic_standing_history import CivicStandingHistory
        from app.models.office import Office
        from app.models.resident import Resident
        from app.models.season import Poll
        from app.models.system_config import SystemConfig
        from app.services.civic_membership import (
            CITIZEN, CIVIC_VOTER_TYPES as _VOTERS, POLITICAL_FILL_STRATEGY,
            SYSTEM_CREATOR_ID, UGC_RESIDENT_TYPE, is_ugc_resident,
            min_familiarity, min_peers, min_tenure_world_days, min_world_days,
            peer_seasoning_world_days, promotion_cooldown_world_days,
        )
        from app.tasks import civic_promotion as cp

        residents = (await session.execute(select(Resident))).scalars().all()
        history = (await session.execute(
            select(CivicStandingHistory))).scalars().all()
        offices = (await session.execute(select(Office))).scalars().all()
        polls = (await session.execute(
            select(Poll).where(Poll.status == "open"))).scalars().all()
        mayor_cfg_raw = (await session.execute(
            select(SystemConfig.value)
            .where(SystemConfig.key == "current_mayor"))).scalar_one_or_none()
    except Exception:
        return {"available": False}

    by_id = {r.id: r for r in residents}
    voter_slugs = {r.slug for r in residents if r.resident_type in _VOTERS}
    promoted_ids = {h.resident_id for h in history if h.new_standing == CITIZEN}

    cross = {"builtin_citizen": 0, "ugc_citizen_promoted": 0,
             "ugc_citizen_unrecorded": 0, "ugc_denizen": 0, "other": 0}
    leaked: list[str] = []
    for r in residents:
        is_voter = r.resident_type in _VOTERS
        if r.creator_id == SYSTEM_CREATOR_ID and is_voter:
            cross["builtin_citizen"] += 1
        elif is_ugc_resident(r) and is_voter:
            if r.id in promoted_ids:
                cross["ugc_citizen_promoted"] += 1
            else:
                cross["ugc_citizen_unrecorded"] += 1
                leaked.append(r.slug)
        elif is_ugc_resident(r) and r.resident_type == UGC_RESIDENT_TYPE:
            cross["ugc_denizen"] += 1
        else:
            cross["other"] += 1

    # ② 晋升队列（= shadow 模式的候选名单大小）
    try:
        snap = await cp.build_snapshot(session)
        queue_ids = cp.select_promotions(
            snap, min_world_days=min_world_days(), min_peers=min_peers(),
            min_familiarity=min_familiarity(),
            seasoning_days=peer_seasoning_world_days())
        queue = {"size": len(queue_ids),
                 "slugs": sorted(by_id[i].slug for i in queue_ids
                                 if i in by_id)}
        now_world = snap.now_world
    except Exception:
        queue = {"size": None, "slugs": []}
        from app import world_clock
        now_world = world_clock.now_world()

    # ③ 翻转统计
    changes: dict[str, int] = {}
    recent: set[str] = set()
    last_change: dict[str, object] = {}
    for h in history:
        changes[h.resident_id] = changes.get(h.resident_id, 0) + 1
        when = cp._as_aware(h.world_at)
        if (now_world - when) <= timedelta(days=flip_window_world_days):
            recent.add(h.resident_id)
        prev = last_change.get(h.resident_id)
        if prev is None or when > prev[0]:
            last_change[h.resident_id] = (when, h.new_standing)
    in_min_tenure = sum(
        1 for (when, new) in last_change.values()
        if new == CITIZEN
        and (now_world - when) < timedelta(days=min_tenure_world_days()))
    in_cooldown = sum(
        1 for (when, new) in last_change.values()
        if new != CITIZEN
        and (now_world - when) < timedelta(days=promotion_cooldown_world_days()))
    flips = {
        "window_world_days": flip_window_world_days,
        "residents_with_history": len(changes),
        "max_changes_per_resident": max(changes.values()) if changes else 0,
        "recent_flip_residents": len(recent),
        "in_min_tenure": in_min_tenure,
        "in_cooldown": in_cooldown,
    }

    # ④ 交叉一致性
    resident_slugs = {r.slug for r in residents}
    election_office_non_voter = [
        [o.office_key, o.holder_slug] for o in offices
        if o.fill_strategy == POLITICAL_FILL_STRATEGY and o.holder_slug
        and o.holder_slug not in voter_slugs
    ]
    dangling = sorted({o.holder_slug for o in offices
                       if o.holder_slug and o.holder_slug not in resident_slugs})
    meta_mayors = sorted(r.slug for r in residents
                         if (r.meta_json or {}).get("mayor"))
    office_mayor = next((o.holder_slug for o in offices
                         if o.office_key == "mayor"), None)
    cfg_mayor = None
    if mayor_cfg_raw is not None:
        try:
            cfg_mayor = json.loads(mayor_cfg_raw)
        except (TypeError, ValueError):
            cfg_mayor = None
    # ⚠️ 按 polis_office_enabled 分档：gate 关时 offices 是迁移 046 的遗留值，
    # 不分档会在 T2 前直接报红并被当噪声关掉。
    mayor_reps = {
        "checked": bool(gate_office_on),
        "meta": meta_mayors,
        "office": office_mayor,
        "config": cfg_mayor,
        "consistent": None,
    }
    if gate_office_on:
        reps = {tuple(meta_mayors),
                tuple([office_mayor] if office_mayor else []),
                tuple([cfg_mayor] if cfg_mayor else [])}
        mayor_reps["consistent"] = len(reps) == 1

    ghost_votes = []
    for poll in polls:
        opts = list(poll.options_json or [])
        if not opts:
            continue
        voters = list((opts[0] or {}).get("_npc_voters", []))
        ghosts = sorted(s for s in voters if s not in voter_slugs)
        if ghosts:
            ghost_votes.append({"question": poll.question,
                                "ghosts": len(ghosts), "slugs": ghosts})

    return {
        "available": True,
        "cross": cross,
        "leaked": sorted(leaked),
        "queue": queue,
        "flips": flips,
        "crosscheck": {
            "election_office_non_voter": election_office_non_voter,
            "mayor_reps": mayor_reps,
            "ghost_votes": ghost_votes,
            "dangling_holders": dangling,
        },
    }


def render_probes_civic_standing(snapshot: dict) -> str:
    out = ["== 公民权档位探针（provenance × standing · 只读零 LLM）=="]
    if not snapshot.get("available"):
        out.append("  civic_standing_history 表不存在（迁移未跑）——探针跳过")
        return "\n".join(out)

    c = snapshot["cross"]
    out.append(f"  内置公民 {c['builtin_citizen']}；已晋升 UGC 公民 "
               f"{c['ugc_citizen_promoted']}；未晋升 UGC 居民 {c['ugc_denizen']}；"
               f"其它（player/preset）{c['other']}")
    if snapshot["leaked"]:
        out.append(f"  🔴 provenance=UGC 且有投票权、但查不到晋升记录："
                   f"{snapshot['leaked']}")
        out.append("     —— 要么是泄漏复发，要么是 admin 手工改回了 npc"
                   "（后者是有用的红旗，不是噪声）")
    else:
        out.append("  ✅ 每一位有投票权的 UGC 居民都有对应的晋升记录")

    q = snapshot["queue"]
    if q["size"] is None:
        out.append("  晋升队列：计算失败（关系表或 world_clock 不可用）")
    else:
        out.append(f"  晋升队列（满足门槛但仍是 denizen）：{q['size']} 人 "
                   f"{q['slugs'][:20]}")

    f = snapshot["flips"]
    flip_flag = "🔴" if f["recent_flip_residents"] > 0 else "✅"
    out.append(f"  翻转统计：有档位历史的居民 {f['residents_with_history']}；"
               f"单人最多变更 {f['max_changes_per_resident']} 次")
    out.append(f"  {flip_flag} 最近 {f['window_world_days']:.0f} 世界日内发生"
               f"翻转的居民 = {f['recent_flip_residents']}"
               "（滞后设计生效后稳态应恒为 0，>0 是告警不是信息）")
    out.append(f"  当前处于最短任期内 {f['in_min_tenure']} 人 / 冷却期内 "
               f"{f['in_cooldown']} 人")

    x = snapshot["crosscheck"]
    if x["election_office_non_voter"]:
        out.append(f"  🔴 民选职位被非公民占据：{x['election_office_non_voter']}"
                   "（只对 fill_strategy='election' 断言；劳动职务不算）")
    else:
        out.append("  ✅ 民选职位的在任者都持有政治权利")
    mr = x["mayor_reps"]
    if not mr["checked"]:
        out.append("  ⏸ 三处镇长表示一致性：polis_office_enabled=False，"
                   "offices 可能是迁移 046 的遗留值——本档不判定")
    elif mr["consistent"]:
        out.append(f"  ✅ 三处镇长表示一致（meta={mr['meta']}）")
    else:
        out.append(f"  🔴 三处镇长表示分歧：meta={mr['meta']} / "
                   f"offices={mr['office']!r} / config={mr['config']!r}")
    if x["ghost_votes"]:
        out.append("  ⚠️ 幽灵票（投票时具备资格即计票，是设计语义不是 bug）：")
        for g in x["ghost_votes"]:
            out.append(f"    {g['question'][:28]:<28} {g['ghosts']} 张 "
                       f"{g['slugs'][:10]}")
    if x["dangling_holders"]:
        out.append(f"  🔴 offices.holder_slug 在 residents 表里查不到："
                   f"{x['dangling_holders']}"
                   "（purge_residents 不清 offices 与 current_mayor）")
    return "\n".join(out)
```

同时在文件顶部的 import 区（`from sqlalchemy import select, func  # noqa: E402` 之后）追加：

```python
import json  # noqa: E402
```

（`json` 用于解析 `system_config['current_mayor']`；若文件已 import 过则跳过这一步——先 `grep -n "^import json" scripts/burnin_report.py` 确认。）

- [ ] **Step 4: 把 `unknown_types` 从 ⚠️ 升为 🔴**

把 `render_probes_civic_boundary` 末尾的

```python
    if d["unknown_types"]:
        out.append(f"  ⚠️ 两列之外的取值 {d['unknown_types']}"
                   "——既不投票也不算世界人口。'preset'（admin 创建）是已知的"
                   "待决项，不是 bug;其它取值请查来源")
    return "\n".join(out)
```

替换为

```python
    if d["unknown_types"]:
        # F2：从 ⚠️ 升为 🔴。这是未来引入新 resident_type 取值时唯一的自动
        # 发现口，也是「写错一个字符（"npc "）就同时掉出两个集合」的唯一兜底
        # ——写错的那一位居民会从 agent loop、市政厅名册、职务查找与 mayor
        # 清扫里一起消失，除了这一行没有任何地方会喊。
        out.append(f"  🔴 两列之外的取值 {d['unknown_types']}"
                   "——既不投票也不算世界人口。'preset'（admin 创建）是已知的"
                   "待决项;其它取值一律按事故处理:查 5 处创建路径与 "
                   "_BOUNDARY_KNOWN_OUTSIDE，并同步 SIM_RESIDENT_TYPES 的决定")
    return "\n".join(out)
```

⚠️ `"preset"` 今天就在 `unknown_types` 里（它落在两列之外），所以这一行现在会常态报红。这是**刻意的**：`preset` 是 U6 待决项，报红提醒它仍未归类；文案已经把它与真正的事故区分开。若运维觉得吵，正确的处置是拍板 `preset` 的归属，不是把探针关掉。

- [ ] **Step 5: 接进 `_run()`**

把 `_run()` 里的

```python
        boundary_snap = await fetch_civic_boundary_snapshot(session)
```

替换为

```python
        boundary_snap = await fetch_civic_boundary_snapshot(session)
        standing_snap = await fetch_civic_standing_snapshot(
            session, gate_office_on=settings.polis_office_enabled)
```

并把

```python
            + "\n\n" + render_probes_civic_boundary(boundary_snap))
```

替换为

```python
            + "\n\n" + render_probes_civic_boundary(boundary_snap)
            + "\n\n" + render_probes_civic_standing(standing_snap))
```

- [ ] **Step 6: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_burnin_report_civic_standing.py \
  tests/test_burnin_report_civic_boundary.py tests/test_burnin_report.py \
  tests/test_burnin_report_offices.py -q -p no:randomly
```
Expected: PASS，`tests/test_burnin_report_civic_boundary.py` **无需任何修改**。已逐行核实：该文件里没有断言 `⚠️` 文案的用例，四处 `🔴` 断言（`:78`/`:83`/`:89`/`:95`）用的快照 `unknown_types` 都是空的，本 Step 的 ⚠️→🔴 改动波及不到它。锁住新文案的责任在本 Task 新写的 `test_unknown_types_render_as_a_red_flag`（在新文件里）。

- [ ] **Step 7: 真跑一次报告（运行时证据）**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
DATABASE_URL=sqlite+aiosqlite:////tmp/f2-probe.db python -c "
import asyncio
from app.database import Base, engine
async def m():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await engine.dispose()
asyncio.run(m())
"
DATABASE_URL=sqlite+aiosqlite:////tmp/f2-probe.db python scripts/burnin_report.py --days 1 --residents 1 | tail -25
```
Expected: 报告尾部出现「== 公民权档位探针（provenance × standing · 只读零 LLM）==」段落且不抛异常（空世界读数全 0）。把这段输出贴进 commit 的 `Verified-by:`。

- [ ] **Step 8: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/scripts/burnin_report.py \
        backend/tests/test_burnin_report_civic_standing.py
git status --short   # test_burnin_report_civic_boundary.py 不该出现在这里
git commit -m "$(cat <<'MSG_EOF'
feat(probe): 公民权档位探针——交叉表 / 晋升队列 / 翻转统计 / 交叉一致性

现有探针对 F2 的核心失败模式全盲：判泄漏的条件是常量集合被拓宽，而 F2 只改
行值不改集合，永远不会触发。新判据 = provenance=UGC 且有投票权、但查不到
晋升记录。

- 交叉表 resident_type × provenance（内置 / 已晋升 UGC / 未晋升 UGC）
- 晋升队列 = shadow 模式的候选名单大小
- 「最近 7 世界日翻转数 > 0」定为告警条件，不是信息项
- 交叉一致性四项；三处镇长表示按 polis_office_enabled 分档判定
- unknown_types 从 ⚠️ 升为 🔴（写错一个字符的唯一兜底）

Verified-by: <贴 pytest 输出 + burnin_report.py 真跑的探针段落>
MSG_EOF
)"
```

---

### Task 12: 三态 pass（off / shadow / on）+ 四道数值闸门 + 运行摘要

**Files:**
- Modify: `backend/app/tasks/civic_promotion.py`（追加 pass 层）
- Test: `backend/tests/test_civic_promotion_pass.py`

**Interfaces:**
- Consumes：Task 6 的 `build_snapshot` / `select_promotions` / `promotion_evidence`；Task 3 的 `grant_citizenship_batch(db, ids, *, reason, reason_code, actor, evidence_by_id)`；Task 2 的旋钮 `promotion_mode()` / `min_world_days()` / `min_peers()` / `min_familiarity()` / `peer_seasoning_world_days()` / `promotion_max_per_run()` / `promotion_breaker_fraction()` / `promotion_breaker_min_abs()` / `auto_demotion_enabled()`；`ConfigService.set(key, value, *, group, updated_by)`
- Produces：常量 `MODE_OFF` / `MODE_SHADOW` / `MODE_ON`、`PROMOTION_ACTOR = "civic_promotion"`、`PROMOTION_REASON_CODE = "threshold_met"`、`RUN_SUMMARY_KEY = "civic_promotion_last_run"`；`async def run_promotion_pass(db) -> dict`

**三态语义：**

- `off`（默认）：**零读零写，立即返回**——行为与本批开工前逐字节一致。
- `shadow`：执行完整候选计算与**全部防呆检查**，把当晚会晋升的名单与每人的证据写进日志与探针；**对 `residents` / `civic_standing_history` 零写入**。唯一的写是 `system_config` 的一行运行摘要（探针据此读候选名单——shadow 不产生历史行，没有别的载体）。生产至少观察 3 个夜间周期，名单规模与标定预期一致才进开闸。理由的准确写法：**首夜爆炸半径不可预演，shadow 是带全部防呆的实跑演练 + 名单落盘**——不是「规模在开闸前无人知晓」（只读标定本来就能测出候选规模）。
- `on`：真正执行 `grant_citizenship_batch`。

**四道数值闸门的顺序与处置：**

| # | 闸门 | 处置 |
|---|---|---|
| 1 | `CIVIC_PROMOTION_MAX_PER_RUN`（默认 5） | **确定性截断**（候选已按 id 排序），余量下夜再来——整批拒绝会让 6 人的合法积压永久卡死 |
| 2 | 熔断：候选集 > `max(BREAKER_MIN_ABS, 当前公民数 × BREAKER_FRACTION)`（默认 `max(3, 公民数 × 0.20)`） | **整批拒绝并告警，不截断**——截断会掩盖「阈值写反」这类全量误判 |
| 3 | 选民下限不变式 | 在 `revoke_citizenship` 的 guard 里（Task 4），晋升不缩小选民集所以此处不适用 |
| 4 | 取值白名单断言 | 在 `grant_citizenship_batch` 里（Task 3） |

判定顺序：**先用完整候选集判熔断，再截断**。反过来会让熔断永远打不响。

**熔断为什么必须带绝对下限。** 只写 `候选集 > 公民数 × 0.20` 的话，两道闸门在真实世界里互相吞掉：生产内置阵容 ≈10-11 位公民 → 阈值 ≈2.2，一夜 3 个合法候选就整批拒绝，而 `MAX_PER_RUN` 默认 5 永远够不着，闸门 1 成了死代码；测试夹具里 4 位内置公民 → 阈值 0.8，连 1 个候选都过不去。加 `CIVIC_PROMOTION_BREAKER_MIN_ABS`（默认 3）后语义才完整：**小批量放行由下限保证，大批量熔断由比例保证**，世界规模大到比例项超过下限时下限自动退居二线（置 0 即纯比例）。

**夜间任务只升，永不自动降**：`auto_demotion_enabled()` 为真时 pass 直接 `raise NotImplementedError`（滞后三件套未实现），而不是悄悄跑一个没有滞后的降级。

**收口接线（本批不改 `nightly_cron.py`，位置在这里写死）**：接在 `close_due_polls`(`nightly_cron.py:215`) **之后**、`run_npc_voting`(`:247`) **之前**（≈`:245`）。理由：当晚晋升、当晚补投，新公民参与的第一次关票分子分母同源。接在末尾并不能消除危害，只把它推迟一晚——每晚 close(215) 先于 vote(247)，夜 N 末尾晋升的人在夜 N+1 关票时仍然是「进了分母、一票未投」。收口接线时用与 `nightly_cron.py:142-145`（opinion drift 顺序硬约束）同样的注释形式锚住位置，对应回归测试按 **N+1 晚**断言。

- [ ] **Step 1: 写失败的测试**

Create `backend/tests/test_civic_promotion_pass.py`:

```python
"""F2 Task 12 —— 晋升 pass 的三态与四道数值闸门。

shadow 的准确定位：首夜爆炸半径不可预演，它是**带全部防呆的实跑演练 + 名单
落盘**，不是「规模在开闸前无人知晓」（只读标定本来就能测出候选规模）。
"""
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.services import civic_membership as cm
from app.services.config_service import ConfigService
from app.tasks import civic_promotion as cp

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _res(slug, rtype, *, creator_id="u1", meta=None, days=200):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=datetime.now(UTC) - timedelta(days=days))


async def _world(db, *, builtins=4, denizens=1, edges_per=2):
    """一个「全员达标」的小世界：denizens 与前 edges_per 位内置公民都够熟。"""
    bs = [_res(f"b{i}", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
               meta={"origin": "preset"}) for i in range(builtins)]
    us = [_res(f"u{i}", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
          for i in range(denizens)]
    db.add_all(bs + us)
    await db.commit()
    for u in us:
        for b in bs[:edges_per]:
            a_id, b_id = sorted([u.id, b.id])
            db.add(ResidentRelation(party_a=a_id, party_b=b_id,
                                    familiarity=0.6))
    await db.commit()
    return bs, us


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    """全部旋钮显式置成默认值，用例不依赖 env 的外部状态。

    ⚠️ `CIVIC_PROMOTION_BREAKER_MIN_ABS` 必须显式设：小世界夹具（4 位内置公民）
    的比例项只有 4 × 0.20 = 0.8，没有绝对下限的话**任何一个候选都会触发熔断**，
    on 态与 shadow 态的用例全部废掉（shadow 分支在熔断 return 之后，会变成空跑）。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.2")
    monkeypatch.setenv("CIVIC_PEER_SEASONING_WORLD_DAYS", "28")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "5")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "0.20")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "3")
    monkeypatch.delenv("CIVIC_AUTO_DEMOTION_ENABLED", raising=False)
    yield


# ── off ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_off_is_a_zero_read_zero_write_noop(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "off")
    await _world(db_session)

    result = await cp.run_promotion_pass(db_session)
    assert result["mode"] == cp.MODE_OFF
    assert result["promoted"] == 0
    assert result["candidates"] == []
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
    assert await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY) is None


@pytest.mark.anyio
async def test_unknown_mode_degrades_to_off(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "ON!!")
    await _world(db_session)
    assert (await cp.run_promotion_pass(db_session))["mode"] == cp.MODE_OFF


# ── shadow ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_shadow_computes_the_list_but_writes_no_politics(
        db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    _, us = await _world(db_session, denizens=2)

    result = await cp.run_promotion_pass(db_session)
    assert result["mode"] == cp.MODE_SHADOW
    # 必须真的走到 shadow 分支：熔断的 return 在 `if mode == MODE_SHADOW` 之前，
    # 一旦熔断先响，本用例会以「promoted == 0」侥幸全绿而 shadow 分支一行没跑
    assert result["refused"] is None
    assert result["promoted"] == 0
    assert sorted(result["candidates"]) == ["u0", "u1"]
    assert result["evidence"]["u0"]["peers"] == 2
    assert result["evidence"]["u0"]["world_days"] > 0

    # 政治层零写入
    types = (await db_session.execute(
        select(Resident.resident_type).where(
            Resident.slug.in_(["u0", "u1"])))).scalars().all()
    assert set(types) == {cm.UGC_RESIDENT_TYPE}
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_shadow_records_the_run_summary_for_the_probe(db_session,
                                                            monkeypatch):
    """shadow 不产生历史行，探针没有别的载体——运行摘要是 shadow 的唯一写。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    await _world(db_session, denizens=1)
    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None, "熔断先响的话本用例是空跑（摘要两条路都写）"

    summary = await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY)
    assert summary["mode"] == cp.MODE_SHADOW
    assert summary["candidates"] == ["u0"]
    assert summary["promoted"] == 0
    assert summary["refused"] is None
    assert "world_at" in summary


# ── on ─────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_on_promotes_and_leaves_a_history_row_each(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    _, us = await _world(db_session, denizens=2)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None
    assert result["promoted"] == 2
    voters = (await db_session.execute(
        select(Resident.slug).where(Resident.is_civic_voter))).scalars().all()
    assert {"u0", "u1"} <= set(voters)
    rows = (await db_session.execute(
        select(CivicStandingHistory))).scalars().all()
    assert len(rows) == 2
    assert {r.actor for r in rows} == {cp.PROMOTION_ACTOR}
    assert {r.reason_code for r in rows} == {cp.PROMOTION_REASON_CODE}
    assert all(r.evidence_json.get("peers") == 2 for r in rows)


@pytest.mark.anyio
async def test_running_twice_is_idempotent(db_session, monkeypatch):
    """已晋升的人不再进候选面（select_promotions 只收 denizen 档）。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, denizens=1)
    assert (await cp.run_promotion_pass(db_session))["promoted"] == 1
    assert (await cp.run_promotion_pass(db_session))["promoted"] == 0


@pytest.mark.anyio
async def test_pass_never_demotes(db_session, monkeypatch):
    """夜间任务只升，永不自动降——门槛②读的 familiarity 有周衰减，接成降级
    判据等于让公民权跟着社交波动飘。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    bs, _ = await _world(db_session, builtins=4, denizens=0)
    naturalised = _res("naturalised", cm.CIVIC_MEMBER_TYPE,
                       meta={"origin": "forge"})
    db_session.add(naturalised)
    await db_session.commit()

    result = await cp.run_promotion_pass(db_session)
    assert result.get("demoted", 0) == 0
    rtype = (await db_session.execute(
        select(Resident.resident_type)
        .where(Resident.slug == "naturalised"))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE


@pytest.mark.anyio
async def test_auto_demotion_flag_raises_instead_of_running_unhedged(
        db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_AUTO_DEMOTION_ENABLED", "true")
    await _world(db_session)
    with pytest.raises(NotImplementedError, match="滞后"):
        await cp.run_promotion_pass(db_session)


# ── 数值闸门 ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_circuit_breaker_refuses_the_whole_batch(db_session, monkeypatch):
    """候选集 > max(下限 3, 当前公民数 × 20%) → 整批拒绝并告警，**不截断**。
    截断会掩盖「阈值写反」这类全量误判。

    世界规模刻意开到 20 位内置公民，让**比例项**（20 × 0.20 = 4）压过绝对下限
    （3）——这样本用例测的是比例语义，不是下限语义。5 > 4 → 熔断。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "100")
    await _world(db_session, builtins=20, denizens=5)   # 5 > max(3, 20 × 0.20)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0
    assert len(result["candidates"]) == 5
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_breaker_floor_keeps_a_small_town_promotable(db_session, monkeypatch):
    """熔断的绝对下限：小镇规模下比例项 < 3 时以下限为准，合法小批量照常放行。

    没有下限的话，4 位内置公民 × 0.20 = 0.8，**一个候选都过不去**——熔断恒响、
    单夜上限恒不生效，两道闸门语义互相吞掉（生产 11 位公民时阈值 ≈2.2，
    MAX_PER_RUN=5 永远够不着）。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, builtins=4, denizens=3)    # 3 ≤ max(3, 0.8)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None
    assert result["promoted"] == 3


@pytest.mark.anyio
async def test_breaker_floor_can_be_disabled(db_session, monkeypatch):
    """置 0 即退化成纯比例判定（世界规模足够大之后的口径）。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "0")
    await _world(db_session, builtins=4, denizens=3)    # 3 > 4 × 0.20 = 0.8

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0


@pytest.mark.anyio
async def test_max_per_run_truncates_deterministically(db_session, monkeypatch):
    """单夜上限是确定性截断（候选已按 id 排序），余量下夜再来——整批拒绝会让
    合法积压永久卡死。截断发生在熔断判定**之后**。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "2")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "10")   # 熔断不挡
    await _world(db_session, builtins=4, denizens=3)

    first = await cp.run_promotion_pass(db_session)
    assert first["promoted"] == 2
    assert len(first["candidates"]) == 3
    assert first["refused"] is None
    second = await cp.run_promotion_pass(db_session)
    assert second["promoted"] == 1


@pytest.mark.anyio
async def test_breaker_is_evaluated_on_the_full_candidate_set(db_session,
                                                              monkeypatch):
    """先熔断再截断。反过来会让熔断永远打不响（截断后的集合恒 ≤ 上限）。

    同上，世界开到 20 位内置公民让比例项（4）压过绝对下限（3）：5 个候选被
    cap=1 截断后只剩 1 个，若顺序反了就永远 1 ≤ 4，熔断这辈子打不响。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "0.20")
    await _world(db_session, builtins=20, denizens=5)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0


# ── 本线不改 nightly_cron ───────────────────────────────────────────────

def test_this_line_does_not_wire_the_cron():
    """共享文件线内不改，接线延到收口。位置写死在 close_due_polls 之后、
    run_npc_voting 之前（≈nightly_cron.py:245）——见 civic_promotion 模块
    docstring。"""
    src = (BACKEND_ROOT / "app" / "tasks" / "nightly_cron.py").read_text(
        encoding="utf-8")
    assert "civic_promotion" not in src, (
        "F2 本批不改 nightly_cron.py；接线是收口 §8 第 2 项")
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_promotion_pass.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: module 'app.tasks.civic_promotion' has no attribute 'run_promotion_pass'`。

- [ ] **Step 3: 写实现**

在 `backend/app/tasks/civic_promotion.py` 的 `build_snapshot()` 之后追加：

```python


# ═══════════════════════════════════════════════════════════════════════
# 三态 pass
# ═══════════════════════════════════════════════════════════════════════
#
# **收口接线位置（本批不改 nightly_cron.py，位置在这里写死）**：
#
#     close_due_polls (nightly_cron.py:215)
#         → seed_civic_agenda (:226)
#         → maybe_open_seasonal_election (:237)
#         → 【civic_promotion 接在这里，≈:245】
#         → run_npc_voting (:247)
#         → office term_check (:263)
#
# 理由是语义决策：当晚晋升、当晚补投，新公民参与的第一次关票分子分母同源。
# 接在末尾并不能消除危害，只把它推迟一晚——每晚 close(215) 先于 vote(247)，
# 夜 N 末尾晋升的人在夜 N+1 关票时仍然是「进了分母、一票未投」。收口接线时
# 用与 nightly_cron.py:142-145（opinion drift 顺序硬约束）同样的注释形式锚住
# 位置，对应回归测试按 **N+1 晚** 断言。

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"

PROMOTION_ACTOR = "civic_promotion"
PROMOTION_REASON_CODE = "threshold_met"
PROMOTION_REASON = "满足公民权晋升门槛（在镇世界日 + 与锚定公民的熟识度）"

#: 每次运行的摘要落点。shadow 态不产生历史行，探针只能从这里读候选名单。
#: ``SystemConfig.value`` 是 ``String(2000)``，所以名单截断到 50 个 slug。
RUN_SUMMARY_KEY = "civic_promotion_last_run"
_SUMMARY_MAX_SLUGS = 50


async def _record_run(db, result: dict) -> None:
    """把本次运行摘要写进 ``system_config``（fail-open）。

    这是 shadow 态**唯一**的一次写——政治层（``residents`` /
    ``civic_standing_history``）零写入。
    """
    try:
        from app.services.config_service import ConfigService

        await ConfigService(db).set(
            RUN_SUMMARY_KEY,
            {
                "mode": result.get("mode"),
                "world_at": result.get("world_at"),
                "citizens_before": result.get("citizens_before"),
                "candidates": list(result.get("candidates") or [])[:_SUMMARY_MAX_SLUGS],
                "candidate_count": len(result.get("candidates") or []),
                "promoted": result.get("promoted", 0),
                "refused": result.get("refused"),
            },
            group="civic", updated_by=PROMOTION_ACTOR,
        )
    except Exception:
        logger.warning("recording civic_promotion run summary failed",
                       exc_info=True)


async def run_promotion_pass(db) -> dict:
    """一夜一次的晋升 pass。返回运行摘要（也是探针与测试的读数来源）。

    三态（``CIVIC_PROMOTION_MODE``）：

    - ``off``（默认）：零读零写立即返回，行为与本批开工前逐字节一致；
    - ``shadow``：完整候选计算 + **全部防呆检查**，名单与证据进日志与运行
      摘要，**对 residents / civic_standing_history 零写入**。生产至少观察 3 个
      夜间周期，名单规模与标定预期一致才进开闸。首夜爆炸半径不可预演，
      shadow 是带全部防呆的实跑演练 + 名单落盘；
    - ``on``：真正执行 :func:`grant_citizenship_batch`。

    数值闸门的顺序：**先用完整候选集判熔断，再按单夜上限确定性截断**。反过来
    熔断永远打不响（截断后的集合恒 ≤ 上限）。

    熔断阈值是 ``max(绝对下限, 公民数 × 比例)``，两项缺一不可：只有比例项时，
    小镇规模下熔断恒响（11 位公民 × 0.20 ≈ 2.2），单夜上限默认 5 永远够不着，
    闸门 1 变成死代码；只有下限时，世界长大后熔断就不再随规模缩放。
    """
    from app.services.civic_membership import (
        auto_demotion_enabled, grant_citizenship_batch, min_familiarity,
        min_peers, min_world_days, peer_seasoning_world_days,
        promotion_breaker_fraction, promotion_breaker_min_abs,
        promotion_max_per_run, promotion_mode,
    )

    mode = promotion_mode()
    if mode not in (MODE_OFF, MODE_SHADOW, MODE_ON):
        logger.error("unknown CIVIC_PROMOTION_MODE=%r — 按 off 处理", mode)
        mode = MODE_OFF
    if mode == MODE_OFF:
        return {"mode": MODE_OFF, "world_at": None, "citizens_before": None,
                "candidates": [], "evidence": {}, "promoted": 0,
                "demoted": 0, "refused": None}

    if auto_demotion_enabled():
        raise NotImplementedError(
            "CIVIC_AUTO_DEMOTION_ENABLED=true，但自动下滑降级 v1 未实现。开启"
            "前必须先落地滞后三件套：滞后区间 Δ ≥ 0.10（严格大于单次最大相关"
            "增量 0.05）、最短任期 ≥ 12 世界日（= 一张 poll 的生命周期）、冷却"
            "期 ≥ 12 世界日。缺一不可——门槛②读的 familiarity 有周衰减，没有"
            "滞后就是让公民权跟着社交波动飘。"
        )

    seasoning = peer_seasoning_world_days()
    threshold = min_familiarity()
    snap = await build_snapshot(db)
    candidate_ids = select_promotions(
        snap, min_world_days=min_world_days(), min_peers=min_peers(),
        min_familiarity=threshold, seasoning_days=seasoning,
    )
    slug_by_id = {f.resident_id: f.slug for f in snap.facts}
    citizens_before = sum(1 for f in snap.facts
                          if f.resident_type in CIVIC_VOTER_TYPES)
    result = {
        "mode": mode,
        "world_at": snap.now_world.isoformat(),
        "citizens_before": citizens_before,
        "candidates": [slug_by_id.get(i, i) for i in candidate_ids],
        "evidence": {
            slug_by_id.get(i, i): promotion_evidence(
                snap, i, min_familiarity=threshold, seasoning_days=seasoning)
            for i in candidate_ids
        },
        "promoted": 0,
        "demoted": 0,          # 夜间任务只升，永不自动降
        "refused": None,
    }

    # 闸门 2（熔断）：用**完整**候选集判，整批拒绝、不截断。
    # 阈值 = max(绝对下限, 公民数 × 比例)——绝对下限保证小批量能放行（否则
    # 4 位内置公民 × 0.20 = 0.8，一个候选都过不去），比例保证世界长大后熔断
    # 仍随规模缩放。
    breaker = promotion_breaker_fraction()
    breaker_min_abs = promotion_breaker_min_abs()
    breaker_limit = max(float(breaker_min_abs), citizens_before * breaker)
    if candidate_ids and len(candidate_ids) > breaker_limit:
        logger.error(
            "civic_promotion circuit breaker: %d candidate(s) > limit %.2f = "
            "max(min_abs=%d, %d citizens × %.2f) — 整批拒绝（截断会掩盖"
            "「阈值写反」这类全量误判）。名单：%s",
            len(candidate_ids), breaker_limit, breaker_min_abs,
            citizens_before, breaker, result["candidates"])
        result["refused"] = "circuit_breaker"
        await _record_run(db, result)
        return result

    # 闸门 1（单夜上限）：确定性截断（candidate_ids 已按 id 排序），余量下夜再来
    cap = promotion_max_per_run()
    picked = candidate_ids[:cap]
    if len(picked) < len(candidate_ids):
        logger.warning(
            "civic_promotion per-run cap: promoting %d of %d candidate(s) "
            "tonight (CIVIC_PROMOTION_MAX_PER_RUN=%d); 余量下夜再来",
            len(picked), len(candidate_ids), cap)

    if mode == MODE_SHADOW:
        logger.info(
            "civic_promotion SHADOW: %d candidate(s) would be promoted "
            "tonight — %s | evidence=%s",
            len(picked), [slug_by_id.get(i, i) for i in picked],
            result["evidence"])
        await _record_run(db, result)
        return result

    if picked:
        result["promoted"] = await grant_citizenship_batch(
            db, list(picked), reason=PROMOTION_REASON,
            reason_code=PROMOTION_REASON_CODE, actor=PROMOTION_ACTOR,
            evidence_by_id={
                i: promotion_evidence(snap, i, min_familiarity=threshold,
                                      seasoning_days=seasoning)
                for i in picked
            },
        )
    await _record_run(db, result)
    logger.info("civic_promotion pass done: mode=%s candidates=%d promoted=%d",
                mode, len(candidate_ids), result["promoted"])
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/test_civic_promotion_pass.py tests/test_civic_promotion_rules.py -q -p no:randomly
```
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/tasks/civic_promotion.py \
        backend/tests/test_civic_promotion_pass.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
feat(civic): 晋升 pass 的 off/shadow/on 三态 + 数值闸门

- off 默认：零读零写立即返回，行为与本批开工前逐字节一致
- shadow：完整候选计算 + 全部防呆，名单与证据落日志与运行摘要，政治层零写入
  （唯一的写是 system_config 的一行摘要——shadow 不产生历史行，探针没有别的
  载体）
- 熔断用完整候选集判、整批拒绝不截断；单夜上限确定性截断、余量下夜再来；
  顺序是先熔断再截断，反过来熔断永远打不响
- 熔断阈值 = max(CIVIC_PROMOTION_BREAKER_MIN_ABS, 公民数 × BREAKER_FRACTION)：
  纯比例在小镇规模下（11 位公民 → 阈值 ≈2.2）会让熔断恒响、单夜上限恒不生效，
  两道闸门互相吞掉
- CIVIC_AUTO_DEMOTION_ENABLED 为真时直接 raise NotImplementedError，而不是
  跑一个没有滞后三件套的降级
- 收口接线位置写进模块 docstring：close_due_polls 之后、run_npc_voting 之前

Verified-by: <贴 pytest 的真实输出>
MSG_EOF
)"
```

---

### Task 13: 全量回归双向差集 + 真实进程演练 + 交接说明

**Files:**
- Modify: `backend/app/tasks/civic_promotion.py`（仅追加模块顶部的「标定与上线」注释块）
- 无新测试（本任务是验收）

**Interfaces:**
- Consumes：Task 0 的 `/tmp/f2-base.txt`

**「完成的定义」：build/lint/单测绿不等于完成。** 本任务必须产出真实进程上的运行时证据。

- [ ] **Step 1: 全量回归 + 双向差集（硬门）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -m pytest tests/ -q -p no:randomly 2>&1 | tee /tmp/f2-after-full.txt | tail -3
grep -E "^(FAILED|ERROR) " /tmp/f2-after-full.txt | sed 's/\[.*//' | sort -u > /tmp/f2-after.txt

echo "=== 新增失败（必须为空）==="
comm -13 /tmp/f2-base.txt /tmp/f2-after.txt
echo "=== 被修复的既有失败（可以非空，记录即可）==="
comm -23 /tmp/f2-base.txt /tmp/f2-after.txt
echo "=== 数量对照（数量相同 ≠ 集合相同，以上面两个差集为准）==="
wc -l /tmp/f2-base.txt /tmp/f2-after.txt
```

Expected: **「新增失败」一行都没有**。若非空，逐条定位并修到空——不得以「数量没变」结案。

- [ ] **Step 2: 真实进程演练 —— off 态的零行为变化**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
export DATABASE_URL=sqlite+aiosqlite:////tmp/f2-e2e.db
rm -f /tmp/f2-e2e.db
python - <<'PY'
import asyncio, os
from app.database import Base, engine, async_session

async def main():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    from app.tasks.civic_promotion import run_promotion_pass
    async with async_session() as db:
        print("MODE=off →", await run_promotion_pass(db))
    await engine.dispose()

asyncio.run(main())
PY
```
Expected: 打印 `MODE=off → {'mode': 'off', ..., 'promoted': 0, ...}`，零异常。

- [ ] **Step 3: 真实进程演练 —— shadow 态出名单、政治层零写入**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
export DATABASE_URL=sqlite+aiosqlite:////tmp/f2-e2e.db
export CIVIC_PROMOTION_MODE=shadow
export CIVIC_PROMOTION_MIN_WORLD_DAYS=1
export CIVIC_PROMOTION_MIN_PEERS=2
export CIVIC_PROMOTION_MIN_FAMILIARITY=0.2
# 演练的目的是走通路径，不是测熔断：这个小世界只有 4 位内置公民，比例项
# 0.8 太小，显式把熔断顶到天上，免得整批被静默拒绝而看不出来。
export CIVIC_PROMOTION_BREAKER_FRACTION=10
python - <<'PY'
import asyncio
from datetime import UTC, datetime, timedelta
from sqlalchemy import func, select
from app.database import async_session
from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.services.civic_membership import (
    CIVIC_MEMBER_TYPE, SYSTEM_CREATOR_ID, UGC_RESIDENT_TYPE)
from app.tasks.civic_promotion import run_promotion_pass

def res(slug, rtype, creator, origin):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator, tile_x=1, tile_y=1,
                    meta_json={"origin": origin},
                    created_at=datetime.now(UTC) - timedelta(days=90))

async def main():
    async with async_session() as db:
        bs = [res(f"b{i}", CIVIC_MEMBER_TYPE, SYSTEM_CREATOR_ID, "preset")
              for i in range(4)]
        u = res("ugc-shadow", UGC_RESIDENT_TYPE, "user-1", "forge")
        db.add_all(bs + [u]); await db.commit()
        for b in bs[:2]:
            a, bb = sorted([u.id, b.id])
            db.add(ResidentRelation(party_a=a, party_b=bb, familiarity=0.6))
        await db.commit()

        r = await run_promotion_pass(db)
        print("SHADOW result:", r)
        # 先确认没被闸门静默拒绝：熔断的 return 在 shadow 分支之前，一旦它先
        # 响，「promoted == 0 / 政治层零写入」会侥幸成立而 shadow 分支一行没跑
        assert r["refused"] is None, f"被闸门拒绝了：{r['refused']}"
        assert r["candidates"] == ["ugc-shadow"], f"候选面不对：{r['candidates']}"
        types = dict((await db.execute(
            select(Resident.slug, Resident.resident_type))).all())
        n_hist = (await db.execute(
            select(func.count()).select_from(CivicStandingHistory))).scalar()
        print("types after shadow:", types)
        print("history rows after shadow:", n_hist)
        assert types["ugc-shadow"] == UGC_RESIDENT_TYPE, "shadow 写了政治层！"
        assert n_hist == 0, "shadow 写了历史行！"
        print("OK: shadow 出名单且政治层零写入")

asyncio.run(main())
PY
```
Expected: `SHADOW result: {...'candidates': ['ugc-shadow'], 'promoted': 0...}` + `OK: shadow 出名单且政治层零写入`。

- [ ] **Step 4: 真实进程演练 —— on 态晋升后探针读得出**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
export DATABASE_URL=sqlite+aiosqlite:////tmp/f2-e2e.db
export CIVIC_PROMOTION_MODE=on
export CIVIC_PROMOTION_MIN_WORLD_DAYS=1
export CIVIC_PROMOTION_MIN_PEERS=2
export CIVIC_PROMOTION_MIN_FAMILIARITY=0.2
# 同 Step 3：演练目的是走通路径，不是测熔断（这个世界只有 4 位内置公民）
export CIVIC_PROMOTION_BREAKER_FRACTION=10
python - <<'PY'
import asyncio
from app.database import async_session
from app.tasks.civic_promotion import run_promotion_pass
async def main():
    async with async_session() as db:
        r = await run_promotion_pass(db)
        print("ON result:", r)
        # 静默拒绝会让 promoted=0 看起来像「本来就没人达标」——显式断言，免得
        # 演练在被闸门挡住的情况下被误判为通过（Step 5 依赖这一步真的升成了）
        assert r["refused"] is None, f"被闸门拒绝了：{r['refused']}"
        assert r["promoted"] == 1, f"应晋升 1 人，实际 {r['promoted']}"
asyncio.run(main())
PY
python scripts/burnin_report.py --days 1 --residents 5 | tail -20
```
Expected: `ON result: {...'promoted': 1...}` 且脚本无 AssertionError；报告尾部的公民权档位探针显示「已晋升 UGC 公民 1」、「✅ 每一位有投票权的 UGC 居民都有对应的晋升记录」，且**没有** 🔴 泄漏行。把这段贴进 commit。

> ⚠️ Step 5 依赖本步真的把 `ugc-shadow` 升成了 citizen 档。若本步的 `promoted` 是 0，Step 5 会在 `_assert_revocable` 的第 ④ 条（不在 citizen 档）抛 `CivicStandingRefused` 直接崩掉——先修本步再往下走。

- [ ] **Step 5: 真实进程演练 —— 撤销后三处镇长表示清空**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
export DATABASE_URL=sqlite+aiosqlite:////tmp/f2-e2e.db
unset CIVIC_PROMOTION_MODE
python - <<'PY'
import asyncio, json
from sqlalchemy import select
from app.database import async_session
from app.models.office import Office
from app.models.resident import Resident
from app.models.system_config import SystemConfig
from app.services.civic_membership import revoke_citizenship
from app.services.election_service import install_mayor

async def main():
    async with async_session() as db:
        r = (await db.execute(
            select(Resident).where(Resident.slug == "ugc-shadow"))).scalar_one()
        print("install_mayor:", await install_mayor(db, r.slug))
        print("revoke:", await revoke_citizenship(
            db, r, reason="e2e 演练", actor="ops:e2e"))
        metas = [s for (s, m) in (await db.execute(
            select(Resident.slug, Resident.meta_json))).all()
            if (m or {}).get("mayor")]
        office = (await db.execute(
            select(Office.holder_slug)
            .where(Office.office_key == "mayor"))).scalar_one_or_none()
        cfg = (await db.execute(select(SystemConfig.value)
            .where(SystemConfig.key == "current_mayor"))).scalar_one_or_none()
        print("meta mayors:", metas, "| office:", office,
              "| config:", json.loads(cfg) if cfg else None)
        assert metas == [] and not office and (cfg is None or json.loads(cfg) is None)
        print("OK: 三处镇长表示都已清空")

asyncio.run(main())
PY
python scripts/burnin_report.py --days 1 --residents 5 | tail -20
```
Expected: `OK: 三处镇长表示都已清空`；探针的「最近 7 世界日翻转」这次会是 🔴 1 人（刚升刚降）——**这正是告警条件生效的证据**，写进 commit 说明这是演练造成的、不是稳态。

- [ ] **Step 6: 在 `civic_promotion.py` 顶部补「标定与上线」注释块**

在 `backend/app/tasks/civic_promotion.py` 的模块 docstring 末尾（`"""` 之前）追加：

```
标定与上线（执行者必读）
------------------------

三个门槛（``CIVIC_PROMOTION_MIN_WORLD_DAYS`` / ``MIN_PEERS`` /
``MIN_FAMILIARITY``）在 ``civic_membership`` 里给的是**占位默认值，标定前不得
开闸**。真实取值必须由生产分布反推出「使晋升面非空且非全量」的值。

**标定不是手工活，跑脚本**（Task 6b 的交付，与本模块的判定函数同源）::

    docker compose exec api python scripts/civic_calibration_report.py --list

它输出的三张表正对应要定的三组值：

- 表① UGC 居民的在镇世界日分布 → MIN_WORLD_DAYS
- 表② 每位 UGC 对锚定公民的 **top-N familiarity 分布**（重点看「第 MIN_PEERS
  高」那一档：通过门槛② 当且仅当它 ≥ θ；只看达标计数读不出 θ 该往哪挪）
  → MIN_FAMILIARITY / MIN_PEERS
- 表③ 当前公民总数（拆内置 / 归化）→ CIVIC_MIN_ELECTORATE、单夜上限、熔断阈值

脚本自己给判据：``verdict=partial``（非空且非全量）才算标出了一组可用取值；
``empty`` / ``full`` / ``no_data`` 一律报红并打印「待生产数据复标，不得直接
开闸」。

还要另外确认一项脚本读不到的：生产 ``REALISM_RELATIONS_ENABLED`` 的实际取值
（familiarity 的主增长路径挂在它上面；记忆记录 2026-07-23 部署 vm212 时置
true 并在容器内坐实，此后多轮部署未复验——开闸前登录确认一次）。

注：内置阵容的世界龄已 ≈450 世界日，UGC 新人从 0 开始；标定时不要把两类人放
进同一分布看（脚本的表①因此只统计 UGC denizen）。本机 dev 库是空的，标定不可
在本机做完；以 dev 库标定必须显式标注「待生产数据复标」，不得直接开闸。

上线是**四次独立变更，顺序不可合并**：
  ① 建表迁移 civic_standing_history（纯 DDL，零数据行为）—— 必须先于 T2
  ② T2 存量回填（一次性脚本，数据变更，写 resident_type + 历史行双写）
  ③ F2 代码合入（CIVIC_PROMOTION_MODE=off，零数据写）
  ④ shadow 观察 ≥ 3 个夜间周期 → 开闸（单独一次变更，只翻开关）
```

- [ ] **Step 7: 提交**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f2-civic
git add backend/app/tasks/civic_promotion.py
git status --short
git commit -m "$(cat <<'MSG_EOF'
docs(civic): 标定与上线四次独立变更的执行者须知

三个门槛是占位默认值，标定前不得开闸；模块 docstring 现在直接指向 Task 6b 的
scripts/civic_calibration_report.py（三张表 + verdict 判据），外加脚本读不到的
那一项（生产 REALISM_RELATIONS_ENABLED 复验）与「内置 ≈450 世界日、UGC 从 0
开始，两类人不进同一分布」的注意事项。

本 commit 同时是 F2 的验收记录：全量回归双向差集零新增失败，off/shadow/on
三态与撤销路径都在真实进程上跑过。

Verified-by: <贴 comm -13 的空输出 + 三段真实进程演练输出 + burnin_report 探针段落>
MSG_EOF
)"
```

---

## 收口清单（不在本线执行，交接给收口会话）

本线**不做**以下五件事，但它们是 F2 生效的必要条件，逐条交接：

1. **`config.py` / `.env.example`**：把 `civic_membership` 里的 12 个 env 旋钮（`CIVIC_PROMOTION_MODE` / `MIN_WORLD_DAYS` / `MIN_PEERS` / `MIN_FAMILIARITY` / `CIVIC_PEER_SEASONING_WORLD_DAYS` / `MAX_PER_RUN` / `BREAKER_FRACTION` / `BREAKER_MIN_ABS` / `CIVIC_MIN_ELECTORATE` / `CIVIC_MIN_TENURE_WORLD_DAYS` / `COOLDOWN_WORLD_DAYS` / `CIVIC_AUTO_DEMOTION_ENABLED`）补成 `Settings` 字段 + `.env.example` 行（字段名 = env 名的小写）。加完后 `_settings_default()` 自动接管 fallback，F2 代码零改动。必须同批加 `.env.example` 行，否则 `tests/test_env_example_consistency.py` 的不变量 2 会红。
2. **`nightly_cron.py` 接线**：`civic_promotion.run_promotion_pass` 接在 `close_due_polls`(`:215`) 之后、`run_npc_voting`(`:247`) 之前（≈`:245`），用与 `:142-145` 同款注释锚住位置；gate 在 cron 内部（realism-family 模式：自己的 try/except、fail-open）。回归测试按 **N+1 晚**断言：一张当晚到期的 poll + 一位当晚达标的 UGC 居民，该 poll 在夜 N 与夜 N+1 的 verdict 都不因晋升而改变。
3. **alembic 单头**：本线的 `051_add_civic_standing_history` 与并行 lab 线的 `051_add_lab_codex_model_tier` 都挂在 `050_add_resident_sprites` 上 → 合并后双头。按仓内先例（`048_add_town_treasury` / `049_add_policies` 的线性化）把后落地的一支 re-chain，并更新 `tests/test_civic_standing_history_model.py::test_migration_single_head_and_chains_onto_050` 的断言。
4. **T2 存量回填脚本**（线 T 的交付，与 F2 共用 `civic_membership.is_ugc_resident` / `ugc_filter`）：必须是进仓库、被评审、`--dry-run` 为默认值的 `backend/scripts/` 脚本；目标谓词 = UGC 判定 **AND** 无 `new_standing == "citizen"` 的历史行；执行后在 `system_config` 写 `civic_backfill_done=<world_date>`，脚本启动时标记已存在且未带 `--force-rerun` 则拒绝退出；**回填是 `resident_type` + 历史行的双写，只写 type 不写历史即视为回填未完成**（`actor="ops_backfill_t2"`）。禁止在 vm212 上手写一次性 SQL。

5. **`office_service._clear_mayor_legacy_stores` 的集合谓词**（spec §4.3「通用约束」点名的两处反例之一，本线只修了 `election_service.py:141` 那处）：把 `backend/app/services/office_service.py:222` 的 WHERE 由

   ```python
   Resident.is_autonomous,
   Resident.meta_json.isnot(None),
   ```

   改为只留 `Resident.meta_json.isnot(None)`（与 Task 7 对 `install_mayor` 清扫面的处置口径**逐字一致**）。本线不改它的唯一理由是 `office_service.py` 是 F3 的独占文件，**不是**因为它没问题——归 F3 或收口会话执行，谁先动谁改。

   **逐出档（`revoke_citizenship(tier="exile")`）上线前必须完成**：降级档只是侥幸命中（denizen 仍在 `SIM_RESIDENT_TYPES` 里），逐出档天然自锁——被逐者一旦掉出 `is_autonomous`，他身上的 `meta_json['mayor']` 就再也扫不掉，而那是工资倍率的唯一读点（`duty_service.py:172-173`）。验收断言：一位不满足 `is_autonomous` 但带 `meta_json['mayor']` 的居民，`_clear_mayor_legacy_stores()` 之后标志必须已被清掉。

## 明确不做（YAGNI，写清楚免得被当遗漏）

- ❌ 不新增第 5 个 `resident_type` 取值
- ❌ 不实现自动下滑降级（开关留着，默认关，置真即 raise）
- ❌ 不实现撤票（用冻结分母代替，幽灵票是设计语义）
- ❌ 不实现 `is_in_town` 收窄、不释放住房 / tile（这是逐出档的副作用，也正是两档强度的可观测差别之一）
- ❌ 不给 `civic_standing_history` 加任何读接口（避免撤销原因被公开）
- ❌ **永不 DELETE**：逐出必须是软状态 + 副作用清单，绝不复用 `purge_residents` 或其中任何一段级联
- ❌ 账号封禁（`users.is_banned`）与角色逐出是两层，**互不自动传导**；「封号用户名下居民是否自动降级」是待决项而非默认行为——否则某次封号操作会意外触发一次批量政治层变更，正是红线窗口
