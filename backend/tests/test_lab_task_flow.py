"""P1 — LabTask state machine end-to-end with the MockAdapter (spec §4.1, §6).

publish → fund(hold) → assign → run(mock) → review → accept → settle
(creator share + treasury + sink), plus cancel/expire refunds, open-recruitment
auto-dispatch, reject-once, and artifact-lock-until-release.

Cross-session note: like test_location_tracker, the runner + service open their
own ``async_session`` — patched here onto the shared in-memory engine. Each step
uses a fresh session so no stale identity-map row leaks across the boundary.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.models.lab_task import LabTask
from app.models.lab_run import LabRun
from app.models.lab_artifact import LabArtifact
from app.services import coin_service
from app.services import lab_task_service as svc
from app.lab.runner import run_one


@pytest.fixture
def lab_env(db_engine, monkeypatch):
    from app.config import settings
    for k, v in {
        "lab_enabled": True, "lab_adapter": "mock", "lab_creator_share": 0.2,
        "lab_platform_fee_rate": 0.1, "lab_default_budget_usd": 0.5,
        "lab_daily_tasks_per_user": 20, "lab_auto_release_hours": 72,
        "lab_task_deadline_hours": 24,
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.lab.runner.async_session", factory), \
         patch("app.services.lab_task_service.async_session", factory), \
         patch("app.services.lab_task_service.emit", new_callable=AsyncMock):
        yield factory


async def _seed(factory, *, issuer_balance=1000, with_researcher=True):
    async with factory() as s:
        s.add(User(id="issuer", name="Issuer", email="i@t.com", soul_coin_balance=issuer_balance))
        s.add(User(id="creator_user", name="Creator", email="c@t.com", soul_coin_balance=0))
        if with_researcher:
            s.add(Resident(
                slug="sage", name="Sage", creator_id="creator_user", resident_type="npc",
                meta_json={"lab": {"access": True, "tier": "senior", "skills": ["web_search"]}},
            ))
        await s.commit()


@pytest.mark.anyio
async def test_happy_path_publish_run_accept_settles(lab_env):
    factory = lab_env
    await _seed(factory)

    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="调研任务", brief="调研一下 X",
            scopes=["web_search"], reward_sc=100, researcher_slug="sage",
        )
        task_id, run_id = task.id, task.accepted_run_id
        assert task.status == "assigned"
        assert task.platform_fee_sc == 10  # ceil(100*0.1)

    # Escrow debited reward+fee.
    async with factory() as s:
        assert await coin_service.get_balance(s, "issuer") == 890

    await run_one(run_id)

    # Run succeeded, task moved to review, steps + artifact landed.
    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "succeeded"
        task = await s.get(LabTask, task_id)
        assert task.status == "review"
        arts = (await s.execute(select(LabArtifact).where(LabArtifact.task_id == task_id))).scalars().all()
        assert len(arts) == 1
        art_id = arts[0].id

    # Accept → settle: creator 20, treasury 80, fee 10 sink.
    async with factory() as s:
        task = await svc.accept_result(s, task_id, "issuer")
        assert task.status == "completed"
    async with factory() as s:
        assert await coin_service.get_balance(s, "creator_user") == 20
        assert await coin_service.treasury_balance(s, "sage") == 80
        # Artifact now unlocked (task completed).
        art = await s.get(LabArtifact, art_id)
        task = await s.get(LabTask, task_id)
        view = svc.serialize_artifact(art, task.status == "completed")
        assert view["unlocked"] is True and view["text_md"]


@pytest.mark.anyio
async def test_cancel_before_run_refunds(lab_env):
    factory = lab_env
    await _seed(factory)
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="取消我", brief="...",
            scopes=["web_search"], reward_sc=100, researcher_slug="sage",
        )
        tid = task.id
    async with factory() as s:
        task = await svc.cancel_task(s, tid, "issuer")
        assert task.status == "cancelled"
    async with factory() as s:
        assert await coin_service.get_balance(s, "issuer") == 1000  # fully refunded


@pytest.mark.anyio
async def test_open_recruitment_auto_dispatch(lab_env):
    factory = lab_env
    await _seed(factory)
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="公开招募", brief="...",
            scopes=["web_search"], reward_sc=50, researcher_slug=None,
        )
        # Auto-dispatched to the only idle researcher.
        assert task.researcher_slug == "sage"
        assert task.status == "assigned"
        assert task.accepted_run_id is not None


@pytest.mark.anyio
async def test_reject_once_then_blocks(lab_env):
    factory = lab_env
    await _seed(factory)
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="拒收测试", brief="...",
            scopes=["web_search"], reward_sc=30, researcher_slug="sage",
        )
        tid, rid = task.id, task.accepted_run_id
    await run_one(rid)
    async with factory() as s:
        task = await svc.reject_result(s, tid, "issuer")
        assert task.status == "rejected" and task.reject_count == 1
    async with factory() as s:
        with pytest.raises(svc.LabTaskError):
            await svc.reject_result(s, tid, "issuer")  # only once


@pytest.mark.anyio
async def test_expire_refunds_unstarted(lab_env):
    factory = lab_env
    # No researcher → open task stays funded/unassigned, then expires.
    await _seed(factory, with_researcher=False)
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="会过期", brief="...",
            scopes=["web_search"], reward_sc=40, researcher_slug=None,
        )
        tid = task.id
        assert task.status == "funded"  # nobody to assign
    # Force the deadline into the past, then run the expiry sweep.
    from datetime import datetime, UTC
    async with factory() as s:
        task = await s.get(LabTask, tid)
        task.deadline_at = datetime(2020, 1, 1, tzinfo=UTC)
        await s.commit()
    async with factory() as s:
        n = await svc.expire_lab_tasks(s)
        assert n >= 1
    async with factory() as s:
        task = await s.get(LabTask, tid)
        assert task.status == "expired"
        assert await coin_service.get_balance(s, "issuer") == 1000  # refunded


@pytest.mark.anyio
async def test_insufficient_balance_rejected(lab_env):
    factory = lab_env
    await _seed(factory, issuer_balance=5)
    async with factory() as s:
        with pytest.raises(svc.LabTaskError):
            await svc.create_task(
                s, issuer_id="issuer", title="穷", brief="...",
                scopes=["web_search"], reward_sc=100, researcher_slug="sage",
            )
    async with factory() as s:
        assert await coin_service.get_balance(s, "issuer") == 5  # untouched
