# PR #14 runtime hygiene corrective implementation plan

**Goal:** 修复 PR #14 审查确认的全部七项问题：realism 关闭时 movement prompt 与解析契约分叉、目标移动缺少独立提交、自由漫游使用错误的八邻接、`公寓` 指向不存在的 slug、工资可突破 reserve floor、Hosted Agent 暂停状态无法投影，以及 `.env.example` 的工资说明失真。

**Architecture:** 保留 `REALISM_ENABLED` 的回滚契约：开关开时使用地点 slug/name 并由服务端解析，关时恢复旧坐标 prompt。目标移动与自由漫游都在 execute phase 自己提交。自由漫游与 A* 统一为四邻接。泛称 `公寓` 不猜测具体住宅。工资采用“floor 永远不可突破；有滚动收入时受 budget ratio 限制；滚动收入为零时才允许从 floor 以上存量兜底”的口径。Hosted Agent 公开状态以 desired `paused/disabled` 优先，再投影阻塞型 runtime 状态。

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, pytest + anyio, fakeredis.

## Constraints and verified interfaces

- Worktree: `/Volumes/data/dev/simverse-world`, branch `fix/town-runtime-hygiene`.
- Python: `/Volumes/data/dev/simverse-world/backend/.venv/bin/python`.
- `build_decision_prompt(...) -> tuple[str, str]`: `backend/app/agent/prompts.py:50-62`.
- `BasicDecidePlugin._llm_decide(ctx) -> ActionResult | None`: `backend/app/agent/phases/decide/basic.py:636-687`.
- `BasicExecutePlugin.execute(ctx: TickContext) -> TickContext`: `backend/app/agent/phases/execute/basic.py:155-309`.
- `ActionResult(action, target_slug, target_tile, reason)`: `backend/app/agent/actions.py:36-43`.
- `town_to_resident(db, resident_slug, amount, *, reason, ref_key=None, wage_budget_ratio=None, wage_window_days=7) -> bool`: `backend/app/services/treasury_service.py:474-568`.
- `HostedAgentController.desired_status` accepts `running/paused/disabled`; `runtime_status` accepts `provisioning/idle/claimed/backoff/budget_paused/auth_blocked/error/disabled`: `backend/app/models/hosted_agent.py:25-37`.
- No migration, deployment, push, merge, or production data change is part of this plan.
- Strict TDD: every task first runs at least one newly added or strengthened regression assertion red, applies the exact production change, reruns green, then commits once with the listed message and real `Verified-by:` output. Supporting characterization cases may already pass before the fix, but are not used as the red gate.

## Task 1: keep the movement prompt consistent with the realism gate

**Files:**

- Modify: `backend/app/agent/prompts.py`
- Modify: `backend/tests/test_agent_prompts.py`

### Step 1.1: add the failing regression test

Append this complete test to `backend/tests/test_agent_prompts.py`:

```python
import pytest


@pytest.mark.parametrize(
    ("realism_enabled", "target_slug_line", "target_tile_line", "movement_rule"),
    [
        (
            False,
            '"target_slug": "<居民slug或null>"',
            '"target_tile": [x, y] 或 null',
            "WANDER/VISIT_DISTRICT 填入 target_tile（使用地点入口坐标）",
        ),
        (
            True,
            '"target_slug": "<居民slug、地点ID/名称或null>"',
            '"target_tile": null',
            "VISIT_DISTRICT/WANDER 可在 target_slug 填入地点ID",
        ),
    ],
)
def test_decision_prompt_movement_contract_matches_realism_gate(
    monkeypatch,
    realism_enabled,
    target_slug_line,
    target_tile_line,
    movement_rule,
):
    from app.agent.actions import ActionType
    from app.agent.prompts import build_decision_prompt
    from app.config import settings

    monkeypatch.setattr(settings, "realism_enabled", realism_enabled)
    system, _ = build_decision_prompt(
        resident=_mk_resident(),
        schedule_phase="afternoon",
        world_time="14:00",
        nearby_residents=[],
        memories=[],
        today_actions=[],
        available_actions=[ActionType.WANDER, ActionType.VISIT_DISTRICT],
        max_daily_actions=20,
    )

    assert target_slug_line in system
    assert target_tile_line in system
    assert movement_rule in system
```

Run the red gate (the `False` parameter is the required regression failure; the `True` parameter protects the already-correct gated contract):

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_prompts.py::test_decision_prompt_movement_contract_matches_realism_gate -q
```

Expected red: the `False` parameter still receives the unconditional landmark contract.

### Step 1.2: implement the conditional prompt contract

Add the import at the top of `backend/app/agent/prompts.py`:

```python
from app.config import settings
```

Replace the two target fields and the movement rule in `DECISION_SYSTEM` with:

```python
  "target_slug": {target_slug_contract},
  "target_tile": {target_tile_contract},
