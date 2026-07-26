# 线 F3 · 官员任期 + 卸任审计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补上 `term_check()` 到期后没有补选的断链（世界不得停在「无限期无镇长」），并在官员离任时生成只读的任内财政审计记录。

**Architecture:** 两件事都挂在 `OfficeService` 的既有出缺路径上。补选是 `app/services/office_service.py` 新增的模块级钩子 `trigger_backfill(db, office_key, *, reason)`——它 import 调用 `election_service.open_election`，不改 `election_service` 任何函数体；F2 的撤销收口时调同一个钩子。审计是新模块 `app/tasks/office_audit.py`，纯 SELECT 汇总（镇财政余额 / 财政政策改动 / 任内经公决**通过**的财政议案 / 离任者个人钱包），落一行 `system_config`（本线不允许迁移）。两者都 fail-open——**而且 fail-open 要覆盖 session，不只是返回值**：吞异常前必须 `rollback`，否则异常一旦来自 flush/commit（`propose` 与 `ConfigService.set` 内部都有 `db.commit()`），调用方后续任何语句都会抛 `PendingRollbackError`，等于把炸点推后一条语句。审计或补选出错绝不能让 vacate 失败。

**Tech Stack:** Python 3.12 / SQLAlchemy 2.x async / FastAPI / pytest-anyio / sqlite(测试) + PostgreSQL(生产)。零 LLM、零迁移、零新增 config 旋钮。

## Global Constraints

以下逐条抄自 `docs/PARALLEL_WORKSTREAMS_2026-07-27.md`（§5 / §9）与本批次口头硬约束，违反任一条即为计划失败：

- 独占文件：`app/services/office_service.py`、新建 `app/tasks/office_audit.py`、对应测试。对 `election_service.py` 只 import 调用，不改函数体（`install_mayor` 的收口归 F2）。
- **不改** `app/tasks/nightly_cron.py`（接线延到收口）。
- **不改** `app/config.py` / `.env.example`（三条线的新开关一次性延到收口补齐）→ 本线不得新增任何 `settings.*` 字段，所有阈值以函数关键字默认值形式给出，收口时改为读 settings。
- 「声誉影响」不在本线，切成收口接线步（依赖 F1 的修复后语义）。
- 开关默认关；`polis_office_mayor_term_days` 保持 0 直到本线验收通过。
- 与 F2 的接口约定：F2 的撤销只保证职位出缺并广播 `civic_standing_changed`，补选由本线的钩子接手（收口时接线）。允许的空缺上限 = 1 个夜间周期，超出由探针报红旗。
- 硬门：任期到期后世界不得出现「无限期无镇长」状态——须有测试推进世界时钟越过 `term_ends_at` 并断言补选已开，**且须有一条闭环测试证明这张 poll 关票后真的坐上了新镇长**（只断言「poll 已开」测不到硬门自己声称防的那个状态：`close_due_polls → _execute_outcome(type="mayor") → install_mayor → appoint` 这条链断了的话，F3 全绿而世界照样无镇长）。注意 `polis_office_mayor_term_days = 0` 且 `term_check` 被 gate 整段跳过时，**gate 开与关都没有自动收回路径**，撤销是唯一的下台方式；两种 gate 状态都要有测试覆盖。
- **正确性不得依赖 `polis_office_enabled` 的取值**：默认 False 时 `offices` 表可能根本没有 mayor 行，也可能有迁移 046 遗留的陈旧 `holder_slug`。gate 开与关两种状态都要有测试覆盖。
- **凡是清理「已离开集合 S 的居民」的扫描，不能用 S 本身做 WHERE。** `office_service.py:222` 现在正是这么写的（用 `Resident.is_autonomous`），本线要按 slug 直查改掉它。
- 任期算术唯一入口是 `app/world_clock.py`，禁止裸 `datetime.now()` 与世界节律比较。
- TDD：严格红→绿，一 step 一 commit，commit 末尾带**真实** `Verified-by:` 输出。禁 `--no-verify` / `amend` / `squash` / 编造测试数据。
- 硬门 = **相对基线零新增失败**（本机预存 51 failed / 17 errors，需 redis/testcontainers），判定用失败集**双向差集**（`comm -13` / `comm -23`），不是数量比较。
- 不要在 worktree 内创建 `backend/.env`（会破坏 conftest 的测试隔离）。
- `git checkout <branch> -- <path>` 会连带写入暂存区，用它取文件后必须先看 `git status` 再提交。

## 写计划时已实测核实的前提（不要重新怀疑，直接用）

在 `git worktree add --detach /tmp/f3-probe master` 的一次性探针里跑过，6/6 PASS（探针已删除）：

| # | 前提 | 结果 |
|---|---|---|
| 1 | `open_election(db)` 面对 3 个裸 npc 居民（无 SBTI、heat=0）能开出 poll | PASS：走 heat 回落取 3 人，`poll.id` 是 `str`，`question` 以 `镇长选举` 开头 |
| 2 | gate 关时 `current_mayor(db)` 无视 offices 里的陈旧 `holder_slug` | PASS：返回 `None`（Task 7 的红是真红） |
| 3 | `monkeypatch.setattr(world_clock, "now_real", ...)` 能驱动 `term_check()` 的默认 now | PASS：`term_check() == 1`，holder 变 `None` |
| 4 | sqlite 上 `Office.updated_at` 存 aware / 读 naive，`_as_utc` 回贴 UTC 后 `isoformat()` 与原值相等 | PASS |
| 5 | `Policy` / `ResidentTreasury` / `TownTreasury` 直插 + `real_to_world` 差值 = 5 真实日 × k | PASS：`20.0` 世界日 |
| 6 | Task 1 的红是真红：`resident_type="player"` 的离任镇长今天保留 `meta_json['mayor']` | PASS（现状确实没清掉） |

`world_clock_k = 4`（`app/config.py:211`）、`world_epoch = "2026-01-01T00:00:00+08:00"`（`:212`）。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/app/services/office_service.py` | Modify | ① `_clear_mayor_legacy_stores` 改为 slug 直查 + 无成员谓词的残留扫描；② `vacate` 预读离任者与任期起点，新增 `audit` 开关；③ 新增模块级 `trigger_backfill()` / `_fill_strategy()` / `_effective_holder()` 补选钩子（F2 收口调用同一入口）；④ `term_check` 在每个真实出缺后依次调审计与补选 |
| `backend/app/tasks/office_audit.py` | Create | 卸任财政审计：`collect_fiscal_audit` / `record_term_audit` / `list_term_audits` / `audit_key` / `_fit`，以及探针输入 `overdue_vacancies`。纯 SELECT + 一行 `system_config` 写入，绝不改账 |
| `backend/tests/test_office_vacancy_sweep.py` | Create | Task 1：镇长遗留存储清理不得用成员谓词做 WHERE |
| `backend/tests/test_office_backfill.py` | Create | Task 2/3/7：补选钩子语义、term_check 断链修复、gate 开关两态矩阵 |
| `backend/tests/test_office_term_audit.py` | Create | Task 4/5/6：审计汇总与落盘、空缺红旗、出缺路径接线 |

**不改动但被 import 调用的既有接口（已逐一读过源码核实签名）：**

- `app.services.election_service.open_election(db, *, candidate_slugs: list[str] | None = None, days: int | None = None) -> Poll | None`（`election_service.py:32`）
- `app.services.election_service.current_mayor(db) -> str | None`（`election_service.py:196`，gate-aware：`polis_office_enabled` 开时读 offices，否则/回落读 `system_config['current_mayor']`）
- `app.services.election_service.ELECTION_TAG = "镇长选举"`（`election_service.py:29`）
- `app.services.config_service.ConfigService.set(key: str, value, *, group: str, updated_by: str)` / `.get(key: str, *, default=None)`
- `app.services.treasury_service.balance(db) -> int`、`treasury_service.LAST_SPEND_KEY = "town_last_spend_at"`
- `app.services.coin_service.treasury_balance(db, slug: str) -> int`
- `app.services.policy_service.FISCAL_POLICY_KEYS = frozenset({"tax_rate","medical_subsidy_sc","npc_default_wage_sc","housing_development_scale"})`
- `app.world_clock.now_real() -> datetime`、`real_to_world(dt) -> datetime`（k=4，锚 Asia/Shanghai）
- 模型列：`Office(id, office_key, holder_slug, institution, perms_json, fill_strategy, term_started_at, term_ends_at, created_at, updated_at)`；`SystemConfig(key: String(200), value: String(2000), group, updated_at, updated_by)`；`Poll(id: str, season_id, question, options_json, closes_at, status)`；`Policy(id, key, value, tier, procedure, group, version, updated_by, created_at, updated_at)`；`TownTreasury(key, balance_sc, updated_at)`，`TOWN_KEY = "town"`

**与 F2 的接口契约（F2 的计划会逐字引用这两个签名）：**

```python
# app/services/office_service.py（模块级，不是 OfficeService 方法）
async def trigger_backfill(db: AsyncSession, office_key: str, *, reason: str) -> str | None: ...
#   返回：开出的 Poll.id（str）；None = 没开（非民选职位 / 仍在任 / 已有 open 选举 /
#   election_enabled|civic_polls_enabled 关 / 候选不足 / 内部异常已被 fail-open 吞掉）
#   reason 建议取 REASON_TERM_EXPIRED="term_expired" / REASON_CIVIC_REVOCATION="civic_revocation"
#   / REASON_MANUAL="manual"（office_service 模块级常量）
#   ★ 调用时机硬约束：必须在 meta_json['mayor'] 与 system_config['current_mayor']
#     两个遗留存储都清干净之后再调——否则 current_mayor() 仍读得到人，钩子会判「未出缺」直接返回 None。
#     F2 的撤销顺序（防呆 → 卸职 → 清 meta → 清 config → 改档位 → 写历史行 → 断言 → 广播）
#     天然满足这一点：把 trigger_backfill 放在广播之后即可。

# app/tasks/office_audit.py
async def record_term_audit(
    db: AsyncSession, *, office_key: str, holder_slug: str | None,
    term_started_at: datetime | None, term_ended_at: datetime | None = None,
) -> dict | None: ...
#   返回落盘的 payload dict；None = 无可审计对象或写入失败（fail-open）

# app/services/office_service.py（OfficeService 方法，F2 可用一次调用拿到「卸职 + 清遗留 + 审计」）
async def vacate(self, office_key: str, *, audit: bool = False) -> bool: ...

# ★ session 契约（两个 fail-open 入口共同保证，F2 可以依赖）：
#   内部异常被吞掉时**一定已经 rollback**，调用方拿到 None/True 之后可以在同一个
#   session 上继续写（改档位 / 写历史行 / 广播）。反过来说：这两个函数不会替你
#   保留任何未提交的写——调它们之前请先把自己的写 commit 掉。
```

---

## Task 0: worktree 与基线捕获

**Files:**
- Create: `/Volumes/data/dev/simverse-world/.worktrees/f3-office`（git worktree）
- Create: `/tmp/f3-base.txt`（基线失败集，不进仓库）

**Interfaces:**
- Consumes: master @ `origin/master`
- Produces: 工作区路径 `<WT> = /Volumes/data/dev/simverse-world/.worktrees/f3-office`；基线失败集 `/tmp/f3-base.txt`（后续每个 Task 的硬门都对它做双向差集）

- [ ] **Step 1: 建 worktree**

```bash
cd /Volumes/data/dev/simverse-world
git worktree add -b feat/f3-office-term-audit .worktrees/f3-office master
```

- [ ] **Step 2: 路径守卫（逐字照抄，不通过就停）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
source /Volumes/data/dev/simverse-world/backend/.venv/bin/activate
python -c "import app; p=app.__file__; assert '.worktrees/' in p, f'WRONG: {p}'; print('OK',p)"
```

Expected: 打印 `OK /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend/app/__init__.py`

- [ ] **Step 3: 确认 worktree 内没有 .env（有就删，它会破坏 conftest 隔离）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
ls -la .env 2>/dev/null && echo "FATAL: worktree 内存在 .env，必须删除" || echo "OK: no .env"
```

Expected: `OK: no .env`

- [ ] **Step 4: 捕获基线失败集**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/ -q -p no:randomly 2>&1 \
  | grep -E "^(FAILED|ERROR) " | awk '{print $1, $2}' | sort -u > /tmp/f3-base.txt
wc -l /tmp/f3-base.txt
```

> **不要在这里加 `sed 's/\[.*//'`。** 它会把 parametrize 的 case id 整段砍掉，把同一个
> 测试函数的多个参数化用例塌缩成一条——于是「某函数已有任一参数用例在基线里失败」就会
> 让 F3 打断它的其他参数用例**不出现在新增失败里**（实测基线里就有这种形状：
> `test_real_adapter_start_is_fail_closed_when_unconfigured[CodexAdapter]` 失败而同函数
> 其他参数用例是绿的）。而且本仓库 pytest 在 `-q` 下的 short summary 行就是裸的
> `FAILED tests/x.py::test_y`（实测无 ` - AssertionError: ...` 后缀），`awk '{print $1, $2}'`
> 取前两个字段已经足够稳，抖动担忧不成立。

Expected: 约 68 条（clean master 上的预存失败集 = 51 failed + 17 errors；保留完整
nodeid 后不再塌缩，所以条目数 ≈ 原始行数）。数字差得远就停下来查是不是跑错了 worktree
（在主工作区跑会多出几条来自未提交改动的失败）。**这个文件是本线所有硬门的分母，不得重新生成覆盖。**

