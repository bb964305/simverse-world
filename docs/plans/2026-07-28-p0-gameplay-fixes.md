# SimVerse P0 玩家实测问题修复批次 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉玩家实测暴露的 6 条 P0 缺陷——赛季页整页崩溃（连带议案投票不可用 + 内部字段泄漏）、辩论生命周期无驱动器（玩家押注币永久冻结）、村落日报正文为空且永久钉死、赛季系统无开季入口（记分全静默丢弃）、幽灵票压死真人投票。

**Architecture:** 五个互相独立的改动面，可并行实现、串行合并。后端全部走「服务层改逻辑 + TDD 覆盖 + cron 接线」，不动任何 schema（零新迁移）。前端只改 `SeasonsPage` 一个页面 + 其 API 类型。所有新增 Settings 字段必须同步 `.env.example`（有硬门测试）。

**Tech Stack:** FastAPI + SQLAlchemy 2.x async (`Mapped`/`mapped_column`) + PostgreSQL(pgvector) + Redis；pytest + anyio + fakeredis + httpx ASGITransport；前端 React 19 + vitest 4 + @testing-library/react + jsdom。

## Global Constraints

- **零迁移**：本批次不新增任何 alembic revision。`051_add_civic_standing_history` 是唯一 head，不许动。
- **测试基线**：本机全量口径 `LAB_ADAPTER=mock LAB_ENABLED=false`，基线 `49 failed / 2591 passed / 1 skipped`，49 条全在 `tests/test_lab_*` 下。验收判据是**相对基线零新增失败**，不是 literal 0 failed。基线失败清单已存 `/tmp/sv_baseline_failures.txt`。
- **前端基线**：`npm test`（= `vitest run`）当前 `34 files / 139 tests` 全绿。前端硬线是 **0 failed**。
- **新增 Settings 字段必须同步 `.env.example`**：`backend/tests/test_env_example_consistency.py:180` 的 `test_every_settings_field_is_documented_or_allowlisted` 会红。字段名小写 ↔ 环境变量大写自动映射（`Settings` 无 `env_prefix`）。
- **新增 admin 路由会被自动纳入鉴权 sweep**：`backend/tests/test_admin_authz_sweep.py:26-45` 用 `pkgutil.iter_modules` 发现 `app/routers/admin/*.py` 的每条路由，断言匿名→401、非 admin→403、被封 admin→403。每个 handler 必须写 `admin: User = Depends(require_admin)`。
- **测试 fixture 名固定**：DB 用 `db_session`，HTTP 用 `client`（两者共享 `db_engine` 但是不同 session，跨 fixture 读数据前必须 `await db_session.commit()`）。异步测试统一 `@pytest.mark.anyio`。
- **`db_session` 是 `expire_on_commit=False`**：验证「真落库的值」时必须用列级 `select(Poll.options_json)` 绕开 identity map，不能 `refresh` 后读实体（多处既有测试就是这么写的，照抄）。
- **改既有测试断言时**，commit message 里必须写明「改的是规格，不是为了让它绿」，并说清规格为何变。
- **一 step 一 commit**，commit 末尾带真实 `Verified-by:` 输出（贴实际命令与结果行）。禁 `--no-verify` / `amend` / `squash`。
- **本批次只改代码，不碰生产库**。存量数据处置（辩论退款 / 空日报补数 / 幽灵票重算）是**单独一次变更**，在代码上线并观察之后进行。

## 已拍板的产品决策（不要重新讨论）

1. **镇长选举保留在赛季页，但拆成独立区块单独标记展示**（不与普通议案混列）。
2. **幽灵票只撤「slug 已不在 `residents` 表」的票**，按 `resident_type` 降级的**保留**——尊重 F2 的「投票时具备资格即计票」语义（`civic_service.py:31-42`），只清 2026-07-25 事故的物理删除残留。这个口径下 `test_civic_frozen_denominator.py:286` 的 `test_ghost_votes_are_kept_by_design` 仍应为绿（它的 `_demote()` 只改 `resident_type`，Resident 行还在），**该测试不许改**。

## File Structure

| 文件 | 责任 | 归属 Task |
|---|---|---|
| `backend/app/services/script_service.py` | `open_polls` 的对外投影 + `is_election` 标记 | 1 |
| `backend/app/routers/townhall.py` | `_open_proposals` 复用 `is_election`；`_recent_election` 同类投影 | 1 |
| `backend/tests/test_polls_api.py`（新） | `/polls/open` 的 HTTP 层回归护栏（形状 + 不泄漏） | 1 |
| `frontend/src/services/api/world.ts` | `PollOption`/`PollData` 真实类型 | 1 |
| `frontend/src/pages/SeasonsPage.tsx` | 取 `.label` 渲染 + 选举独立区块 | 1 |
| `frontend/src/pages/SeasonsPage.test.tsx`（新） | 对象形状不崩 + 投票路径 | 1 |
| `backend/app/services/debate_service.py` | `drive_due_debates` 推进器 + 超时兜底 | 2 |
| `backend/app/tasks/event_cron.py` | 推进器接线 | 2 |
| `backend/app/config.py` / `backend/.env.example` | 2 个 debate 旋钮、1 个 season 旋钮 | 2, 4 |
| `backend/tests/test_debate_driver.py`（新） | 状态机三段转换 + 超时退款 | 2 |
| `backend/app/services/digest_service.py` | compose 走 `chat()` 包装 + 空正文守卫 | 3 |
| `backend/tests/test_digest_empty_guard.py`（新） | 空返回不落库 / 回填 / 幂等 | 3 |
| `backend/app/routers/admin/seasons.py`（新） | 开季 / 结季 admin 入口 | 4 |
| `backend/app/routers/seasons.py` | `GET /seasons` 列表 | 4 |
| `backend/app/services/script_service.py` | `ensure_active_season` 自动开季 | 4 |
| `backend/tests/test_season_admin.py`（新） | 开季 / 缓存失效 / 自动开季 | 4 |
| `backend/app/services/civic_service.py` | `_npc_voters` 结构升级 + 撤票 + 结票候选人校验 | 5 |
| `backend/tests/test_ghost_vote_revocation.py`（新） | 旧格式兼容 / 只撤已删除 / 候选人失效归零 | 5 |

---

## Task 1: 投票链——后端白名单投影 + 选举标记，前端修渲染 + 独立区块

一次解掉 #4（React #31 整页崩）、#2（投票按钮从未进 DOM）、#4b（`_npc_voters` 全名单泄漏给匿名客户端）。

**Files:**
- Modify: `backend/app/services/script_service.py:122-151`
- Modify: `backend/app/routers/townhall.py:81-107`
- Create: `backend/tests/test_polls_api.py`
- Modify: `frontend/src/services/api/world.ts:90-97`
- Modify: `frontend/src/pages/SeasonsPage.tsx`
- Create: `frontend/src/pages/SeasonsPage.test.tsx`

**Interfaces:**
- Produces: `script_service.public_option(o: dict | str) -> dict`，返回恒为 `{"label": str, "npc_votes": int}`。
- Produces: `open_polls()` 每个元素新增 `"is_election": bool` 键（`question.startswith(ELECTION_TAG)`）。
- Produces: 前端 `export interface PollOption { label: string; npc_votes?: number }`；`PollData.options: PollOption[]`、`season_id: string | null`、`is_election?: boolean`。

### 背景事实（实现时不要重新查）

- `open_polls` 现签名：`async def open_polls(db, season_id: str | None = None, user_id: str | None = None) -> list[dict]`（`script_service.py:125`）。泄漏点是 `:140` 的 `"options": poll.options_json or [],`。
- `options_json` 元素恒为 dict（唯一写入口 `civic_service.propose:61-64`），`opts[0]` 上另挂 `_proposer_slug` / `_eligible_at_open` / `_npc_voters`，结票后 `opts[win]` 挂 `won` / `final_votes`；policy poll 的 `opts[0]` 还挂 `_policy_key` / `_policy_threshold` / `_policy_quorum` / `_policy_outcome`。**必须白名单，不能黑名单剔 `_` 前缀。**
- 但 `backend/tests/test_script_season.py:84` 手工造的 season poll 用的是**字符串列表** `["管家", "园丁", "医生"]` —— string 分支必须保留。
- `ELECTION_TAG = "镇长选举"`（`election_service.py:31`），election poll 的 question 是 `"镇长选举:谁来当下一任镇长?"`（半角冒号问号）。
- `townhall.py:85` 现在写的是 `[p for p in polls if not p["question"].startswith(election_service.ELECTION_TAG)]`。
- `open_polls` 的两个调用点：`polls.py:38`（传 `user_id`）、`townhall.py:82`（不传）。
- `app/tasks/office_audit.py:177` 与 `civic_service._close_one` 读的是 **ORM 上的 `poll.options_json`**，不经过 `open_polls`，不受本改动影响。
- 前端 `SeasonsPage.tsx` 崩溃点是 `:177` 的 `{opt}`；`PollCard` 在 `:116-188`；页面「🗳️ 投票」区块在 `:329-341`。

- [ ] **Step 1: 写后端失败测试（形状 + 不泄漏 + 选举标记）**

创建 `backend/tests/test_polls_api.py`：

```python
"""/polls/open 的对外契约护栏：白名单投影 + 选举标记。

玩家实测 #4/#2/#4b 的根因是 open_polls 把 options_json 原样吐出：前端按
string[] 渲染 dict 触发 React #31 整页崩，同时 _npc_voters 全名单、未落地
建筑坐标、提案人 slug 全部泄漏给未鉴权客户端。这里钉的是「对外只有
label + npc_votes」，杜绝形状与泄漏面再次漂移。
"""
import json

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.models.season import Poll
from app.services import civic_service, election_service, script_service


def _res(slug, name):
    return Resident(slug=slug, name=name, district="town_hall", status="idle",
                    resident_type="npc", creator_id="sys", tile_x=1, tile_y=1)


@pytest.mark.anyio
async def test_open_polls_projects_only_label_and_npc_votes(db_session):
    """一张真 civic poll（带全部内部 blob）投影后只剩两个键。"""
    db_session.add_all([_res("prop", "提案人"), _res("voter", "投票人")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "在南苑空地兴建一座邮局",
        [{"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
            "slug": "post_office", "bounds": [44, 100, 48, 106]}}},
         {"label": "暂缓,维持现状", "effect": None}],
        proposer_slug="prop",
    )
    assert poll is not None
    await civic_service.run_npc_voting(db_session)

    out = await script_service.open_polls(db_session)
    assert len(out) == 1
    for opt in out[0]["options"]:
        assert set(opt) == {"label", "npc_votes"}
        assert isinstance(opt["label"], str)
        assert isinstance(opt["npc_votes"], int)


@pytest.mark.anyio
async def test_open_polls_leaks_no_internal_fields(db_session):
    """整个响应序列化后不得出现任何内部键——黑名单挡不住新增键，这里查全文。"""
    db_session.add_all([_res("prop", "提案人"), _res("voter", "投票人")])
    await db_session.commit()
    await civic_service.propose(
        db_session, "在东岸花园兴建一座剧院",
        [{"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
            "slug": "theater", "center": [175, 45]}}},
         {"label": "暂缓,维持现状", "effect": None}],
        proposer_slug="prop",
    )
    await civic_service.run_npc_voting(db_session)

    blob = json.dumps(await script_service.open_polls(db_session), ensure_ascii=False)
    for leaked in ("_npc_voters", "_proposer_slug", "_eligible_at_open",
                   "effect", "theater", "175"):
        assert leaked not in blob, f"{leaked} 泄漏到了对外响应里"


@pytest.mark.anyio
async def test_open_polls_flags_elections(db_session):
    """选举 poll 带 is_election=True，普通议案为 False——前端据此拆区块。"""
    db_session.add_all([_res("a", "候选甲"), _res("b", "候选乙")])
    await db_session.commit()
    await election_service.open_election(db_session, candidate_slugs=["a", "b"])
    await civic_service.propose(
        db_session, "广场是否加装长椅",
        [{"label": "支持", "effect": None}, {"label": "反对", "effect": None}],
    )

    out = await script_service.open_polls(db_session)
    by_election = {p["is_election"]: p["question"] for p in out}
    assert by_election[True].startswith(election_service.ELECTION_TAG)
    assert by_election[False] == "广场是否加装长椅"


@pytest.mark.anyio
async def test_string_options_still_supported(db_session):
    """历史/回滚数据可能是 string[]（test_script_season 就这么造）——不许炸。"""
    db_session.add(Poll(question="谁是凶手？", options_json=["管家", "园丁"],
                        status="open"))
    await db_session.commit()

    out = await script_service.open_polls(db_session)
    assert out[0]["options"] == [{"label": "管家", "npc_votes": 0},
                                 {"label": "园丁", "npc_votes": 0}]


@pytest.mark.anyio
async def test_polls_open_endpoint_is_anonymous_and_clean(client, db_session):
    """HTTP 层同样干净——生产泄漏就是匿名 curl 拿到的。"""
    db_session.add_all([_res("prop", "提案人"), _res("voter", "投票人")])
    await db_session.commit()
    await civic_service.propose(
        db_session, "旱季供水改造",
        [{"label": "赞成", "effect": {"type": "system_config", "key": "x",
                                       "value": 1}},
         {"label": "反对", "effect": None}],
        proposer_slug="prop",
    )
    await civic_service.run_npc_voting(db_session)

    resp = await client.get("/polls/open")
    assert resp.status_code == 200
    body = resp.text
    assert "_npc_voters" not in body and "_proposer_slug" not in body
    assert "system_config" not in body
    polls = resp.json()["polls"]
    assert polls and set(polls[0]["options"][0]) == {"label", "npc_votes"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_polls_api.py -q`