```

```python
{movement_target_rule}
```

Immediately before `system = DECISION_SYSTEM.format(...)`, add:

```python
    if settings.realism_enabled:
        target_slug_contract = '"<居民slug、地点ID/名称或null>"'
        target_tile_contract = "null"
        movement_target_rule = (
            "- VISIT_DISTRICT/WANDER 可在 target_slug 填入地点ID（如 "
            "central_plaza, tavern 等）或地点名称（如 中央广场、酒馆 等），"
            "服务端会自动导航；自由闲逛填 null"
        )
    else:
        target_slug_contract = '"<居民slug或null>"'
        target_tile_contract = "[x, y] 或 null"
        movement_target_rule = (
            "- WANDER/VISIT_DISTRICT 填入 target_tile（使用地点入口坐标），"
            "其余为 null"
        )
```

Pass the three values into `DECISION_SYSTEM.format(...)`:

```python
        target_slug_contract=target_slug_contract,
        target_tile_contract=target_tile_contract,
        movement_target_rule=movement_target_rule,
```

### Step 1.3: verify and commit

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_prompts.py tests/test_map_integration.py -q
```

Acceptance:

- Gate off prompt requests legacy coordinates.
- Gate on prompt requests server-owned landmark resolution.
- Existing prompt and map integration tests pass.

Commit: `fix(agent): align movement prompt with realism gate`

## Task 2: restore execute-phase durability for targeted movement

**Files:**

- Modify: `backend/app/agent/phases/execute/basic.py`
- Modify: `backend/tests/test_agent_phases.py`

### Step 2.1: strengthen the existing test and observe red

Add this assertion to `test_basic_execute_movement` after the coordinate assertions:

```python
    ctx.db.commit.assert_awaited_once()
```

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_phases.py::test_basic_execute_movement -q
```

Expected red: targeted movement changes in-memory coordinates but never awaits `db.commit()`.

### Step 2.2: restore the targeted branch commit

In `BasicExecutePlugin.execute`, add this line after the target branch has set `ctx.new_tile` for success, arrival, or unreachable outcomes, and immediately before `elif action == ActionType.WANDER`:

```python
                    await ctx.db.commit()
```

The free WANDER and invalid-target branches keep their existing commits.

### Step 2.3: verify and commit

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_phases.py tests/test_realism_movement.py -q
```

Acceptance:

- Successful targeted movement commits once.
- Arrival and unreachable target state remain inside the same target branch.
- Free WANDER and invalid targets still commit once.

Commit: `fix(agent): persist targeted movement in execute phase`

## Task 3: make free wandering topologically valid and remove the dead housing alias

**Files:**

- Modify: `backend/app/agent/phases/execute/basic.py`
- Modify: `backend/app/agent/map_data.py`
- Modify: `backend/tests/test_agent_phases.py`
- Modify: `backend/tests/test_map_data.py`

### Step 3.1: add failing topology and alias tests plus an orthogonal characterization

Append these complete tests to `backend/tests/test_agent_phases.py`:

```python
@pytest.mark.anyio
async def test_basic_execute_free_wander_rejects_diagonal_only_tile():
    from app.agent.actions import ActionResult
    from app.agent.phases.execute.basic import BasicExecutePlugin

    ctx = _make_ctx()
    ctx.action_result = ActionResult(
        action=ActionType.WANDER,
        target_slug=None,
        target_tile=None,
        reason="自由散步",
    )

    with patch(
        "app.agent.phases.execute.basic.get_walkable_tiles",
        return_value={(10, 10), (11, 11)},
    ):
        ctx = await BasicExecutePlugin().execute(ctx)

    assert (ctx.resident.tile_x, ctx.resident.tile_y) == (10, 10)
    assert ctx.resident.status == "idle"
    assert ctx.new_tile == (10, 10)
    ctx.db.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_basic_execute_free_wander_accepts_orthogonal_tile():
    from app.agent.actions import ActionResult
    from app.agent.phases.execute.basic import BasicExecutePlugin

    ctx = _make_ctx()
    ctx.action_result = ActionResult(
        action=ActionType.WANDER,
        target_slug=None,
        target_tile=None,
        reason="自由散步",
    )

    with patch(
        "app.agent.phases.execute.basic.get_walkable_tiles",
        return_value={(10, 10), (11, 10)},
    ):
        ctx = await BasicExecutePlugin().execute(ctx)

    assert (ctx.resident.tile_x, ctx.resident.tile_y) == (11, 10)
    assert ctx.resident.status == "walking"
    assert ctx.new_tile == (11, 10)
    ctx.db.commit.assert_awaited_once()
```