- [ ] **Step 5: 不提交（Task 0 无代码产出）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git status --short   # 期望为空
```

---

## Task 1: 镇长遗留存储清理——按 slug 直查，WHERE 里不得出现成员谓词

**Files:**
- Modify: `backend/app/services/office_service.py:125-143`（`vacate`：预读离任者）
- Modify: `backend/app/services/office_service.py:175-176`（`term_check` 里的调用点）
- Modify: `backend/app/services/office_service.py:211-242`（`_clear_mayor_legacy_stores`）
- Test: `backend/tests/test_office_vacancy_sweep.py`

**Interfaces:**
- Consumes: `Office`、`Resident`（`app/models/resident.py`，`is_autonomous` = `resident_type in {"npc","resident"}`）、`SystemConfig`
- Produces: `OfficeService._clear_mayor_legacy_stores(self, *, holder_slug: str | None = None) -> None`；`vacate` 内部变量 `prior_row: Office | None`（Task 6 复用它的 `term_started_at`）

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_office_vacancy_sweep.py`：

```python
"""F3 Task 1 — 清理「已离开集合 S 的居民」的扫描不得用 S 本身做 WHERE。

office_service.py 原实现用 ``Resident.is_autonomous`` 圈定要清 meta_json
['mayor'] 的行。这个谓词恰好会漏掉唯一必须清的那个人：任内被降级 / 逐出
/ 本来就是玩家化身的镇长——他掉出 SIM_RESIDENT_TYPES，工资倍率标记于是
永久留在 meta_json 上（gotcha #1，双份工资倍率的来源）。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.models.office import Office
from app.models.resident import Resident
from app.services.office_service import OfficeService


def _res(slug, name, meta=None, rtype="npc"):
    return Resident(
        slug=slug, name=name, district="central_plaza", status="idle",
        resident_type=rtype, creator_id="sys", tile_x=70, tile_y=56,
        meta_json=meta,
    )


@pytest.mark.anyio
async def test_vacate_clears_mayor_flag_for_non_autonomous_holder(db_session):
    """离任者已不在人口集合内（player）——仍然必须被清掉工资倍率标记。"""
    holder = _res("ex-mayor", "前镇长", meta={"mayor": True}, rtype="player")
    db_session.add(holder)
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")
    assert await svc.vacate("mayor") is True

    await db_session.refresh(holder)
    assert not (holder.meta_json or {}).get("mayor")


@pytest.mark.anyio
async def test_vacate_residual_sweep_has_no_membership_predicate(db_session):
    """残留扫描的唯一谓词是 meta_json IS NOT NULL：另一个挂着陈年 mayor
    标记、类型为 preset（既不在 CIVIC_VOTER_TYPES 也不在 SIM_RESIDENT_TYPES）
    的居民同样要被清掉。"""
    stale = _res("stale-flag", "陈年标记", meta={"mayor": True}, rtype="preset")
    sitting = _res("sitting", "在任", meta={"mayor": True})
    db_session.add_all([stale, sitting])
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "sitting", fill_strategy="election")
    assert await svc.vacate("mayor") is True

    await db_session.refresh(stale)
    await db_session.refresh(sitting)
    assert not (stale.meta_json or {}).get("mayor")
    assert not (sitting.meta_json or {}).get("mayor")


@pytest.mark.anyio
async def test_term_check_clears_flag_for_non_autonomous_holder(db_session):
    """term_check 走的是同一个清理入口，同样按 slug 直查离任者。"""
    holder = _res("termed-out", "任满", meta={"mayor": True}, rtype="player")
    db_session.add(holder)
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "termed-out", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1

    await db_session.refresh(holder)
    assert not (holder.meta_json or {}).get("mayor")
    assert (await db_session.execute(
        select(Office.holder_slug).where(Office.office_key == "mayor")
    )).scalar_one() is None


@pytest.mark.anyio
async def test_vacate_still_nulls_system_config_current_mayor(db_session):
    """回归：slug 直查改造不得弄丢 system_config 回落值的清理。"""
    from app.services.config_service import ConfigService

    db_session.add(_res("old", "老镇长", meta={"mayor": True}))
    await db_session.commit()
    await ConfigService(db_session).set(
        "current_mayor", "old", group="civic", updated_by="test")

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "old", fill_strategy="election")
    assert await svc.vacate("mayor") is True
    assert await ConfigService(db_session).get("current_mayor") is None
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_vacancy_sweep.py -v -p no:randomly
```

Expected: `test_vacate_clears_mayor_flag_for_non_autonomous_holder`、`test_vacate_residual_sweep_has_no_membership_predicate`、`test_term_check_clears_flag_for_non_autonomous_holder` 三条 FAIL，报错形如
`assert not {'mayor': True}.get('mayor')` / `AssertionError`（标记没被清掉，因为 `WHERE resident_type IN ('npc','resident')` 把 player/preset 排除了）。第四条 PASS。

- [ ] **Step 3: 写最小实现**

`backend/app/services/office_service.py` —— 把 `vacate` 整体替换为：

```python
    async def vacate(self, office_key: str) -> bool:
        """Clear the office holder + term end. Guard UPDATE — returns True
        only when an actual holder was cleared (idempotent no-op otherwise).

        The pre-read is NOT a guard (the UPDATE's rowcount still decides): it
        only captures who is leaving, because the legacy-store cleanup must be
        keyed on that identity rather than on a membership predicate."""
        prior_row = (await self.db.execute(
            select(Office).where(Office.office_key == office_key)
        )).scalar_one_or_none()
        prior_holder = prior_row.holder_slug if prior_row is not None else None
        res = await self.db.execute(
            update(Office)
            .where(Office.office_key == office_key, Office.holder_slug.isnot(None))
            .values(holder_slug=None, term_ends_at=None,
                    updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        vacated = (res.rowcount or 0) > 0
        if vacated and office_key == "mayor":
            await self._clear_mayor_legacy_stores(holder_slug=prior_holder)
        await self.db.commit()
        if vacated:
            await self._emit_office_changed(
                "office_vacated", office_key, holder_slug=None,
            )
        return vacated
```

`term_check` 内的调用点（原 `office_service.py:175-176`）改为把离任者 slug 传下去：

```python
            if office.office_key == "mayor":
                await self._clear_mayor_legacy_stores(
                    holder_slug=office.holder_slug,
                )
```

> `synchronize_session=False` 让已加载的 ORM 行保持 UPDATE 前的值，且 `app/database.py:24`
> 的 sessionmaker 是 `expire_on_commit=False`——所以 `office.holder_slug` 在这里读到的
> 仍然是离任者，不会触发懒加载。

`_clear_mayor_legacy_stores` 整体替换为：

```python
    async def _clear_mayor_legacy_stores(
        self, *, holder_slug: str | None = None,
    ) -> None:
        """Keep the two legacy mayor stores in step with an offices-side
        vacate: pop meta_json['mayor'] (the wage multiplier — gotcha #1) and
        null system_config['current_mayor'] (the read fallback). Flushed into
        the caller's transaction; fail-open.

        NEITHER query may carry a membership predicate. The pre-F3 version
        scanned ``WHERE Resident.is_autonomous``, i.e. it selected the set the
        departing holder may have just left (demoted / exiled / a player
        avatar) — exactly the row that must be cleaned. Two disjoint reads
        replace it:

        1. targeted — ``WHERE slug = :holder_slug``, identity not membership;
        2. residual — ``WHERE meta_json IS NOT NULL AND slug <> :holder_slug``,
           catching stale flags any other path left behind.
        """
        try:
            from sqlalchemy.orm.attributes import flag_modified
            from app.models.resident import Resident

            targets: list = []
            if holder_slug:
                leaving = (await self.db.execute(
                    select(Resident).where(Resident.slug == holder_slug)
                )).scalar_one_or_none()
                if leaving is not None:
                    targets.append(leaving)
            residual_stmt = select(Resident).where(Resident.meta_json.isnot(None))
            if holder_slug:
                residual_stmt = residual_stmt.where(Resident.slug != holder_slug)
            targets.extend((await self.db.execute(residual_stmt)).scalars().all())

            for r in targets:
                meta = dict(r.meta_json or {})
                if meta.get("mayor"):
                    meta.pop("mayor", None)
                    r.meta_json = meta
                    flag_modified(r, "meta_json")
            from app.models.system_config import SystemConfig
            import json
            cfg = (await self.db.execute(
                select(SystemConfig).where(SystemConfig.key == "current_mayor")
            )).scalar_one_or_none()
            if cfg is not None:
                cfg.value = json.dumps(None)
                cfg.updated_by = "office_term_check"
                cfg.updated_at = datetime.now(UTC)
        except Exception:
            logger.warning("clearing legacy mayor stores failed", exc_info=True)
```

同时把模块 docstring 里 "Mayor special-case" 段落末尾补一句（紧接 "so the three representations never diverge after a term expiry."）：

```
Neither legacy-store query filters by resident_type / is_autonomous: the row
that must be cleaned is precisely the one that may have just left that set.
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_vacancy_sweep.py tests/test_office_service.py \
  tests/test_office_integration.py tests/test_burnin_report_offices.py \
  -q -p no:randomly
```

Expected: PASS，0 failed（既有 office 测试全绿——本 step 不改变任何既有断言）。

- [ ] **Step 5: 提交**

> **执行前先把 Step 4 的真实 stdout 粘进 `Verified-by:`。heredoc 里的 `<...>` 是占位符，
> 原样提交 = 假验证记录 = 计划失败**（Task 8 Step 4 有机械门会把它抓出来）。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git add backend/app/services/office_service.py backend/tests/test_office_vacancy_sweep.py
git commit -m "$(cat <<'EOF'
fix(office): 镇长遗留存储清理按 slug 直查——不再用 is_autonomous 做 WHERE

原实现扫 WHERE Resident.is_autonomous 去清 meta_json['mayor']。这个谓词圈的
正是「离任者可能刚离开的那个集合」：被降级/逐出/玩家化身的镇长掉出
SIM_RESIDENT_TYPES 后，工资倍率标记永久留在 meta_json 上(gotcha #1)。

改为两条互斥且都不带成员谓词的读：
- targeted: WHERE slug = :holder_slug（按身份，不按集合）
- residual: WHERE meta_json IS NOT NULL AND slug <> :holder_slug

vacate 增加一次预读拿离任者 slug——预读不是 guard，rowcount 仍然是唯一判据。

Verified-by: <贴 Step 4 实际输出>
EOF
)"
```

---

## Task 2: 补选钩子 `trigger_backfill`（基础语义）

**Files:**
- Modify: `backend/app/services/office_service.py`（模块尾部新增模块级函数与常量）
- Test: `backend/tests/test_office_backfill.py`

> 本 Task 只落**基础语义**（民选职位 / 未在任 / 无 open 选举 / 盖 cadence 戳 / fail-open）。
> 「正确性不得依赖 `polis_office_enabled`」那一层由 **Task 7** 用红→绿补上——把它拆开是
> 故意的：Task 7 的 gate 矩阵必须先真红一次，否则那条硬约束就没有被测试证明过。

**Interfaces:**
- Consumes: `election_service.open_election(db, *, candidate_slugs=None, days=None) -> Poll | None`、`election_service.ELECTION_TAG`、`OfficeService.get_holder(office_key) -> str | None`、`ConfigService.set`
- Produces:
  - `REASON_TERM_EXPIRED = "term_expired"` / `REASON_CIVIC_REVOCATION = "civic_revocation"` / `REASON_MANUAL = "manual"`
  - `async def _rollback_quietly(db: AsyncSession) -> None`（fail-open 分支的 session 收尾，自身绝不抛）
  - `async def _fill_strategy(db: AsyncSession, office_key: str) -> str`（Task 7 会加 OFFICE_DEFS 回落）
  - `async def trigger_backfill(db: AsyncSession, office_key: str, *, reason: str) -> str | None`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_office_backfill.py`：

