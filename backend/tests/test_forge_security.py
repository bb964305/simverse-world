"""Regression coverage for Forge durability, quotas, ownership, and metering."""

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.forge.legacy_pipeline import run_quick_pipeline
from app.forge.legacy_sessions import (
    ForgeSessionNotFound,
    claim_internal_run,
    get_status,
    start_forge,
    start_quick_forge,
    submit_answer,
)
from app.models.forge_session import ForgeSession
from app.models.resident import Resident
from app.models.transaction import Transaction
from app.models.user import User
from app.services.auth_service import create_token
from app.services.ugc_creation_quota import (
    DailyCreationLimitExceeded,
    claim_creation_slot,
    claim_forge_reward,
)


async def _user(db: AsyncSession, email: str) -> User:
    user = User(name=email.split("@")[0], email=email, soul_coin_balance=100)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
async def concurrent_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'forge-concurrency.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.anyio
async def test_legacy_session_is_durable_and_owner_bound(concurrent_factory):
    async with concurrent_factory() as db:
        owner = await _user(db, "owner@forge.test")
        other = await _user(db, "other@forge.test")
        started = await start_forge(db, owner.id, "Durable Resident")

    forge_id = started["forge_id"]
    async with concurrent_factory() as db:
        advanced = await submit_answer(db, forge_id, owner.id, "ability answer")
        assert advanced["step"] == 2

    # A different worker/session can continue and read the same state.
    async with concurrent_factory() as db:
        status = await get_status(db, forge_id, owner.id)
        assert status["answers"]["2"] == "ability answer"

        with pytest.raises(ForgeSessionNotFound):
            await get_status(db, forge_id, other.id)
        with pytest.raises(ForgeSessionNotFound):
            await submit_answer(db, forge_id, other.id, "injected")


@pytest.mark.anyio
async def test_legacy_execution_claim_allows_only_one_worker(concurrent_factory):
    async with concurrent_factory() as db:
        user = await _user(db, "claim@forge.test")
        session = await start_quick_forge(db, user.id, "Claim", "source material")
        forge_id = session.id

    async def claim():
        async with concurrent_factory() as db:
            row = await claim_internal_run(db, forge_id)
            return row is not None

    assert sorted(await asyncio.gather(claim(), claim())) == [False, True]
    async with concurrent_factory() as db:
        row = await db.get(ForgeSession, forge_id)
        assert row.status == "running"


@pytest.mark.anyio
async def test_stale_legacy_generation_is_persistently_terminal(db_session):
    user = await _user(db_session, "stale@forge.test")
    session = await start_quick_forge(db_session, user.id, "Stale", "source")
    session.status = "running"
    session.updated_at = datetime.now(UTC) - timedelta(minutes=21)
    await db_session.commit()

    status = await get_status(db_session, session.id, user.id)
    assert status["status"] == "error"
    assert "stalled" in status["error"]
    await db_session.refresh(session)
    assert session.status == "error"


