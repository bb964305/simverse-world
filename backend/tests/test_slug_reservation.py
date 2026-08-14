"""Cross-worker slug reservation regressions for Forge and UGC imports."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.forge.legacy_pipeline import run_quick_pipeline
from app.forge.legacy_sessions import start_forge, start_quick_forge
from app.forge.pipeline import ForgePipeline, ForgeSlugConflict
from app.models.forge_session import ForgeSession
from app.models.resident import Resident
from app.models.transaction import Transaction
from app.models.user import User
from app.services.auth_service import create_token
from app.services.slug_reservation import (
    SlugReservationConflict,
    _import_reservation_stale_after,
    consume_slug_reservation,
    import_work_timeout_seconds,
    release_slug_reservation,
    reserve_slug,
)


async def _user(db: AsyncSession, email: str) -> User:
    user = User(name=email.split("@")[0], email=email, soul_coin_balance=100)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
async def reservation_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'slug-reservations.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    yield factory
    await engine.dispose()


@pytest.mark.anyio
async def test_concurrent_slug_reservations_have_exactly_one_winner(
    reservation_factory,
):
    async with reservation_factory() as db:
        user = await _user(db, "slug-race@forge.test")
        user_id = user.id

    async def attempt(owner_kind: str) -> str | None:
        async with reservation_factory() as db:
            try:
                reservation = await reserve_slug(
                    db,
                    user_id=user_id,
                    character_name="Same Name",
                    requested_slug="same-name",
                    owner_kind=owner_kind,
                )
                await db.commit()
                return reservation.id
            except SlugReservationConflict:
                await db.rollback()
                return None

    winners = await asyncio.gather(attempt("import_card"), attempt("skill_zip"))
    assert sum(value is not None for value in winners) == 1

    async with reservation_factory() as db:
        held = await db.scalar(
            select(func.count())
            .select_from(ForgeSession)
            .where(ForgeSession.target_slug == "same-name")
        )
        assert held == 1


@pytest.mark.anyio
async def test_import_reservation_blocks_canonical_before_router_and_release_reopens(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ugc_daily_creation_limit", 5)
    user = await _user(db_session, "import-first@forge.test")
    reservation = await reserve_slug(
        db_session,
        user_id=user.id,
        character_name="Reserved Name",
        requested_slug="reserved-name",
        owner_kind="import_card",
    )
    await db_session.commit()
    reservation_id = reservation.id
    user_id = user.id

    router_response = SimpleNamespace(
        content=[SimpleNamespace(text='{"route":"quick"}')]
    )
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=router_response)
    pipeline = ForgePipeline(db_session, client, client, model="model")
    pipeline._check_budget = AsyncMock()

    with pytest.raises(ForgeSlugConflict):
        await pipeline.start(user.id, "Reserved Name", "source")
    assert client.messages.create.await_count == 0
    await db_session.refresh(user)
    # The losing Forge quota claim and session insert were one transaction.
    assert user.ugc_creation_count == 0

    assert await release_slug_reservation(
        db_session, reservation_id, user_id=user_id
    )
    await db_session.commit()
    with patch("app.forge.router_stage.record_usage", new=AsyncMock()):
        session = await pipeline.start(user_id, "Reserved Name", "source")

    assert session.target_slug == "reserved-name"
    assert session.status == "routed"
    assert client.messages.create.await_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "status", "age"),
    [
        ("deep", "building", timedelta(minutes=21)),
        ("legacy_quick", "running", timedelta(minutes=21)),
        ("guided", "collecting", timedelta(hours=2)),
    ],
)
async def test_new_reservation_reaps_abandoned_forge_without_status_poll(
    db_session,
    monkeypatch,
    mode,
    status,
    age,
):
    monkeypatch.setattr(settings, "forge_session_ttl_hours", 1)
    owner = await _user(db_session, f"abandoned-{mode}@forge.test")
    newcomer = await _user(db_session, f"new-{mode}@forge.test")
    abandoned = ForgeSession(
        user_id=owner.id,
        character_name="Abandoned",
        target_slug=f"abandoned-{mode}",
        mode=mode,
        status=status,
        current_stage="build" if status != "collecting" else "collecting",
        research_data={},
        extraction_data={},
        build_output={},
        validation_report={},
        refinement_log={},
        updated_at=datetime.now(UTC) - age,
    )
    db_session.add(abandoned)
    await db_session.commit()

    replacement = await reserve_slug(
        db_session,
        user_id=newcomer.id,
        character_name="Replacement",
        requested_slug=f"abandoned-{mode}",
        owner_kind="import_card",
    )
    await db_session.commit()
    await db_session.refresh(abandoned)

    assert abandoned.status == "error"
    assert abandoned.target_slug is None
    assert replacement.target_slug == f"abandoned-{mode}"
    assert "error" in (abandoned.refinement_log or {})


@pytest.mark.anyio
async def test_import_reservation_outlives_forge_stale_window_but_is_bounded(
    db_session, monkeypatch,
):
    work_timeout = import_work_timeout_seconds()
    # The proof no longer depends on either a daily quota or a per-minute burst
    # being a total queue bound.
    monkeypatch.setattr(settings, "ugc_daily_creation_limit", 0)
    monkeypatch.setattr(settings, "rest_rate_limit_import_per_minute", 1)
    assert import_work_timeout_seconds() == work_timeout
    assert (
        _import_reservation_stale_after().total_seconds()
        > import_work_timeout_seconds()
    )
    owner = await _user(db_session, "long-import@forge.test")
    newcomer = await _user(db_session, "long-import-new@forge.test")
    owner_id = owner.id
    newcomer_id = newcomer.id
    reservation = await reserve_slug(
        db_session,
        user_id=owner_id,
        character_name="Long Import",
        requested_slug="long-import",
        owner_kind="skill_import",
    )
    await db_session.commit()

    # A healthy import may exceed Forge's 20-minute runner window because it
    # can perform two independently retried LLM calls.  It must keep its slug.
    reservation.updated_at = datetime.now(UTC) - timedelta(minutes=21)
    await db_session.commit()
    with pytest.raises(SlugReservationConflict):
        await reserve_slug(
            db_session,
            user_id=newcomer_id,
            character_name="Too Early",
            requested_slug="long-import",
            owner_kind="import_card",
        )
    await db_session.rollback()
    await db_session.refresh(reservation)
    assert reservation.status == "reserved"
    assert reservation.target_slug == "long-import"

    # A crashed import still has a finite lease and is recoverable by the next
    # reservation attempt after that import-specific bound.
    reservation.updated_at = (
        datetime.now(UTC) - _import_reservation_stale_after() - timedelta(seconds=1)
    )
    await db_session.commit()
    replacement = await reserve_slug(
        db_session,
        user_id=newcomer_id,
        character_name="Recovered",
        requested_slug="long-import",
        owner_kind="import_card",
    )
    await db_session.commit()
    await db_session.refresh(reservation)

    assert reservation.status == "expired"
    assert reservation.target_slug is None
    assert replacement.target_slug == "long-import"


@pytest.mark.anyio
async def test_standalone_consume_hands_slug_to_resident_atomically(db_session):
    user = await _user(db_session, "consume@forge.test")
    reservation = await reserve_slug(
        db_session,
        user_id=user.id,
        character_name="Consumed",
        requested_slug="consumed",
        owner_kind="skill_zip",
    )
    await db_session.commit()

    resident = Resident(
        slug="consumed",
        name="Consumed",
        creator_id=user.id,
        resident_type="resident",
        district="free",
        status="idle",
    )
    db_session.add(resident)
    await db_session.flush()
    assert await consume_slug_reservation(
        db_session, reservation.id, user_id=user.id
    ) == "consumed"
    await db_session.commit()
    await db_session.refresh(reservation)

    assert reservation.status == "consumed"
    assert reservation.target_slug is None
    with pytest.raises(SlugReservationConflict):
        await reserve_slug(
            db_session,
            user_id=user.id,
            character_name="Too Late",
            requested_slug="consumed",
            owner_kind="import_card",
        )
    await db_session.rollback()


@pytest.mark.anyio
async def test_internal_reservations_are_not_public_forge_or_admin_runs(
    client,
    db_session,
):
    from app.routers.admin.forge_monitor import (
        _get_forge_session,
        _list_forge_sessions,
    )

    owner = await _user(db_session, "hidden-reservation@forge.test")
    admin = User(
        name="admin",
        email="hidden-reservation-admin@forge.test",
        is_admin=True,
    )
    db_session.add(admin)
    await db_session.commit()
    reservation = await reserve_slug(
        db_session,
        user_id=owner.id,
        character_name="Internal Only",
        requested_slug="internal-only",
        owner_kind="skill_zip",
    )
    await db_session.commit()

    public = await client.get(
        f"/forge/deep-status/{reservation.id}",
        headers={"Authorization": f"Bearer {create_token(owner.id)}"},
    )
    active = await client.get(
        "/admin/forge/active",
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    sessions, total = await _list_forge_sessions(db_session)

    assert public.status_code == 404
    assert active.status_code == 200
    assert active.json() == []
    assert sessions == []
    assert total == 0
    assert await _get_forge_session(db_session, reservation.id) is None


@pytest.mark.anyio
async def test_legacy_suffix_is_reserved_before_llm_and_used_at_completion(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ugc_daily_creation_limit", 5)
    monkeypatch.setattr(settings, "forge_daily_reward_limit", 1)
    user = await _user(db_session, "legacy-suffix@forge.test")
    user.forge_reward_date = datetime.now(UTC).date()
    user.forge_reward_count = 1
    await db_session.commit()
    first = await start_forge(db_session, user.id, "Same Legacy Name")
    base = await db_session.get(ForgeSession, first["forge_id"])
    assert base.target_slug == "same-legacy-name"

    session = await start_quick_forge(
        db_session, user.id, "Same Legacy Name", "source material"
    )
    reserved_slug = session.target_slug
    assert reserved_slug is not None
    assert reserved_slug.startswith("same-legacy-name-")

    response = SimpleNamespace(content=[SimpleNamespace(
        text="# Ability\nA===SPLIT===# Persona\nP===SPLIT===# Soul\nS"
    )])
    llm = MagicMock()
    llm.messages.create = AsyncMock(return_value=response)
    with (
        patch("app.forge.legacy_pipeline.get_client", return_value=llm),
        patch("app.forge.legacy_pipeline.record_usage", new=AsyncMock()),
        patch("app.forge.legacy_pipeline.enforce_forge_budget", new=AsyncMock()),
        patch(
            "app.forge.legacy_pipeline.allocate_resident_location",
            new=AsyncMock(return_value=("free", 1, 1, None)),
        ),
        patch("app.services.sbti_service.compute_sbti", new=AsyncMock(return_value=None)),
        patch("app.forge.legacy_pipeline.notify_forge_progress", new=AsyncMock()),
        patch("app.forge.legacy_pipeline.notify_forge_done", new=AsyncMock()),
    ):
        await run_quick_pipeline(session.id, db_session)

    created = await db_session.scalar(
        select(Resident).where(
            Resident.creator_id == user.id,
            Resident.name == "Same Legacy Name",
        )
    )
    await db_session.refresh(session)
    await db_session.refresh(user)
    assert created is not None
    assert created.slug == reserved_slug
    assert session.status == "done"
    assert session.target_slug is None
    assert session.validation_report["reward_granted"] is False
    assert user.soul_coin_balance == 100
    assert await db_session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.reason == f"forge_creation:{session.id}"
        )
    ) == 0