```python
"""F3 Task 2/3/7 — 出缺后的补选钩子。

断链修复的核心：term_check() 到期只 vacate，没有任何路径触发补选，
current_mayor() 的两个回落也被同一次 term_check 清干净，世界于是进入
「无镇长且无人接任」的稳态。trigger_backfill 是补上的那一截，同时是 F2
撤销收口时的调用入口。

正确性硬约束：不得依赖 polis_office_enabled。gate 关时 offices 可能没有
mayor 行，也可能留着迁移 046 的陈旧 holder_slug——两种都要判对（Task 7 的
矩阵专门证明这一点）。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.office import Office
from app.models.resident import Resident
from app.models.season import Poll, Vote
from app.services.election_service import ELECTION_TAG
from app.services.office_service import (
    OfficeService,
    REASON_CIVIC_REVOCATION,
    REASON_MANUAL,
    REASON_TERM_EXPIRED,
    trigger_backfill,
)


def _res(slug, name, meta=None, rtype="npc"):
    return Resident(
        slug=slug, name=name, district="central_plaza", status="idle",
        resident_type=rtype, creator_id="sys", tile_x=70, tile_y=56,
        meta_json=meta,
    )


async def _seed_voters(db):
    db.add_all([_res("a", "甲"), _res("b", "乙"), _res("c", "丙")])
    await db.commit()


async def _open_election_polls(db) -> list[Poll]:
    return (await db.execute(
        select(Poll).where(
            Poll.status == "open", Poll.question.like(f"{ELECTION_TAG}%"),
        )
    )).scalars().all()


@pytest.mark.anyio
async def test_backfill_opens_election_for_vacant_elected_office(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    poll_id = await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED)
    assert poll_id
    poll = (await db_session.execute(
        select(Poll).where(Poll.id == poll_id)
    )).scalar_one()
    assert poll.status == "open"
    assert poll.question.startswith(ELECTION_TAG)
    assert len(poll.options_json) >= 2


@pytest.mark.anyio
async def test_backfill_noop_while_office_still_occupied(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_MANUAL) is None
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_ignores_labour_offices(db_session, monkeypatch):
    """town_clerk / postman / doctor 是劳动职务，不走选举补缺。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("postman", "a", fill_strategy="seed")
    await svc.vacate("postman")

    assert await trigger_backfill(db_session, "postman", reason=REASON_TERM_EXPIRED) is None
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_is_idempotent_against_open_election(db_session, monkeypatch):
    """已有一张 open 的选举 poll 时不得再开第二张。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    db_session.add(Poll(
        question=f"{ELECTION_TAG}:谁来当下一任镇长?",
        options_json=[], closes_at=datetime.now(UTC) + timedelta(days=3),
        status="open",
    ))
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED) is None
    assert len(await _open_election_polls(db_session)) == 1


@pytest.mark.anyio
async def test_backfill_stamps_election_cadence(db_session, monkeypatch):
    """开完补选要盖 election_last_opened，否则同一晚 maybe_open_seasonal_election
    会再开一张。"""
    from app.services.config_service import ConfigService

    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_CIVIC_REVOCATION)
    stamped = await ConfigService(db_session).get("election_last_opened")
    assert stamped == datetime.now(UTC).date().isoformat()


@pytest.mark.anyio
async def test_backfill_survives_open_election_failure(db_session, monkeypatch):
    """fail-open：选举服务炸了也不能把异常抛回 vacate 路径。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    async def _boom(db, **kwargs):
        raise RuntimeError("election service down")

    monkeypatch.setattr(
        "app.services.election_service.open_election", _boom)
    assert await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED) is None


@pytest.mark.anyio
async def test_backfill_failure_leaves_session_usable(db_session, monkeypatch):
    """fail-open 必须连 session 一起 fail-open。

    上一条的 _boom 是「进门就 raise」——一个 DB 语句都没跑过,session 是干净的,
    加不加 rollback 都会绿。真实故障形状是异常来自 **flush/commit**
    (open_election → civic_service.propose 里有 db.add + db.commit):那时
    session 停在 needs-rollback 状态,后续任何语句都抛 PendingRollbackError
    (本机实测:失败 flush 后不 rollback,下一条 SELECT 即 PendingRollbackError)。
    所以这里故意让写炸在 flush 里。
    """
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    # 一张已关闭的旧选举 poll:它的主键待会儿被拿去制造 flush 期冲突。
    # status="closed" 所以不会被 trigger_backfill 的「已有 open 选举」早退命中。
    old = Poll(question=f"{ELECTION_TAG}:上一届", options_json=[],
               closes_at=datetime.now(UTC) - timedelta(days=1), status="closed")
    db_session.add(old)
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    async def _boom_in_flush(db, **kwargs):
        db.add(Poll(id=old.id, question=f"{ELECTION_TAG}:x", options_json=[],
                    closes_at=datetime.now(UTC) + timedelta(days=1),
                    status="open"))
        await db.flush()          # IntegrityError:主键冲突,炸在 flush 里

    monkeypatch.setattr(
        "app.services.election_service.open_election", _boom_in_flush)
    assert await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED) is None
    # 关键断言:session 仍可用。没有 rollback 这里会抛 PendingRollbackError,
    # 「返回 None」只是把炸点推到了下一条语句。
    assert await OfficeService(db_session).get_holder("mayor") is None
    # 半途写入不得留在库里
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_declines_when_civic_gates_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    monkeypatch.setattr(settings, "election_enabled", False)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    await svc.vacate("mayor")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_TERM_EXPIRED) is None
    assert await _open_election_polls(db_session) == []
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_backfill.py -v -p no:randomly
```

Expected: 收集阶段就 FAIL，报错形如
`ImportError: cannot import name 'trigger_backfill' from 'app.services.office_service'`。

- [ ] **Step 3: 写最小实现**

在 `backend/app/services/office_service.py` **文件末尾**（`OfficeService` 类之后）追加：

```python
# ── F3: the missing second half of a vacancy ───────────────────────────
#
# term_check() only ever vacated. current_mayor()'s two fallbacks were cleared
# by the same pass, so the world settled into "no mayor and nobody arriving".
# trigger_backfill is that missing link, and it is also the single entry point
# F2's revocation calls (KICKOFF 2026-07-27 §5 与 F2 的接口约定).

REASON_TERM_EXPIRED = "term_expired"
REASON_CIVIC_REVOCATION = "civic_revocation"
REASON_MANUAL = "manual"


async def _rollback_quietly(db: AsyncSession) -> None:
    """Roll back after a swallowed failure so the caller's session stays
    usable. Never raises — a fail-open path may not explode on its way out."""
    try:
        await db.rollback()
    except Exception:
        logger.warning("rollback after a swallowed office failure also failed",
                       exc_info=True)


async def _fill_strategy(db: AsyncSession, office_key: str) -> str:
    """The office's refill procedure, read off the offices row."""
    row = (await db.execute(
        select(Office.fill_strategy).where(Office.office_key == office_key)
    )).scalar_one_or_none()
    return str(row or "")


async def _effective_holder(db: AsyncSession, office_key: str) -> str | None:
    """Who holds ``office_key`` right now."""
    return await OfficeService(db).get_holder(office_key)


async def trigger_backfill(
    db: AsyncSession, office_key: str, *, reason: str,
) -> str | None:
    """Refill a now-vacant office. Returns the opened Poll.id, else None.

    None means: not an elected office / still occupied / an election poll is
    already open / election|civic gates off / not enough candidates / an
    internal failure (fail-open — a broken election must never break the
    vacate that called us).
    """
    try:
        if not office_key:
            return None
        if await _fill_strategy(db, office_key) != "election":
            return None
        from app.config import settings
        if not (settings.election_enabled and settings.civic_polls_enabled):
            return None
        if await _effective_holder(db, office_key):
            return None

        from app.models.season import Poll
        from app.services import election_service
        existing = (await db.execute(
            select(Poll).where(
                Poll.status == "open",
                Poll.question.like(f"{election_service.ELECTION_TAG}%"),
            )
        )).scalars().first()
        if existing is not None:
            logger.info("office backfill skipped (%s/%s): election already open",
                        office_key, reason)
            return None

        poll = await election_service.open_election(db)
        if poll is None:
            logger.info("office backfill produced no poll (%s/%s)",
                        office_key, reason)
            return None
        try:
            from app.services.config_service import ConfigService
            await ConfigService(db).set(
                "election_last_opened",
                datetime.now(UTC).date().isoformat(),
                group="civic", updated_by=f"office_backfill:{reason}",
            )
        except Exception:
            logger.warning("stamping election_last_opened failed", exc_info=True)
            # Same reason as the outer handler: ConfigService.set writes and
            # commits, so a failure here can leave the session needing a
            # rollback. The poll itself is already committed by propose().
            await _rollback_quietly(db)
        logger.info("office backfill opened election %s for %s (%s)",
                    poll.id, office_key, reason)
        return poll.id
    except Exception:
        logger.warning("office backfill failed (%s/%s)", office_key, reason,
                       exc_info=True)
        # Fail-open has to cover the SESSION, not just the return value.
        # open_election → civic_service.propose does db.add + db.commit, so the
        # exception may well come out of a flush/commit (IntegrityError, a
        # dropped connection, a column-width overflow). A session left in the
        # needs-rollback state makes every LATER statement raise
        # PendingRollbackError — i.e. returning None would merely move the
        # explosion one statement down (term_check's next due office, or F2's
        # 改档位 → 写历史行 → 广播 after vacate returned True).
        await _rollback_quietly(db)
        return None
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_backfill.py tests/test_m6_election.py -q -p no:randomly
```

Expected: PASS，0 failed。

- [ ] **Step 5: 提交**

> **执行前先把 Step 4 的真实 stdout 粘进 `Verified-by:`。heredoc 里的 `<...>` 是占位符，
> 原样提交 = 假验证记录 = 计划失败**（Task 8 Step 4 有机械门会把它抓出来）。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git add backend/app/services/office_service.py backend/tests/test_office_backfill.py
git commit -m "$(cat <<'EOF'
feat(office): 新增 trigger_backfill 补选钩子(F2 撤销与任期到期共用入口)

出缺后没有任何路径触发补选是世界停在「无限期无镇长」的直接原因。钩子只
import 调用 election_service.open_election,不改它的函数体。

本 commit 落基础语义:仅 fill_strategy == "election" 的职位走补选;仍在任
不补;已有 open 的选举 poll 不重开;开完盖 election_last_opened,避免同一晚
maybe_open_seasonal_election 再开一张;全函数 fail-open,选举服务出错不得
把异常抛回 vacate 路径。

fail-open 覆盖 session 而不只是返回值:open_election → civic_service.propose
里有 db.add + db.commit,异常可能来自 flush/commit,那时 session 停在
needs-rollback 状态、后续任何语句都抛 PendingRollbackError。所以吞异常前一律
_rollback_quietly。测试用「炸在 flush 里」的形状证明它(进门就 raise 的假红
无论加不加 rollback 都会绿)。

「正确性不依赖 polis_office_enabled」由后续 gate 矩阵 commit 红→绿补上。

Verified-by: <贴 Step 4 实际输出>
EOF
)"
```

---

## Task 3: `term_check` 接上补选 —— 断链修复与硬门测试

**Files:**
- Modify: `backend/app/services/office_service.py:145-182`（`term_check`）
- Test: `backend/tests/test_office_backfill.py`（追加）

**Interfaces:**
- Consumes: `trigger_backfill(db, office_key, *, reason)`（Task 2）、`REASON_TERM_EXPIRED`
- Produces: `OfficeService.term_check(self, *, now: datetime | None = None) -> int`（签名不变，返回值语义不变 = 真实出缺数）

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/test_office_backfill.py` 末尾追加：

```python
# ── Task 3: term_check 断链修复（硬门）──────────────────────────────

@pytest.mark.anyio
async def test_term_check_triggers_backfill_frozen_clock(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1

    assert await svc.get_holder("mayor") is None
    polls = await _open_election_polls(db_session)
    assert len(polls) == 1  # 世界不得停在「无限期无镇长」


@pytest.mark.anyio
async def test_world_clock_advance_past_term_end_opens_backfill(db_session, monkeypatch):
    """硬门：推进世界时钟越过 term_ends_at，断言补选已开。

    term_days 是世界日；k=4 时 8 世界日 ≈ 2 真实日,所以把真实钟推 8/k+1 日
    一定越过 term_ends_at。用 world_clock 换算而不是裸 utcnow 比较。
    """
    from app import world_clock

    monkeypatch.setattr(settings, "polis_office_enabled", True)
    monkeypatch.setattr(settings, "polis_office_mayor_term_days", 8)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election",
                      term_days=settings.polis_office_mayor_term_days)

    base = world_clock.now_real()
    jump = timedelta(days=8 / settings.world_clock_k + 1)
    monkeypatch.setattr(world_clock, "now_real", lambda: base + jump)

    assert await svc.term_check() == 1              # 默认 now 走世界时钟
    assert await svc.get_holder("mayor") is None
    polls = await _open_election_polls(db_session)
    assert len(polls) == 1
    assert polls[0].question.startswith(ELECTION_TAG)


@pytest.mark.anyio
async def test_term_check_backfill_failure_does_not_break_vacate(db_session, monkeypatch):
    """补选炸了,出缺本身仍然成立(fail-open)。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    async def _boom(db, **kwargs):
        raise RuntimeError("election service down")

    monkeypatch.setattr("app.services.election_service.open_election", _boom)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1
    assert await svc.get_holder("mayor") is None


@pytest.mark.anyio
async def test_term_check_does_not_backfill_labour_office(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("postman", "a", fill_strategy="seed", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_poll_closing_actually_seats_a_successor(db_session, monkeypatch):
    """硬门本体:补选开出 → 关票 → 新镇长就位。

    spec §5 的硬门目标是「任期到期后世界不得出现『无限期无镇长』状态」,
    「开出一张 poll」只是半截:中间还隔着
    close_due_polls → _close_one → _execute_outcome(type="mayor")
    → install_mayor → OfficeService.appoint 这条链。这条链现在是通的,但只断言
    poll 数的话,它哪天断了 F3 的测试仍然全绿而世界照样停在无镇长。
    不违反独占文件约束:civic_service / election_service 只 import 调用。
    """
    from app.services import civic_service

    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1
    assert await svc.get_holder("mayor") is None

    polls = await _open_election_polls(db_session)
    assert len(polls) == 1
    poll = polls[0]
    winner_slug = poll.options_json[0]["effect"]["slug"]
    # 投一票给 0 号候选,并把截止时间挪到过去(与 test_m6_election 同姿势:
    # 走 votes 表而不是手改 options_json,免得跟 flag_modified 较劲)
    db_session.add(Vote(poll_id=poll.id, user_id="u1", option_idx=0))
    poll.closes_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    assert await civic_service.close_due_polls(db_session) == 1
    # 不再是「无限期无镇长」:继任者真的坐上了位置
    assert await svc.get_holder("mayor") == winner_slug
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_backfill.py -v -p no:randomly \
  -k "term_check_triggers or world_clock_advance or actually_seats_a_successor"
```

Expected: 三条 FAIL，报错形如 `assert 0 == 1`（`len(polls)` 为 0——`term_check` 只 vacate，没开补选）。
`test_backfill_poll_closing_actually_seats_a_successor` 也停在同一处（`assert len(polls) == 1`），
它的后半截闭环（关票 → 继任者就位）要等 Step 3 之后才跑得到。

- [ ] **Step 3: 写最小实现**

把 `backend/app/services/office_service.py` 的 `term_check` 整体替换为：