Expected: FAIL —— 前三条因 `set(opt) == {"label","npc_votes"}` 不成立 / `KeyError: 'is_election'` 而红。

- [ ] **Step 3: 实现后端投影**

在 `backend/app/services/script_service.py` 的 `open_polls` 之前（`:122` 的分节注释之后）插入：

```python
def public_option(o) -> dict:
    """把一个 poll option 投影成对外形状。

    ``options_json`` 元素恒为 ``civic_service.propose`` 写的 dict，且 opts[0]
    上挂着 ``effect`` / ``_proposer_slug`` / ``_npc_voters`` / ``_eligible_at_open``
    / policy 的 ``_policy_*`` 等内部 blob —— 一个都不许出网。用白名单而不是
    黑名单剔 ``_`` 前缀：黑名单挡不住将来新增的非下划线内部键（``won`` /
    ``final_votes`` 就是现成的例子）。

    string 分支只为兜历史/回滚数据（``tests/test_script_season.py`` 手工造的
    season poll 用的就是 ``["管家", "园丁"]``）。
    """
    if isinstance(o, str):
        return {"label": o, "npc_votes": 0}
    return {
        "label": str((o or {}).get("label", "")),
        "npc_votes": int((o or {}).get("npc_votes") or 0),
    }
```

然后把 `open_polls` 的 `out.append({...})` 块（`:137-141`）整体替换为：

```python
        from app.services.election_service import ELECTION_TAG
        out.append({
            "id": poll.id, "season_id": poll.season_id, "question": poll.question,
            "options": [public_option(o) for o in (poll.options_json or [])],
            "closes_at": poll.closes_at.isoformat() if poll.closes_at else None,
            # 选举与普通议案共用 polls 表；前端按这个标记拆区块，市政厅按它
            # 过滤。判据集中在这一处，避免两边各写一次 startswith。
            "is_election": bool((poll.question or "").startswith(ELECTION_TAG)),
        })
```

（`ELECTION_TAG` 用函数内延迟 import：`election_service` 顶层不引 `script_service`，但沿用本仓通行的延迟 import 风格更稳。把这行 import 提到 `open_polls` 函数体顶部、`now = datetime.now(UTC)` 之前更省事——每个 poll 重复 import 无害但没必要。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_polls_api.py -q`

Expected: `5 passed`

- [ ] **Step 5: 市政厅侧复用标记 + 补 `_recent_election` 投影**

`backend/app/routers/townhall.py`，把 `_open_proposals`（`:81-85`）改为：

```python
async def _open_proposals(db: AsyncSession) -> list[dict]:
    polls = await script_service.open_polls(db)
    # Elections ride the same Poll table; the panel lists them separately, so
    # the "proposals" section drops anything flagged as a mayor election.
    # 判据来自 open_polls 的 is_election，不在这里第二次写 startswith。
    return [p for p in polls if not p.get("is_election")]
```

把 `_recent_election`（`:88-107`）的 `"options": opts,` 一行改为：

```python
        # 结票场景额外放行 won / final_votes（面板要显示得票），其余内部键同样
        # 不出网 —— 一旦有已结束选举，原样返回会漏 _npc_voters 全名单。
        "options": [
            {**script_service.public_option(o),
             "won": bool((o or {}).get("won")),
             "final_votes": (o or {}).get("final_votes")}
            for o in opts
        ],
```

- [ ] **Step 6: 跑市政厅回归**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_townhall.py tests/test_polls_api.py tests/test_script_season.py tests/test_m3_civic.py tests/test_m6_election.py -q`

Expected: 全绿。特别确认 `test_townhall.py::test_overview_aggregates_mayor_duties_polls_and_finances` 仍过（它断言 `recent_election` 的 `winner_name`/`winner_votes`，这两个走的是 `_recent_election` 顶层字段而非 `options`，不受投影影响）。

- [ ] **Step 7: 提交后端部分**

```bash
git add backend/app/services/script_service.py backend/app/routers/townhall.py backend/tests/test_polls_api.py
git commit -m "$(cat <<'EOF'
fix(polls): /polls/open 白名单投影 option + 标记选举——修 #4/#2/#4b

open_polls 原样吐 options_json，导致三件事同时发生：
- 前端按 string[] 渲染 dict → React #31，赛季页整页被 ErrorBoundary 换掉（#4）
- 投票按钮因此从未进入 DOM，votes 表恒为 0（#2，不是接口缺失）
- _npc_voters 全名单 / 未落地建筑坐标 / 提案人 slug 泄漏给匿名客户端（#4b）

用白名单投影（只留 label + npc_votes）而非黑名单剔 _ 前缀：黑名单挡不住
新增的非下划线内部键（won / final_votes 就是现成例子）。同时输出 is_election
标记，市政厅的过滤改为复用它，判据不再两处各写一次 startswith。
_recent_election 走同一投影并额外放行 won / final_votes。

保留 string 分支兜历史/回滚数据。office_audit 与 _close_one 读的是 ORM 上的
poll.options_json，不经过本函数，不受影响。

Verified-by: pytest tests/test_polls_api.py tests/test_townhall.py
             tests/test_script_season.py tests/test_m3_civic.py
             tests/test_m6_election.py -q → <贴真实结果行>
EOF
)"
```

- [ ] **Step 8: 前端类型改真**

`frontend/src/services/api/world.ts`，把 `PollData`（`:90-97`）替换为：

```ts
export interface PollOption {
  label: string
  npc_votes?: number
}

export interface PollData {
  id: string
  /** 生产实测 civic poll 恒为 null（不挂赛季）——原来声明 string 是假的 */
  season_id: string | null
  question: string
  options: PollOption[]
  closes_at: string | null
  my_vote?: number
  /** 镇长选举与普通议案共用 polls 表，后端标记，前端拆区块展示 */
  is_election?: boolean
}
```

- [ ] **Step 9: 写前端失败测试**

创建 `frontend/src/pages/SeasonsPage.test.tsx`：

```tsx
import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SeasonsPage } from './SeasonsPage'

const getCurrentSeason = vi.fn()
const getSeasonLeaderboard = vi.fn()
const getOpenPolls = vi.fn()
const votePoll = vi.fn()

vi.mock('../services/api', () => ({
  getCurrentSeason: (...a: unknown[]) => getCurrentSeason(...a),
  getSeasonLeaderboard: (...a: unknown[]) => getSeasonLeaderboard(...a),
  getOpenPolls: (...a: unknown[]) => getOpenPolls(...a),
  votePoll: (...a: unknown[]) => votePoll(...a),
}))

vi.mock('../components/TopNav', () => ({ TopNav: () => <nav /> }))
vi.mock('../stores/gameStore', () => ({
  useGameStore: (sel: (s: unknown) => unknown) => sel({ user: { id: 'u1' } }),
}))

// 生产 /polls/open 的真实形状：option 是对象，不是字符串。
// 这里刻意保留旧后端才会出现的内部字段，钉住「前端自己也不能崩」。
const CIVIC_POLL = {
  id: 'poll-civic',
  season_id: null,
  question: '在南苑空地兴建一座邮局',
  options: [
    { label: '赞成兴建', npc_votes: 20 },
    { label: '暂缓,维持现状', npc_votes: 3 },
  ],
  closes_at: null,
  is_election: false,
}

const ELECTION_POLL = {
  id: 'poll-elec',
  season_id: null,
  question: '镇长选举:谁来当下一任镇长?',
  options: [{ label: '赵启文', npc_votes: 17 }, { label: '何巧云', npc_votes: 5 }],
  closes_at: null,
  is_election: true,
}

beforeEach(() => {
  getCurrentSeason.mockReset().mockResolvedValue({ season: null })
  getSeasonLeaderboard.mockReset().mockResolvedValue({ top: [], season: null })
  getOpenPolls.mockReset().mockResolvedValue({ polls: [CIVIC_POLL] })
  votePoll.mockReset().mockResolvedValue({ ok: true })
})

afterEach(cleanup)

function renderPage() {
  return render(<MemoryRouter><SeasonsPage /></MemoryRouter>)
}

describe('SeasonsPage', () => {
  it('renders object-shaped options as their label instead of crashing', async () => {
    renderPage()
    expect(await screen.findByRole('button', { name: /赞成兴建/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /暂缓/ })).toBeInTheDocument()
  })

  it('casts a vote and marks the chosen option', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /赞成兴建/ }))
    await waitFor(() => expect(votePoll).toHaveBeenCalledWith('poll-civic', 0))
    expect(await screen.findByText('✓已投')).toBeInTheDocument()
  })

  it('restores the voted marker from my_vote across reloads', async () => {
    getOpenPolls.mockResolvedValue({ polls: [{ ...CIVIC_POLL, my_vote: 1 }] })
    renderPage()
    expect(await screen.findByText('✓已投')).toBeInTheDocument()
    expect(votePoll).not.toHaveBeenCalled()
  })

  it('shows the already-voted branch when the backend rejects a repeat', async () => {
    votePoll.mockRejectedValue(new Error('API 400: {"detail":"already voted on this poll"}'))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /赞成兴建/ }))
    expect(await screen.findByText('已投过')).toBeInTheDocument()
  })

  it('splits mayor elections into their own labelled section', async () => {
    getOpenPolls.mockResolvedValue({ polls: [CIVIC_POLL, ELECTION_POLL] })
    renderPage()
    expect(await screen.findByText('🏛️ 镇长选举')).toBeInTheDocument()
    expect(screen.getByText('🗳️ 议案投票')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /赵启文/ })).toBeInTheDocument()
  })

  it('hides the election section when there is no election running', async () => {
    renderPage()
    expect(await screen.findByText('🗳️ 议案投票')).toBeInTheDocument()
    expect(screen.queryByText('🏛️ 镇长选举')).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 10: 跑前端测试确认失败**

Run: `cd frontend && npm test -- src/pages/SeasonsPage.test.tsx`

Expected: FAIL —— 第一条就因 React #31（"Objects are not valid as a React child"）挂掉。

- [ ] **Step 11: 修 SeasonsPage 渲染 + 拆区块**

`frontend/src/pages/SeasonsPage.tsx` 改三处。

(a) `PollCard` 的 option 循环（`:155` 起），把 `poll.options.map` 那段改为：

```tsx
        {poll.options.map((opt, idx) => {
          // 后端已投影成 {label, npc_votes}；保留 string 分支，使后端未部署 /
          // 回滚到旧版本时页面仍然只是显示得糙，而不是整页崩。
          const label = typeof opt === 'string' ? opt : (opt?.label ?? `选项 ${idx + 1}`)
          const chosen = state?.kind === 'voted' && state.idx === idx
```

并把 `:177` 的渲染行改为：

```tsx
              {label}{chosen && <span style={{ marginLeft: 8, fontWeight: 600 }}>✓已投</span>}
```

(b) 在 `PollCard` 之后新增一个区块渲染器：

```tsx
function PollSection({ polls }: { polls: PollData[] }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {polls.map((p) => <PollCard key={p.id} poll={p} />)}
    </div>
  )
}
```

(c) 把页面 body 里「🗳️ 投票」那一段（`:329-341`）整体替换为：

```tsx
          {(() => {
            const elections = (polls ?? []).filter((p) => p.is_election)
            const proposals = (polls ?? []).filter((p) => !p.is_election)
            return (
              <>
                <SectionTitle>🗳️ 议案投票</SectionTitle>
                {polls === null && !pollsErr && <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>加载中…</div>}
                {pollsErr && <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{pollsErr}</div>}
                {polls !== null && proposals.length === 0 && (
                  <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无进行中的议案。</div>
                )}
                {proposals.length > 0 && <PollSection polls={proposals} />}

                {elections.length > 0 && (
                  <>
                    <SectionTitle>🏛️ 镇长选举</SectionTitle>
                    <div style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 8 }}>
                      镇长选举与普通议案分开计票，当选者将获得全镇工资加成。
                    </div>
                    <PollSection polls={elections} />
                  </>
                )}
              </>
            )
          })()}
