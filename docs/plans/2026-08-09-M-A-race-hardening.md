# M-A 并发竞态加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M-A 终审确认的两处并发竞态（分数税账 carry 的 last-writer-wins、items 库存的无守卫读-改-写）从"随流量再治"改成根治：carry 整数化为 milli-SC 并走数据库内原子增量 + guarded 兑换，库存从 `payload_json` 列化后走 `UPDATE ... WHERE stock >= qty`。

**Architecture:** 两条独立线。①carry 线不需要迁移（值住在 `system_config` 一行里），换新键名 `town_tax_carry_milli` 存整数 milli-SC，累加压进 SQL 的 `CAST(value AS BIGINT) + delta`，凑满 1 SC 的兑换走 guarded UPDATE——Python 侧不再有读-改-写窗口；它天然在既有的 `TAX_CARRY_ENABLED`（默认关、在产从未开过）闸后面。②库存线需要迁移 056 加 `items.stock` 列并从 payload 回填（**暗上，零读者**），扣减逻辑收进唯一入口 `app/services/item_stock.py`，由新闸 `ITEM_STOCK_GUARD_ENABLED`（默认关）切换新旧路径——闸关逐字节走旧 payload 读-改-写，所以"迁移与开闸"是两次变更、两次部署（红线 feedback-no-migration-with-flag-flip）。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x async / Alembic / PostgreSQL（在产）+ aiosqlite（测试）/ pytest + anyio。

## Global Constraints

- 基线纪律：全套测试相对既有基线 **54 failed（49 lab + 5 postpone）零新增失败**。跑测试用主 checkout 的 venv：`/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest ...`（本 worktree 没有自己的 .venv）。
- 红线：**迁移/回填与行为变更不得同一次变更**。迁移 056 落地时 `ITEM_STOCK_GUARD_ENABLED` 默认 false，行为逐字节等同现状。
- `treasury_service` 模块头三条军规照旧适用：`amount <= 0` 静默 no-op；**守卫零行时绝不 `db.rollback()`**（什么都没写，且 rollback 会 expire 整个 session → asyncio 下一次惰性取属性 `MissingGreenlet`）；`synchronize_session=False` 后调用方必须重读，不能信内存里的 ORM 对象。
- 新增 pytest marker 必须同时进 `pyproject.toml` 的 `markers` 与 `addopts` 的 `-m 'not ...'`（`tests/test_pytest_default_selection.py` 守着这条一致性）。
- 严格 TDD：先写失败测试、跑出红、再实现、再跑绿、再 commit。一 step 一 commit，commit message 用仓内既有风格（`fix(economy): ...` / `feat(economy): ...` / `test(economy): ...`）。
- 并发竞态测试必须是**确定性**的：文件型 sqlite 双 session（pysqlite 的 SELECT 不开事务、不持读锁，所以"两边都先读、再依次写"的交错是确定性的）走默认门；真 PG 的真并发（`asyncio.gather`）走 opt-in marker，作为实证证据手工跑。
- 分支：`feat/npc-economy-ma-race-hardening`（base = `feat/npc-economy-ma` @ `e93e8a6`）。不 push、不合并——由验收者决策。

---

## File Structure

| 文件 | 责任 | 动作 |
|------|------|------|
| `backend/app/services/treasury_service.py` | carry 整数化：新键名 + 三个整数 KV 原语 + `_skim` 改写 | 修改 |
| `backend/tests/test_tax_carry.py` | carry 语义 + 新增并发段 | 修改 |
| `backend/alembic/versions/056_add_item_stock.py` | 加 `items.stock` 列 + 从 payload 回填（暗上） | 新建 |
| `backend/app/models/shop.py` | `Item.stock` 列 | 修改 |
| `backend/tests/test_item_stock_migration.py` | 迁移形态（单头/长度/可加载）+ `_backfill_stock` 单测 | 新建 |
| `backend/app/config.py` | `item_stock_guard_enabled` 闸 | 修改 |
| `backend/.env.example`、`deploy/backend/.env.example` | 新闸文档 | 修改 |
| `backend/tests/test_npc_trade_config.py` | 新闸默认值断言 | 修改 |
| `backend/app/services/item_stock.py` | **唯一**库存扣减入口：闸关旧 RMW / 闸开 guarded UPDATE | 新建 |
| `backend/tests/test_item_stock_guard.py` | `take_stock` 单测 + 双 session 超卖复现 | 新建 |
| `backend/app/services/shop_effects.py` | 玩家购买路径接入 + 售罄退款 | 修改 |
| `backend/app/services/shop_service.py` | 把实付额 `charged_sc` 递进 effect（退款不能退牌价） | 修改 |
| `backend/app/services/npc_trade_service.py` | NPC 夜间消费路径接入 | 修改 |
| `backend/app/services/caravan_service.py` | 商队收购路径接入 + 进口货上架写列 | 修改 |
| `backend/app/services/duty_service.py` | 居民作品上架写列 | 修改 |
| `backend/pyproject.toml` | 新 marker `economy_postgres` + addopts 排除 | 修改 |
| `backend/tests/integration/test_economy_concurrency_postgres.py` | 真 PG 真并发实证（opt-in） | 新建 |
| `docs/ROADMAP.md` | 加固落档 | 修改 |

---

## Task 1: carry 整数化 —— milli-SC + 数据库内原子增量

**Files:**
- Modify: `backend/app/services/treasury_service.py:56`（`TAX_CARRY_KEY`）、`:117-173`（KV 原语区）、`:176-218`（`_skim`）
- Modify: `backend/tests/test_tax_carry.py`
- Modify: `backend/app/config.py:549`、`backend/.env.example:504`、`deploy/backend/.env.example:280-281`

**Interfaces:**
- Consumes: 既有 `kv_read(db, key, default=None) -> str | None`、`tax_pending(db, amount, reason="") -> None`、`fiscal_policy_service.tax_rate(db, fallback) -> float`
- Produces:
  - `TAX_CARRY_KEY = "town_tax_carry_milli"`、`CARRY_SCALE = 1000`
  - `async def kv_read_int(db: AsyncSession, key: str) -> int`
  - `async def kv_add_int_pending(db: AsyncSession, key: str, delta: int, *, group: str = "town", updated_by: str) -> None`
  - `async def kv_take_int_pending(db: AsyncSession, key: str, amount: int, *, updated_by: str) -> bool`
  - `skim_tax_pending` / `skim_tax` 签名与返回语义**不变**（`-> int`，返回实际征到的 SC）

- [ ] **Step 1: 写失败测试 —— 两条连接各累尾数，谁也不许抹掉谁**

在 `backend/tests/test_tax_carry.py` 末尾追加（文件已有 `sessions` / `treasury_on` fixture 与 `_carry_row` helper）：

```python
# --------------------------------------------------------------------------- #
# 9. 并发:跨进程累尾数不许 last-writer-wins                                     #
# --------------------------------------------------------------------------- #

async def test_two_connections_accrue_without_losing_each_other(sessions):
    """终审已知限制 1 的根治断言:两条连接各累 400 milli,总账必须是 800。

    旧实现是 `kv_read` → Python 加法 → `kv_upsert_pending` 盲覆盖:两边都读到
    0,后写的那个把前一个抹掉,总账停在 400(last-writer-wins)。这里刻意先让
    两条连接都读一眼再依次落库——复现的就是那个顺序。

    为什么这个交错在文件型 sqlite 上是确定性的:pysqlite 只在 DML 前才发
    BEGIN,SELECT 不开事务也不持读锁,所以"读1/读2/写1提交/写2提交"跑得通,不会
    撞上 sqlite 的库级写锁。真 PG 的真并发版(asyncio.gather)在
    tests/integration/test_economy_concurrency_postgres.py。
    """
    key = treasury_service.TAX_CARRY_KEY
    async with sessions() as db1, sessions() as db2:
        assert await treasury_service.kv_read_int(db1, key) == 0   # 两边都先读一眼
        assert await treasury_service.kv_read_int(db2, key) == 0
        await treasury_service.kv_add_int_pending(db1, key, 400, updated_by="a")
        await db1.commit()
        await treasury_service.kv_add_int_pending(db2, key, 400, updated_by="b")
        await db2.commit()

    row = await _carry_row(sessions)
    assert row is not None
    assert int(row.value) == 800
    assert row.group == "town"          # SystemConfig.group 非 Optional 无默认


async def test_kv_take_int_is_guarded_and_never_goes_negative(sessions):
    """兑换 1 SC 走守卫:够就扣、不够零行返回 False(且不许写成负数)。"""
    key = treasury_service.TAX_CARRY_KEY
    async with sessions() as db:
        await treasury_service.kv_add_int_pending(db, key, 1200, updated_by="t")
        assert await treasury_service.kv_take_int_pending(
            db, key, 1000, updated_by="t") is True
        assert await treasury_service.kv_take_int_pending(
            db, key, 1000, updated_by="t") is False
        await db.commit()

    assert int((await _carry_row(sessions)).value) == 200


async def test_kv_read_int_tolerates_missing_and_garbage(sessions):
    """记账不是钱:行不在 / 值不是整数 → 0,绝不抛(抛会连坐掉一笔买卖)。"""
    async with sessions() as db:
        assert await treasury_service.kv_read_int(db, "no_such_key") == 0
        await treasury_service.kv_upsert_pending(
            db, "junk_key", "not-a-number", updated_by="t")
        await db.commit()
        assert await treasury_service.kv_read_int(db, "junk_key") == 0
```