@pytest.mark.anyio
async def test_stale_deep_generation_releases_slug_reservation(client, db_session):
    user = await _user(db_session, "stale-deep@forge.test")
    session = ForgeSession(
        user_id=user.id,
        character_name="Stale Deep",
        target_slug="stale-deep",
        mode="deep",
        status="building",
        current_stage="build",
        research_data={},
        extraction_data={},
        build_output={},
        validation_report={},
        refinement_log={},
        updated_at=datetime.now(UTC) - timedelta(minutes=21),
    )
    db_session.add(session)
    await db_session.commit()

    response = await client.get(
        f"/forge/deep-status/{session.id}",
        headers={"Authorization": f"Bearer {create_token(user.id)}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    await db_session.refresh(session)
    assert session.target_slug is None


@pytest.mark.anyio
async def test_legacy_runner_timeout_marks_durable_error(
    concurrent_factory, monkeypatch
):
    import app.routers.forge as forge_router

    async with concurrent_factory() as db:
        user = await _user(db, "timeout@forge.test")
        session = await start_quick_forge(db, user.id, "Timeout", "source")
        forge_id = session.id

    async def never_finishes(forge_id, db):
        await asyncio.Event().wait()

    monkeypatch.setattr(forge_router, "async_session", concurrent_factory)
    monkeypatch.setattr(forge_router, "FORGE_PIPELINE_TIMEOUT_S", 0.01)
    await forge_router._run_legacy_pipeline_bg(forge_id, never_finishes)

    async with concurrent_factory() as db:
        row = await db.get(ForgeSession, forge_id)
        assert row.status == "error"
        assert "timed out" in (row.refinement_log or {})["error"]


@pytest.mark.anyio
async def test_creation_and_reward_quota_claims_are_atomic(concurrent_factory, monkeypatch):
    monkeypatch.setattr(settings, "ugc_daily_creation_limit", 1)
    monkeypatch.setattr(settings, "forge_daily_reward_limit", 1)
    async with concurrent_factory() as db:
        user = await _user(db, "quota@forge.test")
        user_id = user.id

    async def attempt(claim):
        async with concurrent_factory() as db:
            try:
                await claim(db, user_id)
                await db.commit()
                return True
            except DailyCreationLimitExceeded:
                await db.rollback()
                return False

    creation = await asyncio.gather(
        attempt(claim_creation_slot), attempt(claim_creation_slot)
    )
    reward = await asyncio.gather(
        attempt(claim_forge_reward), attempt(claim_forge_reward)
    )
    assert sorted(creation) == [False, True]
    assert sorted(reward) == [False, True]

    async with concurrent_factory() as db:
        user = await db.get(User, user_id)
        assert user.ugc_creation_count == 1
        assert user.forge_reward_count == 1


@pytest.mark.anyio
async def test_legacy_and_deep_status_hide_foreign_sessions(client, db_session):
    owner = await _user(db_session, "idor-owner@forge.test")
    other = await _user(db_session, "idor-other@forge.test")
    legacy = await start_forge(db_session, owner.id, "Private source")
    deep = ForgeSession(
        user_id=owner.id,
        character_name="Private deep source",
        target_slug="private-deep-source",
        mode="deep",
        status="routed",
        current_stage="router",
        research_data={"raw_text": "secret source material"},
        extraction_data={},
        build_output={},
        validation_report={},
        refinement_log={},
    )
    db_session.add(deep)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {create_token(other.id)}"}
    legacy_status = await client.get(
        f"/forge/status/{legacy['forge_id']}", headers=headers
    )
    legacy_answer = await client.post(
        "/forge/answer",
        json={"forge_id": legacy["forge_id"], "answer": "tamper"},
        headers=headers,
    )
    deep_status = await client.get(f"/forge/deep-status/{deep.id}", headers=headers)
    assert legacy_status.status_code == 404
    assert legacy_answer.status_code == 404
    assert deep_status.status_code == 404


@pytest.mark.anyio
async def test_shared_ugc_limit_blocks_import_after_forge(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "ugc_daily_creation_limit", 1)
    user = await _user(db_session, "shared-quota@forge.test")
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}

    started = await client.post("/forge/start", json={"name": "First"}, headers=headers)
    assert started.status_code == 200
    imported = await client.post(
        "/residents/import-card",
        json={"name": "Second", "ability_md": "# Ability\ncontent"},
        headers=headers,
    )
    assert imported.status_code == 429
    assert imported.json()["detail"]["code"] == "daily_creation_limit"


@pytest.mark.anyio
async def test_guided_final_answer_budget_gate_preserves_collecting_state(
    client, db_session
):
    user = await _user(db_session, "guided-budget@forge.test")
    started = await start_forge(db_session, user.id, "Budgeted")
    forge_id = started["forge_id"]
    for answer in ("ability", "persona", "soul"):
        await submit_answer(db_session, forge_id, user.id, answer)

    headers = {"Authorization": f"Bearer {create_token(user.id)}"}
    with patch("app.routers.forge.forge_blocked", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/forge/answer",
            json={"forge_id": forge_id, "answer": "final material"},
            headers=headers,
        )
    assert response.status_code == 402
    status = await get_status(db_session, forge_id, user.id)
    assert status["status"] == "collecting"
    assert status["step"] == 4
    assert "5" not in status["answers"]


@pytest.mark.anyio
async def test_all_forge_entry_inputs_are_bounded(client, db_session):
    from app.forge.pipeline import FORGE_INPUT_MAX_CHARS

    user = await _user(db_session, "bounded@forge.test")
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}
    started = await start_forge(db_session, user.id, "Bounded")
    oversized = "x" * (FORGE_INPUT_MAX_CHARS + 1)

    answer = await client.post(
        "/forge/answer",
        json={"forge_id": started["forge_id"], "answer": oversized},
        headers=headers,
    )
    quick = await client.post(
        "/forge/quick",
        json={"name": "Quick", "raw_text": oversized},
        headers=headers,
    )
    with patch("app.routers.forge.get_llm_client", return_value=MagicMock()):
        deep = await client.post(
            "/forge/deep-start",
            json={"character_name": "Deep", "user_material": oversized},
            headers=headers,
        )

    assert answer.status_code == 400
    assert quick.status_code == 400
    assert deep.status_code == 400
    assert (await get_status(
        db_session, started["forge_id"], user.id
    ))["step"] == 1