```

- [ ] **Step 12: 跑前端测试确认通过**

Run: `cd frontend && npm test -- src/pages/SeasonsPage.test.tsx`

Expected: `6 passed`

- [ ] **Step 13: 跑前端全量 + typecheck**

Run: `cd frontend && npm test && npx tsc -b --noEmit`

Expected: `35 files / 145 tests` 全绿（原 34/139 + 新增 1 文件 6 用例），tsc 无错。

- [ ] **Step 14: 提交前端部分**

```bash
git add frontend/src/services/api/world.ts frontend/src/pages/SeasonsPage.tsx frontend/src/pages/SeasonsPage.test.tsx
git commit -m "$(cat <<'EOF'
fix(seasons): 赛季页按 label 渲染 option + 选举拆独立区块——修 #4/#2

world.ts 声明 options: string[] 是假的（后端从来只写 dict），SeasonsPage:177
把对象当 React child 渲染触发 #31，ErrorBoundary 把整页（含排行榜）换成
「页面出错了」——投票按钮因此从未进入 DOM，这才是 votes 表恒为 0 的原因。

season_id 一并从 string 改成 string | null（生产实测三张 poll 全是 null）。
保留 typeof opt === 'string' 分支，使后端未部署/回滚时页面只是显示得糙而不崩。

镇长选举拆到带说明的独立区块（产品拍板：保留可投但不与普通议案混列）。
SeasonsPage 此前零测试覆盖，是这个 bug 活到生产的直接原因——本次补 6 个用例，
mock 数据用生产真实的对象形状。

Verified-by: npm test → <贴真实结果行>；npx tsc -b --noEmit → 无输出
EOF
)"
```

---

## Task 2: 辩论推进器——drive_due_debates + event_cron 接线 + 超时退款兜底

修 #3-1 / E3：`run_live` 与 `settle` 在 `app/` 下零调用方，辩论建出来就永远停在 `announced`，玩家押注已扣币但 `payout` 永远 NULL。

**Files:**
- Modify: `backend/app/config.py`（Settings 加 3 个字段）
- Modify: `backend/.env.example`（加对应 3 行）
- Modify: `backend/app/services/debate_service.py`（新增 `drive_due_debates`）
- Modify: `backend/app/tasks/event_cron.py`（接线）
- Create: `backend/tests/test_debate_driver.py`

**Interfaces:**
- Produces: `debate_service.drive_due_debates(db) -> dict`，返回 `{"live": int, "settled": int, "refunded": int}`。
- Consumes: 既有 `run_live(db, debate: Debate) -> Debate`（要求 `status == "announced"`，否则 raise `DebateError`）、`settle(db, debate_id: str) -> dict`（要求 `status == "voting"`，`settled` 时幂等返回）、`_auto_draw_refund(db, debate: Debate) -> None`。

### 背景事实

- `Debate` 只有 `starts_at`（创建时 `default=now`）与 `settled_at`，**没有记录进入 voting 时刻的列**。不动 schema 的前提下，`settle` 的判据只能用 `starts_at + stake_window + vote_window` 推算。
- `event_cron_loop`（`event_cron.py:20-73`）：60s 一轮，`async with async_session() as db:` **单个 session 包住整轮所有子步骤**（与 nightly_cron 的每步一个 session 相反），每个子步骤延迟 import + 自己一层 `try/except → logger.warning(..., exc_info=True)`。
- `config.py` 现在**没有任何 `debate_` 字段**，`.env.example` 也没有 `DEBATE_`。
- `run_live` 内部 LLM 失败会自己走 `_auto_draw_refund` 并 `return debate`（不抛），所以推进器里调它是安全的。
- 生产存量：辩论 `1c00ba36` 建于 2026-07-26，至今 `announced`；玩家 10 SC 已扣（`transactions` 有 `debate_stake:1c00ba36...` -10，无对应 win/refund）。上线后第一个 tick 会被 24h 兜底捞走并退款——这属于代码生效的自然结果，不算「写生产数据」。

- [ ] **Step 1: 加 Settings 字段 + .env.example**

`backend/app/config.py`，在 `election_interval_days`（`:537`）那一组之后插入：

```python
    # E9 辩论擂台生命周期推进（event_cron 每 60s 一轮）。两个窗口都从
    # Debate.starts_at 起算——debates 表没有记录进入 voting 时刻的列，不动
    # schema 的前提下 settle 的判据只能是 stake_window + vote_window 之和。
    debate_stake_window_min: int = 30    # announced 满这么久 → 开打（run_live）
    debate_vote_window_min: int = 60     # voting 满这么久 → 结算（settle）
    debate_stuck_hours: int = 24         # 卡在非终态超过这么久 → 平局全额退款
```

`backend/.env.example`，在 `ELECTION_INTERVAL_DAYS=28`（`:500`）之后插入：

```
# ── E9 辩论擂台生命周期（event_cron 60s 轮询推进）────────────────────────
# 两个窗口都从 debates.starts_at 起算：表里没有记录进入 voting 时刻的列。
# announced 满 STAKE_WINDOW 分钟 → run_live（押注截止，开始辩论）
DEBATE_STAKE_WINDOW_MIN=30
# voting 满 VOTE_WINDOW 分钟（即 starts_at + STAKE + VOTE）→ settle（结算派彩）
DEBATE_VOTE_WINDOW_MIN=60
# 兜底：卡在 announced/live/voting 超过这么多小时 → 平局全额退款，绝不让
# 玩家的押注币无限期冻结（2026-07-28 生产上一笔 10 SC 冻结了 2 天）。
DEBATE_STUCK_HOURS=24
```

- [ ] **Step 2: 确认一致性门通过**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_env_example_consistency.py -q`

Expected: `passed`（若红，说明 `.env.example` 键名与字段名没对上）。

- [ ] **Step 3: 写推进器失败测试**

创建 `backend/tests/test_debate_driver.py`：

```python
"""E3/#3-1 辩论生命周期推进器。

run_live 与 settle 在 app/ 下零调用方（debate_service.py:57-58 的注释自己
承认了），辩论建出来就停在 announced：不产生辩词、不开投票、不结算，而
stake 接口是开放的且真扣币——玩家的押注币被永久冻结。生产上 1c00ba36 冻结
了一笔 10 SC 超过 2 天。
"""
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.debate import Debate, DebateStake
from app.models.resident import Resident
from app.models.user import User
from app.services import debate_service as ds


def _mock_client(text="我方观点更站得住脚。"):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _user(db, email, bal=1000):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _residents(db):
    db.add_all([
        Resident(slug="ann", name="安", creator_id="system", district="cafe",
                 status="idle", tile_x=1, tile_y=1),
        Resident(slug="bo", name="波", creator_id="system", district="cafe",
                 status="idle", tile_x=2, tile_y=2),
    ])
    await db.commit()


async def _debate(db, *, status="announced", age_min=0):
    await _residents(db)
    d = await ds.create_debate(db, "猫和狗谁更好", "ann", "bo")
    d.status = status
    d.starts_at = datetime.now(UTC) - timedelta(minutes=age_min)
    await db.commit()
    return d


@pytest.mark.anyio
async def test_announced_inside_the_stake_window_is_left_alone(db_session):
    """押注窗口没满就开打 = 提前掐断玩家下注。"""
    d = await _debate(db_session, age_min=1)
    with patch.object(ds, "get_client", return_value=_mock_client()):
        moved = await ds.drive_due_debates(db_session)
    assert moved["live"] == 0
    await db_session.refresh(d)
    assert d.status == "announced"


@pytest.mark.anyio
async def test_announced_past_the_stake_window_goes_live_then_voting(db_session):
    d = await _debate(db_session, age_min=settings.debate_stake_window_min + 1)
    with patch.object(ds, "get_client", return_value=_mock_client()), \
         patch.object(ds, "record_usage", new_callable=AsyncMock):
        moved = await ds.drive_due_debates(db_session)
    assert moved["live"] == 1
    await db_session.refresh(d)
    assert d.status == "voting"
    assert len(d.transcript_json) == ds.ROUNDS


@pytest.mark.anyio
async def test_voting_past_the_vote_window_settles_and_pays_out(db_session):
    d = await _debate(db_session)
    a1 = await _user(db_session, "drv-a@d.com", bal=1000)
    b1 = await _user(db_session, "drv-b@d.com", bal=1000)
    await ds.stake(db_session, d.id, a1.id, "a", 100)
    await ds.stake(db_session, d.id, b1.id, "b", 100)
    d.status = "voting"
    d.votes_a = 5  # a 胜
    d.starts_at = datetime.now(UTC) - timedelta(
        minutes=settings.debate_stake_window_min + settings.debate_vote_window_min + 1)
    await db_session.commit()

    moved = await ds.drive_due_debates(db_session)
    assert moved["settled"] == 1
    await db_session.refresh(d)
    assert d.status == "settled" and d.winner == "a"
    stakes = (await db_session.execute(
        select(DebateStake).where(DebateStake.debate_id == d.id))).scalars().all()
    assert all(s.payout is not None for s in stakes)


@pytest.mark.anyio
async def test_voting_inside_the_vote_window_is_left_alone(db_session):
    d = await _debate(db_session, status="voting",
                      age_min=settings.debate_stake_window_min + 1)
    moved = await ds.drive_due_debates(db_session)
    assert moved["settled"] == 0
    await db_session.refresh(d)
    assert d.status == "voting"


@pytest.mark.anyio
async def test_a_debate_stuck_past_the_deadline_refunds_every_stake(db_session):
    """兜底：无论卡在哪个非终态，超过 stuck_hours 一律平局全额退款。

    这条正是生产上 1c00ba36 的处境——建于 07-26，到 07-28 仍是 announced，
    玩家 10 SC 冻结。上线后第一个 tick 必须把它捞走。
    """
    d = await _debate(db_session, age_min=settings.debate_stuck_hours * 60 + 1)
    u = await _user(db_session, "stuck@d.com", bal=1000)
    await ds.stake(db_session, d.id, u.id, "a", 10)
    await db_session.refresh(u)
    assert u.soul_coin_balance == 990

    moved = await ds.drive_due_debates(db_session)
    assert moved["refunded"] == 1
    await db_session.refresh(d)
    assert d.status == "settled" and d.winner == "draw"
    await db_session.refresh(u)
    assert u.soul_coin_balance == 1000  # 全额退回


@pytest.mark.anyio
async def test_stuck_sweep_takes_priority_over_going_live(db_session):
    """超期的 announced 走退款，不该先被 run_live 拉起来再跑一场 LLM。"""
    d = await _debate(db_session, age_min=settings.debate_stuck_hours * 60 + 1)
    client = _mock_client()
    with patch.object(ds, "get_client", return_value=client):
        moved = await ds.drive_due_debates(db_session)
    assert moved["refunded"] == 1 and moved["live"] == 0
    client.messages.create.assert_not_called()


@pytest.mark.anyio
async def test_settled_debates_are_never_touched_again(db_session):
    d = await _debate(db_session, status="settled",
                      age_min=settings.debate_stuck_hours * 60 + 1)
    d.winner = "a"
    await db_session.commit()
    moved = await ds.drive_due_debates(db_session)
    assert moved == {"live": 0, "settled": 0, "refunded": 0}


@pytest.mark.anyio
async def test_one_failing_debate_does_not_block_the_others(db_session):
    """每条独立 try/except——一场辩论炸了不能让整轮 cron 停摆。"""
    good = await _debate(db_session, age_min=settings.debate_stake_window_min + 1)
    bad = Debate(topic="坏辩论", resident_a_slug="ghost-a",
                 resident_b_slug="ghost-b", status="announced",
                 starts_at=datetime.now(UTC) - timedelta(
                     minutes=settings.debate_stake_window_min + 1))
    db_session.add(bad)
    await db_session.commit()

    calls = {"n": 0}
    real_run_live = ds.run_live

    async def _flaky(db, debate):
        calls["n"] += 1
        if debate.id == bad.id:
            raise RuntimeError("boom")
        return await real_run_live(db, debate)

    with patch.object(ds, "run_live", _flaky), \
         patch.object(ds, "get_client", return_value=_mock_client()), \
         patch.object(ds, "record_usage", new_callable=AsyncMock):
        moved = await ds.drive_due_debates(db_session)

    assert calls["n"] == 2       # 两条都试过了
    assert moved["live"] == 1    # 好的那条成功推进
    await db_session.refresh(good)
    assert good.status == "voting"
```