- [ ] **Step 2: 跑测试确认红**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_tax_carry.py -k "connections or kv_take_int or kv_read_int" -v`（cwd = `backend/`）
Expected: FAIL — `AttributeError: module 'app.services.treasury_service' has no attribute 'kv_read_int'`

- [ ] **Step 3: 实现三个整数 KV 原语**

`backend/app/services/treasury_service.py`：把 `from sqlalchemy import select, update` 改成 `from sqlalchemy import BigInteger, String, cast, select, update`，并在 `kv_upsert_pending` 之后插入：

```python
async def kv_read_int(db: AsyncSession, key: str) -> int:
    """读一个整数 KV(缺行 / 值不是整数 → 0)。

    绝不抛:这些键是**记账**不是钱,一个脏值不该把调用它的那笔买卖连坐掉。
    """
    raw = await kv_read(db, key)
    try:
        return int(raw)                     # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


async def kv_add_int_pending(
    db: AsyncSession,
    key: str,
    delta: int,
    *,
    group: str = "town",
    updated_by: str,
) -> None:
    """原子增量 upsert:新值由**数据库**在写的那一刻从当前值算出来。

    与 ``kv_upsert_pending`` 只差这一点,但那正是竞态的根:盲写版本写回的是调
    用方几毫秒前读到的值,两个进程同时累尾数时后写的抹掉先写的
    (last-writer-wins)。这里 `value = CAST(value AS BIGINT) + delta` 整条在 SQL
    里,方言分派与 ``tax_pending`` 逐行同构。

    值必须是纯整数串(milli-SC),不能是 ``"0.8"``:真 PostgreSQL 上
    ``CAST('0.8' AS BIGINT)`` 直接抛,sqlite 则静默截成 0。
    """
    now = datetime.now(UTC)
    delta = int(delta)
    values = {
        "key": key, "value": str(delta), "group": group,
        "updated_at": now, "updated_by": updated_by,
    }
    bumped = cast(cast(SystemConfig.value, BigInteger) + delta, String)
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        statement = insert(SystemConfig).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[SystemConfig.key],
            set_={"value": bumped, "updated_at": now, "updated_by": updated_by},
        )
        await db.execute(statement)
    else:
        result = await db.execute(
            update(SystemConfig)
            .where(SystemConfig.key == key)
            .values(value=bumped, updated_at=now, updated_by=updated_by)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            db.add(SystemConfig(**values))
    await db.flush()


async def kv_take_int_pending(
    db: AsyncSession, key: str, amount: int, *, updated_by: str,
) -> bool:
    """守卫扣减:``... SET value = value - amount WHERE value >= amount``。

    返回是否真扣到。零行 = 别人抢先扣走了,**不 rollback**(军规 2:什么都没写
    就没有什么要撤,而 rollback 会 expire 调用方 session 里的所有 ORM 对象)。
    """
    if amount <= 0:
        return False
    amount = int(amount)
    result = await db.execute(
        update(SystemConfig)
        .where(SystemConfig.key == key,
               cast(SystemConfig.value, BigInteger) >= amount)
        .values(
            value=cast(cast(SystemConfig.value, BigInteger) - amount, String),
            updated_at=datetime.now(UTC),
            updated_by=updated_by,
        )
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return (result.rowcount or 0) > 0
```

- [ ] **Step 4: 跑测试确认绿**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_tax_carry.py -k "connections or kv_take_int or kv_read_int" -v`
Expected: 3 passed

- [ ] **Step 5: 变异证非空（不 commit 变异）**

把 `kv_add_int_pending` 的 `bumped` 临时改成盲写 `str(await kv_read_int(db, key) + delta)`（即旧的读-改-写），重跑上面三条：
Expected: `test_two_connections_accrue_without_losing_each_other` FAIL（`800 == 400`）。
确认后 `git checkout -- backend/app/services/treasury_service.py` 之外的写法：直接手工改回原实现，并再跑一次确认回绿。把红/绿两段输出留在 commit message 的 `Verified-by:` 里。

- [ ] **Step 6: 改 `TAX_CARRY_KEY` 与 `_skim`**

`treasury_service.py:53-56` 的常量块整体替换：

```python
# M-A C5: the fractional tax ledger. Same "scalar state lives in system_config"
# discipline as LAST_SPEND_KEY — the sub-1-SC remainder of every skim accrues
# here instead of evaporating in an ``int()``, so no migration is needed.
#
# M-A 加固:值是**整数 milli-SC**(1 SC = 1000),不再是 "0.800000" 这样的浮点
# 串——只有整数才能走 kv_add_int_pending 的数据库内原子增量。键名跟着换
# (`town_tax_carry` → `town_tax_carry_milli`):单位藏在值里迟早出事,而且万一
# 哪个 dev 库里还留着老键的浮点串,新版的 CAST 在真 PostgreSQL 上会直接抛。
# 老键从此无人读写,留着不动(删数据不进这次变更)。
TAX_CARRY_KEY = "town_tax_carry_milli"
CARRY_SCALE = 1000                              # 1 SC = 1000 milli
```

`_skim` 的 carry 分支（原 `:206-218`）替换为：

```python
    # M-A 加固:两步都在数据库里做完,Python 侧不留读-改-写窗口。
    # ① 尾数原子累加(谁也抹不掉谁);② 凑满整 SC 走守卫兑换(零行 = 别人抢先
    # 兑走了,这一笔就只累不征,钱一分不少地留在账上)。
    exact_milli = int(round(exact * CARRY_SCALE))
    wrote = False
    if exact_milli > 0:
        await kv_add_int_pending(
            db, TAX_CARRY_KEY, exact_milli, updated_by=f"skim_tax:{reason}")
        wrote = True

    carry_milli = await kv_read_int(db, TAX_CARRY_KEY)   # 含自己刚 flush 的那笔
    cut = min(carry_milli // CARRY_SCALE, gross)
    if cut > 0 and await kv_take_int_pending(
            db, TAX_CARRY_KEY, cut * CARRY_SCALE, updated_by=f"skim_tax:{reason}"):
        await tax_pending(db, cut, reason)
        wrote = True
    else:
        cut = 0
    if not wrote:
        # 零税率且账上也凑不出 1 SC:no-op skim 必须仍是零写入。
        return 0, False
    return cut, True
```

同时把 `skim_tax_pending` docstring 里的 `` `town_tax_carry` `` 改成 `` `town_tax_carry_milli`(整数 milli-SC) ``。

> 单线程下与旧算法逐笔等价：旧 `total = exact + carry; cut = min(int(total), gross); carry' = total - cut`，新 `carry' = carry + exact_milli; cut = min(carry'//1000, gross); carry'' = carry' - cut*1000` —— 同一个式子换成整数。

- [ ] **Step 7: 更新既有 carry 用例的口径（浮点 → milli 整数）**

`backend/tests/test_tax_carry.py`：
- `_carry()` helper：`return 0.0 if row is None else float(row.value)` → `return 0 if row is None else int(row.value)`，返回类型标注改 `-> int`，docstring 补一句「值是 milli-SC 整数」。
- `test_carry_on_accrues_the_fraction_until_it_pays_out`：`pytest.approx(0.8)` → `800`，`pytest.approx(0.4)` → `400`。
- `test_carry_row_carries_group_and_updated_at`：`ConfigService(db).get(...) == pytest.approx(0.8)` → `== 800`，注释改成「ConfigService.get 会 json.loads,milli 整数是合法 JSON 数字」。
- `test_carry_upsert_updates_the_existing_row_in_place`：`pytest.approx(0.6)` → `600`。
- `test_skim_tax_commits_on_its_own`：`pytest.approx(0.0)` → `0`。
- `test_skim_tax_commits_a_cut_of_zero_when_only_carry_moved`：`pytest.approx(0.8)` → `800`。
- `test_cut_is_capped_at_gross_when_carry_overflows`：种子值 `"5.000000"` → `"5000"`。
- 文件头 docstring 第 3 条：`` `system_config.town_tax_carry` `` → `` `system_config.town_tax_carry_milli`(整数 milli-SC) ``。

- [ ] **Step 8: 跑整个 carry 文件 + 所有依赖它的经济测试**

Run:
```bash
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_tax_carry.py tests/test_caravan.py tests/test_npc_consumption.py tests/test_economy_conservation.py tests/test_treasury_service.py tests/test_nightly_npc_trade.py -q
```
Expected: all passed

- [ ] **Step 9: 同步配置注释与 .env.example**

- `backend/app/config.py:549`：`# C5 分数税账:尾数累入 town_tax_carry;关=旧 int() 截断` → `# C5 分数税账:尾数以整数 milli-SC 累入 town_tax_carry_milli(原子增量);关=旧 int() 截断`
- `backend/.env.example` M-A 段 `TAX_CARRY_ENABLED=false` 上方注释同步键名。
- `deploy/backend/.env.example:280` 同步键名，并在段②验收清单那句 SQL 旁补一行：`# 开闸后读数:select value from system_config where key='town_tax_carry_milli'; → 0 ≤ 值 < 1000`
- `backend/app/services/shop_effects.py:49` docstring 里的 `` `town_tax_carry` `` → `` `town_tax_carry_milli` ``

- [ ] **Step 10: Commit**

```bash
git add backend/app/services/treasury_service.py backend/app/services/shop_effects.py backend/app/config.py backend/.env.example deploy/backend/.env.example backend/tests/test_tax_carry.py
git commit -m "fix(economy): 分数税账整数化——carry 走 milli-SC 原子增量+守卫兑换,消除跨进程 last-writer-wins"
```

---

## Task 2: 真 PG 真并发实证（carry）

**Files:**
- Modify: `backend/pyproject.toml:30-43`（markers + addopts）
- Create: `backend/tests/integration/test_economy_concurrency_postgres.py`
- Test: 同上（opt-in marker，默认门不跑）

**Interfaces:**
- Consumes: Task 1 的 `treasury_service.skim_tax` / `TAX_CARRY_KEY` / `CARRY_SCALE`
- Produces: marker `economy_postgres`；env 变量约定 `ECONOMY_TEST_DATABASE_URL`（回落 `LAB_TEST_DATABASE_URL`）；fixture `pg_sessions`

- [ ] **Step 1: 注册 marker 并选边**

`backend/pyproject.toml` 的 `markers` 列表末尾加一条：

```toml
    "economy_postgres: required real-Postgres concurrency evidence for the M-A economy (carry / stock races)",
```

`addopts` 改成：

```toml
addopts = "-m 'not lab_oci and not lab_postgres and not lab_redis and not lab_staging and not lab_capacity and not economy_postgres'"
```

- [ ] **Step 2: 跑一致性闸确认没漏边**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_pytest_default_selection.py -v`
Expected: 3 passed

- [ ] **Step 3: 写真 PG 并发测试**

Create `backend/tests/integration/test_economy_concurrency_postgres.py`：

```python
"""真 PostgreSQL 上的经济并发实证(M-A 加固,opt-in)。

文件型 sqlite 只能做**确定性交错**(两边先读、再依次写),做不了真并发——它有
库级写锁,两条连接的 UPDATE 不可能同时在飞。所以竞态的最终证据放在这里:真
PG、真 asyncio.gather、真行锁。

跑法(需要一个可丢弃的 PG):

    docker run -d --rm --name simverse-econ-pg -e POSTGRES_PASSWORD=pg \\
      -p 55432:5432 postgres:16
    ECONOMY_TEST_DATABASE_URL=postgresql+asyncpg://postgres:pg@localhost:55432/postgres \\
      .venv/bin/python -m pytest tests/integration/test_economy_concurrency_postgres.py \\
      -m economy_postgres -v

只建这几张用得上的表(不是 create_all):全量建表会拖进 pgvector 依赖,而这几条
断言一张 embedding 表都不需要。
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.resident_treasury import ResidentTreasury
from app.models.shop import Item
from app.models.system_config import SystemConfig
from app.models.town_treasury import TownTreasury
from app.services import item_stock, treasury_service

pytestmark = [pytest.mark.economy_postgres, pytest.mark.anyio]

DB_URL = (os.environ.get("ECONOMY_TEST_DATABASE_URL")
          or os.environ.get("LAB_TEST_DATABASE_URL") or "")
TABLES = (SystemConfig.__table__, TownTreasury.__table__, Item.__table__,
          ResidentTreasury.__table__)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def pg_sessions():
    if not DB_URL.startswith("postgresql+asyncpg"):
        pytest.skip("ECONOMY_TEST_DATABASE_URL must be postgresql+asyncpg://...")
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        for table in reversed(TABLES):
            await conn.run_sync(table.drop, checkfirst=True)
        for table in TABLES:
            await conn.run_sync(table.create)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        for table in reversed(TABLES):
            await conn.run_sync(table.drop, checkfirst=True)
    await engine.dispose()


async def test_concurrent_skims_never_lose_a_millisecond_of_carry(
    pg_sessions, monkeypatch,
):
    """20 笔并发 skim(每笔 exact = 0.8 SC)。

    守恒式:征进镇库的 SC × 1000 + 账上剩余 milli == 累计应征 milli。
    旧的浮点读-改-写在这里会丢尾数(两个事务读到同一个 carry,后提交的抹掉先
    提交的),等式左边小于右边。
    """
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "tax_carry_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    rounds, gross, rate = 20, 16, 0.05          # 16 × 0.05 = 0.8 SC = 800 milli

    async def one(i: int) -> int:
        async with pg_sessions() as db:
            return await treasury_service.skim_tax(db, gross, rate, f"race:{i}")

    cuts = await asyncio.gather(*(one(i) for i in range(rounds)))

    async with pg_sessions() as db:
        town = await treasury_service.balance(db)
        carry = await treasury_service.kv_read_int(db, treasury_service.TAX_CARRY_KEY)

    expected = rounds * int(round(gross * rate * treasury_service.CARRY_SCALE))
    assert sum(cuts) == town
    assert town * treasury_service.CARRY_SCALE + carry == expected
```

- [ ] **Step 4: 起一个可丢弃 PG 并跑出实证**

```bash
docker run -d --rm --name simverse-econ-pg -e POSTGRES_PASSWORD=pg -p 55432:5432 postgres:16
```

Run（cwd = `backend/`）:
```bash
ECONOMY_TEST_DATABASE_URL=postgresql+asyncpg://postgres:pg@localhost:55432/postgres /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/integration/test_economy_concurrency_postgres.py -m economy_postgres -v
```
Expected: 1 passed（把输出留作 `Verified-by:`）

- [ ] **Step 5: 变异证非空**

把 `treasury_service._skim` 的 carry 分支临时换回浮点读-改-写（`carry = float(await kv_read(...) or "0"); total = exact + carry; ...; kv_upsert_pending(..., f"{total-cut:.6f}")`），重跑 Step 4：
Expected: FAIL（`town*1000 + carry` 明显小于 16000）。记下实测数字，然后手工改回。

- [ ] **Step 6: 确认默认门没被污染**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/integration/test_economy_concurrency_postgres.py -q`
Expected: `1 deselected`（marker 生效，裸机不需要 PG）

- [ ] **Step 7: Commit**

```bash
git add backend/pyproject.toml backend/tests/integration/test_economy_concurrency_postgres.py
git commit -m "test(economy): 真 PG 并发实证——20 笔并发 skim 的尾数守恒(economy_postgres marker,默认门排除)"
```

---

## Task 3: 迁移 056 —— `items.stock` 列化（暗上，零读者）

**Files:**
- Create: `backend/alembic/versions/056_add_item_stock.py`
- Modify: `backend/app/models/shop.py:23-24`
- Create: `backend/tests/test_item_stock_migration.py`

**Interfaces:**
- Consumes: 既有 `Item` 模型、alembic 链头 `055_add_commission_acceptor`
- Produces:
  - `Item.stock: Mapped[int | None]`（nullable，默认 `None`）
  - 迁移模块级函数 `_backfill_stock(bind) -> int`（返回回填行数，供测试直接调用）
  - revision id `"056_add_item_stock"`（17 字符，`alembic_version.version_num` 是 varchar(32)）

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_item_stock_migration.py`：

```python
"""迁移 056:items.stock 列化(暗上边界)。

暗上的判据不是"没写数据",而是"**没有读者**":056 加列 + 从 payload_json 回填
初值,而三处扣减在 ITEM_STOCK_GUARD_ENABLED 翻开之前一行都不读这列。回填是新
列的初值(从同一行推导),不是数据修复,也不删 payload 里的 stock —— 旧镜像回滚
后照样按 payload 跑。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.shop import Item

pytestmark = pytest.mark.anyio

MIGRATION = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
             / "056_add_item_stock.py")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_056", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # type: ignore[union-attr]
    return module


def test_migration_chains_onto_the_measured_head():
    """单头,且挂在本 worktree 实测的链头 055 上。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("056_add_item_stock")
    assert rev is not None
    assert rev.down_revision == "055_add_commission_acceptor"
    assert len("056_add_item_stock") <= 32   # alembic_version.version_num


def test_item_model_has_a_nullable_stock_column():
    """nullable 是故意的:consumable/gift/decor/tip 根本没有库存概念,
    NULL = 不计库存;只有 resident_work / import_good 有值。"""
    col = Item.__table__.columns["stock"]
    assert isinstance(col.type, sa.Integer)
    assert col.nullable is True


async def test_backfill_copies_payload_stock_into_the_column(tmp_path):
    """回填:payload 里有 stock 的行搬进新列,没有的行保持 NULL。"""
    module = _load_migration()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with sessions() as db:
        db.add_all([
            Item(code="work_a", kind="resident_work", name="陶罐", price_sc=15,
                 payload_json={"creator_slug": "maker", "stock": 3}, active=True),
            Item(code="import_tea", kind="import_good", name="茶叶", price_sc=6,
                 payload_json={"caravan": True, "stock": 2}, active=True),
            Item(code="gift_flower", kind="gift", name="一束花", price_sc=15,
                 payload_json={"relationship_boost": 0.1}, active=True),
            Item(code="junk", kind="resident_work", name="脏值", price_sc=1,
                 payload_json={"stock": "three"}, active=True),
        ])
        await db.commit()
        # create_all 已经带上了新列,把它清成 NULL 才是迁移前的形态
        await db.execute(sa.text("UPDATE items SET stock = NULL"))
        await db.commit()

    async with engine.begin() as conn:
        filled = await conn.run_sync(lambda sync_conn: module._backfill_stock(sync_conn))
    assert filled == 2

    async with sessions() as db:
        got = dict((await db.execute(sa.select(Item.code, Item.stock))).all())
    assert got == {"work_a": 3, "import_tea": 2, "gift_flower": None, "junk": None}
    await engine.dispose()


async def test_backfill_is_idempotent(tmp_path):
    """回填跑两遍结果一样(部署重跑 / 回滚再上都不该越滚越怪)。"""
    module = _load_migration()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mig2.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        db.add(Item(code="work_a", kind="resident_work", name="陶罐", price_sc=15,
                    payload_json={"creator_slug": "maker", "stock": 3}, active=True))
        await db.commit()
        await db.execute(sa.text("UPDATE items SET stock = NULL"))
        await db.commit()

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: module._backfill_stock(c))
        await conn.run_sync(lambda c: module._backfill_stock(c))

    async with sessions() as db:
        assert (await db.execute(
            sa.select(Item.stock).where(Item.code == "work_a"))).scalar_one() == 3
    await engine.dispose()
```

- [ ] **Step 2: 跑测试确认红**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_item_stock_migration.py -v`
Expected: FAIL — `KeyError: 'stock'` / 找不到 `056_add_item_stock.py`

- [ ] **Step 3: 加模型列**

`backend/app/models/shop.py`，在 `payload_json` 之后插入：

```python
    # M-A 加固:库存从 payload_json 抬成真列,扣减才能走
    # `UPDATE ... WHERE stock >= qty` 的守卫(payload_json 是 JSON 列,判据没法
    # 写进 WHERE,只能读-改-写,两个进程撞上就超卖)。
    # nullable:绝大多数商品(consumable/gift/decor/tip)没有库存概念,NULL = 不计
    # 库存;只有 resident_work / import_good 有值。
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
```

- [ ] **Step 4: 写迁移**

Create `backend/alembic/versions/056_add_item_stock.py`：

```python
"""Add items.stock (M-A 加固:库存列化).

**暗上**:加一列 + 从 payload_json 回填初值,而**没有任何读者**——三处扣减
(shop_effects / npc_trade_service / caravan_service)在 ``ITEM_STOCK_GUARD_ENABLED``
翻开之前一行都不读这列,走的仍是旧 payload 读-改-写。开闸是 deploy/.env 的另
一次变更(红线:迁移与行为变更不同车,2026-07-25 事故复盘)。

回填不是"数据修复":它从同一行的 payload_json 推导新列的初值,不改变任何既有
语义,也**不删** payload 里的 stock —— 回滚回旧镜像后照样按 payload 跑。

nullable=True 是故意的:items 里绝大多数行(consumable/gift/decor/tip)根本没有
库存概念,NULL = 不计库存。

Revision ID: 056_add_item_stock
Revises: 055_add_commission_acceptor
Create Date: 2026-08-09
"""
import json

from alembic import op
import sqlalchemy as sa


revision = "056_add_item_stock"
down_revision = "055_add_commission_acceptor"
branch_labels = None
depends_on = None


# 显式带类型的 Core 表(不是裸 text()):psycopg 在 PG 上把 json 列还原成 dict、
# sqlite 上却给字符串,类型化的列让两边都走同一段 Python。
_items = sa.table(
    "items",
    sa.column("id", sa.String),
    sa.column("payload_json", sa.JSON),
    sa.column("stock", sa.Integer),
)


def _backfill_stock(bind) -> int:
    """``payload_json['stock']`` → ``items.stock``。返回回填的行数。

    单独拎成函数是为了能被测试直接调用(tests/test_item_stock_migration.py)——
    回填是这条迁移里唯一有逻辑的部分,不该只能靠一次真部署来验。
    整表 items 是百行量级(商品目录),逐行 UPDATE 没有性能问题。
    """
    rows = bind.execute(sa.select(_items.c.id, _items.c.payload_json)).fetchall()
    filled = 0
    for row in rows:
        payload = row.payload_json
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        if not isinstance(payload, dict) or "stock" not in payload:
            continue
        try:
            stock = int(payload["stock"])
        except (TypeError, ValueError):
            continue                        # 脏值不猜,留 NULL 给自愈路径处理
        bind.execute(
            _items.update().where(_items.c.id == row.id).values(stock=stock))
        filled += 1
    return filled


def upgrade() -> None:
    op.add_column("items", sa.Column("stock", sa.Integer(), nullable=True))
    _backfill_stock(op.get_bind())


def downgrade() -> None:
    op.drop_column("items", "stock")
```

- [ ] **Step 5: 跑测试确认绿**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_item_stock_migration.py -v`
Expected: 5 passed

- [ ] **Step 6: 真 PG 上把迁移链跑到头（实证，不入库）**

```bash
docker run -d --rm --name simverse-mig-pg -e POSTGRES_PASSWORD=pg -p 55433:5432 pgvector/pgvector:pg16
```
（用 pgvector 镜像：全链迁移会建 embedding 表，裸 postgres 镜像会炸在 `CREATE EXTENSION vector`。）

Run（cwd = `backend/`）:
```bash
DATABASE_URL=postgresql+asyncpg://postgres:pg@localhost:55433/postgres /Volumes/data/dev/simverse-world/backend/.venv/bin/.../alembic upgrade head
```
（用 venv 里的 alembic；实际命令按 `backend/alembic.ini` 的既有跑法。）
Expected: 末行 `Running upgrade 055_add_commission_acceptor -> 056_add_item_stock`；随后
```bash
docker exec simverse-mig-pg psql -U postgres -c "\d items"
```
Expected: 有 `stock | integer |` 一行。把输出留作 `Verified-by:`。

- [ ] **Step 7: 跑一遍受影响的既有测试（应当零变化——没有读者）**

Run:
```bash
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_caravan.py tests/test_npc_consumption.py tests/test_m1_economy.py tests/test_shop.py tests/test_economy_conservation.py -q
```
Expected: all passed（加了一列而没人读，行为逐字节不变）

- [ ] **Step 8: Commit**

```bash
git add backend/alembic/versions/056_add_item_stock.py backend/app/models/shop.py backend/tests/test_item_stock_migration.py
git commit -m "feat(economy): 迁移 056——items.stock 列化+payload 回填(暗上,零读者)"
```

---

## Task 4: `item_stock.take_stock` —— 唯一扣减入口（新闸默认关）

**Files:**
- Create: `backend/app/services/item_stock.py`
- Modify: `backend/app/config.py`（`item_stock_guard_enabled`）
- Modify: `backend/tests/test_npc_trade_config.py`
- Modify: `backend/.env.example`、`deploy/backend/.env.example`
- Create: `backend/tests/test_item_stock_guard.py`

**Interfaces:**
- Consumes: `Item`（含 Task 3 的 `stock` 列）、`settings.item_stock_guard_enabled`
- Produces: `async def take_stock(db: AsyncSession, item: Item, qty: int = 1) -> int | None` —— 返回扣完剩余库存；守卫零行（售罄 / 已下架 / 行没了）返回 `None`。flush-owned：不 commit、零行不 rollback。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_item_stock_guard.py`：

```python
"""M-A 加固 — 库存扣减的守卫语义与超卖复现。

终审已知限制 2:三处扣减(shop_effects / npc_trade_service / caravan_service)
都是 ORM 读-改-写,cron(商队/夜间消费)与 API 玩家购买撞上同一行 items 时,两
边都读到 stock=1、都写回 0 —— 一件货卖两次。

断言一律新开 session 重读:conftest 的 :memory: 引擎走 StaticPool,所有 session
共用一条连接,事务边界会假绿,所以本模块自建文件型 sqlite。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.shop import Item
from app.services import item_stock

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stock.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setattr(settings, "item_stock_guard_enabled", True)
    return settings


@pytest.fixture
def guard_off(monkeypatch):
    monkeypatch.setattr(settings, "item_stock_guard_enabled", False)
    return settings


def _work(code="work_a", stock=3, active=True):
    return Item(code=code, kind="resident_work", name="陶罐", description="",
                price_sc=15, payload_json={"creator_slug": "maker", "stock": stock},
                stock=stock, active=active)


async def _seed(sessions, *items):
    async with sessions() as db:
        db.add_all(list(items))
        await db.commit()


async def _row(sessions, code="work_a") -> Item:
    async with sessions() as db:
        return (await db.execute(select(Item).where(Item.code == code))).scalar_one()


async def test_guard_on_decrements_the_column_and_mirrors_the_payload(
    sessions, guard_on,
):
    await _seed(sessions, _work(stock=3))
    async with sessions() as db:
        item = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        assert await item_stock.take_stock(db, item, 1) == 2
        await db.commit()

    row = await _row(sessions)
    assert row.stock == 2
    assert row.active is True
    assert row.payload_json["stock"] == 2       # 镜像同步(闸翻回去不丢账)


async def test_guard_on_deactivates_at_zero(sessions, guard_on):
    await _seed(sessions, _work(stock=1))
    async with sessions() as db:
        item = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        assert await item_stock.take_stock(db, item, 1) == 0
        await db.commit()

    row = await _row(sessions)
    assert row.stock == 0
    assert row.active is False


async def test_guard_on_returns_none_when_sold_out(sessions, guard_on):
    await _seed(sessions, _work(stock=1))
    async with sessions() as db:
        item = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        assert await item_stock.take_stock(db, item, 1) == 0
        assert await item_stock.take_stock(db, item, 1) is None   # 第二次抢不到
        await db.commit()

    assert (await _row(sessions)).stock == 0    # 绝不写成 -1


async def test_guard_on_returns_none_when_qty_exceeds_stock(sessions, guard_on):
    await _seed(sessions, _work(stock=2))
    async with sessions() as db:
        item = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        assert await item_stock.take_stock(db, item, 3) is None
        await db.commit()

    assert (await _row(sessions)).stock == 2    # 一件都不许扣


async def test_guard_on_returns_none_for_inactive_item(sessions, guard_on):
    await _seed(sessions, _work(stock=3, active=False))
    async with sessions() as db:
        item = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        assert await item_stock.take_stock(db, item, 1) is None


async def test_guard_on_self_heals_a_null_column_from_the_payload(sessions, guard_on):
    """暗上窗口里旧镜像挂上架的行:列还是 NULL,第一次扣减先从 payload 自愈。"""
    item = _work(stock=3)
    item.stock = None
    await _seed(sessions, item)
    async with sessions() as db:
        loaded = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        assert await item_stock.take_stock(db, loaded, 1) == 2
        await db.commit()

    assert (await _row(sessions)).stock == 2


async def test_guard_off_is_byte_for_byte_the_legacy_payload_path(sessions, guard_off):
    """闸关 = 旧行为:只改 payload、永不返回 None(旧路径没有"抢不到"这回事)。"""
    await _seed(sessions, _work(stock=1))
    async with sessions() as db:
        item = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        assert await item_stock.take_stock(db, item, 1) == 0
        assert await item_stock.take_stock(db, item, 1) == 0   # 旧路径照扣不误
        await db.commit()

    row = await _row(sessions)
    assert row.payload_json["stock"] == 0
    assert row.active is False


async def test_two_sessions_cannot_oversell_the_last_copy(sessions, guard_on):
    """双 session 超卖复现:两条连接各自读到 stock=1,只有一条能扣成。

    这就是 cron(商队/夜间消费)与玩家 purchase 撞同一行时的真实时序 ——
    玩家路径先 SELECT 出 item、扣款、commit,再把**那个已经读到手的对象**交给
    effect 扣库存(shop_service.py:107),中间隔着一次 commit 的时间。
    """
    await _seed(sessions, _work(stock=1))
    async with sessions() as db1, sessions() as db2:
        item1 = (await db1.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        item2 = (await db2.execute(select(Item).where(Item.code == "work_a"))).scalar_one()

        got1 = await item_stock.take_stock(db1, item1, 1)
        await db1.commit()
        got2 = await item_stock.take_stock(db2, item2, 1)
        await db2.commit()

    assert (got1, got2) == (0, None)            # 只卖出一件
    row = await _row(sessions)
    assert row.stock == 0
    assert row.active is False
```

- [ ] **Step 2: 跑测试确认红**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_item_stock_guard.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.item_stock'`

- [ ] **Step 3: 加闸**

`backend/app/config.py`，在 `tax_carry_enabled` 那一行之后插入：

```python
    item_stock_guard_enabled: bool = False        # C6 库存守卫:扣减走 items.stock 的 guarded UPDATE;关=旧 payload 读-改-写
```

`backend/tests/test_npc_trade_config.py` 的 `test_npc_trade_gates_default_off` 末尾加一行：

```python
    assert s.item_stock_guard_enabled is False   # 加固闸:迁移 056 暗上后单独翻
```

`backend/.env.example` M-A 段 `TAX_CARRY_ENABLED=false` 之后加：

```
# 加固闸:库存扣减走 items.stock 的 guarded UPDATE(迁移 056 必须先落库)。
# 关 = 旧 payload_json 读-改-写(与现状逐字节一致)。
ITEM_STOCK_GUARD_ENABLED=false
```

`deploy/backend/.env.example` 同段落同样加一份（措辞按该文件既有风格，注明"迁移 056 落库 + 观察一晚后再单独翻"）。

- [ ] **Step 4: 写 `item_stock.py`**

Create `backend/app/services/item_stock.py`：

```python
"""M-A 加固 — items 库存的唯一原子扣减口。

**为什么要有这个模块。** 扣减原本在三处各写一遍(``shop_effects``:343-347、
``npc_trade_service``:204-210、``caravan_service``:151-157),形态都是 ORM 读-改-写:
``payload = dict(item.payload_json); stock -= qty; 整体重赋值``。cron 进程(商队
到访 / NPC 夜间消费)与 API 玩家购买撞上同一行 items 时,两边都读到 stock=1、都
写回 0 —— 一件货卖了两次。钱是守恒的(各人付各人的),但库存不是,而且作者被付
了两次货款。

**根治。** stock 从 ``payload_json`` 抬成真列(迁移 056)之后,判据可以写进
WHERE:``UPDATE items SET stock = stock - qty WHERE code = :code AND active AND
stock >= qty``。判据与写入在同一条语句里,互斥交给数据库,零行就是"没抢到"。

**两条路径。** ``ITEM_STOCK_GUARD_ENABLED`` 关 = 逐字节旧路径(且永不返回
None,旧行为里没有"抢不到"这回事),所以迁移可以先暗上、开闸是另一次变更
(红线:迁移与行为变更不同车)。闸开时 ``payload_json['stock']`` 仍被同步更新
—— 它退化成镜像(真相在列上),但这让闸翻回去不丢账。

**事务纪律。** flush-owned:不 commit(调用方拥有事务);守卫零行时**不
rollback** —— 什么都没写,而 rollback 会 expire 调用方 session 里的所有 ORM 对象
(``treasury_service`` 模块头军规 2)。
"""
from __future__ import annotations

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.shop import Item


def _legacy_take(item: Item, qty: int) -> int:
    """闸关路径:与旧的三处 payload 读-改-写逐行等价。

    ``payload_json`` 没有 mutable 跟踪(app/models/shop.py:23),就地改会被静默
    丢弃,所以必须"拷贝→改→整体重赋值"。永不返回 None。
    """
    payload = dict(item.payload_json or {})
    stock = int(payload.get("stock", 1)) - qty
    payload["stock"] = max(0, stock)
    item.payload_json = payload
    if stock <= 0:
        item.active = False
    return max(0, stock)


async def take_stock(db: AsyncSession, item: Item, qty: int = 1) -> int | None:
    """扣 ``qty`` 件库存。返回扣完剩余;守卫零行返回 ``None``。

    返回 ``None`` 的三种情形:售罄(``stock < qty``)、已下架(``active`` 为假)、
    行没了。调用方拿到 None 时:如果此前**什么都没写**就直接返回(别 rollback);
    如果已经动过钱(付款/扣款),就地 ``rollback`` 再退出。
    """
    if qty < 1:
        return None
    if not settings.item_stock_guard_enabled:
        return _legacy_take(item, qty)

    row = (await db.execute(
        select(Item.stock, Item.payload_json).where(Item.code == item.code)
    )).first()
    if row is None:
        return None
    if row.stock is None:
        # 暗上窗口(迁移已落库、闸还没翻)里由旧镜像挂上架的行:列还是 NULL。
        # 守卫 `stock IS NULL` 让两个进程同时自愈也只落一次。
        await db.execute(
            update(Item)
            .where(Item.code == item.code, Item.stock.is_(None))
            .values(stock=int((row.payload_json or {}).get("stock", 1)))
            .execution_options(synchronize_session=False)
        )

    result = await db.execute(
        update(Item)
        .where(Item.code == item.code, Item.active.is_(True), Item.stock >= qty)
        .values(
            stock=Item.stock - qty,
            active=case((Item.stock - qty <= 0, False), else_=True),
        )
        .execution_options(synchronize_session=False)
    )
    if (result.rowcount or 0) == 0:
        return None

    # synchronize_session=False:内存里的 item 是旧的,剩余量只能重读(事务内
    # SELECT 看得到自己尚未 commit 的改动)。
    remaining = (await db.execute(
        select(Item.stock).where(Item.code == item.code))).scalar_one()
    payload = dict(item.payload_json or {})
    payload["stock"] = remaining
    item.payload_json = payload
    if remaining <= 0:
        item.active = False
    await db.flush()
    return remaining
```

- [ ] **Step 5: 跑测试确认绿**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_item_stock_guard.py tests/test_npc_trade_config.py -v`
Expected: all passed

- [ ] **Step 6: 变异证非空**

把 `take_stock` 的守卫条件 `Item.stock >= qty` 临时删掉，重跑：
Expected: `test_two_sessions_cannot_oversell_the_last_copy` / `test_guard_on_returns_none_when_sold_out` FAIL。确认后改回、复跑回绿。

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/item_stock.py backend/app/config.py backend/.env.example deploy/backend/.env.example backend/tests/test_item_stock_guard.py backend/tests/test_npc_trade_config.py
git commit -m "feat(economy): item_stock.take_stock——库存 guarded UPDATE 唯一入口(ITEM_STOCK_GUARD_ENABLED 默认关)"
```

---

## Task 5: 三处扣减接入 + 上架写列 + 售罄退款

**Files:**
- Modify: `backend/app/services/shop_effects.py:319-368`
- Modify: `backend/app/services/shop_service.py:104-108`
- Modify: `backend/app/services/npc_trade_service.py:165-216`
- Modify: `backend/app/services/caravan_service.py:125-167`、`:188-203`
- Modify: `backend/app/services/duty_service.py:394-420`
- Modify: `backend/tests/test_item_stock_guard.py`（追加跨路径超卖用例）

**Interfaces:**
- Consumes: Task 4 的 `item_stock.take_stock(db, item, qty) -> int | None`
- Produces:
  - `shop_effects._resident_work_effect` 返回值新增 `"sold_out": True` / `"refunded_sc": int` 两个键（仅售罄分支），`"stock"` 语义改为"扣完剩余"
  - `shop_service.purchase` 递给 `apply_effect` 的 context 新增 `"charged_sc": int`（实付额，含集市日折扣）
  - `Item.stock` 由 `duty_service._maybe_list_resident_work` / `caravan_service._stock_import_goods` 在上架时写入

- [ ] **Step 1: 写失败测试 —— 跨路径超卖 + 售罄退款**

追加到 `backend/tests/test_item_stock_guard.py` 末尾（需要 `User` / `ResidentTreasury` / `Resident` 种子，退款走 `coin_service.reward` 打 users 表）：

```python
# --------------------------------------------------------------------------- #
# 跨路径:cron(商队) vs 玩家购买                                                 #
# --------------------------------------------------------------------------- #

async def test_caravan_and_player_cannot_oversell_the_last_copy(
    sessions, guard_on, monkeypatch,
):
    """商队(cron)与玩家(API)抢同一件 stock=1 的作品:只准成交一次。

    玩家侧刻意在商队动手**之前**就把 item 读到手 —— 这不是测试造的场景,
    `shop_service.purchase` 就是先 SELECT item、扣款、commit,再把那个对象交给
    effect(shop_service.py:85/107)。

    旧路径下两边都会成交:作者收两份货款、库存只掉一件的账。
    """
    from app.models.resident import Resident
    from app.models.resident_treasury import ResidentTreasury
    from app.models.user import User
    from app.services import caravan_service, coin_service, shop_effects, feed_service

    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "caravan_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)

    async def _no_feed(*a, **kw):
        return None
    monkeypatch.setattr(feed_service, "push", _no_feed)

    async with sessions() as db:
        db.add_all([
            _work(stock=1),
            Resident(id="r-1", slug="maker", name="陶匠", district="free",
                     status="idle", resident_type="npc"),
            ResidentTreasury(resident_slug="maker", balance_sc=0),
            User(id="u-1", email="p@example.com", username="player",
                 hashed_password="x", soul_coin_balance=0),
        ])
        await db.commit()

    summary = {"bought": 0, "spent": 0, "tax": 0}
    async with sessions() as db_player:
        item_p = (await db_player.execute(
            select(Item).where(Item.code == "work_a"))).scalar_one()

        async with sessions() as db_cron:          # 商队抢先买走最后一件
            spent = await caravan_service._buy_one(db_cron, "work_a", "maker", summary)
        assert spent == 15

        effect = await shop_effects._resident_work_effect(
            db_player, "u-1", item_p, 1, {"charged_sc": 15})

    assert effect is not None
    assert effect.get("sold_out") is True
    assert effect["refunded_sc"] == 15             # 玩家原路拿回实付额

    async with sessions() as db:
        assert (await coin_service.treasury_balance(db, "maker")) == 15   # 只收一份
        assert (await db.execute(
            select(Item.stock).where(Item.code == "work_a"))).scalar_one() == 0
        player = (await db.execute(
            select(User).where(User.id == "u-1"))).scalar_one()
    assert player.soul_coin_balance == 15          # 退款落库


async def test_player_refund_returns_the_discounted_price_not_the_list_price(
    sessions, guard_on, monkeypatch,
):
    """集市日打过折的单子售罄退款只能退**实付额** —— 退牌价就是凭空印钱。"""
    from app.models.user import User
    from app.services import shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    async with sessions() as db:
        db.add_all([
            _work(stock=1),
            User(id="u-1", email="p@example.com", username="player",
                 hashed_password="x", soul_coin_balance=0),
        ])
        await db.commit()

    async with sessions() as db:
        item = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        await item_stock.take_stock(db, item, 1)           # 先让它售罄
        await db.commit()

    async with sessions() as db:
        item = (await db.execute(select(Item).where(Item.code == "work_a"))).scalar_one()
        effect = await shop_effects._resident_work_effect(
            db, "u-1", item, 1, {"charged_sc": 14})        # 15 × 0.9 → 14
    assert effect["refunded_sc"] == 14

    async with sessions() as db:
        user = (await db.execute(select(User).where(User.id == "u-1"))).scalar_one()
    assert user.soul_coin_balance == 14
```

> 执行前先用 `Grep` 核对 `app/models/user.py` 的 `User` 必填列（`email` / `username` / `hashed_password` / `soul_coin_balance` 的真实字段名），种子行按实际列名写；`ResidentTreasury` 的列名以 `app/models/resident_treasury.py` 为准。

- [ ] **Step 2: 跑测试确认红**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_item_stock_guard.py -k "caravan_and_player or refund_returns" -v`
Expected: FAIL — `effect.get("sold_out")` 是 None（旧路径两边都成交，作者拿到 30）

- [ ] **Step 3: 接入 `shop_effects._resident_work_effect`**

把 `backend/app/services/shop_effects.py:334-348` 这一段：

```python
    gross = item.price_sc * qty
    # S1-5: the town's primary tax intake — ...
    cut = await _skim_town_tax(
        db, gross, settings.town_tax_rate_sales, f"sales_tax:{item.code}")
    earned = gross - cut
    if earned > 0:
        await coin_service.treasury_credit(db, creator_slug, earned, reason=f"work_sold:{item.code}")

    stock = int(payload.get("stock", 1)) - qty
    payload["stock"] = max(0, stock)
    item.payload_json = payload
    if stock <= 0:
        item.active = False
    await db.commit()
```

替换为：

```python
    gross = item.price_sc * qty
    # M-A 加固:库存守卫必须**先于**任何钱动作 —— 抢不到货就不能给作者付款。
    # 玩家的钱已经在 shop_service.purchase 里扣过了(先 charge 后 effect),所以
    # 这一支只能原路退款,退的是**实付额**(集市日打过折),退牌价等于凭空印钱。
    from app.services import item_stock
    remaining = await item_stock.take_stock(db, item, qty)
    if remaining is None:
        refund = int((context or {}).get("charged_sc") or gross)
        await coin_service.reward(db, user_id, refund, f"sold_out_refund:{item.code}")
        logger.info("resident_work sold out under the guard, refunded %d SC (%s)",
                    refund, item.code)
        return {"resident_work": item.code, "creator_slug": creator_slug,
                "sold_out": True, "refunded_sc": refund, "stock": 0}

    # S1-5: the town's primary tax intake — a sales-tax skim off resident-made
    # goods. Gate off → cut == 0 → ``earned`` is the untouched gross (status quo).
    cut = await _skim_town_tax(
        db, gross, settings.town_tax_rate_sales, f"sales_tax:{item.code}")
    earned = gross - cut
    if earned > 0:
        await coin_service.treasury_credit(db, creator_slug, earned, reason=f"work_sold:{item.code}")
    await db.commit()
```

并把函数末尾的返回值 `"stock": payload["stock"]` 改成 `"stock": remaining`。

- [ ] **Step 4: 把实付额递进 effect**

`backend/app/services/shop_service.py`，把 `purchase` 末尾的

```python
    effect = await apply_effect(db, user_id, item, qty, context)
```

改成

```python
    # M-A 加固:守卫扣库存零行(并发售罄)时 effect 要原路退款,退的必须是**实付
    # 额** —— 集市日打过折,退牌价就是凭空印钱。context 是入参的浅拷贝,不动调
    # 用方的 dict,也不动已经落库的 Purchase.context_json。
    effect = await apply_effect(
        db, user_id, item, qty, {**(context or {}), "charged_sc": total})
```

- [ ] **Step 5: 接入 `npc_trade_service._buy`**

把 `backend/app/services/npc_trade_service.py:203-210` 的 payload 读-改-写五行删掉，并在 `treasury_debit_pending` 成功之后、`skim_tax_pending` 之前插入：

```python
    # M-A 加固:库存守卫。买方的钱已经 debit 了(半笔账),抢不到货必须就地
    # rollback —— 悬挂的 debit 会被下一笔的 commit 带落库。
    from app.services import item_stock
    if await item_stock.take_stock(db, item, 1) is None:
        await db.rollback()
        return False
```

- [ ] **Step 6: 接入 `caravan_service._buy_one` 与 `_stock_import_goods`**

`_buy_one`：把 `:151-157` 的 payload 读-改-写整段删掉，并在 `price = item.price_sc` 之后、`skim_tax_pending` 之前插入：

```python
    # M-A 加固:库存守卫先行 —— 抢不到货(玩家刚买走最后一件)就一分钱都不动。
    # 这里**还什么都没写**,直接返回,不 rollback(rollback 会 expire 整个
    # session,treasury_service 模块头军规 2)。
    from app.services import item_stock
    if await item_stock.take_stock(db, item, 1) is None:
        return 0
```

`_stock_import_goods`：上架时把 stock 写进列（payload 里的镜像保留不动）：

```python
    payload = {"caravan": True, "stock": IMPORT_STOCK}
    for d in IMPORT_DEFS:
        existing = (await db.execute(
            select(Item).where(Item.code == d["code"]))).scalar_one_or_none()
        if existing is None:
            db.add(Item(**d, kind=IMPORT_KIND, payload_json=dict(payload),
                        stock=IMPORT_STOCK, active=True))
        else:
            existing.active = True
            existing.payload_json = dict(payload)
            existing.stock = IMPORT_STOCK       # M-A 加固:列是真相,payload 是镜像
            existing.price_sc = d["price_sc"]
```

- [ ] **Step 7: 接入 `duty_service._maybe_list_resident_work`**

```python
    payload = {
        "creator_slug": resident.slug,
        "stock": settings.npc_work_item_stock,
    }
    if existing is None:
        db.add(Item(
            code=code, kind="resident_work", name=name, description=description,
            icon=icon, price_sc=settings.npc_work_item_price_sc,
            payload_json=payload, stock=settings.npc_work_item_stock, active=True,
        ))
    else:
        existing.active = True
        existing.payload_json = payload
        existing.stock = settings.npc_work_item_stock   # M-A 加固:列是真相
        existing.price_sc = settings.npc_work_item_price_sc
```

- [ ] **Step 8: 跑测试确认绿**

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_item_stock_guard.py -v`
Expected: all passed

- [ ] **Step 9: 跑闸关回归（必须逐字节等同现状）**

Run:
```bash
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_caravan.py tests/test_caravan_hook.py tests/test_npc_consumption.py tests/test_m1_economy.py tests/test_shop.py tests/test_economy_conservation.py tests/test_nightly_npc_trade.py tests/test_duty_service.py tests/test_treasury_service.py -q
```
Expected: all passed（这些用例都在 `ITEM_STOCK_GUARD_ENABLED=false` 下跑，走 `_legacy_take`，payload 镜像语义不变）

- [ ] **Step 10: 加一条闸开的守恒复验**

在 `backend/tests/test_economy_conservation.py` 末尾追加（沿用该文件既有的 fixture / helper 名字，执行前先读一遍该文件确认 `_stocks` / `_sold` / `sessions` 的真实签名）：

```python
async def test_conservation_holds_with_the_stock_guard_on(sessions, monkeypatch, ...):
    """闸开后守恒式不变:库存守卫只决定"卖不卖得成",不改变钱怎么流。"""
    monkeypatch.setattr(settings, "item_stock_guard_enabled", True)
    # ... 复用本文件既有的一轮 nightly + caravan 跑法,断言与闸关版本同口径
```

Run: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_economy_conservation.py -q`
Expected: all passed

- [ ] **Step 11: Commit**

```bash
git add backend/app/services/shop_effects.py backend/app/services/shop_service.py backend/app/services/npc_trade_service.py backend/app/services/caravan_service.py backend/app/services/duty_service.py backend/tests/test_item_stock_guard.py backend/tests/test_economy_conservation.py
git commit -m "fix(economy): 三处库存扣减改走 guarded UPDATE——cron 与玩家并发不再超卖(闸关逐字节旧路径)"
```

---

## Task 6: 真 PG 真并发实证（库存）+ 全套回归 + 落档

**Files:**
- Modify: `backend/tests/integration/test_economy_concurrency_postgres.py`
- Modify: `docs/ROADMAP.md`

**Interfaces:**
- Consumes: Task 2 的 `pg_sessions` fixture、Task 4 的 `item_stock.take_stock`
- Produces: 无新 API

- [ ] **Step 1: 追加库存并发用例**

在 `backend/tests/integration/test_economy_concurrency_postgres.py` 末尾追加：

```python
async def test_concurrent_buyers_cannot_oversell_a_single_copy(
    pg_sessions, monkeypatch,
):
    """8 个并发买家抢同一件 stock=1 的作品:恰好一个抢到,其余全 None。

    真行锁的实证:PG 会把 8 条 `UPDATE ... WHERE stock >= 1` 串起来,第一条之后
    的 7 条守卫都不再匹配。闸关(旧 payload 读-改-写)时 8 条会全部"成交"。
    """
    monkeypatch.setattr(settings, "item_stock_guard_enabled", True)
    async with pg_sessions() as db:
        db.add(Item(code="work_a", kind="resident_work", name="陶罐",
                    description="", price_sc=15,
                    payload_json={"creator_slug": "maker", "stock": 1},
                    stock=1, active=True))
        await db.commit()

    async def buy() -> int | None:
        async with pg_sessions() as db:
            item = (await db.execute(
                select(Item).where(Item.code == "work_a"))).scalar_one()
            got = await item_stock.take_stock(db, item, 1)
            await db.commit()
            return got

    results = await asyncio.gather(*(buy() for _ in range(8)))

    assert sum(1 for r in results if r is not None) == 1
    async with pg_sessions() as db:
        row = (await db.execute(
            select(Item.stock, Item.active).where(Item.code == "work_a"))).one()
    assert row.stock == 0
    assert row.active is False
```

- [ ] **Step 2: 跑真 PG 实证**

```bash
docker run -d --rm --name simverse-econ-pg -e POSTGRES_PASSWORD=pg -p 55432:5432 postgres:16
```
Run:
```bash
ECONOMY_TEST_DATABASE_URL=postgresql+asyncpg://postgres:pg@localhost:55432/postgres /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/integration/test_economy_concurrency_postgres.py -m economy_postgres -v
```
Expected: 2 passed

- [ ] **Step 3: 变异证非空**

把 `settings.item_stock_guard_enabled` 的 monkeypatch 临时改成 `False`（走旧 payload 路径），重跑 Step 2：
Expected: `test_concurrent_buyers_cannot_oversell_a_single_copy` FAIL（8 个全都"成交"）。记下实测数字后改回。

- [ ] **Step 4: 全套回归对基线**

Run（cwd = `backend/`）:
```bash
/Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest -q 2>&1 | tail -30
```
Expected: `54 failed, ... passed` —— **failed 数与失败用例集合相对基线零新增**（基线 54 = 49 lab + 5 postpone）。若数字不是 54，逐条比对失败列表，新增的必须修掉。

- [ ] **Step 5: 停掉临时容器**

```bash
docker rm -f simverse-econ-pg simverse-mig-pg
```

- [ ] **Step 6: ROADMAP 落档**

在 `docs/ROADMAP.md` 的 M-A 相关章节追加一小节（措辞按该文件既有风格）：迁移 056 暗上 → `ITEM_STOCK_GUARD_ENABLED` 单独开闸的两段式；carry 键名换成 `town_tax_carry_milli`（整数 milli-SC）；两条竞态各自的实证跑法（`-m economy_postgres` + `ECONOMY_TEST_DATABASE_URL`）。

- [ ] **Step 7: Commit**

```bash
git add backend/tests/integration/test_economy_concurrency_postgres.py docs/ROADMAP.md
git commit -m "test(economy): 真 PG 并发实证——8 买家抢最后一件只成交一次;加固两段式部署落档"
```

---

## 部署顺序（交给验收者，不在本 plan 的执行范围内）

红线：迁移与开闸不同车。本次加固在 M-A 既有三段式之外再插两段：

| 段 | 变更 | 验收读数 |
|----|------|----------|
| ①′ 暗上 | 部署镜像（含迁移 056）。四个 M-A 闸 + `ITEM_STOCK_GUARD_ENABLED` 全关 | `select version_num from alembic_version` → `056_add_item_stock`；`\d items` 有 `stock`；`select count(*) from items where stock is not null` > 0；行为与暗上前逐字节同形 |
| ②′ 开库存守卫 | `/opt/skills-world/deploy/.env` 加 `ITEM_STOCK_GUARD_ENABLED=true`，`docker compose up -d` | 次日 `select code, stock, payload_json->>'stock' from items where kind in ('resident_work','import_good')` 两边一致；`select count(*) from items where stock < 0` = 0；日志无 `sold out under the guard` 异常爆发（偶发一两条正是守卫在生效） |

`TAX_CARRY_ENABLED` 那一段的读数从 `town_tax_carry` 改成 `town_tax_carry_milli`（`0 ≤ 值 < 1000`）。回滚都是把对应的 env 键改回 false 再 `up -d`，不需要回滚迁移。

---

## Self-Review

**1. Spec coverage**
- 用户要求 ①「carry 整数化为 milli-SC + 数据库内原子增量 upsert（镜像 `tax_pending` 的 `balance_sc + amount`）+ 兑换 1 SC 入镇库用 guarded UPDATE」→ Task 1 Step 3/6（`kv_add_int_pending` 完整镜像 `tax_pending` 的方言分派；`kv_take_int_pending` 是 guarded UPDATE）。
- 用户要求 ②「stock 从 payload_json 列化（迁移）后改 `guarded UPDATE ... WHERE stock >= qty`」→ Task 3（迁移 056）+ Task 4（`take_stock`）+ Task 5（三处接入：`caravan_service:130-161`、`shop_effects:330-348`、外加同形态的 `npc_trade_service:204-210`——终审只点了前两处，但第三处是同一个 bug 的第三份拷贝，漏掉它等于没治）。
- 「严格 TDD」→ 每个 Task 都是「写失败测试 → 跑红 → 实现 → 跑绿 → 变异证非空 → commit」。
- 「并发竞态用文件型 sqlite 双 session 或真 PG 复现」→ 两者都做：默认门 sqlite 双 session（Task 1 Step 1、Task 4 Step 1、Task 5 Step 1），opt-in 真 PG `asyncio.gather`（Task 2、Task 6）。
- 「迁移与开闸分离」→ 迁移 056 在 Task 3 单独 commit 且零读者；行为由 Task 4 引入的 `ITEM_STOCK_GUARD_ENABLED`（默认关）控制，部署上是两段。carry 那条不涉及迁移，且天然在既有 `TAX_CARRY_ENABLED`（在产从未开过）后面。
- 「相对基线 54 零新增失败」→ Task 6 Step 4。

**2. Placeholder scan**
- Task 5 Step 1 的 `User` / `ResidentTreasury` 种子行列名标了"执行前 Grep 核对"——这是必要的类型核对提示，不是占位符；Task 5 Step 10 的守恒用例明确要求先读该文件的既有 fixture 签名再补齐，属同类。其余步骤都给了完整可粘贴代码。

**3. Type consistency**
- `take_stock(db, item, qty) -> int | None`：Task 4 定义，Task 5 三处调用、Task 6 PG 用例调用，签名一致（第二个参数一律是 ORM `Item`，不是 code 字符串）。
- `kv_add_int_pending` / `kv_take_int_pending` 的 `updated_by` 一律 keyword-only，`_skim` 与测试都按 keyword 传。
- `TAX_CARRY_KEY` / `CARRY_SCALE` 在 Task 1 定义，Task 2 PG 用例引用同名常量。
- `_backfill_stock(bind) -> int` 在 Task 3 迁移里定义，同 Task 的测试按 `module._backfill_stock(sync_conn)` 调用。
- `charged_sc` 键名在 `shop_service.purchase`（写）与 `_resident_work_effect`（读）两处拼写一致。