Add this assertion to `test_get_location_id_by_name_and_aliases` in `backend/tests/test_map_data.py`:

```python
    assert get_location_id_by_name("公寓") is None
    assert get_location_id_by_name("去月华公寓看看") == "apt_moon"
```

Run only the two required red gates first:

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_phases.py::test_basic_execute_free_wander_rejects_diagonal_only_tile tests/test_map_data.py::test_get_location_id_by_name_and_aliases -q
```

Expected red: diagonal tile is selected and generic apartment resolves to the nonexistent `apartment` slug.

### Step 3.2: implement four-neighbour wandering and remove the invalid alias

Replace the free-wander candidate comprehension with:

```python
                    candidates = [
                        (curr[0] + dx, curr[1] + dy)
                        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                        if (curr[0] + dx, curr[1] + dy) in walkable
                    ]
```

Delete this exact entry from `LOCATION_ALIASES`:

```python
    "公寓": "apartment",
```

The exact alias removal exposes the existing partial-display fallback: because
ten concrete apartment names contain `公寓`, returning the first partial match
would still guess a residence. Replace the display-name partial loop with an
ambiguity-safe unique match:

```python
    display_matches = {
        loc_id
        for loc_id, loc in LOCATIONS.items()
        if (loc_name := loc.get("name"))
        and (loc_name in cleaned or cleaned in loc_name)
    }
    if len(display_matches) == 1:
        return next(iter(display_matches))
    if len(display_matches) > 1:
        return None
```

Keep all specific apartment aliases and the existing dynamic `theater` contract unchanged.

### Step 3.3: verify and commit

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_phases.py tests/test_map_data.py tests/test_realism_movement.py -q
```

Acceptance:

- Free WANDER can only move to a four-neighbour walkable tile.
- A diagonal-only tile leaves the resident idle in place.
- `公寓` returns `None` rather than choosing the first of ten partial display-name matches; exact and uniquely partial specific apartment names continue to resolve.

Commit: `fix(agent): constrain free wander and reject generic housing alias`

## Task 4: enforce the wage reserve as a hard floor and make docs truthful

**Files:**

- Modify: `backend/app/services/treasury_service.py`
- Modify: `backend/tests/test_town_treasury_ledger.py`
- Modify: `backend/app/config.py`
- Modify: `backend/.env.example`

### Step 4.1: add a failing hard-floor test, lock the existing zero-income boundary, and isolate budget tests

Add these complete tests after `test_reserve_allows_wages_when_rolling_income_is_zero`:

```python
@pytest.mark.anyio
async def test_income_budget_cannot_break_reserve_floor(db_session, monkeypatch):
    monkeypatch.setattr(settings, "town_ledger_enabled", True)
    monkeypatch.setattr(settings, "town_wage_reserve_floor_sc", 20)
    await treasury_service.tax(db_session, 20, reason="sales_tax:test")

    assert not await treasury_service.town_to_resident(
        db_session,
        "clerk",
        1,
        reason="wage:clerk",
        wage_budget_ratio=0.70,
    )
    assert await treasury_service.balance(db_session) == 20
    assert await coin_service.treasury_balance(db_session, "clerk") == 0


@pytest.mark.anyio
async def test_ledger_off_allows_only_surplus_above_floor(db_session, monkeypatch):
    monkeypatch.setattr(settings, "town_ledger_enabled", False)
    monkeypatch.setattr(settings, "town_wage_reserve_floor_sc", 20)
    db_session.add(
        TownTreasury(
            key=TOWN_KEY,
            balance_sc=21,
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    assert await treasury_service.town_to_resident(
        db_session,
        "clerk",
        1,
        reason="wage:clerk",
        wage_budget_ratio=0.70,
    )
    assert await treasury_service.balance(db_session) == 20
    assert await treasury_service.wage_window_totals(db_session) == (0, 0)
    assert not await treasury_service.town_to_resident(
        db_session,
        "postman",
        1,
        reason="wage:postman",
        wage_budget_ratio=0.70,
    )
```

In `test_rolling_income_budget_keeps_thirty_percent_reserve`, add:

```python
    monkeypatch.setattr(settings, "town_wage_reserve_floor_sc", 0)
```