- [ ] **Step 4: 跑测试确认失败**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_debate_driver.py -q`

Expected: FAIL —— `AttributeError: module 'app.services.debate_service' has no attribute 'drive_due_debates'`

- [ ] **Step 5: 实现推进器**

`backend/app/services/debate_service.py`，在 `_auto_draw_refund`（`:195-207`）之后、`# Settlement` 分节注释之前插入：

```python
def _aware(ts: datetime | None) -> datetime | None:
    """DB 可能回 naive datetime（sqlite 一定会）——统一补 UTC 再比较。"""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


async def drive_due_debates(db) -> dict:
    """把到期的辩论推过 announced → live → voting → settled。

    E3/#3-1：``run_live`` 与 ``settle`` 在 app/ 下本来零调用方，辩论建出来就
    停在 announced，而 stake 接口是开放的且真扣币 —— 玩家的押注被永久冻结。

    时间判据全部从 ``Debate.starts_at`` 起算：debates 表没有记录进入 voting
    时刻的列，不动 schema 就只能这么推算（``settle`` 的门槛 = stake_window +
    vote_window）。

    **超期兜底先跑**：卡在任何非终态超过 ``debate_stuck_hours`` 的一律平局
    全额退款，且优先于 run_live —— 否则一场卡了两天的辩论会先被拉起来跑一轮
    LLM，钱还是玩家的、时间却是错的。

    每条辩论单独 try/except：一场炸了不能让整轮 cron 停摆。
    """
    from app.config import settings

    now = datetime.now(UTC)
    moved = {"live": 0, "settled": 0, "refunded": 0}

    stuck_before = now - timedelta(hours=settings.debate_stuck_hours)
    rows = (await db.execute(
        select(Debate).where(Debate.status.in_(("announced", "live", "voting")))
    )).scalars().all()

    handled: set[str] = set()
    for d in rows:
        started = _aware(d.starts_at)
        if started is None or started > stuck_before:
            continue
        try:
            await _auto_draw_refund(db, d)
            handled.add(d.id)
            moved["refunded"] += 1
            logger.warning("debate %s stuck in %s for over %dh — auto-draw refunded",
                           d.id, d.status, settings.debate_stuck_hours)
        except Exception:
            logger.warning("debate stuck-sweep failed for %s", d.id, exc_info=True)

    live_before = now - timedelta(minutes=settings.debate_stake_window_min)
    for d in rows:
        if d.id in handled or d.status != "announced":
            continue
        started = _aware(d.starts_at)
        if started is None or started > live_before:
            continue
        try:
            await run_live(db, d)
            moved["live"] += 1
        except Exception:
            logger.warning("debate run_live failed for %s", d.id, exc_info=True)

    settle_before = now - timedelta(
        minutes=settings.debate_stake_window_min + settings.debate_vote_window_min)
    for d in rows:
        if d.id in handled or d.status != "voting":
            continue
        started = _aware(d.starts_at)
        if started is None or started > settle_before:
            continue
        try:
            await settle(db, d.id)
            moved["settled"] += 1
        except Exception:
            logger.warning("debate settle failed for %s", d.id, exc_info=True)

    return moved
```

同时把文件顶部的 import（`:21`）改为：

```python
from datetime import datetime, timedelta, UTC
```

> 注意：`run_live` 在这一轮里会把 `announced` 推到 `voting`，第三个循环用的是同一批 `rows` 对象（已被 `run_live` 就地改成 `voting`）。这是有意的——同一轮内刚开打的辩论 `starts_at` 必然还很新，过不了 `settle_before` 的门槛，不会被立刻结算。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_debate_driver.py tests/test_debates.py -q`

Expected: 全绿（新 8 条 + 既有 8 条）。

- [ ] **Step 7: event_cron 接线**

`backend/app/tasks/event_cron.py`，在 C3 块（`:51-61`）之后、`for event, phase in changes:` 广播循环（`:62`）之前插入：

```python
                # E3: 推进辩论生命周期（announced → live → voting → settled）。
                # run_live/settle 此前在 app/ 下零调用方，押注币会被永久冻结。
                try:
                    from app.services.debate_service import drive_due_debates
                    moved = await drive_due_debates(db)
                    if any(moved.values()):
                        logger.info("Event cron: debates live=%d settled=%d refunded=%d",
                                    moved["live"], moved["settled"], moved["refunded"])
                except Exception:
                    logger.warning("E3 debate driver step failed", exc_info=True)
```

- [ ] **Step 8: 写接线测试并跑**

在 `backend/tests/test_debate_driver.py` 末尾追加：

```python
def test_event_cron_wires_the_debate_driver():
    """接线本身是回归面：推进器写好了但没人调 = 什么都没修。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "tasks"
           / "event_cron.py").read_text(encoding="utf-8")
    assert "drive_due_debates" in src, "event_cron 必须调用辩论推进器"
```

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_debate_driver.py -q`

Expected: `9 passed`

- [ ] **Step 9: 提交**

```bash
git add backend/app/config.py backend/.env.example backend/app/services/debate_service.py backend/app/tasks/event_cron.py backend/tests/test_debate_driver.py
git commit -m "$(cat <<'EOF'
feat(debates): 补辩论生命周期推进器 + 超期退款兜底——修 #3-1/E3

run_live 与 settle 在 app/ 下零调用方（debate_service.py:57-58 的注释自己
承认了），辩论建出来就停在 announced：不产生辩词、不开投票、不结算。而
POST /debates/{id}/stake 是开放的且真扣币 —— 玩家押注被永久冻结。生产上
1c00ba36 冻了一笔 10 SC 超过 2 天。

drive_due_debates 接进 event_cron 的 60s 轮询。时间判据全部从 starts_at
起算：debates 表没有记录进入 voting 时刻的列，本次不动 schema。

超期兜底（debate_stuck_hours=24）优先于 run_live：卡了两天的辩论应该退钱，
不该先被拉起来跑一轮 LLM。每条辩论单独 try/except，一场炸了不停整轮 cron。

三个旋钮同步进 .env.example（test_env_example_consistency 是硬门）。

Verified-by: pytest tests/test_debate_driver.py tests/test_debates.py -q
             → <贴真实结果行>
EOF
)"
```

---

## Task 3: 日报——compose 走 chat() 包装 + 空正文不落库/可回填

修 #6-1 / #6-2：`compose_digest` 绕过 `llm.client.chat()` → thinking 没关 + `max_tokens=800` 把输出吃光 → 空正文；空正文照样落库，且 `(scope,date,user_id)` 幂等把空行永久钉死。

**Files:**
- Modify: `backend/app/services/digest_service.py`
- Modify: `backend/tests/test_weekly_recap.py`（patch 点变更，属规格变更）
- Modify: `backend/tests/test_opinion_service.py:535-563`（同上）
- Create: `backend/tests/test_digest_empty_guard.py`

**Interfaces:**
- Consumes: `app.llm.client.chat(system_prompt: str, messages: list[dict], model: str | None = None, max_tokens: int | None = None, *, owner: str = "system", meter: Meter | None = None, expects_json: bool = False) -> str`（**返回 str，不是 response 对象**）。
- Consumes: `app.llm.metering.Meter(scenario: str, resident_id=None, user_id=None, conversation_id=None, attempt_no=1)`（dataclass）。
- Produces: `digest_service.DigestComposeEmpty(RuntimeError)`。
- Produces: `compose_digest(day, material) -> tuple[str, str]`（签名不变）。

### 背景事实（两处必须纠正诊断报告的说法）

1. **换 `extract_text` 没有任何收益**：`client.py:83-92` 的 `extract_text` 与 `digest_service.py:32-36` 的 `_extract_text` **实现逐字相同**（都是"返回第一个有 `.text` 属性的 block"）。诊断报告把"显式跳过 ThinkingBlock"列为收益是错的。真正起作用的只有两条：`thinking={"type":"disabled"}`（`client.py:148-149`，只有 `chat()` 会加）和 `max_tokens` 800 → 2000。
2. **`chat()` 会换模型**：`client.py:140` 对 `owner="system"` 解析的是 `settings.background_model`，而不是 `compose_digest` 现在用的 `settings.effective_model`。生产 `.env` 没设 `BACKGROUND_LLM_MODEL`（已核实），两者当前相等——但**必须显式传 `model=settings.effective_model` 锁死**，不能靠这个巧合。

另：`llm_metering_enabled` 在测试里被 autouse fixture `_disable_llm_metering` 置为 False，所以带 `meter=` 不会产生真实 DB 写。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_digest_empty_guard.py`：

```python
"""#6-1/#6-2 村落日报正文为空 + 空行被幂等永久钉死。

compose_digest 绕过 app.llm.client.chat() 直调 messages.create，于是
thinking 没被关掉（chat() 是全仓唯一会加 thinking={"type":"disabled"} 的
地方），800 的 max_tokens 被推理吃光，响应里没有可用 text block → 返回空串。
generate_village_digest 拿到空串后无条件落库，而 (scope,date,user_id) 唯一
约束 + 「行存在就早返回」的幂等让这一天永远是空的。

生产实证：2026-07-17/24/25/26 四天 content_md 长度为 0，且四天全部落在
output_tokens=801（max_tokens 触顶）的样本里。
"""
from datetime import date, datetime, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, func

from app.models.digest import Digest
from app.models.memory import Memory


async def _material(db):
    """给 gather_material 一点素材，避免走冷启动兜底分支。"""
    db.add(Memory(resident_id="r1", type="event", content="今天大家聊得很开心",
                  importance=0.9, source="chat_resident",
                  created_at=datetime.now(UTC)))
    await db.commit()


@pytest.mark.anyio
async def test_compose_disables_thinking_and_raises_the_token_budget(db_session):
    """走 chat() 包装，且显式锁定 model —— 不靠 background_model 恰好相等。"""
    from app.config import settings
    from app.services import digest_service as ds

    captured = {}

    async def _fake_chat(system_prompt, messages, model=None, max_tokens=None, **kw):
        captured.update(system=system_prompt, messages=messages,
                        model=model, max_tokens=max_tokens, kw=kw)
        return "# 今日头条\n小镇很热闹"

    with patch.object(ds, "llm_chat", _fake_chat):
        title, content = await ds.compose_digest(date(2026, 7, 28), {
            "events": [], "chats": ["聊得开心"], "shifts": [], "arc_lines": [],
            "heat_top": [], "stats": {},
        })

    assert title == "今日头条" and "热闹" in content
    assert captured["max_tokens"] == ds.DIGEST_MAX_TOKENS >= 2000
    assert captured["model"] == settings.effective_model
    assert captured["kw"]["owner"] == "system"
    assert captured["kw"]["meter"].scenario == "digest"


@pytest.mark.anyio
async def test_empty_compose_result_is_not_persisted(db_session):
    """空正文不许落库 —— 一旦落了，幂等会把这一天永久钉死。"""
    from app.services import digest_service as ds

    await _material(db_session)
    with patch.object(ds, "compose_digest", AsyncMock(return_value=("t", "   "))):
        with pytest.raises(ds.DigestComposeEmpty):
            await ds.generate_village_digest(db_session, date(2026, 7, 24))

    n = (await db_session.execute(
        select(func.count()).select_from(Digest))).scalar()
    assert n == 0, "空正文落库了 —— 这一天从此再也不会自愈"


@pytest.mark.anyio
async def test_an_existing_empty_row_is_refilled_not_short_circuited(db_session):
    """存量空行（生产有 4 天）必须能被重新生成填回去，走 UPDATE 而非 INSERT。"""
    from app.services import digest_service as ds

    day = date(2026, 7, 25)
    db_session.add(Digest(scope="village", date=day, user_id="",
                          title=f"{day} 村落日报", content_md="", stats_json={}))
    await db_session.commit()
    await _material(db_session)

    with patch.object(ds, "compose_digest",
                      AsyncMock(return_value=("补写的头条", "# 补写的头条\n有内容了"))):
        d = await ds.generate_village_digest(db_session, day)

    assert d.title == "补写的头条" and "有内容了" in d.content_md
    # 唯一约束还在 → 必须是 UPDATE，不是第二行
    n = (await db_session.execute(
        select(func.count()).select_from(Digest))).scalar()
    assert n == 1


@pytest.mark.anyio
async def test_a_nonempty_row_still_short_circuits(db_session):
    """有正文的才算完成 —— 幂等语义不能被上一条改坏。"""
    from app.services import digest_service as ds

    day = date(2026, 7, 26)
    db_session.add(Digest(scope="village", date=day, user_id="", title="原标题",
                          content_md="# 原标题\n原来的正文", stats_json={}))
    await db_session.commit()

    compose = AsyncMock(return_value=("不该被调用", "不该被调用"))
    with patch.object(ds, "compose_digest", compose):
        d = await ds.generate_village_digest(db_session, day)

    compose.assert_not_awaited()
    assert d.title == "原标题"


@pytest.mark.anyio
async def test_cold_start_fallback_is_still_allowed_to_persist(db_session):
    """冷启动兜底文案不是「空」—— 它有正文，必须照常落库。"""
    from app.services import digest_service as ds

    d = await ds.generate_village_digest(db_session, date(2026, 7, 9))
    assert "静悄悄" in d.content_md
    n = (await db_session.execute(
        select(func.count()).select_from(Digest))).scalar()
    assert n == 1


@pytest.mark.anyio
async def test_no_module_bypasses_the_chat_wrapper_in_digest(db_session):
    """结构守卫：digest_service 不得再出现直调 messages.create。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "services"
           / "digest_service.py").read_text(encoding="utf-8")
    assert "messages.create" not in src, (
        "digest 路径必须走 app.llm.client.chat()——它是全仓唯一会加 "
        "thinking={'type':'disabled'} 的地方")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_digest_empty_guard.py -q`

Expected: FAIL —— `AttributeError: ... has no attribute 'llm_chat'` / `'DigestComposeEmpty'`。

- [ ] **Step 3: 改 digest_service**

`backend/app/services/digest_service.py`：

(a) 把顶部 import（`:15-16`）替换为：

```python
from app.llm.client import chat as llm_chat
from app.llm.metering import Meter
```

（`get_client` 与 `record_usage` 在本文件不再需要——`chat()` 内部统一计量。）

(b) 删掉本地 `_extract_text`（`:32-36`）——它与 `client.extract_text` 逐字相同，`chat()` 已经在内部做了。

(c) 在 `DIGEST_SYSTEM` 之后加常量与异常：

```python
#: DIGEST_SYSTEM 要的是「3-5 段、不超过 600 字中文 + 标题」，800 从一开始就
#: 不够；叠上没关掉的 thinking 直接把输出吃光（生产 12 天里 7 天触顶 801，
#: 其中 4 天正文长度为 0）。
DIGEST_MAX_TOKENS = 2000
WEEKLY_MAX_TOKENS = 1500