```python
    async def term_check(self, *, now: datetime | None = None) -> int:
        """Nightly: vacate every office whose term_ends_at has passed, then
        hand each freed seat to :func:`trigger_backfill`.

        Returns the number of offices actually vacated. ``now`` is injectable
        for frozen-clock tests; the default reads the world clock's real 'now'
        (term_ends_at is stored in real UTC, converted at appoint time).

        The backfill call is the F3 断链 fix: before it, an expired term left
        the office empty AND cleared both current_mayor fallbacks, so nothing
        in the world could ever seat a successor.
        """
        if now is None:
            from app import world_clock
            now = world_clock.now_real().astimezone(UTC)
        due = (await self.db.execute(
            select(Office).where(
                Office.holder_slug.isnot(None),
                Office.term_ends_at.isnot(None),
                Office.term_ends_at <= now,
            )
        )).scalars().all()
        n = 0
        for office in due:
            # Captured BEFORE the guard UPDATE: synchronize_session=False keeps
            # the loaded row at its pre-update values on purpose, and the
            # departing holder is what the legacy-store clear is keyed on.
            office_key = office.office_key
            prior_holder = office.holder_slug
            res = await self.db.execute(
                update(Office)
                .where(
                    Office.id == office.id,
                    Office.holder_slug.isnot(None),
                    Office.term_ends_at <= now,
                )
                .values(holder_slug=None, term_ends_at=None,
                        updated_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            if (res.rowcount or 0) == 0:
                continue  # re-appointed concurrently — not expired anymore
            if office_key == "mayor":
                await self._clear_mayor_legacy_stores(holder_slug=prior_holder)
            await self.db.commit()
            n += 1
            await self._emit_office_changed(
                "office_vacated", office_key, holder_slug=None,
            )
            # F3: the second half. trigger_backfill is fail-open internally,
            # so a broken election can never turn a completed vacate into an
            # exception. It runs AFTER the legacy stores were cleared above —
            # that ordering is what makes the vacancy visible to it.
            await trigger_backfill(
                self.db, office_key, reason=REASON_TERM_EXPIRED,
            )
        return n
```

同时更新模块 docstring 中 `term_check` 的描述行（原第 12-14 行 "``term_check``: per-row guard UPDATE ..." 那条），在其后追加一行：

```
  A vacate that actually landed then calls ``trigger_backfill`` — without it
  the office stays empty forever (F3 断链).
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_backfill.py tests/test_office_service.py \
  tests/test_office_integration.py tests/test_office_vacancy_sweep.py \
  tests/test_m6_election.py -q -p no:randomly
```

Expected: PASS，0 failed。

- [ ] **Step 5: 提交**

> **执行前先把 Step 4 的真实 stdout 粘进 `Verified-by:`。heredoc 里的 `<...>` 是占位符，
> 原样提交 = 假验证记录 = 计划失败**（Task 8 Step 4 有机械门会把它抓出来）。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git add backend/app/services/office_service.py backend/tests/test_office_backfill.py
git commit -m "$(cat <<'EOF'
fix(office): term_check 到期后触发补选——修掉「无限期无镇长」断链

到期只 vacate,而 current_mayor 的两个回落(meta_json['mayor'] /
system_config['current_mayor'])被同一次 term_check 清掉,世界于是稳态无镇长。
真实出缺后按 REASON_TERM_EXPIRED 调 trigger_backfill,顺序在两个遗留存储清
干净之后——否则钩子会判「仍在任」。

硬门测试:monkeypatch world_clock.now_real 把真实钟推过 term_ends_at
(8 世界日 / k + 1 真实日),断言恰好一张 open 的选举 poll;外加一条闭环测试
把这张 poll 投票关掉,断言新镇长真的就位——「poll 已开」只是半截,
close_due_polls → _execute_outcome → install_mayor → appoint 这条链断了的话
只断言 poll 数的测试仍然全绿,而世界照样停在无镇长。

Verified-by: <贴 Step 4 实际输出>
EOF
)"
```

---

## Task 4: `app/tasks/office_audit.py` —— 卸任财政审计（只读汇总，不改账）

**Files:**
- Create: `backend/app/tasks/office_audit.py`
- Test: `backend/tests/test_office_term_audit.py`

**Interfaces:**
- Consumes: `treasury_service.balance(db) -> int`、`treasury_service.LAST_SPEND_KEY`、`coin_service.treasury_balance(db, slug) -> int`、`policy_service.FISCAL_POLICY_KEYS`、`ConfigService.set/get`、`world_clock.real_to_world(dt)`、模型 `Office` / `SystemConfig` / `Policy` / `Poll` / `TownTreasury`
- Produces:
  - `AUDIT_GROUP = "office_audit"` / `AUDIT_KEY_PREFIX = "office_audit"` / `AUDIT_SCHEMA_VERSION = 1`
  - `def audit_key(office_key: str, holder_slug: str, term_started_at: datetime | None) -> str`
  - `def _fit(payload: dict) -> dict`
  - `async def _rollback_quietly(db: AsyncSession) -> None`（fail-open 分支的 session 收尾，自身绝不抛）
  - `async def collect_fiscal_audit(db, *, office_key: str, holder_slug: str, term_started_at: datetime | None, term_ended_at: datetime | None = None) -> dict`
  - `async def record_term_audit(db, *, office_key: str, holder_slug: str | None, term_started_at: datetime | None, term_ended_at: datetime | None = None) -> dict | None`
  - `async def list_term_audits(db, *, office_key: str | None = None, limit: int = 20) -> list[dict]`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_office_term_audit.py`：

```python
"""F3 Task 4/5/6 — 卸任财政审计。

只读汇总:每个数字都是 SELECT,唯一的写是自己那一行 system_config。
镇财政没有流水表(transactions.user_id 是 users.id 硬 FK,见
app/models/town_treasury.py),所以可审计面就是 S1-5 留下的余额 +
updated_at + system_config 戳,加上 S2-5 的财政政策行与推动它们的公决。

存储用 system_config:本线不允许迁移(§5 独占文件没有 models/migrations)。
value 是 String(2000) 且 ConfigService.set 用 json.dumps(ensure_ascii=True),
一个汉字 6 字节——所以每个 payload 落盘前必须过 _fit。
"""
from datetime import datetime, timedelta, UTC

import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.office import Office
from app.models.policy import Policy
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.season import Poll
from app.models.system_config import SystemConfig
from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.services.office_service import OfficeService
from app.tasks import office_audit


def _res(slug, name, meta=None, rtype="npc"):
    return Resident(
        slug=slug, name=name, district="central_plaza", status="idle",
        resident_type=rtype, creator_id="sys", tile_x=70, tile_y=56,
        meta_json=meta,
    )


@pytest.mark.anyio
async def test_collect_fiscal_audit_shape_and_numbers(db_session):
    started = datetime.now(UTC) - timedelta(days=5)
    ended = datetime.now(UTC)

    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=250,
                     updated_at=ended - timedelta(hours=2)),
        ResidentTreasury(resident_slug="ex-mayor", balance_sc=88),
        Office(office_key="mayor", institution="town_hall",
               fill_strategy="election", holder_slug=None),
        # 任内的财政政策改动
        Policy(key="tax_rate", value="0.2", tier="simple_majority",
               procedure="civic_poll", group="fiscal", version=3,
               updated_by="poll:p1", created_at=started,
               updated_at=started + timedelta(days=1)),
        # 任期之前的改动——不得计入
        Policy(key="npc_default_wage_sc", value="7", tier="simple_majority",
               procedure="civic_poll", group="fiscal", version=2,
               updated_by="poll:p0", created_at=started - timedelta(days=30),
               updated_at=started - timedelta(days=20)),
        # 任内经公决**通过**的财政议案(won 落在赞成项上,
        # 与 civic_service._close_one 的落库形状一致)
        Poll(question="镇务征询:把税率提到 0.2",
             options_json=[
                 {"label": "赞成", "won": True, "final_votes": 5, "effect": {
                     "type": "policy", "key": "tax_rate", "value": 0.2}},
                 {"label": "反对", "effect": None},
             ],
             closes_at=started + timedelta(days=1), status="closed"),
        # 财政议案但被否决:赞成项照样带着财政 effect,只是没赢——不得计入,
        # 否则「任内通过的财政议案」会把加税失败也算成政绩
        Poll(question="镇务征询:把税率提到 0.9",
             options_json=[
                 {"label": "赞成", "effect": {
                     "type": "policy", "key": "tax_rate", "value": 0.9}},
                 {"label": "反对", "won": True, "final_votes": 9,
                  "effect": None},
             ],
             closes_at=started + timedelta(days=1, hours=1), status="closed"),
        # 非财政议案——不得计入
        Poll(question="镇务征询:在南苑空地兴建一座邮局",
             options_json=[{"label": "赞成", "won": True, "effect": {
                 "type": "dynamic_location", "data": {"slug": "post_office"}}}],
             closes_at=started + timedelta(days=2), status="closed"),
    ])
    await db_session.commit()

    payload = await office_audit.collect_fiscal_audit(
        db_session, office_key="mayor", holder_slug="ex-mayor",
        term_started_at=started, term_ended_at=ended,
    )

    assert payload["schema_version"] == office_audit.AUDIT_SCHEMA_VERSION
    assert payload["office_key"] == "mayor"
    assert payload["fill_strategy"] == "election"
    assert payload["holder_slug"] == "ex-mayor"
    assert payload["town_balance_sc_end"] == 250
    assert payload["holder_balance_sc_end"] == 88
    assert payload["mayor_wage_multiplier"] == settings.election_mayor_wage_bonus
    assert [c["key"] for c in payload["fiscal_policy_changes"]] == ["tax_rate"]
    assert payload["fiscal_policy_changes"][0]["version"] == 3
    # 只认赢的那一项:被否决的加税提案(0.9 那张)不得计入
    assert payload["fiscal_polls_passed"] == 1
    assert payload["fiscal_poll_questions"] == ["镇务征询:把税率提到 0.2"]
    # 任期长度按世界日,不是真实日(k=4 → 5 真实日 = 20 世界日)
    assert payload["term_world_days"] == pytest.approx(
        5 * settings.world_clock_k, abs=0.01)
    assert payload["truncated"] is False


@pytest.mark.anyio
async def test_collect_fiscal_audit_never_writes(db_session):
    """只读汇总:跑一次审计不得改动任何余额。"""
    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=333),
        ResidentTreasury(resident_slug="ex-mayor", balance_sc=44),
    ])
    await db_session.commit()

    await office_audit.collect_fiscal_audit(
        db_session, office_key="mayor", holder_slug="ex-mayor",
        term_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    assert (await db_session.execute(
        select(TownTreasury.balance_sc).where(TownTreasury.key == TOWN_KEY)
    )).scalar_one() == 333
    assert (await db_session.execute(
        select(ResidentTreasury.balance_sc)
        .where(ResidentTreasury.resident_slug == "ex-mayor")
    )).scalar_one() == 44


@pytest.mark.anyio
async def test_record_and_list_term_audit(db_session):
    started = datetime.now(UTC) - timedelta(days=3)
    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=120),
    ])
    await db_session.commit()

    payload = await office_audit.record_term_audit(
        db_session, office_key="mayor", holder_slug="ex-mayor",
        term_started_at=started,
    )
    assert payload is not None

    key = office_audit.audit_key("mayor", "ex-mayor", started)
    row = (await db_session.execute(
        select(SystemConfig).where(SystemConfig.key == key)
    )).scalar_one()
    assert row.group == office_audit.AUDIT_GROUP
    assert row.updated_by == "office_term_audit"
    assert len(row.value) <= 2000
    assert json.loads(row.value)["holder_slug"] == "ex-mayor"

    listed = await office_audit.list_term_audits(db_session, office_key="mayor")
    assert len(listed) == 1
    assert listed[0]["holder_slug"] == "ex-mayor"
    assert await office_audit.list_term_audits(
        db_session, office_key="postman") == []


@pytest.mark.anyio
async def test_record_term_audit_without_holder_is_noop(db_session):
    assert await office_audit.record_term_audit(
        db_session, office_key="mayor", holder_slug=None,
        term_started_at=None) is None
    assert (await db_session.execute(
        select(SystemConfig).where(
            SystemConfig.group == office_audit.AUDIT_GROUP)
    )).scalars().all() == []


def test_fit_keeps_payload_under_system_config_limit():
    """value 是 String(2000) 且 ConfigService 用 ensure_ascii=True 序列化:
    汉字每字 6 字节,中文议案标题是真正会撑爆列宽的部分。"""
    payload = {
        "schema_version": 1,
        "office_key": "mayor",
        "holder_slug": "he-qiaoyun",
        "fiscal_policy_changes": [
            {"key": "tax_rate", "value": "0.25", "version": i,
             "updated_by": f"poll:{i}",
             "updated_at": datetime.now(UTC).isoformat()}
            for i in range(60)
        ],
        "fiscal_poll_questions": ["镇务征询:关于税率的第若干号提案" * 6] * 8,
    }
    out = office_audit._fit(payload)
    assert len(json.dumps(out)) <= 2000
    assert out["truncated"] is True
    assert len(out["fiscal_policy_changes"]) <= office_audit._MAX_POLICY_CHANGES
    assert len(out["fiscal_poll_questions"]) <= office_audit._MAX_POLL_QUESTIONS
    # 不可裁剪的标识字段必须原样保留
    assert out["office_key"] == "mayor"
    assert out["holder_slug"] == "he-qiaoyun"


def test_audit_key_fits_system_config_key_column():
    key = office_audit.audit_key(
        "mayor", "x" * 200, datetime(2026, 7, 27, 3, 4, 5, tzinfo=UTC))
    assert len(key) <= 200
    assert key.startswith("office_audit:mayor:")
    assert key.endswith("20260727T030405")
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_term_audit.py -v -p no:randomly
```

Expected: 收集阶段 FAIL，报错形如
`ModuleNotFoundError: No module named 'app.tasks.office_audit'`。

- [ ] **Step 3: 写最小实现**

创建 `backend/app/tasks/office_audit.py`：