In `test_funding_split_is_dark_then_public_only`, change the tax seed from 20 to 30:

```python
    await treasury_service.tax(db_session, 30, reason="sales_tax:legacy")
```

Run only the required red gate first. The ledger-off test is a characterization of the already-supported zero-income fallback and joins the green verification after implementation:

```bash
cd backend && .venv/bin/python -m pytest tests/test_town_treasury_ledger.py::test_income_budget_cannot_break_reserve_floor -q
```

Expected red: a positive rolling budget pays below the floor.

### Step 4.2: implement the budget/fallback/floor decision

Replace the current reserve decision block in `town_to_resident` with:

```python
            income_budget = int(income * ratio)
            from app.config import settings
            reserve_floor = max(
                0,
                int(getattr(settings, "town_wage_reserve_floor_sc", 20)),
            )
            reserve_available = max(0, int(locked_balance - reserve_floor))
            budget_ok = wages + amount <= income_budget
            reserve_fallback_ok = income == 0 and amount <= reserve_available
            floor_ok = amount <= reserve_available

            if not floor_ok or not (budget_ok or reserve_fallback_ok):
                logger.info(
                    "town wage budget exhausted: resident=%s amount=%d "
                    "income=%d wages=%d ratio=%.3f balance=%d floor=%d",
                    resident_slug,
                    amount,
                    income,
                    wages,
                    ratio,
                    locked_balance,
                    reserve_floor,
                )
                await db.commit()
                return False
```

Update the Settings comment to:

```python
    town_wage_reserve_floor_sc: int = 20        # 工资支付后的镇库硬下限；零滚动收入时仅可动用下限以上存量
```

Replace the payroll documentation block in `backend/.env.example` with:

```dotenv
# Sustainable duty payroll is an independent dark gate. false preserves the
# legacy all-duty/perk wage behavior; true pays only public duties at the flat
# rate. Positive trailing-window income is capped by income × budget ratio.
# Requires TOWN_TREASURY_ENABLED=true. TOWN_LEDGER_ENABLED=true is recommended
# so the rolling income budget sees real flows. With the ledger dark, rolling
# income is 0 and wages may use only existing balance above the hard reserve
# floor; the floor itself is never spendable.
TOWN_DUTY_FUNDING_ENABLED=false
TOWN_PUBLIC_DUTY_WAGE_SC=1
TOWN_WAGE_INCOME_WINDOW_DAYS=7
TOWN_WAGE_BUDGET_RATIO=0.70
# Minimum town balance left after every sustainable public-duty wage payment.
TOWN_WAGE_RESERVE_FLOOR_SC=20
```

### Step 4.3: verify and commit

```bash
cd backend && .venv/bin/python -m pytest tests/test_town_treasury_ledger.py tests/test_treasury_service.py tests/test_env_example_consistency.py -q
```

Acceptance:

- No sustainable wage can reduce balance below floor.
- Positive income remains subject to budget ratio.
- Zero income, including ledger-off mode, can use only surplus above floor.
- `.env.example` explains the actual ledger-off and floor behavior.
- Env consistency remains 23/23.

Commit: `fix(economy): enforce the public wage reserve floor`

## Task 5: project Hosted Agent controller states accurately

**Files:**

- Modify: `backend/app/services/agent_player_service.py`
- Modify: `backend/tests/test_agent_players_api.py`

### Step 5.1: add the failing public API status matrix

Add this import to `backend/tests/test_agent_players_api.py`:

```python
from app.models.hosted_agent import HostedAgentController
```

Append this complete parameterized test after the public town snapshot test:

```python
@pytest.mark.parametrize(
    ("desired_status", "runtime_status", "expected"),
    [
        ("paused", "idle", "paused"),
        ("paused", "error", "paused"),
        ("disabled", "disabled", "disabled"),
        ("running", "error", "error"),
        ("running", "backoff", "backoff"),
        ("running", "budget_paused", "budget_paused"),
        ("running", "auth_blocked", "auth_blocked"),
    ],
)
@pytest.mark.anyio
async def test_public_town_snapshot_maps_hosted_controller_status(
    client,
    db_session,
    desired_status,
    runtime_status,
    expected,
):
    created, _credentials, _session = await _register_and_session(
        client,
        f"托管状态-{desired_status}-{runtime_status}",
    )
    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None
    resident = await db_session.get(Resident, profile.resident_id)
    assert resident is not None
    profile.control_kind = "hosted_agent"
    db_session.add(
        HostedAgentController(
            owner_user_id=profile.user_id,
            request_id=str(uuid.uuid4()),
            create_request_hash=hashlib.sha256(
                f"{desired_status}:{runtime_status}".encode()
            ).hexdigest(),
            agent_player_id=profile.id,
            desired_status=desired_status,
            runtime_status=runtime_status,
            provider_host="api.example.com",
            model="test-model",
            provider_validation_required=False,
            secret_envelope="test-envelope",
            identity_json={},
            policy_json={},
        )
    )
    await db_session.commit()

    response = await client.get("/api/v1/public/town/snapshot")
    assert response.status_code == 200, response.text
    actor = next(
        item
        for item in response.json()["residents"]
        if item.get("slug") == resident.slug
    )
    assert actor["activity_status"] == expected
```

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_players_api.py::test_public_town_snapshot_maps_hosted_controller_status -q
```

Expected red: paused resolves as dormant and the blocked runtime states collapse to online/dormant.

### Step 5.2: implement desired-first status projection

Replace the current hosted override with:

```python
        if profile.control_kind == "hosted_agent" and profile.id in hosted_controllers:
            hc = hosted_controllers[profile.id]
            if hc.desired_status in {"paused", "disabled"}:
                activity_status = hc.desired_status
            elif hc.runtime_status in {
                "error",
                "backoff",
                "budget_paused",
                "auth_blocked",
            }:
                activity_status = hc.runtime_status
```

### Step 5.3: verify and commit

```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_players_api.py tests/test_hosted_agents.py -q
```

Acceptance:

- desired paused wins over runtime idle/error.
- desired disabled is visible as disabled.
- running controllers expose blocking runtime states.
- ordinary online/dormant behavior remains unchanged.

Commit: `fix(snapshot): project hosted controller activity states`

## Final verification

Run targeted regression suite:

```bash
cd backend && .venv/bin/python -m pytest \
  tests/test_agent_prompts.py \
  tests/test_agent_phases.py \
  tests/test_realism_movement.py \
  tests/test_map_data.py \
  tests/test_map_integration.py \
  tests/test_town_treasury_ledger.py \
  tests/test_treasury_service.py \
  tests/test_env_example_consistency.py \
  tests/test_agent_players_api.py \
  tests/test_hosted_agents.py -q
```

Run formatting/static guards:

```bash
git diff --check origin/master...HEAD
cd backend && .venv/bin/python -m compileall -q app tests
```

### Verification-discovered diff hygiene cleanup

If the range check reports `backend/tests/test_duty_service.py:414: new blank line at EOF.`, delete the single empty line after the final `assert "市政厅" in mem.content`. This is an existing PR-range formatting defect with no runtime behavior change.

Verify:

```bash
cd backend && .venv/bin/python -m pytest tests/test_duty_service.py -q
git diff --check origin/master...HEAD
```

Commit: `style(test): remove trailing duty test whitespace`

Run the full backend suite, retain its failed node IDs as an artifact under `/tmp`, and compare that exact set with the master CI log from Actions run `32735386500` (not merely the count). The PR may not add any failure beyond the known master set of 49 nodes. If the Actions log is unavailable, create an isolated temporary `origin/master` worktree and reproduce the baseline locally before drawing a no-regression conclusion.

Run real application verification with isolated SQLite and local Redis:

1. `docker info` and `docker compose ps` must show healthy Redis.
2. Start uvicorn with `AUTO_CREATE_TABLES=true`, `RUN_BACKGROUND_TASKS=false`, and an isolated temporary SQLite database.
3. `GET /health` must return HTTP 200 with `{"status":"ok"}`.
4. Register and pair an Agent over HTTP, seed a Hosted Agent controller in the same isolated database, then fetch `/api/v1/public/town/snapshot`; verify paused and blocked status values from the real JSON response.
5. Exercise `town_to_resident` against an isolated async database and show that balance 20/floor 20 refuses a 1 SC wage while balance 21/floor 20 allows exactly one zero-income reserve payment.
6. Stop the server cleanly, confirm `git status` contains only intended commits, and inspect `git show HEAD` after the `/Volumes/data` drain sentinel.

## Plan self-check

| Gate | Evidence |
|---|---|
| Spec coverage | Tasks 1-5 cover all seven reviewed issues; Task 3 and Task 4 each cover two closely coupled issues. |
| Placeholder scan | All code blocks are complete; no unfinished markers, omitted bodies, or pseudo-code remain. |
| Type consistency | Signatures match the explored `build_decision_prompt`, `ActionResult`, `TickContext`, `town_to_resident`, `HostedAgentController`, and pytest fixtures. |
| Step size | Each task is one independently testable behavior group and ends in one commit. |