class DigestComposeEmpty(RuntimeError):
    """LLM 返回了空正文。

    宁可让 nightly_cron 的 try/except 记一条 error，也不要把空串写进
    digests —— (scope, date, user_id) 唯一约束 + 幂等早返回会把这一天
    永久钉死，玩家连着几天打开日报都是空白面板。
    """
```

(d) 把 `compose_digest`（`:153-167`）整体替换为：

```python
async def compose_digest(day: date_type, material: dict) -> tuple[str, str]:
    """一次 LLM 调用产出（标题, 正文）。

    必须走 ``app.llm.client.chat()``：它是全仓唯一会加
    ``thinking={"type": "disabled"}`` 的地方（client.py:148-149，条件是
    ``not settings.llm_thinking``，默认 False），也统一了 llm_usage 计量。
    直调 ``messages.create`` 时推理会把 max_tokens 吃光，响应里没有可用的
    text block。

    ``model`` 显式传 ``effective_model``：``chat()`` 对 ``owner="system"``
    默认解析的是 ``settings.background_model``（client.py:140），今天两者
    恰好相等（生产未设 BACKGROUND_LLM_MODEL），但日报是玩家可见内容，模型
    不该跟着后台路由走。
    """
    text = (await llm_chat(
        DIGEST_SYSTEM,
        [{"role": "user", "content": _build_prompt(day, material)}],
        model=settings.effective_model,
        max_tokens=DIGEST_MAX_TOKENS,
        owner="system",
        meter=Meter(scenario="digest"),
    )).strip()
    title = f"{day} 村落日报"
    if text.startswith("#"):
        first_line = text.splitlines()[0].lstrip("# ").strip()
        if first_line:
            title = first_line
    return title, text
```

(e) 把 `generate_village_digest`（`:201-242`）的头部到落库段替换为：

```python
async def generate_village_digest(db: AsyncSession, day: date_type | None = None) -> Digest:
    day = day or datetime.now(UTC).date()

    existing = (await db.execute(
        select(Digest).where(Digest.scope == "village", Digest.date == day, Digest.user_id == "")
    )).scalar_one_or_none()
    # 幂等的判据是「已经有正文」，不是「行存在」：存量空行（生产 07-17/24/25/26
    # 四天）必须能被重新生成填回去，否则那几天永远是空白面板。
    if existing is not None and (existing.content_md or "").strip():
        return existing

    material = await gather_material(db, day)
    if not material["has_material"]:
        title = f"{day} 村落日报"
        content = f"# {title}\n\n今天的小镇静悄悄，居民们各自忙碌，没有特别的大事发生。明天再来看看吧。"
    else:
        title, content = await compose_digest(day, material)

    if not content.strip():
        logger.error("digest compose returned empty text for %s (stats=%s)",
                     day, material["stats"])
        raise DigestComposeEmpty(f"empty digest body for {day}")

    if existing is not None:
        # 回填已有的空行 —— UPDATE 而不是 INSERT，避开唯一约束。
        existing.title = title
        existing.content_md = content
        existing.stats_json = material["stats"]
        await db.commit()
        await db.refresh(existing)
        digest = existing
    else:
        digest = Digest(
            scope="village", date=day, user_id="", title=title,
            content_md=content, stats_json=material["stats"],
        )
        db.add(digest)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return (await db.execute(
                select(Digest).where(Digest.scope == "village", Digest.date == day, Digest.user_id == "")
            )).scalar_one()
        await db.refresh(digest)
```

（其后的 `_pin_digest_bulletin` / broadcast / `return digest` 三段保持不变。）

(f) 把 `generate_weekly_recap` 里的 LLM 段（`:328-335`）替换为：

```python
        body = (await llm_chat(
            WEEKLY_SYSTEM,
            [{"role": "user", "content": f"本周人格标签：{tag}\n{material}"}],
            model=settings.effective_model,
            max_tokens=WEEKLY_MAX_TOKENS,
            owner="system",
            meter=Meter(scenario="weekly_recap"),
        )).strip()
        title = f"{week_key} 本周回顾"
```

- [ ] **Step 4: 跑新测试确认通过**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_digest_empty_guard.py -q`

Expected: `6 passed`

- [ ] **Step 5: 改两处既有测试的 patch 点（规格变更）**

这两处断言的是「digest 路径直调了 `messages.create` 恰好 1 次」。改走 `chat()` 包装后这个断言的**前提**没了——不是断言变松，是被观测的接口换了一层。

`backend/tests/test_weekly_recap.py` 的 `test_recap_one_llm_call_per_week`，把 `with` 块与断言改为：

```python
    calls = []

    async def _fake_chat(system_prompt, messages, model=None, max_tokens=None, **kw):
        calls.append({"system": system_prompt, "messages": messages, "kw": kw})
        return "你和三位居民建立了联系。"

    with patch.object(ds, "llm_chat", _fake_chat):
        d1 = await ds.generate_weekly_recap(db_session, user.id)
        d2 = await ds.generate_weekly_recap(db_session, user.id)  # idempotent

    assert d1.id == d2.id
    assert len(calls) == 1  # only one LLM call this week
```

同文件 `test_cold_start_no_llm` 改为：

```python
    called = []

    async def _fake_chat(*a, **kw):
        called.append(1)
        return "不该被调用"

    with patch.object(ds, "llm_chat", _fake_chat):
        d = await ds.generate_weekly_recap(db_session, user.id)

    assert not called
    assert d.scope == "personal" and "太安静" in d.content_md
```

`backend/tests/test_opinion_service.py` 的 `test_integration_digest_opinion_line_zero_new_llm`（`:535-563`），把 mock 段与断言改为：

```python
    calls = []

    async def _fake_chat(system_prompt, messages, model=None, max_tokens=None, **kw):
        calls.append(messages)
        return "# 今日头条\n镇上为夜市吵起来了"

    with patch.object(ds, "llm_chat", _fake_chat):
        digest = await ds.generate_village_digest(db_session, day)

    assert len(calls) == 1  # 素材增强零新增调用
    prompt = calls[0][0]["content"]
    assert "小镇舆论" in prompt and "夜市该不该扩建" in prompt
    assert "夜市" in digest.content_md
```

（该文件顶部原有的 `from unittest.mock import AsyncMock, MagicMock, patch` 保留即可；`MagicMock` 若不再使用，一并从该用例内的 import 行删掉。）

- [ ] **Step 6: 跑 digest 相关全量**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_digest.py tests/test_digest_empty_guard.py tests/test_weekly_recap.py tests/test_opinion_service.py tests/test_bulletin_posts.py -q`

Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/digest_service.py backend/tests/test_digest_empty_guard.py backend/tests/test_weekly_recap.py backend/tests/test_opinion_service.py
git commit -m "$(cat <<'EOF'
fix(digest): 日报走 chat() 包装 + 空正文不落库/可回填——修 #6-1/#6-2

compose_digest 绕过 app.llm.client.chat() 直调 messages.create，于是
thinking 没被关掉（chat() 是全仓唯一会加 thinking={"type":"disabled"} 的
地方），800 的 max_tokens 被推理吃光，响应里没有可用 text block → 空串。
生产 12 天里 7 天 output_tokens 触顶 801，其中 4 天正文长度为 0。

DIGEST_MAX_TOKENS 提到 2000（DIGEST_SYSTEM 要的是 3-5 段 600 字 + 标题，
800 本来就不够）。model 显式传 effective_model：chat() 对 owner="system"
默认解析 background_model，今天两者恰好相等，但日报是玩家可见内容，模型
不该跟着后台路由走。generate_weekly_recap 同病一并改。

空正文守卫两处：幂等判据从「行存在」改成「已经有正文」，让生产那 4 天存量
空行能被重新生成填回去（走 UPDATE 避开唯一约束）；compose 返回空则抛
DigestComposeEmpty，由 nightly_cron 既有的 try/except 记 error，而不是静默
把空串写进库被幂等永久钉死。

顺带删掉本地 _extract_text：它与 client.extract_text 实现逐字相同，是重复
定义——注意换用它本身没有任何收益，真正修复的是 thinking 与 max_tokens。

改了 test_weekly_recap 与 test_opinion_service 各一处 patch 点：它们断言
「直调 messages.create 恰好 1 次」，改走包装后被观测的接口换了一层。
**改的是规格（LLM 调用改走统一包装），不是为了让它绿** —— 断言的语义
（一周/一天恰好一次 LLM 调用、prompt 里带舆论素材）逐条保留。

Verified-by: pytest tests/test_digest.py tests/test_digest_empty_guard.py
             tests/test_weekly_recap.py tests/test_opinion_service.py
             tests/test_bulletin_posts.py -q → <贴真实结果行>
EOF
)"
```

---

## Task 4: 赛季开季入口——admin 路由 + 自动开季 + GET /seasons

修 E7：`seasons` 表 0 行 → `_active_season_id()` 恒 None → `add_points()` 第一件事就 `return 0`，所有积分静默丢弃。全仓没有任何生产代码构造过 `Season` 行。

**Files:**
- Modify: `backend/app/config.py` + `backend/.env.example`（`season_length_days`）
- Create: `backend/app/routers/admin/seasons.py`
- Modify: `backend/app/routers/admin/__init__.py`（注册）
- Modify: `backend/app/routers/seasons.py`（`GET /seasons`）
- Modify: `backend/app/services/script_service.py`（`ensure_active_season`）
- Modify: `backend/app/tasks/event_cron.py`（接线）
- Create: `backend/tests/test_season_admin.py`

**Interfaces:**
- Produces: `script_service.ensure_active_season(db) -> Season | None`（无 active season 时按 `settings.season_length_days` 开一季，否则返回 None）。
- Consumes: `season_service.get_active_season(db) -> Season | None`、`season_service._invalidate_active() -> None`（**同步函数，无参**）、`season_service.settle_season(db, season: Season) -> dict`。
- Consumes: `require_admin` from `app.routers.admin.middleware`。

### 背景事实