```python
"""F3 卸任财政审计 — a read-only fiscal summary of an official's term.

Written when an office is vacated (term expiry today; F2's revocation wires
in at 收口). The record NEVER touches an account: every number here is a
SELECT, and the only write is this module's own ``system_config`` row.

The town has no ledger table by design — ``transactions.user_id`` is a hard
``users.id`` FK, so a synthetic town account cannot be a ledger row (see
``app/models/town_treasury.py``). The auditable surface is therefore exactly
what S1-5 left behind (balances + ``updated_at`` + the ``town_last_spend_at``
stamp) plus the S2-5 fiscal policy rows and the civic polls that moved them.

Storage is a ``system_config`` row (group ``office_audit``): F3 ships no
migration this batch, and "scalar policy state lives in system_config" is the
established S1-5 pattern. ``system_config.value`` is ``String(2000)`` and
``ConfigService.set`` serializes with ``json.dumps`` (ensure_ascii=True → one
CJK char costs 6 bytes), so every payload goes through :func:`_fit` first.

Fail-open throughout: an audit hiccup must never break the vacate that
triggered it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.office import Office
from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)

#: system_config group every term-audit row is filed under.
AUDIT_GROUP = "office_audit"
#: key shape — ``office_audit:<office_key>:<slug[:60]>:<YYYYmmddTHHMMSS>``.
AUDIT_KEY_PREFIX = "office_audit"
#: payload shape version (bump when a field changes meaning).
AUDIT_SCHEMA_VERSION = 1
#: json.dumps ceiling; system_config.value is String(2000) — leave headroom.
_VALUE_LIMIT = 1900
#: caps on the two unbounded lists in the payload.
_MAX_POLICY_CHANGES = 12
_MAX_POLL_QUESTIONS = 3
#: per-field truncation so one pathological row cannot eat the whole budget.
_MAX_VALUE_CHARS = 64
_MAX_QUESTION_CHARS = 60


def _as_utc(dt: datetime | None) -> datetime | None:
    """UTC-aware coercion. Naive datetimes are assumed UTC (how the DB stores
    them), matching ``world_clock._as_zone``'s assumption."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def _rollback_quietly(db: AsyncSession) -> None:
    """Roll back after a swallowed failure so the caller's session stays
    usable. Never raises — a fail-open path may not explode on its way out."""
    try:
        await db.rollback()
    except Exception:
        logger.warning("rollback after a swallowed audit failure also failed",
                       exc_info=True)


def audit_key(
    office_key: str, holder_slug: str, term_started_at: datetime | None,
) -> str:
    """The system_config key for one term audit. ``SystemConfig.key`` is
    ``String(200)``; the slug is clamped so a 100-char slug cannot overflow
    it (the full slug still travels inside the payload)."""
    stamp = (_as_utc(term_started_at) or datetime.now(UTC)).strftime(
        "%Y%m%dT%H%M%S")
    return f"{AUDIT_KEY_PREFIX}:{office_key}:{holder_slug[:60]}:{stamp}"[:200]


def _fit(payload: dict) -> dict:
    """Shrink the payload until ``json.dumps`` fits system_config.value.

    Measured exactly the way ``ConfigService.set`` will serialize it (default
    ``ensure_ascii=True``) — the Chinese poll questions are the part that
    actually blows the 2000-char column, and they cost 6 chars each escaped.
    """
    out = dict(payload)
    changes = list(out.get("fiscal_policy_changes") or [])
    questions = list(out.get("fiscal_poll_questions") or [])
    truncated = bool(out.get("truncated"))
    if len(changes) > _MAX_POLICY_CHANGES:
        changes, truncated = changes[:_MAX_POLICY_CHANGES], True
    if len(questions) > _MAX_POLL_QUESTIONS:
        questions, truncated = questions[:_MAX_POLL_QUESTIONS], True
    out["fiscal_policy_changes"] = changes
    out["fiscal_poll_questions"] = questions
    out["truncated"] = truncated
    while len(json.dumps(out)) > _VALUE_LIMIT:
        if out["fiscal_poll_questions"]:
            out["fiscal_poll_questions"] = out["fiscal_poll_questions"][:-1]
        elif out["fiscal_policy_changes"]:
            out["fiscal_policy_changes"] = out["fiscal_policy_changes"][:-1]
        else:
            break
        out["truncated"] = True
    return out


async def _fiscal_policy_changes(
    db: AsyncSession, started: datetime | None, ended: datetime,
) -> list[dict]:
    """S2-5 fiscal policy rows whose ``updated_at`` falls inside the term.

    Fail-open on a world whose ``policies`` table predates S2-5.
    """
    from app.models.policy import Policy
    from app.services.policy_service import FISCAL_POLICY_KEYS
    try:
        rows = (await db.execute(
            select(Policy).where(Policy.key.in_(sorted(FISCAL_POLICY_KEYS)))
        )).scalars().all()
    except Exception:
        logger.warning("office audit: policies unreadable", exc_info=True)
        return []
    out: list[dict] = []
    for p in rows:
        ts = _as_utc(p.updated_at)
        if ts is None or ts > ended:
            continue
        if started is not None and ts < started:
            continue
        out.append({
            "key": p.key,
            "value": str(p.value)[:_MAX_VALUE_CHARS],
            "version": int(p.version or 0),
            "updated_by": (p.updated_by or "")[:_MAX_VALUE_CHARS],
            "updated_at": ts.isoformat(),
        })
    out.sort(key=lambda e: e["updated_at"])
    return out


async def _fiscal_polls(
    db: AsyncSession, started: datetime | None, ended: datetime,
) -> tuple[int, list[str]]:
    """Civic polls closed inside the term whose WINNING option carried a fiscal
    effect. Returns (count, up to _MAX_POLL_QUESTIONS questions).

    Only the winner counts, and that is not pedantry: a fiscal referendum is
    shaped ``[{"label":"赞成","effect":{...}}, {"label":"反对","effect":None}]``
    (``policy_service._open_amend_poll``), so scanning *every* option would
    count每一次被否决的加税提案 as fiscal activity — a line reading
    「任内经公决通过的财政议案 = 3」 would then be true of a mayor who never
    passed a single one. ``civic_service._close_one`` stamps
    ``opts[win]["won"] = True`` on the executed option (and deliberately does
    NOT stamp it when a tier-governed poll 流会), which is the same marker
    ``routers/townhall.py`` already reads — so "did it actually pass" is one
    predicate, not a guess.
    """
    from app.models.season import Poll
    from app.services.policy_service import FISCAL_POLICY_KEYS
    try:
        rows = (await db.execute(
            select(Poll).where(Poll.status == "closed")
        )).scalars().all()
    except Exception:
        logger.warning("office audit: polls unreadable", exc_info=True)
        return 0, []
    hits: list[str] = []
    for p in rows:
        ts = _as_utc(p.closes_at)
        if ts is None or ts > ended:
            continue
        if started is not None and ts < started:
            continue
        for opt in (p.options_json or []):
            if not (opt or {}).get("won"):
                continue
            effect = (opt or {}).get("effect") or {}
            if (effect.get("type") in ("policy", "system_config")
                    and effect.get("key") in FISCAL_POLICY_KEYS):
                hits.append(str(p.question or "")[:_MAX_QUESTION_CHARS])
                break
    return len(hits), hits[:_MAX_POLL_QUESTIONS]


async def collect_fiscal_audit(
    db: AsyncSession, *,
    office_key: str,
    holder_slug: str,
    term_started_at: datetime | None,
    term_ended_at: datetime | None = None,
) -> dict:
    """Read-only fiscal summary of ``holder_slug``'s term in ``office_key``.

    Pure SELECTs — this function must never write. ``term_ended_at`` defaults
    to now (the vacate instant).
    """
    from app import world_clock
    from app.config import settings
    from app.models.town_treasury import TOWN_KEY, TownTreasury
    from app.services import coin_service, treasury_service
    from app.services.config_service import ConfigService

    started = _as_utc(term_started_at)
    ended = _as_utc(term_ended_at) or datetime.now(UTC)

    strategy = (await db.execute(
        select(Office.fill_strategy).where(Office.office_key == office_key)
    )).scalar_one_or_none() or ""

    town_balance = await treasury_service.balance(db)
    town_updated = _as_utc((await db.execute(
        select(TownTreasury.updated_at).where(TownTreasury.key == TOWN_KEY)
    )).scalar_one_or_none())

    try:
        last_spend = await ConfigService(db).get(treasury_service.LAST_SPEND_KEY)
    except Exception:
        logger.warning("office audit: town_last_spend_at unreadable",
                       exc_info=True)
        last_spend = None

    holder_balance = await coin_service.treasury_balance(db, holder_slug)

    term_world_days = None
    if started is not None:
        try:
            span = (world_clock.real_to_world(ended)
                    - world_clock.real_to_world(started))
            term_world_days = round(span.total_seconds() / 86400.0, 3)
        except Exception:
            logger.warning("office audit: world-day conversion failed",
                           exc_info=True)

    changes = await _fiscal_policy_changes(db, started, ended)
    polls_passed, poll_questions = await _fiscal_polls(db, started, ended)

    return _fit({
        "schema_version": AUDIT_SCHEMA_VERSION,
        "office_key": office_key,
        "fill_strategy": strategy,
        "holder_slug": holder_slug,
        "term_started_at": started.isoformat() if started else None,
        "term_ended_at": ended.isoformat(),
        "term_world_days": term_world_days,
        "town_balance_sc_end": int(town_balance),
        "town_treasury_updated_at": (
            town_updated.isoformat() if town_updated else None),
        "town_last_spend_at": last_spend if isinstance(last_spend, str) else None,
        "holder_balance_sc_end": int(holder_balance),
        "mayor_wage_multiplier": (
            float(settings.election_mayor_wage_bonus)
            if office_key == "mayor" and settings.election_enabled else 1.0
        ),
        "fiscal_policy_changes": changes,
        # 名字与语义严格对齐:计的是「任内经公决**通过**的财政议案」,
        # 不是「任内出现过的财政议案」——审计要的是前者。
        "fiscal_polls_passed": polls_passed,
        "fiscal_poll_questions": poll_questions,
        "generated_at": datetime.now(UTC).isoformat(),
    })


async def record_term_audit(
    db: AsyncSession, *,
    office_key: str,
    holder_slug: str | None,
    term_started_at: datetime | None,
    term_ended_at: datetime | None = None,
) -> dict | None:
    """Collect + persist one term audit; returns the stored payload.

    None means there was nothing to audit (no holder) or the write failed —
    fail-open, because an audit must never turn a completed vacate into an
    exception.

    F2 contract: call this on the revocation path AFTER the office row was
    vacated, passing the holder and ``term_started_at`` read BEFORE the UPDATE
    (``OfficeService.vacate(..., audit=True)`` does exactly that for you).
    """
    if not office_key or not holder_slug:
        return None
    try:
        payload = await collect_fiscal_audit(
            db, office_key=office_key, holder_slug=holder_slug,
            term_started_at=term_started_at, term_ended_at=term_ended_at,
        )
        from app.services.config_service import ConfigService
        await ConfigService(db).set(
            audit_key(office_key, holder_slug, term_started_at),
            payload, group=AUDIT_GROUP, updated_by="office_term_audit",
        )
        return payload
    except Exception:
        logger.warning("office term audit failed for %s/%s",
                       office_key, holder_slug, exc_info=True)
        # Fail-open has to cover the SESSION, not just the return value.
        # ConfigService.set writes and commits, so the exception can come out
        # of a flush/commit and leave the session in the needs-rollback state —
        # every LATER statement would then raise PendingRollbackError. That is
        # exactly the half-finished shape §4.3 forbids: F2 calls
        # ``vacate("mayor", audit=True)``, gets True back, and then does
        # 改档位 → 写历史行 → 断言 → 广播 on the same session.
        # (The three inner try blocks inside collect_fiscal_audit are pure
        # SELECTs and stay as they are.)
        await _rollback_quietly(db)
        return None


async def list_term_audits(
    db: AsyncSession, *, office_key: str | None = None, limit: int = 20,
) -> list[dict]:
    """Stored term audits, newest term-end first. Read-only."""
    try:
        rows = (await db.execute(
            select(SystemConfig).where(SystemConfig.group == AUDIT_GROUP)
        )).scalars().all()
    except Exception:
        logger.warning("office term audit listing failed", exc_info=True)
        return []
    out: list[dict] = []
    for row in rows:
        if not str(row.key or "").startswith(f"{AUDIT_KEY_PREFIX}:"):
            continue
        try:
            payload = json.loads(row.value)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if office_key and payload.get("office_key") != office_key:
            continue
        out.append(payload)
    out.sort(key=lambda p: str(p.get("term_ended_at") or ""), reverse=True)
    return out[:limit]
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_term_audit.py tests/test_treasury_service.py \
  tests/test_burnin_report_treasury.py -q -p no:randomly
```

Expected: PASS，0 failed。

- [ ] **Step 5: 提交**

> **执行前先把 Step 4 的真实 stdout 粘进 `Verified-by:`。heredoc 里的 `<...>` 是占位符，
> 原样提交 = 假验证记录 = 计划失败**（Task 8 Step 4 有机械门会把它抓出来）。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git add backend/app/tasks/office_audit.py backend/tests/test_office_term_audit.py
git commit -m "$(cat <<'EOF'
feat(office): 新增卸任财政审计模块 office_audit(只读汇总,不改账)

每个数字都是 SELECT,唯一的写是自己那一行 system_config。汇总面:
镇财政期末余额 + updated_at、town_last_spend_at 戳、离任者个人钱包、
任内的 S2-5 财政政策改动(按 FISCAL_POLICY_KEYS 过滤 + 任期窗口)、任内
经公决通过的财政议案计数(只认 _close_one 落库的 won 项,被否决的加税提案不算
政绩)、按 world_clock 换算的任期世界日长度。