@pytest.mark.anyio
async def test_quick_pipeline_never_uses_temp_secret_and_is_idempotent(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "ugc_daily_creation_limit", 5)
    monkeypatch.setattr(settings, "forge_daily_reward_limit", 5)
    user = await _user(db_session, "quick-secure@forge.test")
    session = await start_quick_forge(
        db_session, user.id, "Quick Secure", "long source material"
    )

    response = SimpleNamespace(content=[SimpleNamespace(
        text="# Ability\nA===SPLIT===# Persona\nP===SPLIT===# Soul\nS"
    )])
    llm = MagicMock()
    llm.messages.create = AsyncMock(return_value=response)
    meter = AsyncMock()
    sbti = AsyncMock(return_value=None)

    def temp_secret_forbidden(*args, **kwargs):
        raise AssertionError("Forge must not create an API-key temporary file")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", temp_secret_forbidden)
    with (
        patch("app.forge.legacy_pipeline.get_client", return_value=llm),
        patch("app.forge.legacy_pipeline.record_usage", meter),
        patch("app.forge.legacy_pipeline.enforce_forge_budget", new=AsyncMock()),
        patch(
            "app.forge.legacy_pipeline.allocate_resident_location",
            new=AsyncMock(return_value=("free", 1, 1, None)),
        ),
        patch("app.services.sbti_service.compute_sbti", sbti),
        patch("app.forge.legacy_pipeline.notify_forge_progress", new=AsyncMock()),
        patch("app.forge.legacy_pipeline.notify_forge_done", new=AsyncMock()),
    ):
        await run_quick_pipeline(session.id, db_session)
        # Public/internal retry observes the terminal row and performs no work.
        await run_quick_pipeline(session.id, db_session)

    await db_session.refresh(user)
    await db_session.refresh(session)
    assert session.status == "done"
    assert user.soul_coin_balance == 150
    assert llm.messages.create.await_count == 1
    assert meter.await_count == 1
    assert meter.await_args.kwargs["user_id"] == user.id
    assert meter.await_args.kwargs["conversation_id"] == session.id
    assert sbti.await_args.kwargs == {
        "user_id": user.id,
        "conversation_id": session.id,
    }
    assert (await db_session.scalar(select(func.count()).select_from(Resident))) == 1
    assert (await db_session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.reason == f"forge_creation:{session.id}"
        )
    )) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("raw_status", "current_stage", "expected_stage", "progress"),
    [
        ("routed", "router", "routing", 10),
        ("building", "build", "building", 60),
        ("done", "refinement", "done", 100),
        ("error", "validation", "error", 100),
    ],
)
async def test_deep_status_stable_stage_and_result_contract(
    client,
    db_session,
    raw_status,
    current_stage,
    expected_stage,
    progress,
):
    user = await _user(
        db_session, f"deep-contract-{raw_status}@forge.test"
    )
    session = ForgeSession(
        user_id=user.id,
        character_name="Contract Resident",
        target_slug=f"contract-{raw_status}",
        mode="deep",
        status=raw_status,
        current_stage=current_stage,
        research_data={},
        extraction_data={},
        build_output={
            "ability_md": "ability",
            "persona_md": "persona",
            "soul_md": "soul",
        },
        validation_report={
            "star_rating": 2,
            "district": "free",
            "resident_id": "resident-1",
        },
        refinement_log={"error": "failed validation"} if raw_status == "error" else {},
    )
    db_session.add(session)
    await db_session.commit()

    response = await client.get(
        f"/forge/deep-status/{session.id}",
        headers={"Authorization": f"Bearer {create_token(user.id)}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == raw_status
    assert body["current_stage"] == current_stage
    assert body["character_name"] == "Contract Resident"
    assert body["stage"] == expected_stage
    assert body["progress"] == progress
    assert body["name"] == "Contract Resident"
    assert body["ability_md"] == "ability"
    assert body["persona_md"] == "persona"
    assert body["soul_md"] == "soul"
    assert body["star_rating"] == 2
    assert body["district"] == "free"
    assert body["resident_id"] == "resident-1"
    assert body["error"] == (
        "failed validation" if raw_status == "error" else None
    )


@pytest.mark.anyio
async def test_router_usage_is_bound_to_user_and_request():
    from app.forge.router_stage import InputRouter

    response = SimpleNamespace(content=[SimpleNamespace(text='{"route":"quick"}')])
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    with patch("app.forge.router_stage.record_usage", new=AsyncMock()) as meter:
        await InputRouter(
            client,
            "model",
            user_id="user-1",
            session_id="forge-1",
        ).run("Name", "raw", "")
    assert meter.await_args.kwargs["user_id"] == "user-1"
    assert meter.await_args.kwargs["conversation_id"] == "forge-1"


@pytest.mark.anyio
async def test_build_budget_guard_runs_between_every_paid_call():
    from app.forge.build_stage import BuildStage

    client = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=[
            SimpleNamespace(content=[SimpleNamespace(text="ability")]),
            SimpleNamespace(content=[SimpleNamespace(text="persona")]),
            SimpleNamespace(content=[SimpleNamespace(text="soul")]),
        ]
    )
    guard = AsyncMock()
    with patch("app.forge.build_stage.record_usage", new=AsyncMock()):
        await BuildStage(client, "model", budget_check=guard).run("Name", "research")
    # Before and after each of Build's three calls.
    assert guard.await_count == 6


@pytest.mark.anyio
async def test_canonical_completion_is_atomic_and_replay_safe(db_session, monkeypatch):
    from app.forge.pipeline import ForgePipeline

    monkeypatch.setattr(settings, "ugc_daily_creation_limit", 5)
    monkeypatch.setattr(settings, "forge_daily_reward_limit", 5)
    user = await _user(db_session, "canonical@forge.test")
    router_response = SimpleNamespace(
        content=[SimpleNamespace(text='{"route":"quick"}')]
    )
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=router_response)
    pipeline = ForgePipeline(db_session, client, client, model="model")

    with (
        patch("app.forge.router_stage.record_usage", new=AsyncMock()),
        patch(
            "app.forge.build_stage.BuildStage.run",
            new=AsyncMock(return_value={
                "ability_md": "# Ability",
                "persona_md": "# Persona",
                "soul_md": "# Soul",
            }),
        ),
        patch(
            "app.services.resident_placement.allocate_resident_location",
            new=AsyncMock(return_value=("free", 2, 2, None)),
        ),
        patch("app.forge.pipeline.notify_forge_progress", new=AsyncMock()),
        patch("app.forge.pipeline.notify_forge_done", new=AsyncMock()),
    ):
        session = await pipeline.start(user.id, "Canonical", "source")
        await pipeline.run_to_completion(session.id)
        await pipeline.run_to_completion(session.id)

    await db_session.refresh(user)
    await db_session.refresh(session)
    assert session.status == "done"
    assert session.target_slug is None
    assert session.validation_report["resident_id"]
    assert session.validation_report["district"] == "free"
    assert session.validation_report["star_rating"] == 2
    assert user.soul_coin_balance == 150
    assert user.forge_reward_count == 1
    assert (await db_session.scalar(select(func.count()).select_from(Resident))) == 1
    assert (await db_session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.reason == f"forge_creation:{session.id}"
        )
    )) == 1