- **`settle_due_seasons` 不在 `nightly_cron.py`**（诊断报告说错了）。它在 `script_service.py:221`，由 `event_cron.py:53-55` 调用。`nightly_cron.py` 全文零处 season。
- `config.py` 无任何 `season_` 字段，`.env.example` 无 `SEASON_*`。
- `Season.status` 默认是 `"voting"`，**不是 `"active"`**。开季必须显式写 `status="active"`。
- `_active_season_id` 有 60s 进程内缓存，只有 `_invalidate_active()` 能打掉；该函数**生产代码零调用点**。开完季不调它，新赛季最多 1 分钟不可见。
- `settle_due_seasons` 结算后**也没调** `_invalidate_active()`，同样是缺口。
- **开季会连锁触发一张新的镇长选举**：`nightly_cron.py:261-268` 的 `maybe_open_seasonal_election` 在有 active season 且 `election_last_season != season.id` 时开一张。这与 E8 存量处置（作废幽灵候选选举、让它用当前名册重开）是同一件事的两半——但那属于**存量数据处置**，不在本批次。
- 新 admin 子模块会被 `test_admin_authz_sweep.py` 自动发现并要求 401/403。

- [ ] **Step 1: 加 Settings 字段 + .env.example**

`backend/app/config.py`，在 Task 2 加的 debate 组之后插入：

```python
    # E12/C3 赛季：自动开季的季长（真实日）。seasons 表长期 0 行，导致
    # season_service.add_points 的第一行 `if not season_id: return 0` 把所有
    # 积分静默丢弃 —— 读端和记分端都在，缺的只是写端。
    season_length_days: int = 14
    season_auto_open: bool = True        # 关掉则只能由 admin 手动开季
```

`backend/.env.example`，在 Task 2 加的 DEBATE 组之后插入：

```
# ── E12/C3 赛季 ─────────────────────────────────────────────────────────
# 自动开季的季长（真实日）。seasons 表长期 0 行 → add_points 全部静默丢弃。
SEASON_LENGTH_DAYS=14
# false = 只允许 admin 手动开季（POST /admin/seasons）
SEASON_AUTO_OPEN=true
```

- [ ] **Step 2: 写失败测试**

创建 `backend/tests/test_season_admin.py`：

```python
"""E7 赛季写端：admin 开季 / 结季、自动开季、列表路由。

全仓 `Season(` 的构造此前只出现在类定义与测试里 —— 没有任何生产代码路径
会创建赛季行。后果链：_active_season_id() 恒 None → add_points() 第一行
`if not season_id: return 0` → 所有经 season_scorer 上报的积分全部静默丢弃。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.season import Season
from app.models.user import User
from app.services import script_service as ss
from app.services import season_service
from app.services.auth_service import create_token


async def _admin(db):
    u = User(name="admin", email="season-admin@t.com", is_admin=True, is_banned=False)
    db.add(u)
    await db.commit()
    return u


def _hdr(user):
    return {"Authorization": f"Bearer {create_token(user.id)}"}


@pytest.fixture(autouse=True)
def _clear_season_cache():
    """_active_season_id 有 60s 进程内缓存，跨测试会串味。"""
    season_service._invalidate_active()
    yield
    season_service._invalidate_active()


@pytest.mark.anyio
async def test_admin_can_open_a_season(client, db_session):
    admin = await _admin(db_session)
    resp = await client.post("/admin/seasons", headers=_hdr(admin), json={
        "title": "谜案季", "theme": "小镇疑云", "days": 7,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "谜案季" and body["status"] == "active"

    row = (await db_session.execute(select(Season))).scalars().one()
    assert row.status == "active" and row.title == "谜案季"


@pytest.mark.anyio
async def test_opening_a_season_invalidates_the_active_cache(client, db_session):
    """不打掉 60s 缓存，新赛季最长 1 分钟不可见，记分继续丢。"""
    admin = await _admin(db_session)
    # 先把缓存烧成 "无赛季"
    assert await season_service._active_season_id(db_session) is None

    await client.post("/admin/seasons", headers=_hdr(admin),
                      json={"title": "新季", "theme": "", "days": 7})

    assert await season_service._active_season_id(db_session) is not None


@pytest.mark.anyio
async def test_opening_refuses_while_another_season_is_active(client, db_session):
    admin = await _admin(db_session)
    db_session.add(Season(title="在办季", status="active",
                          starts_at=datetime.now(UTC) - timedelta(days=1),
                          ends_at=datetime.now(UTC) + timedelta(days=6)))
    await db_session.commit()

    resp = await client.post("/admin/seasons", headers=_hdr(admin),
                             json={"title": "抢跑季", "theme": "", "days": 7})
    assert resp.status_code == 400
    assert "active" in resp.json()["detail"]


@pytest.mark.anyio
async def test_admin_can_settle_a_season(client, db_session):
    admin = await _admin(db_session)
    s = Season(title="待结季", status="active",
               starts_at=datetime.now(UTC) - timedelta(days=8),
               ends_at=datetime.now(UTC) - timedelta(days=1))
    db_session.add(s)
    await db_session.commit()

    resp = await client.post(f"/admin/seasons/{s.id}/settle", headers=_hdr(admin))
    assert resp.status_code == 200
    await db_session.refresh(s)
    assert s.status == "settled"


@pytest.mark.anyio
async def test_settle_404s_on_an_unknown_season(client, db_session):
    admin = await _admin(db_session)
    resp = await client.post("/admin/seasons/nope/settle", headers=_hdr(admin))
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_seasons_list_endpoint_is_public(client, db_session):
    db_session.add_all([
        Season(title="第一季", status="settled",
               starts_at=datetime.now(UTC) - timedelta(days=30),
               ends_at=datetime.now(UTC) - timedelta(days=16)),
        Season(title="第二季", status="active",
               starts_at=datetime.now(UTC) - timedelta(days=2),
               ends_at=datetime.now(UTC) + timedelta(days=12)),
    ])
    await db_session.commit()

    resp = await client.get("/seasons")
    assert resp.status_code == 200
    titles = [s["title"] for s in resp.json()["seasons"]]
    assert titles == ["第二季", "第一季"]  # 新的在前


@pytest.mark.anyio
async def test_ensure_active_season_opens_one_when_none_exists(db_session):
    s = await ss.ensure_active_season(db_session)
    assert s is not None and s.status == "active"
    span = (s.ends_at - s.starts_at).days
    assert span == settings.season_length_days


@pytest.mark.anyio
async def test_ensure_active_season_is_a_noop_when_one_is_running(db_session):
    db_session.add(Season(title="在办季", status="active",
                          starts_at=datetime.now(UTC) - timedelta(days=1),
                          ends_at=datetime.now(UTC) + timedelta(days=6)))
    await db_session.commit()

    assert await ss.ensure_active_season(db_session) is None
    n = len((await db_session.execute(select(Season))).scalars().all())
    assert n == 1


@pytest.mark.anyio
async def test_ensure_active_season_respects_the_gate(db_session, monkeypatch):
    monkeypatch.setattr(settings, "season_auto_open", False)
    assert await ss.ensure_active_season(db_session) is None
    assert (await db_session.execute(select(Season))).scalars().all() == []


@pytest.mark.anyio
async def test_points_actually_land_once_a_season_exists(db_engine, db_session,
                                                         monkeypatch):
    """E7 的真正判据：开季之后 add_points 不再静默丢弃。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.models.season import SeasonScore

    await ss.ensure_active_season(db_session)
    season_service._invalidate_active()

    # add_points 自己开 session（不收 db），照 test_seasons.py:14-21 的既有姿势
    # 注入测试 engine —— 这是唯一能让它在测试里工作的方式。
    factory = async_sessionmaker(db_engine, class_=AsyncSession,
                                 expire_on_commit=False)
    monkeypatch.setattr(season_service, "async_session", factory)
    assert await season_service.add_points("u1", 30, "chat") == 30

    score = (await db_session.execute(
        select(SeasonScore).where(SeasonScore.user_id == "u1"))).scalar_one()
    assert score.points == 30


def test_event_cron_wires_auto_season_opening():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "tasks"
           / "event_cron.py").read_text(encoding="utf-8")
    assert "ensure_active_season" in src
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_season_admin.py -q`

Expected: FAIL —— 404（路由不存在）/ `AttributeError: ensure_active_season`。

- [ ] **Step 4: 实现 `ensure_active_season`**

`backend/app/services/script_service.py`，在 `settle_due_seasons`（`:221`）之前插入：

```python
async def ensure_active_season(db) -> Season | None:
    """无 active season 时开下一季，否则返回 None（幂等）。

    E7：全仓此前没有任何生产代码会创建 Season 行，于是
    ``season_service._active_season_id()`` 恒为 None，``add_points()`` 第一行
    就 ``return 0`` —— 读端和记分端都在，缺的只是写端。

    注意 ``Season.status`` 的列默认值是 ``"voting"`` 而不是 ``"active"``，
    必须显式写；开完季要打掉 ``_active_season_id`` 的 60s 缓存，否则新赛季
    最长 1 分钟不可见、记分继续丢。
    """
    from app.services.season_service import get_active_season, _invalidate_active

    if not settings.season_auto_open:
        return None
    if await get_active_season(db) is not None:
        return None

    now = datetime.now(UTC)
    n = len((await db.execute(select(Season))).scalars().all()) + 1
    season = Season(
        title=f"第 {n} 季",
        theme="",
        status="active",
        starts_at=now,
        ends_at=now + timedelta(days=settings.season_length_days),
        payload_json={},
    )
    db.add(season)
    await db.commit()
    await db.refresh(season)
    _invalidate_active()
    logger.info("Opened season %s (%s → %s)", season.title,
                season.starts_at.date(), season.ends_at.date())
    return season
```

并在 `settle_due_seasons` 的 `await db.commit()`（`season.payload_json = merged` 之后那一句）之后补一行缓存失效：

```python
        from app.services.season_service import _invalidate_active
        _invalidate_active()   # 结算完必须打掉缓存，否则 add_points 还往旧季记
```

- [ ] **Step 5: 实现 admin 路由**

创建 `backend/app/routers/admin/seasons.py`：

```python
"""E7 admin 赛季写端 — require_admin on every route.

`seasons` 表长期 0 行，读端（/seasons/current、leaderboard）与记分端
（season_scorer → add_points）都在，唯独没有任何路径创建赛季行，于是所有
积分静默丢弃。这里补上手动写端；自动开季在 script_service.ensure_active_season。
"""
from datetime import datetime, timedelta, UTC

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.season import Season
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.services.season_service import (
    get_active_season, settle_season, _invalidate_active,
)

router = APIRouter(prefix="/seasons", tags=["admin-seasons"])


class SeasonCreate(BaseModel):
    title: str
    theme: str = ""
    days: int | None = None
    world_view: str = ""


def _serialize(s: Season) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "theme": s.theme,
        "status": s.status,
        "starts_at": s.starts_at.isoformat() if s.starts_at else None,
        "ends_at": s.ends_at.isoformat() if s.ends_at else None,
        "payload_json": s.payload_json or {},
    }


@router.get("")
async def list_seasons(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(Season).order_by(Season.starts_at.desc())
    )).scalars().all()
    return {"seasons": [_serialize(s) for s in rows]}


@router.post("")
async def open_season(
    body: SeasonCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """开一季。两季并行会让 add_points 的归属变得不确定，所以拒绝。"""
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    if await get_active_season(db) is not None:
        raise HTTPException(status_code=400,
                            detail="a season is already active; settle it first")
    days = body.days if body.days is not None else settings.season_length_days
    if days <= 0:
        raise HTTPException(status_code=400, detail="days must be positive")

    now = datetime.now(UTC)
    season = Season(
        title=body.title.strip(), theme=body.theme, status="active",
        starts_at=now, ends_at=now + timedelta(days=days),
        payload_json={"world_view": body.world_view} if body.world_view else {},
    )
    db.add(season)
    await db.commit()
    await db.refresh(season)
    # 60s 缓存不打掉的话，新赛季最长 1 分钟不可见、记分继续丢。
    _invalidate_active()
    return _serialize(season)


@router.post("/{season_id}/settle")
async def settle(
    season_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    season = await db.get(Season, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Season not found")
    payload = await settle_season(db, season)
    _invalidate_active()
    return {"season": _serialize(season), "payload": payload}
```

`backend/app/routers/admin/__init__.py`：在 import 段末尾加

```python
from app.routers.admin.seasons import router as seasons_router
```

在 include 段末尾加

```python
router.include_router(seasons_router)
```

- [ ] **Step 6: 加 `GET /seasons` 列表路由**

`backend/app/routers/seasons.py`，在 `current_season` 之前插入：

```python
@router.get("")
async def list_seasons(db: AsyncSession = Depends(get_db)):
    """公开的赛季列表（新的在前）——此前只有 /current，玩家看不到历史赛季。"""
    rows = (await db.execute(
        select(Season).order_by(Season.starts_at.desc()).limit(50)
    )).scalars().all()
    return {"seasons": [{
        "id": s.id, "title": s.title, "theme": s.theme, "status": s.status,
        "starts_at": s.starts_at.isoformat() if s.starts_at else None,
        "ends_at": s.ends_at.isoformat() if s.ends_at else None,
    } for s in rows]}
```

并在文件顶部补 import：

```python
from sqlalchemy import select

from app.models.season import Season
```

- [ ] **Step 7: event_cron 接线自动开季**

`backend/app/tasks/event_cron.py`，把 C3 块里 `settled = await settle_due_seasons(db)` 之后补一行，即该 try 块改为：

```python
                # C3: fire due script acts + settle finished seasons.
                try:
                    from app.services.script_service import (
                        fire_due_scripts, settle_due_seasons, ensure_active_season,
                    )
                    fired = await fire_due_scripts(db)
                    settled = await settle_due_seasons(db)
                    # E7: 结算之后补开下一季 —— 顺序不能反，否则刚开的季会被
                    # 同一轮的 settle 扫到（它按 ends_at 判，新季不会中，但把
                    # 开季放在结算前会让「一季结束到下一季开始」多等 60s）。
                    opened = await ensure_active_season(db)
                    if fired:
                        logger.info("Event cron: fired %d script act(s)", len(fired))
                    if settled:
                        logger.info("Event cron: settled %d season(s)", len(settled))
                    if opened is not None:
                        logger.info("Event cron: opened season %s", opened.title)
                except Exception:
                    logger.warning("C3 script/season cron step failed", exc_info=True)
```

- [ ] **Step 8: 跑测试**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_season_admin.py tests/test_seasons.py tests/test_script_season.py tests/test_admin_authz_sweep.py -q`

Expected: 全绿（`test_admin_authz_sweep` 会自动把三条新路由纳入 401/403 断言）。

- [ ] **Step 9: 提交**

```bash
git add backend/app/config.py backend/.env.example backend/app/routers/admin/seasons.py backend/app/routers/admin/__init__.py backend/app/routers/seasons.py backend/app/services/script_service.py backend/app/tasks/event_cron.py backend/tests/test_season_admin.py
git commit -m "$(cat <<'EOF'
feat(seasons): 补赛季写端（admin 开季 + 自动开季 + 列表）——修 E7

全仓 `Season(` 的构造此前只出现在类定义与测试里，没有任何生产代码路径会
创建赛季行。后果链：_active_season_id() 恒 None → add_points() 第一行
`if not season_id: return 0` → 所有经 season_scorer 上报的积分静默丢弃。
生产实测 seasons=0 / season_scores=0，/seasons/current 恒返回 null。

两条写端：POST /admin/seasons 手动开季（拒绝并行赛季，否则 add_points 的
归属不确定）、event_cron 里 settle 之后 ensure_active_season 自动补开。
补 GET /seasons 公开列表（此前只有 /current）。

三处都调 season_service._invalidate_active()——它有 60s 进程内缓存且此前
生产代码零调用点，不打掉的话新赛季最长 1 分钟不可见、记分继续丢。
settle_due_seasons 结算后同样补上（原来也漏了）。

注意 Season.status 的列默认值是 "voting" 不是 "active"，开季必须显式写。
两个旋钮同步进 .env.example（test_env_example_consistency 是硬门）。

已知连锁：有 active season 后 nightly 的 maybe_open_seasonal_election 会
每季开一张镇长选举。这是预期行为，与 E8 存量选举票的处置在同一件事的两半，
后者属于单独的数据变更，不在本批次。

Verified-by: pytest tests/test_season_admin.py tests/test_seasons.py
             tests/test_script_season.py tests/test_admin_authz_sweep.py -q
             → <贴真实结果行>
EOF
)"
```

---

## Task 5: 幽灵票——只撤「已删除居民」的票 + 结票候选人校验

修 E8 的逻辑侧。**范围已拍板**：只撤 slug 已不在 `residents` 表的票；按 `resident_type` 降级的**保留**。

**Files:**
- Modify: `backend/app/services/civic_service.py`（`run_npc_voting` + `_close_one`）
- Create: `backend/tests/test_ghost_vote_revocation.py`

**Interfaces:**
- Produces: `civic_service._voter_map(opts: list[dict]) -> tuple[dict[str, int], bool]`。返回 `(voters, is_legacy)`：新 `dict[str, int]` 原样返回、`is_legacy=False`；旧 `list[str]` 的每个条目映射成 `-1`（票的归属物理上没存）、`is_legacy=True`；缺键返回 `({}, False)`。
- 改变：`options_json[0]["_npc_voters"]` 的落库形状从 `list[str]` 变为 `dict[str, int]`（slug → 它投的 option 索引）。

### 背景事实 + 硬约束

- **不许改 `test_civic_frozen_denominator.py:286` 的 `test_ghost_votes_are_kept_by_design`**。它的 `_demote()` 只改 `resident_type`，Resident 行仍在表里 —— 按本次口径该测试必须保持绿。这是「没有推翻 F2 语义」的判据。
- `civic_service.py:31-42` 的 `META_ELIGIBLE_AT_OPEN` 注释块里有一句「配套的语义决定：**幽灵票保留，不实现撤票**」需要**补充限定**（不是推翻）：说明保留的是「降级者的票」，物理删除的另论。
- `run_npc_voting` 的选民集是 `select(Resident).where(Resident.is_civic_voter)`（`:168-170`）——这是**投票资格集**，不是**存在性集**。判「是否还在世界里」必须另查全表 slug，不能复用它。
- `_npc_voters` 的读侧还有三处：`scripts/burnin_report.py:1086`（`len()` 计数）、`:1471`（幽灵票探针，`for s in voters`）。**dict 上迭代得到的是 key，两处都仍然正确**，但 `burnin_report.py:1086` 的 `len()` 对 dict 也对。无需改。
- `_close_one` 的 tally 在 `:490-493`。`_PERSON_TYPES = frozenset({"mayor", "office", "duty"})` 已在 `:237` 定义，直接复用。
- 生产现状：3 张 poll 的 `_npc_voters` 各 25 人，其中 14 个 slug 查不到；镇长选举那张 4 个候选全部不存在。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_ghost_vote_revocation.py`：