存 system_config 而非新表:本线不允许迁移(§5 独占文件无 models/migrations),
且「标量政策状态住 system_config」是 S1-5 既定姿势。
value 是 String(2000) 且 ConfigService 用 ensure_ascii=True 序列化(汉字 6
字节),所以落盘前一律过 _fit;key 是 String(200),slug 段裁到 60 字。

Verified-by: <贴 Step 4 实际输出>
EOF
)"
```

---

## Task 5: 空缺红旗输入 `overdue_vacancies` —— 允许的空缺上限 = 1 个夜间周期

**Files:**
- Modify: `backend/app/tasks/office_audit.py`（追加函数）
- Test: `backend/tests/test_office_term_audit.py`（追加）

**Interfaces:**
- Consumes: 模型 `Office`（`holder_slug` / `updated_at` / `fill_strategy`）
- Produces: `async def overdue_vacancies(db: AsyncSession, *, max_vacant_hours: float = 24.0, strategies: frozenset[str] = frozenset({"election"}), now: datetime | None = None) -> list[dict]`，元素形如 `{"office_key": str, "fill_strategy": str, "vacant_since": iso, "vacant_hours": float}`（**恰好这四个键**，探针要 JSON 序列化），按 `office_key` 升序

> 阈值以关键字默认值给出（24 小时 = 一个夜间周期），**本线不改 `config.py`**；收口时把它改成读 `settings`，并由 `scripts/burnin_report.py` 消费（探针文件不在本线独占清单里，本线只交付被消费的纯函数）。
>
> **只看民选职位（`fill_strategy in strategies`，默认 `{"election"}`），判据与 `trigger_backfill` 同源。**
> 四个职位里只有 `mayor` 有自动回填路径：`trigger_backfill` 自己就用
> `_fill_strategy(...) != "election"` 早退，`town_clerk`/`postman` 是 `seed`、`doctor` 是
> `appointment`（`office_service.py:41-46`），没有任何代码会自动补它们。而迁移 046 seed 出
> 四行、其中 `doctor` 连 backfill 都没有（046 只回填 mayor/town_clerk/postman），生产里它
> 的 `holder_slug` 恒为 NULL、`updated_at` 停在迁移那一刻（既有测试注释直说
> `doctor slot exists, vacant (greenfield state)`，`test_burnin_report_offices.py:125`）。
> 不加这个谓词，探针接上的当晚就会**每晚恒定报 2~3 面红旗**，真正该看的镇长空缺淹没在噪声里。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/test_office_term_audit.py` 末尾追加：

```python
# ── Task 5: 空缺超过一个夜间周期 → 红旗输入 ─────────────────────────

@pytest.mark.anyio
async def test_overdue_vacancies_flags_only_stale_empty_offices(db_session):
    now = datetime.now(UTC)
    db_session.add_all([
        # 空缺 30 小时 → 超过一个夜间周期
        Office(office_key="mayor", institution="town_hall",
               fill_strategy="election", holder_slug=None,
               updated_at=now - timedelta(hours=30)),
        # 同样是民选缺位,但只空了 2 小时 → 还在允许窗口内(时间阈值本身的边界)
        Office(office_key="deputy_mayor", institution="town_hall",
               fill_strategy="election", holder_slug=None,
               updated_at=now - timedelta(hours=2)),
        # 在任 → 无论多久都不算空缺
        Office(office_key="postman", institution="post_office",
               fill_strategy="seed", holder_slug="luo-xiaozhou",
               updated_at=now - timedelta(days=90)),
    ])
    await db_session.commit()

    flags = await office_audit.overdue_vacancies(db_session, now=now)
    assert [f["office_key"] for f in flags] == ["mayor"]
    assert flags[0]["fill_strategy"] == "election"
    assert flags[0]["vacant_hours"] == pytest.approx(30.0, abs=0.05)
    assert flags[0]["vacant_since"] == (now - timedelta(hours=30)).isoformat()

    # 阈值可调:放宽到 48 小时后没有红旗
    assert await office_audit.overdue_vacancies(
        db_session, max_vacant_hours=48.0, now=now) == []


@pytest.mark.anyio
async def test_overdue_vacancies_ignores_labour_offices_vacant_forever(db_session):
    """生产的真实形态:迁移 046 seed 出四行,doctor 连 backfill 都没有,
    holder 恒 NULL、updated_at 停在迁移那一刻。它们没有任何自动回填路径
    (trigger_backfill 只认 fill_strategy == "election"),所以不得成为永久红旗
    ——否则探针每晚恒定 2~3 面红旗,镇长空缺淹没在噪声里。"""
    now = datetime.now(UTC)
    db_session.add_all([
        Office(office_key="doctor", institution="clinic",
               fill_strategy="appointment", holder_slug=None,
               updated_at=now - timedelta(days=90)),
        Office(office_key="postman", institution="post_office",
               fill_strategy="seed", holder_slug=None,
               updated_at=now - timedelta(days=90)),
    ])
    await db_session.commit()

    assert await office_audit.overdue_vacancies(db_session, now=now) == []
    # 显式放开策略白名单时才看得到它们(收口若要扩面,这就是入口)
    widened = await office_audit.overdue_vacancies(
        db_session, strategies=frozenset({"election", "appointment", "seed"}),
        now=now)
    assert [f["office_key"] for f in widened] == ["doctor", "postman"]


@pytest.mark.anyio
async def test_overdue_vacancies_shape_is_probe_consumable(db_session):
    """形状契约:钉死探针收口时会依赖的字段集(本线不接线,只能靠这条测试
    保证交接面不漂)。"""
    now = datetime.now(UTC)
    db_session.add(Office(office_key="mayor", institution="town_hall",
                          fill_strategy="election", holder_slug=None,
                          updated_at=now - timedelta(hours=30)))
    await db_session.commit()

    flag = (await office_audit.overdue_vacancies(db_session, now=now))[0]
    assert set(flag) == {"office_key", "fill_strategy",
                         "vacant_since", "vacant_hours"}
    json.dumps(flag)          # 探针要 JSON 序列化,不许塞 datetime 进去


@pytest.mark.anyio
async def test_overdue_vacancies_empty_world(db_session):
    assert await office_audit.overdue_vacancies(db_session) == []
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_term_audit.py -v -p no:randomly -k overdue
```

Expected: 四条（`flags_only_stale_empty_offices` / `ignores_labour_offices_vacant_forever` / `shape_is_probe_consumable` / `empty_world`）全 FAIL，报错形如 `AttributeError: module 'app.tasks.office_audit' has no attribute 'overdue_vacancies'`。

- [ ] **Step 3: 写最小实现**

在 `backend/app/tasks/office_audit.py` 顶部 import 行把 `timedelta` 加进来：

```python
from datetime import datetime, timedelta, UTC
```

在文件末尾追加：

```python
async def overdue_vacancies(
    db: AsyncSession, *,
    max_vacant_hours: float = 24.0,
    strategies: frozenset[str] = frozenset({"election"}),
    now: datetime | None = None,
) -> list[dict]:
    """Offices that have been vacant longer than one nightly cycle.

    The §5 与 F2 的接口约定 caps an acceptable vacancy at one nightly cycle;
    anything past that is a probe red flag. Vacancy age is read off
    ``offices.updated_at`` — the same signal ``scripts/burnin_report.py``'s
    ``office_occupancy`` already derives it from, so the two never disagree.

    Only offices with an ACTUAL refill path can go red, and the predicate is
    the same one ``trigger_backfill`` early-returns on
    (``_fill_strategy(...) != "election"``). Of the four S2-1 slots only the
    mayor is elected: ``town_clerk``/``postman`` are ``seed`` and ``doctor``
    is ``appointment`` (``OFFICE_DEFS``), nothing in the world ever refills
    them automatically, and migration 046 backfills neither ``doctor``'s
    holder nor its ``updated_at``. Without this filter the probe would raise
    2–3 red flags every single night forever and drown the one vacancy that
    actually means something.

    The threshold is a keyword default rather than a settings knob on purpose:
    F3 ships no ``config.py`` change (共享文件延到收口). Read-only, fail-open.
    """
    ref = _as_utc(now) or datetime.now(UTC)
    cutoff = ref - timedelta(hours=float(max_vacant_hours))
    try:
        rows = (await db.execute(
            select(Office).where(
                Office.holder_slug.is_(None),
                Office.fill_strategy.in_(sorted(strategies)),
            )
        )).scalars().all()
    except Exception:
        logger.warning("office vacancy scan failed", exc_info=True)
        return []
    out: list[dict] = []
    for o in rows:
        ts = _as_utc(o.updated_at)
        if ts is None or ts > cutoff:
            continue
        out.append({
            "office_key": o.office_key,
            "fill_strategy": o.fill_strategy,
            "vacant_since": ts.isoformat(),
            "vacant_hours": round((ref - ts).total_seconds() / 3600.0, 2),
        })
    out.sort(key=lambda e: e["office_key"])
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_term_audit.py tests/test_burnin_report_offices.py \
  -q -p no:randomly
```

Expected: PASS，0 failed。

- [ ] **Step 5: 提交**

> **执行前先把 Step 4 的真实 stdout 粘进 `Verified-by:`。heredoc 里的 `<...>` 是占位符，
> 原样提交 = 假验证记录 = 计划失败**（Task 8 Step 4 有机械门会把它抓出来）。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git add backend/app/tasks/office_audit.py backend/tests/test_office_term_audit.py
git commit -m "$(cat <<'EOF'
feat(office): overdue_vacancies——空缺超过一个夜间周期的红旗输入

§5 与 F2 的接口约定:允许的空缺上限 = 1 个夜间周期,超出由探针报红旗。
本线交付被消费的纯函数(空缺龄取 offices.updated_at,与 burnin_report 的
office_occupancy 同源,不会两处打架);探针文件不在本线独占清单,接线归收口
——收口硬门与「本线未交付红旗上报」的记账见计划末尾接线清单第 3 条。

只看有自动回填路径的职位(fill_strategy in {"election"},与 trigger_backfill
同源判据):四个职位里只有 mayor 是民选,seed/appointment 的三个没有任何代码
会自动补,而 046 seed 出的 doctor 行 holder 恒 NULL——不加这个谓词,探针接上
的当晚起每晚恒定 2~3 面红旗,信号当场变噪声。

阈值用关键字默认值(24h)而不是 settings:共享文件 config.py 本线不改。
另有一条形状契约测试钉死输出字段集(探针要 JSON 序列化),防交接面漂移。

Verified-by: <贴 Step 4 实际输出>
EOF
)"
```

---

## Task 6: 把审计接进出缺路径（`term_check` 自动 / `vacate(audit=True)` 供 F2）

**Files:**
- Modify: `backend/app/services/office_service.py`（`vacate` 增 `audit` 关键字；`term_check` 内接审计）
- Test: `backend/tests/test_office_term_audit.py`（追加）

**Interfaces:**
- Consumes: `office_audit.record_term_audit(db, *, office_key, holder_slug, term_started_at, term_ended_at=None) -> dict | None`（Task 4）
- Produces: `OfficeService.vacate(self, office_key: str, *, audit: bool = False) -> bool`（默认 False → 既有调用方行为逐字节不变）

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/test_office_term_audit.py` 末尾追加：

```python
# ── Task 6: 出缺路径接线 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_term_check_records_audit_for_departing_holder(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=175),
    ])
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1

    audits = await office_audit.list_term_audits(db_session, office_key="mayor")
    assert len(audits) == 1
    assert audits[0]["holder_slug"] == "ex-mayor"
    assert audits[0]["town_balance_sc_end"] == 175
    assert audits[0]["term_started_at"] is not None


@pytest.mark.anyio
async def test_vacate_audits_only_when_asked(db_session):
    """默认 audit=False → 既有调用方(admin/测试/F2 之外的路径)行为不变。"""
    db_session.add(_res("ex-mayor", "前镇长"))
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")
    assert await svc.vacate("mayor") is True
    assert await office_audit.list_term_audits(db_session) == []

    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")
    assert await svc.vacate("mayor", audit=True) is True
    audits = await office_audit.list_term_audits(db_session)
    assert len(audits) == 1
    assert audits[0]["holder_slug"] == "ex-mayor"


@pytest.mark.anyio
async def test_vacate_audit_noop_when_office_already_vacant(db_session):
    """没有真正出缺就不该有审计行(guard UPDATE rowcount==0)。"""
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "someone", fill_strategy="election")
    await svc.vacate("mayor")
    assert await svc.vacate("mayor", audit=True) is False
    assert await office_audit.list_term_audits(db_session) == []


@pytest.mark.anyio
async def test_audit_failure_does_not_break_vacate(db_session, monkeypatch):
    db_session.add(_res("ex-mayor", "前镇长"))
    await db_session.commit()

    async def _boom(db, **kwargs):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(office_audit, "collect_fiscal_audit", _boom)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")
    assert await svc.vacate("mayor", audit=True) is True
    assert await svc.get_holder("mayor") is None


@pytest.mark.anyio
async def test_audit_write_failure_leaves_session_usable(db_session, monkeypatch):
    """上一条的 _boom 进门就 raise,session 从没脏过,加不加 rollback 都会绿。
    真实故障形状是异常来自落盘那一步的 flush/commit(ConfigService.set 内部
    有 db.commit()):那时 session 停在 needs-rollback 状态,后续任何语句都抛
    PendingRollbackError——F2 拿到 vacate 的 True 之后还要在同一个 session 上
    改档位 / 写历史行 / 断言 / 广播,那些写会全军覆没(spec §4.3 要防的半途状态)。
    """
    from app.services.config_service import ConfigService

    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=10),
        # 它的主键待会儿被拿去制造 flush 期冲突(SystemConfig.key 是 PK)
        SystemConfig(key="probe-dup", value="1", group="probe",
                     updated_by="test"),
    ])
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")

    async def _boom_in_flush(self, key, value, *, group, updated_by):
        self._db.add(SystemConfig(key="probe-dup", value="2", group="probe",
                                  updated_by="test"))
        await self._db.flush()    # IntegrityError:主键冲突,炸在 flush 里

    monkeypatch.setattr(ConfigService, "set", _boom_in_flush)
    assert await svc.vacate("mayor", audit=True) is True
    # 关键断言:session 仍可用。没有 rollback 这里会抛 PendingRollbackError。
    assert await svc.get_holder("mayor") is None
    assert await office_audit.list_term_audits(db_session) == []
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_term_audit.py -v -p no:randomly \
  -k "records_audit or audits_only_when_asked or audit_noop or audit_failure or audit_write_failure"
```