@pytest.mark.anyio
async def test_reward_limit_never_rolls_back_a_cross_midnight_resident(
    db_session,
    monkeypatch,
):
    from app.forge.pipeline import ForgePipeline

    monkeypatch.setattr(settings, "forge_daily_reward_limit", 1)
    user = await _user(db_session, "reward-exhausted@forge.test")
    user.forge_reward_date = datetime.now(UTC).date()
    user.forge_reward_count = 1
    session = ForgeSession(
        user_id=user.id,
        character_name="Cross Midnight",
        target_slug="cross-midnight",
        mode="quick",
        status="running",
        current_stage="build",
        build_output={
            "ability_md": "# Ability",
            "persona_md": "# Persona",
            "soul_md": "# Soul",
        },
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(session)
    await db_session.commit()

    pipeline = ForgePipeline(db_session, MagicMock(), MagicMock(), model="model")
    with patch(
        "app.services.resident_placement.allocate_resident_location",
        new=AsyncMock(return_value=("free", 3, 3, None)),
    ):
        await pipeline._create_resident(session)
        session.status = "done"
        await db_session.commit()

    await db_session.refresh(user)
    await db_session.refresh(session)
    assert session.status == "done"
    assert session.target_slug is None
    assert session.validation_report["reward_granted"] is False
    assert user.soul_coin_balance == 100
    assert user.forge_reward_count == 1
    assert await db_session.scalar(
        select(func.count()).select_from(Resident).where(
            Resident.slug == "cross-midnight"
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.reason == f"forge_creation:{session.id}"
        )
    ) == 0


@pytest.mark.anyio
async def test_canonical_late_slug_conflict_cannot_reward(db_session):
    from app.forge.pipeline import ForgePipeline

    user = await _user(db_session, "late-slug@forge.test")
    router_response = SimpleNamespace(
        content=[SimpleNamespace(text='{"route":"quick"}')]
    )
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=router_response)
    pipeline = ForgePipeline(db_session, client, client, model="model")

    with patch("app.forge.router_stage.record_usage", new=AsyncMock()):
        session = await pipeline.start(user.id, "Late Conflict", "source")

    # Simulate a non-Forge import taking the slug after reservation but before
    # terminalization. The residents.slug unique constraint is the final fence.
    db_session.add(Resident(
        slug=session.target_slug,
        name="External winner",
        creator_id=user.id,
        resident_type="resident",
        district="free",
        status="idle",
    ))
    await db_session.commit()

    with (
        patch(
            "app.forge.build_stage.BuildStage.run",
            new=AsyncMock(return_value={
                "ability_md": "# Ability",
                "persona_md": "# Persona",
                "soul_md": "# Soul",
            }),
        ),
        patch("app.forge.pipeline.notify_forge_progress", new=AsyncMock()),
        patch("app.forge.pipeline.notify_forge_error", new=AsyncMock()),
    ):
        result = await pipeline.run_to_completion(session.id)

    await db_session.refresh(user)
    assert result.status == "error"
    assert result.target_slug is None
    assert user.soul_coin_balance == 100
    assert user.forge_reward_count == 0
    assert (await db_session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.reason == f"forge_creation:{session.id}"
        )
    )) == 0