```python
"""E8 幽灵票：只撤「已从 residents 表消失」的票。

2026-07-25 的花名册重置删掉了 25 位居民，但票留在了 options_json 里
（npc_votes 是累加计数器、_npc_voters 是只增不减的集合）。生产三张 poll
各带 14 个查不到的 slug，25 张幽灵票让 13 人小镇里的 2 个真玩家永远投不赢
任何议案；镇长选举那张的 4 个候选人全部不存在。

**范围边界（拍板）**：F2 的「投票时具备资格即计票」保护的是**降级者**
（civic_service.py:31-42）——那种情况 Resident 行还在，票保留。这里只清
物理删除的事故残留。test_civic_frozen_denominator.test_ghost_votes_are_kept_by_design
必须保持绿，它就是这条边界的判据。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.resident import Resident
from app.models.season import Poll
from app.services import civic_membership as cm
from app.services import civic_service


def _res(slug, rtype=cm.CIVIC_MEMBER_TYPE):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id="sys", tile_x=1, tile_y=1)


async def _stored(db, poll_id) -> list[dict]:
    """列级读，绕开 identity map（conftest 是 expire_on_commit=False）。"""
    return (await db.execute(
        select(Poll.options_json).where(Poll.id == poll_id))).scalar_one()


@pytest.mark.anyio
async def test_voters_are_recorded_as_slug_to_option_index(db_session):
    """撤票要能定向回滚，就必须知道每个人投的是哪一项。"""
    db_session.add_all([_res("a"), _res("b")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    await civic_service.run_npc_voting(db_session)

    voters = (await _stored(db_session, poll.id))[0]["_npc_voters"]
    assert isinstance(voters, dict)
    assert set(voters) == {"a", "b"}
    assert all(isinstance(v, int) for v in voters.values())


@pytest.mark.anyio
async def test_a_deleted_resident_loses_its_vote(db_session):
    """居民行被删 → 撤票、npc_votes 减回去。"""
    db_session.add_all([_res("stays"), _res("vanishes")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    assert await civic_service.run_npc_voting(db_session) == 2
    before = sum(int(o.get("npc_votes", 0))
                 for o in await _stored(db_session, poll.id))
    assert before == 2

    gone = (await db_session.execute(
        select(Resident).where(Resident.slug == "vanishes"))).scalar_one()
    await db_session.delete(gone)
    await db_session.commit()

    await civic_service.run_npc_voting(db_session)
    opts = await _stored(db_session, poll.id)
    assert sum(int(o.get("npc_votes", 0)) for o in opts) == 1
    assert set(opts[0]["_npc_voters"]) == {"stays"}


@pytest.mark.anyio
async def test_a_demoted_resident_keeps_its_vote(db_session):
    """F2 语义边界：降级（行还在）不撤票 —— 投票时具备资格即计票。"""
    db_session.add_all([_res("keeps"), _res("demoted")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "议题", [{"label": "A", "effect": None},
                             {"label": "B", "effect": None}])
    assert await civic_service.run_npc_voting(db_session) == 2

    r = (await db_session.execute(
        select(Resident).where(Resident.slug == "demoted"))).scalar_one()
    r.resident_type = cm.UGC_RESIDENT_TYPE
    await db_session.commit()

    await civic_service.run_npc_voting(db_session)
    opts = await _stored(db_session, poll.id)
    assert sum(int(o.get("npc_votes", 0)) for o in opts) == 2
    assert "demoted" in opts[0]["_npc_voters"]


@pytest.mark.anyio
async def test_legacy_list_format_is_read_and_upgraded(db_session):
    """存量 poll 的 _npc_voters 是 list[str]，读侧必须兼容且不重复投票。"""
    db_session.add(_res("old-voter"))
    await db_session.commit()
    poll = Poll(question="存量议题", status="open",
                closes_at=datetime.now(UTC) + timedelta(days=3),
                options_json=[{"label": "A", "npc_votes": 1,
                               "_npc_voters": ["old-voter"]},
                              {"label": "B", "npc_votes": 0}])
    db_session.add(poll)
    await db_session.commit()

    assert await civic_service.run_npc_voting(db_session) == 0  # 已投过
    opts = await _stored(db_session, poll.id)
    assert sum(int(o.get("npc_votes", 0)) for o in opts) == 1


@pytest.mark.anyio
async def test_legacy_list_ghosts_are_dropped_without_touching_the_tally(db_session):
    """旧 list 格式不知道幽灵投的是哪一项——只能移出名册，不能瞎减票。

    减错票会凭空改变某个具体选项的得票，比留着一张来源不明的票更糟。存量
    tally 的订正由一次性脚本按备份数据做，不在这条自动路径里。
    """
    db_session.add(_res("alive"))
    await db_session.commit()
    poll = Poll(question="存量议题", status="open",
                closes_at=datetime.now(UTC) + timedelta(days=3),
                options_json=[{"label": "A", "npc_votes": 2,
                               "_npc_voters": ["alive", "deleted-long-ago"]},
                              {"label": "B", "npc_votes": 0}])
    db_session.add(poll)
    await db_session.commit()

    await civic_service.run_npc_voting(db_session)
    opts = await _stored(db_session, poll.id)
    assert "deleted-long-ago" not in opts[0]["_npc_voters"]
    assert opts[0]["npc_votes"] == 2  # 未知归属 → 不动 tally


@pytest.mark.anyio
async def test_closing_zeroes_options_whose_candidate_no_longer_exists(db_session):
    """结票兜底：候选人已不存在的选项归零，避免「有胜者但流会」的误导公告。

    生产那张镇长选举 4 个候选全废（klaus 17 / 夜风侦探 2 / isabella 5 /
    adam 1），不归零的话它会「以 17 票胜出」然后在 install_mayor 阶段流会，
    公告对玩家是误导。
    """
    db_session.add(_res("real-candidate"))
    await db_session.commit()
    poll = Poll(question="镇长选举:谁来当下一任镇长?", status="open",
                closes_at=datetime.now(UTC) - timedelta(days=1),
                options_json=[
                    {"label": "幽灵候选", "effect": {"type": "mayor",
                                                     "slug": "klaus"},
                     "npc_votes": 17},
                    {"label": "真候选", "effect": {"type": "mayor",
                                                   "slug": "real-candidate"},
                     "npc_votes": 3},
                ])
    db_session.add(poll)
    await db_session.commit()

    assert await civic_service.close_due_polls(db_session) == 1
    opts = await _stored(db_session, poll.id)
    assert opts[0]["npc_votes"] == 0        # 幽灵候选归零
    assert opts[1].get("won") is True       # 真候选胜出
    from app.services import election_service
    assert await election_service.current_mayor(db_session) == "real-candidate"


@pytest.mark.anyio
async def test_non_person_effects_are_never_zeroed(db_session):
    """只对 mayor/office/duty 这类「选项即人」的效果做存在性校验。"""
    poll = Poll(question="要不要建邮局", status="open",
                closes_at=datetime.now(UTC) - timedelta(days=1),
                options_json=[
                    {"label": "建", "effect": {"type": "system_config",
                                               "key": "x", "value": 1,
                                               "slug": "not-a-resident"},
                     "npc_votes": 5},
                    {"label": "不建", "effect": None, "npc_votes": 1},
                ])
    db_session.add(poll)
    await db_session.commit()

    await civic_service.close_due_polls(db_session)
    opts = await _stored(db_session, poll.id)
    assert opts[0].get("won") is True and opts[0]["final_votes"] == 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_ghost_vote_revocation.py -q`