Expected: FAIL，报错形如 `TypeError: OfficeService.vacate() got an unexpected keyword argument 'audit'`（含新增的 `test_audit_write_failure_leaves_session_usable`），以及 `assert 0 == 1`（term_check 没写审计行）。

- [ ] **Step 3: 写最小实现**

`backend/app/services/office_service.py` —— 把 `vacate` 替换为（在 Task 1 版本上增加 `audit` 关键字与任期起点捕获）：

```python
    async def vacate(self, office_key: str, *, audit: bool = False) -> bool:
        """Clear the office holder + term end. Guard UPDATE — returns True
        only when an actual holder was cleared (idempotent no-op otherwise).

        ``audit=True`` additionally files a read-only term audit for the
        departing holder (F2's revocation path uses it; default False keeps
        every existing caller byte-identical).

        The pre-read is NOT a guard (the UPDATE's rowcount still decides): it
        captures who is leaving and when the term began, because both the
        legacy-store cleanup and the audit are keyed on that identity.
        """
        prior_row = (await self.db.execute(
            select(Office).where(Office.office_key == office_key)
        )).scalar_one_or_none()
        prior_holder = prior_row.holder_slug if prior_row is not None else None
        prior_started = prior_row.term_started_at if prior_row is not None else None
        res = await self.db.execute(
            update(Office)
            .where(Office.office_key == office_key, Office.holder_slug.isnot(None))
            .values(holder_slug=None, term_ends_at=None,
                    updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        vacated = (res.rowcount or 0) > 0
        if vacated and office_key == "mayor":
            await self._clear_mayor_legacy_stores(holder_slug=prior_holder)
        await self.db.commit()
        if vacated:
            await self._emit_office_changed(
                "office_vacated", office_key, holder_slug=None,
            )
            if audit:
                from app.tasks import office_audit
                await office_audit.record_term_audit(
                    self.db, office_key=office_key, holder_slug=prior_holder,
                    term_started_at=prior_started,
                )
        return vacated
```

`term_check` 的循环体：在 `prior_holder = office.holder_slug` 之后加一行捕获任期起点，并在 `_emit_office_changed` 之后、`trigger_backfill` 之前插入审计调用：

```python
            office_key = office.office_key
            prior_holder = office.holder_slug
            prior_started = office.term_started_at
```

```python
            await self._emit_office_changed(
                "office_vacated", office_key, holder_slug=None,
            )
            # F3: 卸任财政审计 — read-only, fail-open, and chronologically
            # before the backfill (it summarises the term that just ended).
            from app.tasks import office_audit
            await office_audit.record_term_audit(
                self.db, office_key=office_key, holder_slug=prior_holder,
                term_started_at=prior_started, term_ended_at=now,
            )
            # F3: the second half. trigger_backfill is fail-open internally,
            # so a broken election can never turn a completed vacate into an
            # exception. It runs AFTER the legacy stores were cleared above —
            # that ordering is what makes the vacancy visible to it.
            await trigger_backfill(
                self.db, office_key, reason=REASON_TERM_EXPIRED,
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_term_audit.py tests/test_office_backfill.py \
  tests/test_office_vacancy_sweep.py tests/test_office_service.py \
  tests/test_office_integration.py tests/test_burnin_report_offices.py \
  -q -p no:randomly
```

Expected: PASS，0 failed。

- [ ] **Step 5: 提交**

> **执行前先把 Step 4 的真实 stdout 粘进 `Verified-by:`。heredoc 里的 `<...>` 是占位符，
> 原样提交 = 假验证记录 = 计划失败**（Task 8 Step 4 有机械门会把它抓出来）。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git add backend/app/services/office_service.py backend/tests/test_office_term_audit.py
git commit -m "$(cat <<'EOF'
feat(office): 出缺即审计——term_check 自动落审计,vacate 增 audit=True 供 F2

term_check 在真实出缺后、开补选之前落一行卸任审计(时序上先总结刚结束的任期)。
vacate 增关键字 audit,默认 False:既有调用方(admin 端点/既有测试)行为逐字节不变;
F2 的撤销一次调用 vacate("mayor", audit=True) 即拿到「卸职 + 清遗留 + 审计」。

vacate 的预读顺带取 term_started_at——审计要的任期窗口只有 UPDATE 之前读得到。
审计整段 fail-open,且 fail-open 覆盖 session:ConfigService.set 内部有 commit,
异常来自 flush/commit 时不 rollback 的话,F2 拿到 vacate 的 True 之后在同一个
session 上做的「改档位 → 写历史行 → 断言 → 广播」会全部抛 PendingRollbackError
——正是 §4.3「撤销是有序复合事务」要防的半途状态。

Verified-by: <贴 Step 4 实际输出>
EOF
)"
```

---

## Task 7: gate 开与关两态矩阵 —— 正确性不得依赖 `polis_office_enabled`

**Files:**
- Modify: `backend/tests/test_office_backfill.py`（追加矩阵测试）
- Modify: `backend/app/services/office_service.py`（`_fill_strategy` 加 `OFFICE_DEFS` 回落；`_effective_holder` 对 mayor 改走 gate-aware 的 `current_mayor`）

**Interfaces:**
- Consumes: `election_service.current_mayor(db) -> str | None`（gate-aware：`polis_office_enabled` 开时读 offices，否则/回落读 `system_config['current_mayor']`）、`OFFICE_DEFS`（`office_service.py:41`，`OFFICE_DEFS["mayor"]["fill_strategy"] == "election"`）
- Produces: `_fill_strategy` / `_effective_holder` 的最终形态（签名不变，语义变为 gate 无关）

> Task 2 落的是基础语义（用 `offices` 行判在任、缺行即视为非民选职位）。这一版在
> 生产的两种真实形态下都是错的，本 Task 先让它真红，再改成 gate 无关。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/test_office_backfill.py` 末尾追加：

```python
# ── Task 7: gate 开/关两态矩阵 ──────────────────────────────────────

@pytest.mark.anyio
async def test_backfill_works_with_gate_off_and_no_office_row(db_session, monkeypatch):
    """gate 关时迁移 046 的 seed 可能从未跑过:offices 里根本没有 mayor 行。
    「没有行」不等于「不是民选职位」——必须回落 OFFICE_DEFS。"""
    monkeypatch.setattr(settings, "polis_office_enabled", False)
    await _seed_voters(db_session)
    assert (await db_session.execute(select(Office))).scalars().all() == []

    poll_id = await trigger_backfill(db_session, "mayor", reason=REASON_MANUAL)
    assert poll_id
    assert len(await _open_election_polls(db_session)) == 1


@pytest.mark.anyio
async def test_backfill_ignores_stale_migration046_holder_when_gate_off(
    db_session, monkeypatch,
):
    """gate 关时 offices 里可能留着迁移 046 的陈旧 holder_slug,但没有任何
    业务路径认它。判「是否在任」必须走 gate-aware 的 current_mayor,否则被
    陈旧行骗过、永远不补选。"""
    monkeypatch.setattr(settings, "polis_office_enabled", False)
    await _seed_voters(db_session)
    db_session.add(Office(
        office_key="mayor", institution="town_hall", fill_strategy="election",
        holder_slug="stale-046-holder",
    ))
    await db_session.commit()

    poll_id = await trigger_backfill(db_session, "mayor", reason=REASON_MANUAL)
    assert poll_id
    assert len(await _open_election_polls(db_session)) == 1


@pytest.mark.anyio
async def test_backfill_respects_legacy_mayor_when_gate_off(db_session, monkeypatch):
    """gate 关时唯一权威是 system_config['current_mayor'];它有人就是有人。"""
    from app.services.config_service import ConfigService

    monkeypatch.setattr(settings, "polis_office_enabled", False)
    await _seed_voters(db_session)
    await ConfigService(db_session).set(
        "current_mayor", "a", group="civic", updated_by="test")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_MANUAL) is None
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
async def test_backfill_respects_offices_holder_when_gate_on(db_session, monkeypatch):
    """gate 开时 offices 是权威:有人在任就不补选。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    await _seed_voters(db_session)
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")

    assert await trigger_backfill(db_session, "mayor", reason=REASON_MANUAL) is None
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
@pytest.mark.parametrize("gate_on", [True, False])
async def test_zero_term_days_has_no_auto_recall_under_either_gate(
    db_session, monkeypatch, gate_on,
):
    """polis_office_mayor_term_days = 0 = 无限任期:两种 gate 态下都没有自动
    收回路径,term_check 什么都不做,撤销是唯一的下台方式(§5 硬门备注)。"""
    monkeypatch.setattr(settings, "polis_office_enabled", gate_on)
    monkeypatch.setattr(settings, "polis_office_mayor_term_days", 0)
    await _seed_voters(db_session)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election",
                      term_days=settings.polis_office_mayor_term_days)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=10000)) == 0
    assert await svc.get_holder("mayor") == "a"
    assert await _open_election_polls(db_session) == []


@pytest.mark.anyio
@pytest.mark.parametrize("gate_on", [True, False])
async def test_revocation_style_vacate_then_backfill_under_either_gate(
    db_session, monkeypatch, gate_on,
):
    """F2 的撤销形态回放:清干净两个遗留存储后调钩子,两种 gate 态都要补选。"""
    from app.services.config_service import ConfigService

    monkeypatch.setattr(settings, "polis_office_enabled", gate_on)
    await _seed_voters(db_session)
    await ConfigService(db_session).set(
        "current_mayor", "a", group="civic", updated_by="test")

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election")
    assert await svc.vacate("mayor", audit=True) is True   # 清 holder + 两个遗留存储

    poll_id = await trigger_backfill(
        db_session, "mayor", reason=REASON_CIVIC_REVOCATION)
    assert poll_id
    assert len(await _open_election_polls(db_session)) == 1
```

- [ ] **Step 2: 跑测试确认它失败**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_backfill.py -v -p no:randomly
```

Expected: 恰好两条 FAIL：

- `test_backfill_works_with_gate_off_and_no_office_row` —— `assert poll_id` 失败（`AssertionError: assert None`）。Task 2 的 `_fill_strategy` 在 offices 无行时返回 `""`，把「046 seed 没跑」误读成「不是民选职位」。
- `test_backfill_ignores_stale_migration046_holder_when_gate_off` —— `assert poll_id` 失败（`AssertionError: assert None`）。Task 2 的 `_effective_holder` 直接读 offices，被迁移 046 遗留的 `stale-046-holder` 骗成「仍在任」。

其余四条（含两组 `parametrize`）应当直接 PASS——它们是防止过度矫正的对照组，
尤其 `test_backfill_respects_legacy_mayor_when_gate_off`：加了 OFFICE_DEFS 回落
却忘了 gate-aware 在任判定时，它会翻红。

- [ ] **Step 3: 写最小实现**

`backend/app/services/office_service.py` —— 把 Task 2 落的两个私有函数替换为最终形态：

```python
async def _fill_strategy(db: AsyncSession, office_key: str) -> str:
    """The office's refill procedure.

    Falls back to OFFICE_DEFS when the row is missing: with
    ``polis_office_enabled`` off the migration-046 seed may never have run in
    this world, and "no row" must not be read as "not an elected office".
    """
    try:
        row = (await db.execute(
            select(Office.fill_strategy).where(Office.office_key == office_key)
        )).scalar_one_or_none()
    except Exception:
        logger.warning("offices fill_strategy lookup failed: %s", office_key,
                       exc_info=True)
        row = None
    if row:
        return str(row)
    return str(OFFICE_DEFS.get(office_key, {}).get("fill_strategy") or "")


async def _effective_holder(db: AsyncSession, office_key: str) -> str | None:
    """Who effectively holds ``office_key`` right now, under EITHER gate state.

    For the mayor this must NOT be a raw ``offices`` read: correctness may not
    depend on ``polis_office_enabled``. With the gate off the offices row can
    be absent entirely, or carry a stale migration-046 holder_slug that no
    business path honours. ``election_service.current_mayor`` is the one read
    that already encodes both worlds (offices when the gate is on, then the
    ``system_config['current_mayor']`` fallback).
    """
    if office_key == "mayor":
        from app.services import election_service
        return await election_service.current_mayor(db)
    return await OfficeService(db).get_holder(office_key)
```

并把 `trigger_backfill` 的 docstring 末尾补上调用时序约束（F2 的计划引用这一段）：

```python
    CALL ORDER (F2 contract): call this only after both legacy mayor stores
    (``meta_json['mayor']`` and ``system_config['current_mayor']``) have been
    cleared. ``_effective_holder`` reads the fallback on purpose, so calling
    too early reports "still occupied" and silently skips the backfill.
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest \
  tests/test_office_backfill.py tests/test_office_term_audit.py \
  tests/test_office_vacancy_sweep.py tests/test_office_service.py \
  tests/test_office_integration.py tests/test_m6_election.py \
  -q -p no:randomly
```

Expected: PASS，0 failed（含两组 `parametrize` 的 gate 开/关各一份）。

- [ ] **Step 5: 提交**

> **执行前先把 Step 4 的真实 stdout 粘进 `Verified-by:`。heredoc 里的 `<...>` 是占位符，
> 原样提交 = 假验证记录 = 计划失败**（Task 8 Step 4 有机械门会把它抓出来）。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git add backend/app/services/office_service.py backend/tests/test_office_backfill.py
git commit -m "$(cat <<'EOF'
fix(office): 补选正确性与 polis_office_enabled 解耦——gate 开/关两态矩阵

上一版用 offices 行判在任、缺行即视为非民选职位。生产的两种真实形态下都是错的,
两条矩阵测试先真红:
- gate 关 + offices 无 mayor 行(046 seed 没跑)→ 被误读成「不是民选职位」
- gate 关 + offices 有 046 遗留陈旧 holder_slug → 被骗成「仍在任」,永不补选

修法:
- _fill_strategy 缺行时回落 OFFICE_DEFS
- _effective_holder 对 mayor 改走 election_service.current_mayor,它已编码
  两种 gate 态(gate 开读 offices,回落 system_config['current_mayor'])

另两条对照组防过度矫正(gate 关 + system_config 有人 / gate 开 + offices 有人
都必须判在任),外加两组 parametrize(gate 开/关):
- term_days=0 两态都没有自动收回路径(§5 硬门备注),term_check 恒 0
- F2 撤销形态回放:清干净遗留存储后调钩子,两态都补选

Verified-by: <贴 Step 4 实际输出>
EOF
)"
```

---

## Task 8: 全量回归 + 真实进程运行时验证（verify-before-done）

**Files:**
- Create: `/tmp/f3-runtime-check.py`（一次性验证脚本，**不进仓库**）
- Create: `/tmp/f3-after.txt`（收工失败集）

**Interfaces:**
- Consumes: `app.database.async_session`（真实全局 session 工厂）、`OfficeService.term_check`、`office_audit.list_term_audits` / `overdue_vacancies`、`scripts/burnin_report.py` 的 `fetch_office_snapshot` / `office_occupancy`
- Produces: 运行时证据文本；`/tmp/f3-after.txt` 与 `/tmp/f3-base.txt` 的双向差集必须为空

- [ ] **Step 1: 跑全量并做双向差集（硬门）**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/ -q -p no:randomly 2>&1 \
  | tee /tmp/f3-after-raw.txt \
  | grep -E "^(FAILED|ERROR) " | awk '{print $1, $2}' | sort -u > /tmp/f3-after.txt
echo "=== 新增失败(必须为空) ==="; comm -13 /tmp/f3-base.txt /tmp/f3-after.txt
echo "=== 被修复的既有失败(允许非空) ==="; comm -23 /tmp/f3-base.txt /tmp/f3-after.txt
tail -3 /tmp/f3-after-raw.txt
```

Expected: 「新增失败」一节为空。**数量相同不算通过，必须是 `comm -13` 空集。**
抽取管道与 Task 0 Step 4 必须逐字一致（同样禁 `sed 's/\[.*//'`）：两边口径不同的话
`comm` 比的就不是同一个集合。

- [ ] **Step 2: 写运行时验证脚本**

创建 `/tmp/f3-runtime-check.py`：

```python
"""F3 运行时验证:真实进程 + 真实 DB 引擎 + 真实服务代码,零 mock。

走的是夜间 cron 里那一段的真身(nightly_cron.py:258-267 的块体):
    if settings.polis_office_enabled:
        async with async_session() as db:
            n = await OfficeService(db).term_check()
用法:
    DATABASE_URL=sqlite+aiosqlite:////tmp/f3_runtime.db \
    DEBUG=true LLM_API_KEY=dummy \
    /Volumes/data/dev/simverse-world/backend/.venv/bin/python /tmp/f3-runtime-check.py
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, UTC


async def main() -> int:
    from app.config import settings
    from app.database import Base, async_session, engine
    from app.models.office import Office
    from app.models.resident import Resident
    from app.models.season import Poll
    from app.models.town_treasury import TOWN_KEY, TownTreasury
    from app.services.election_service import ELECTION_TAG
    from app.services.office_service import OfficeService
    from app.tasks import office_audit
    from sqlalchemy import select

    settings.polis_office_enabled = True
    settings.polis_office_mayor_term_days = 8

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def _res(slug, name):
        return Resident(slug=slug, name=name, district="central_plaza",
                        status="idle", resident_type="npc", creator_id="sys",
                        tile_x=70, tile_y=56, meta_json=None)

    async with async_session() as db:
        for slug, name in (("he-qiaoyun", "何巧云"), ("jiang-lin", "江林"),
                           ("zhao-qiwen", "赵启文")):
            if (await db.execute(
                select(Resident).where(Resident.slug == slug)
            )).scalar_one_or_none() is None:
                db.add(_res(slug, name))
        if (await db.execute(
            select(TownTreasury).where(TownTreasury.key == TOWN_KEY)
        )).scalar_one_or_none() is None:
            db.add(TownTreasury(key=TOWN_KEY, balance_sc=420))
        await db.commit()

    async with async_session() as db:
        svc = OfficeService(db)
        await svc.appoint("mayor", "he-qiaoyun", fill_strategy="election",
                          term_days=settings.polis_office_mayor_term_days)
        print("BEFORE holder =", await svc.get_holder("mayor"))
        row = (await db.execute(
            select(Office).where(Office.office_key == "mayor"))).scalar_one()
        print("BEFORE term_ends_at =", row.term_ends_at)
        # 把任期推到过去 = 真实钟越过 term_ends_at 之后夜间 cron 看到的状态
        row.term_ends_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

    # ↓↓↓ nightly_cron.py:258-267 的块体原样
    async with async_session() as db:
        n = await OfficeService(db).term_check()
    print("term_check vacated =", n)

    async with async_session() as db:
        svc = OfficeService(db)
        holder = await svc.get_holder("mayor")
        polls = (await db.execute(
            select(Poll).where(Poll.status == "open",
                               Poll.question.like(f"{ELECTION_TAG}%"))
        )).scalars().all()
        audits = await office_audit.list_term_audits(db, office_key="mayor")
        flags = await office_audit.overdue_vacancies(db, max_vacant_hours=0)
        print("AFTER holder =", holder)
        print("AFTER open election polls =",
              [(p.id, p.question, len(p.options_json)) for p in polls])
        print("AFTER audit rows =", json.dumps(audits, ensure_ascii=False,
                                               indent=2))
        print("AFTER vacancy flags(threshold 0h) =", flags)

        import scripts.burnin_report as probe
        snap = await probe.fetch_office_snapshot(db)
        print("PROBE occupancy =", probe.office_occupancy(snap))

    ok = (n == 1 and holder is None and len(polls) == 1
          and len(audits) == 1 and audits[0]["holder_slug"] == "he-qiaoyun")
    print("RESULT =", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 3: 在真实进程上跑一遍，落 exit code 到 /tmp（`/Volumes/data` 挂载陷阱：不信 inline 成功）**

```bash
rm -f /tmp/f3_runtime.db /tmp/f3-runtime-out.txt /tmp/f3-runtime-rc.txt
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office/backend
DATABASE_URL="sqlite+aiosqlite:////tmp/f3_runtime.db" DEBUG=true LLM_API_KEY=dummy \
PYTHONPATH="/Volumes/data/dev/simverse-world/.worktrees/f3-office/backend" \
/Volumes/data/dev/simverse-world/backend/.venv/bin/python /tmp/f3-runtime-check.py \
  > /tmp/f3-runtime-out.txt 2>&1; echo "$?" > /tmp/f3-runtime-rc.txt
cat /tmp/f3-runtime-rc.txt; cat /tmp/f3-runtime-out.txt
```

Expected: `/tmp/f3-runtime-rc.txt` 为 `0`；输出里能看到
`term_check vacated = 1`、`AFTER holder = None`、恰好一条 `AFTER open election polls`、
一条 `AFTER audit rows` 且 `holder_slug` 为 `he-qiaoyun`、`RESULT = PASS`。

- [ ] **Step 4: 复核提交历史与工作区干净**

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git status --short          # 期望为空
git log --oneline master..HEAD
git show --stat HEAD
grep -rnE "TBD|TODO|FIXME|XXX|待补|稍后实现" \
  backend/app/services/office_service.py backend/app/tasks/office_audit.py \
  backend/tests/test_office_backfill.py backend/tests/test_office_term_audit.py \
  backend/tests/test_office_vacancy_sweep.py || echo "OK: no placeholders"

# commit message 里的 Verified-by 占位符（源码扫描扫不到它:模式不同、文件也不同）
git log master..HEAD --format='%B' | grep -nE '<贴|实际输出>' \
  && { echo "FATAL: commit message 里还留着 Verified-by 占位符"; exit 1; } \
  || echo "OK: no Verified-by placeholders"
git log master..HEAD --format='%B' | grep -c '^Verified-by: '   # 期望 >= 7

git diff master..HEAD --stat -- backend/app/config.py backend/app/tasks/nightly_cron.py \
  backend/app/services/election_service.py
```

Expected: 工作区为空；`master..HEAD` 恰为 Task 1/2/3/4/5/6/7 的 7 个 commit；占位符扫描输出 `OK: no placeholders`；`OK: no Verified-by placeholders` 且 `Verified-by:` 行数 ≥ 7；最后一条 `git diff --stat` **输出为空**（共享文件与 `election_service.py` 一个字节都没改）。

**红旗上报的显式记账（本线未交付，不许当成已交付）**：`overdue_vacancies` 只是纯函数，
本线**没有**任何东西会把这面红旗报出来——`scripts/burnin_report.py` 不在独占清单，接线
归收口（见文末接线清单第 3 条的收口硬门）。交接时必须口头/书面点名这一条，否则收口的人
看到探针里早有一份 `office_occupancy` 的 `vacant_days` 口径，很可能以为已经有信号了。

- [ ] **Step 5: 提交验证证据（只有一个空 commit 记录证据，不加任何代码）**

> **三行 `Verified-by:` 必须换成 Step 1 / Step 3 / Step 4 的真实输出。** 这条 commit 在
> Step 4 的机械门之后产生，扫不到它——提交完立刻把 Step 4 的占位符扫描再跑一遍收尾。

```bash
cd /Volumes/data/dev/simverse-world/.worktrees/f3-office
git commit --allow-empty -m "$(cat <<'EOF'
chore(f3): 全量回归 + 真实进程运行时验证通过

硬门用失败集双向差集判定(数量相同不等于集合相同):
  comm -13 /tmp/f3-base.txt /tmp/f3-after.txt → 空(零新增失败)

运行时走的是 nightly_cron.py:258-267 的块体原身,真实 DB 引擎 + 真实服务代码零 mock:
任期到期 → term_check 出缺 → 落卸任审计 → 自动开出补选 poll。

未改共享文件:config.py / nightly_cron.py / election_service.py 的 diff --stat 为空。

Verified-by: <贴 Step 1 的两段 comm 输出>
Verified-by: <贴 Step 3 的 /tmp/f3-runtime-rc.txt 与 /tmp/f3-runtime-out.txt 关键行>
Verified-by: <贴 Step 4 的 git log --oneline master..HEAD 与占位符扫描输出>
EOF
)"
```

---

## 交付后留给收口的接线清单（本线不做，写在这里给收口线）

1. `config.py` / `.env.example`：把 `overdue_vacancies` 的 `max_vacant_hours` 与 `polis_office_mayor_term_days` 的目标值补成 `POLIS_OFFICE_*` 旋钮（`polis_office_mayor_term_days` 验收通过后才从 0 改起）。
2. `nightly_cron.py`：`app/tasks/office_audit.py` 的 `overdue_vacancies` 接进探针路径；`term_check` 块体位置不变（补选已在 `term_check` 内部完成，cron 无需新增调用）。
3. **【收口硬门，不是普通接线项】`scripts/burnin_report.py` 消费 `office_audit.overdue_vacancies` 报红旗。**
   本线交付完毕后，世界里**没有任何东西会报这面红旗**——纯函数写好了但无人调用，spec §5
   「允许的空缺上限 = 1 个夜间周期，超出由探针报红旗」在收口接上之前是空头支票。验收标准：
   - 红旗触发条件：`fill_strategy == "election"` 且 `holder_slug IS NULL` 且
     `now - offices.updated_at > max_vacant_hours`（收口时该阈值改为读 `settings`，默认 24h
     = 一个夜间周期）。
   - 期望读数：健康世界恒为 `[]`；镇长空缺超过一个夜间周期时恰好一条，形如
     `{"office_key","fill_strategy","vacant_since","vacant_hours"}`（形状由
     `test_overdue_vacancies_shape_is_probe_consumable` 钉死，改字段就会红）。
   - 与既有口径的关系：探针里**已经有**一份 `office_occupancy` 的 `vacant_days`
     （`backend/scripts/burnin_report.py:672-687`），两者同源于 `offices.updated_at`，
     **不得再造第三份口径**。`office_occupancy` 是全职位的状态快照（含永远空缺的
     `doctor`/`postman`，不判红），`overdue_vacancies` 是只针对民选缺位的红旗判定——
     收口时把后者作为新增字段挂进探针输出，不要去改前者的语义。
   - 反例（收口时必须避免）：直接拿 `office_occupancy` 的 `vacant_days > 1` 当红旗，
     会把 046 seed 出来的 `doctor`/`postman` 永久空缺算进去，每晚恒定 2~3 面红旗。
4. F2 撤销接线：`OfficeService.vacate("mayor", audit=True)` → 清档位/写历史行/广播 → `trigger_backfill(db, "mayor", reason=REASON_CIVIC_REVOCATION)`。
5. 声誉影响接线（决策 2 切出来的那一步）：把 `office_audit` 的 payload 与 F1 修复后的声誉语义打通。