Expected: FAIL —— `_npc_voters` 仍是 list，`isinstance(voters, dict)` 挂；幽灵候选不归零。

- [ ] **Step 3: 实现 `_voter_map` + 撤票**

`backend/app/services/civic_service.py`，在 `run_npc_voting`（`:156`）之前插入：

```python
def _voter_map(opts: list[dict]) -> tuple[dict[str, int], bool]:
    """读出 ``_npc_voters``，统一成 ``{slug: option_idx}``。

    返回 ``(voters, is_legacy)``。存量 poll 存的是扁平 ``list[str]``，物理上
    没有票的归属 —— 那些条目映射成 ``-1``，``is_legacy`` 为 True，调用方据此
    知道「知道谁投过，但不知道投了哪一项」。
    """
    raw = (opts[0] or {}).get("_npc_voters") if opts else None
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}, False
    if isinstance(raw, list):
        return {str(s): -1 for s in raw}, True
    return {}, False
```

把 `run_npc_voting` 的循环体（`:176-192`）替换为：

```python
    # 「还在不在这个世界里」是存在性问题，不是资格问题：上面的 residents 是
    # is_civic_voter 集合（资格），降级者不在里面但人还在。撤票只针对物理
    # 删除的 slug，所以另查一次全表。
    live_slugs = set((await db.execute(select(Resident.slug))).scalars().all())

    from app.services import relation_service
    by_slug = {r.slug: r for r in residents}
    cast = 0
    # 撤票与 list→dict 的格式升级都可能在 cast == 0 时发生（一张所有活人都
    # 投过、只剩幽灵要清的 poll 就是这种情况）。用显式的 changed 追踪是否需要
    # commit —— 只看 cast 会让撤票白做一场。
    changed = False
    for poll in polls:
        opts = list(poll.options_json or [])
        if not opts:
            continue
        voters, is_legacy = _voter_map(opts)
        if is_legacy:
            changed = True   # 落库时会写成 dict，形状本身就变了

        # E8 撤票：投票人已从 residents 表消失（2026-07-25 花名册重置的事故
        # 残留）→ 把票收回。F2 的「投票时具备资格即计票」保护的是**降级者**
        # （行还在、只是 resident_type 变了），那种票保留 —— 见本模块
        # META_ELIGIBLE_AT_OPEN 的注释与 test_civic_frozen_denominator。
        for slug in [s for s in voters if s not in live_slugs]:
            idx = voters.pop(slug)
            changed = True
            if idx < 0 or idx >= len(opts):
                # 旧 list 格式不知道他投了哪一项。减错票会凭空改变某个具体
                # 选项的得票，比留一张来源不明的票更糟 —— 只移出名册，tally
                # 的订正交给按备份数据做的一次性脚本。
                logger.warning(
                    "poll %s: dropping ghost voter %r with unknown ballot "
                    "(legacy list format) — tally left untouched", poll.id, slug)
                continue
            opts[idx]["npc_votes"] = max(0, int(opts[idx].get("npc_votes", 0)) - 1)

        for r in residents:
            if r.slug in voters:
                continue
            idx = await _npc_choice(db, r, poll, opts, relation_service, by_slug)
            opts[idx]["npc_votes"] = int(opts[idx].get("npc_votes", 0)) + 1
            voters[r.slug] = idx
            cast += 1
        opts[0]["_npc_voters"] = dict(sorted(voters.items()))
        poll.options_json = opts
        flag_modified(poll, "options_json")
    if cast or changed:
        await db.commit()
    return cast
```

> 返回值仍是「本轮新投出的票数」（`cast`），撤票不计入——`nightly_cron` 的日志文案是「%d NPC civic votes cast」，混进撤票数会让那行日志说谎。撤票走 `logger.warning`，运维在日志里单独可见。

- [ ] **Step 4: 实现结票候选人校验**

`backend/app/services/civic_service.py` 的 `_close_one`（`:484`），把 `tally = [...]` 那段（`:490-493`）替换为：

```python
    # E8 结票兜底：选项即人（mayor/office/duty）时校验那个人是否还在世界里。
    # 生产那张镇长选举的 4 个候选全部已被删除，不归零的话它会「以 17 票胜出」
    # 然后在 install_mayor 阶段流会 —— 公告对玩家是误导（明明有得票最高者，
    # 却说本案流会）。归零让流会的原因在票面上就成立。
    live_slugs = set((await db.execute(select(Resident.slug))).scalars().all())
    for i, o in enumerate(opts):
        eff = (o or {}).get("effect")
        if not isinstance(eff, dict) or eff.get("type") not in _PERSON_TYPES:
            continue
        target = eff.get("slug")
        if target and target not in live_slugs and int(o.get("npc_votes", 0)):
            logger.warning(
                "poll %s option %d: candidate %r no longer exists — zeroing its "
                "%d votes before the tally", poll.id, i, target, o["npc_votes"])
            o["npc_votes"] = 0

    rows = (await db.execute(
        select(Vote.option_idx, func.count()).where(Vote.poll_id == poll.id).group_by(Vote.option_idx)
    )).all()
    player_votes = {idx: n for idx, n in rows}
    tally = [
        int(o.get("npc_votes", 0)) + int(player_votes.get(i, 0))
        for i, o in enumerate(opts)
    ]
```

（原来的 `rows = ...` / `player_votes = ...` 两句在 `:486-489`，把它们移到校验之后即可——上面的代码块已包含。）

- [ ] **Step 5: 补充 `META_ELIGIBLE_AT_OPEN` 的注释限定**

`backend/app/services/civic_service.py:38-41`，把「配套的语义决定」那段改为：

```python
#: 配套的语义决定：**降级者的幽灵票保留**——「投票时具备资格即计票」。降级
#: 只改 ``resident_type``，人还在世界里，那张票是他有资格时投的。
#:
#: E8 补充（2026-07-28）：**物理删除**另论。2026-07-25 的花名册重置删掉了 25
#: 位居民，票却留在 ``options_json`` 里，25 张幽灵票让 13 人小镇里的 2 个真
#: 玩家永远投不赢任何议案。``run_npc_voting`` 现在按「slug 是否还在 residents
#: 表」撤这一类票，并把 ``_npc_voters`` 从扁平 slug 列表升级成
#: ``{slug: option_idx}`` 以便定向回滚（读侧兼容旧 list 格式）。
```

- [ ] **Step 6: 跑测试 + 边界回归**

Run: `cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest tests/test_ghost_vote_revocation.py tests/test_civic_frozen_denominator.py tests/test_m3_civic.py tests/test_m6_election.py tests/test_install_mayor_recheck.py tests/test_ugc_resident_no_political_rights.py tests/test_burnin_report_civic_standing.py -q`

Expected: 全绿。**特别确认 `test_civic_frozen_denominator.py::test_ghost_votes_are_kept_by_design` 是 passed** —— 它是「没推翻 F2 语义」的判据。

> 注意 `test_m3_civic.py:83` 断言 `poll.options_json[0]["_npc_voters"] == [npc.slug]`、`test_ugc_resident_no_political_rights.py:240` 断言 `set(...get("_npc_voters", [])) == {"builtin-1"}`、`test_civic_frozen_denominator.py:300` 断言 `"will-be-demoted" in stored[0]["_npc_voters"]`。第二、三条对 dict 仍成立（`set(dict)` / `in dict` 都取 key）；**第一条 `== [npc.slug]` 会失败**，需改成 `== {npc.slug: 0}` 或 `set(...) == {npc.slug}`。这是形状变更的必然后果，改的是规格。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/civic_service.py backend/tests/test_ghost_vote_revocation.py backend/tests/test_m3_civic.py
git commit -m "$(cat <<'EOF'
fix(civic): 撤掉已删除居民的幽灵票 + 结票校验候选人存在——修 E8 逻辑侧

2026-07-25 的花名册重置删掉了 25 位居民，但 npc_votes 是累加计数器、
_npc_voters 是只增不减的集合，票全留在 options_json 里。生产三张 poll 各带
14 个查不到的 slug，25 张幽灵票让 13 人小镇里的 2 个真玩家永远投不赢任何
议案；镇长选举那张的 4 个候选人全部不存在。

**范围边界**：F2 的「投票时具备资格即计票」（civic_service.py:31-42）保护的是
**降级者**——降级只改 resident_type，人还在世界里，票保留。这里只清**物理
删除**的事故残留，判据是 slug 是否还在 residents 表。
test_civic_frozen_denominator.test_ghost_votes_are_kept_by_design 因此保持绿，
F2 的设计语义没有被推翻，只是补了限定。

_npc_voters 从 list[str] 升级成 {slug: option_idx} 才能定向回滚，读侧兼容旧
list 格式。旧格式的幽灵只移出名册、不动 tally：减错票会凭空改变某个具体选项
的得票，比留一张来源不明的票更糟，存量订正交给按备份数据做的一次性脚本。

结票兜底：mayor/office/duty 这类「选项即人」的效果，候选人已不存在则该选项
归零。否则生产那张选举会「以 17 票胜出」再到 install_mayor 阶段流会——明明
有得票最高者却公告流会，对玩家是误导。

改了 test_m3_civic.py:83 的一条断言（[slug] → {slug: idx}）：
**改的是规格（_npc_voters 的落库形状），不是为了让它绿**。同文件其余断言与
test_ugc_resident_no_political_rights / test_civic_frozen_denominator 的断言
对 dict 天然成立（set(dict) / in dict 都取 key），未改。

Verified-by: pytest tests/test_ghost_vote_revocation.py
             tests/test_civic_frozen_denominator.py tests/test_m3_civic.py
             tests/test_m6_election.py tests/test_install_mayor_recheck.py
             tests/test_ugc_resident_no_political_rights.py
             tests/test_burnin_report_civic_standing.py -q → <贴真实结果行>
EOF
)"
```

---

## 收尾：全量门禁 + 运行时验证

- [ ] **Step 1: 后端全量，与基线做双向差集**

```bash
cd backend && LAB_ADAPTER=mock LAB_ENABLED=false /Volumes/data/dev/simverse-world/backend/.venv/bin/python -m pytest -q --no-header -p no:randomly > /tmp/sv_after.log 2>&1; echo "EXIT=$?" >> /tmp/sv_after.log
grep '^FAILED' /tmp/sv_after.log | sort > /tmp/sv_after_failures.txt
echo "=== 新增失败（必须为空）==="; comm -13 /tmp/sv_baseline_failures.txt /tmp/sv_after_failures.txt
echo "=== 消失的失败（信息用）==="; comm -23 /tmp/sv_baseline_failures.txt /tmp/sv_after_failures.txt
```

判据：**新增失败必须为空**。总数应为 `49 failed / 2591+N passed`（N = 本批次新增用例数）。

- [ ] **Step 2: 前端全量 + typecheck**

```bash
cd frontend && npm test && npx tsc -b --noEmit
```

判据：0 failed。

- [ ] **Step 3: 运行时验证（verify-before-done，不许跳过）**

本地起 API（sqlite）+ 前端 dev，登录后走完整路径，逐条贴证据：

1. 进 `/seasons`，确认三张议案渲染出**中文 label**、页面没有被 ErrorBoundary 换掉；
2. 确认镇长选举在**独立区块**「🏛️ 镇长选举」下，不与普通议案混列；
3. 点一个选项 → Network 面板看到 `POST /polls/{id}/vote` 200 `{"ok":true}`；
4. 刷新页面 → 确认「✓已投」被 `my_vote` 回填；
5. 再点一次 → 确认走 `already voted` 分支显示「已投过」；
6. 本地库 `SELECT * FROM votes;` 见到真实行；
7. 匿名 `curl http://localhost:8000/polls/open` → 响应里搜不到 `_npc_voters` / `_proposer_slug` / `effect`。

单测绿 ≠ 完成。没有上面 7 条的运行时证据，不许声明本批次完成。
