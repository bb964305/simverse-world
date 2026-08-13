"""Dry-run/apply/rollback coverage for preset arc reconciliation."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.goal_investment import GoalInvestment
from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal
from scripts import reconcile_preset_arcs as reconcile
from seed.preset_characters import PRESET_ARCS, preset_arc_template_key


pytestmark = pytest.mark.anyio


def _sessions(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _seed_arc_replays(db_session):
    resident = Resident(
        id="zhou",
        slug="zhou-dahe",
        name="周大河",
        district="central_plaza",
        status="idle",
        resident_type="npc",
        creator_id="system",
    )
    title = PRESET_ARCS["zhou-dahe"]["title"]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    canonical = ResidentGoal(
        id="arc-canonical",
        resident_id=resident.id,
        kind="arc",
        title=title,
        template_key=preset_arc_template_key("zhou-dahe"),
        status="achieved",
        created_at=start,
        resolved_at=start + timedelta(days=1),
    )
    duplicate_active = ResidentGoal(
        id="arc-dup-active",
        resident_id=resident.id,
        kind="arc",
        title=title,
        status="active",
        created_at=start + timedelta(days=2),
    )
    duplicate_abandoned = ResidentGoal(
        id="arc-dup-abandoned",
        resident_id=resident.id,
        kind="arc",
        title=title,
        status="abandoned",
        created_at=start + timedelta(days=3),
        resolved_at=start + timedelta(days=4),
    )
    db_session.add_all([resident, canonical, duplicate_active, duplicate_abandoned])
    await db_session.commit()
    return canonical, duplicate_active, duplicate_abandoned


async def test_dry_run_reports_duplicates_and_writes_manifest(
    db_session, monkeypatch, tmp_path,
):
    canonical, duplicate_active, duplicate_abandoned = await _seed_arc_replays(db_session)
    monkeypatch.setattr(reconcile, "async_session", _sessions(db_session))
    manifest = tmp_path / "preset-arcs.json"

    report = await reconcile.main(manifest_path=manifest)

    assert report["mode"] == "dry_run"
    assert report["duplicate_count"] == 2
    assert report["goal_investment_dependency_count"] == 0
    assert report["unsafe_group_count"] == 0
    assert report["manifest_path"] == str(manifest)
    assert manifest.exists()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["duplicate_count"] == 2
    group = payload["groups"][0]
    assert group["canonical"]["id"] == canonical.id
    assert group["canonical"]["status"] == "achieved"
    assert [item["id"] for item in group["duplicates"]] == [
        duplicate_active.id,
        duplicate_abandoned.id,
    ]
    assert [item["status"] for item in group["duplicates"]] == ["active", "abandoned"]
    assert group["canonical"]["resolved_at"] is not None
    assert group["duplicates"][1]["resolved_at"] is not None

    rows = dict((await db_session.execute(
        select(ResidentGoal.id, ResidentGoal.status)
    )).all())
    assert rows == {
        canonical.id: "achieved",
        duplicate_active.id: "active",
        duplicate_abandoned.id: "abandoned",
    }


async def test_apply_refuses_changed_duplicate_count_before_writes(db_session, monkeypatch):
    _, duplicate_active, _ = await _seed_arc_replays(db_session)
    monkeypatch.setattr(reconcile, "async_session", _sessions(db_session))

    with pytest.raises(RuntimeError, match="expected 1 duplicate\\(s\\), found 2"):
        await reconcile.main(apply=True, expect_duplicates=1)

    assert (await db_session.get(ResidentGoal, duplicate_active.id)).status == "active"


async def test_apply_refuses_goal_investment_dependencies_before_writes(
    db_session, monkeypatch,
):
    _, duplicate_active, _ = await _seed_arc_replays(db_session)
    db_session.add(GoalInvestment(goal_id=duplicate_active.id, user_id="u1", amount=5))
    await db_session.commit()
    monkeypatch.setattr(reconcile, "async_session", _sessions(db_session))

    with pytest.raises(RuntimeError, match="goal_investments dependencies"):
        await reconcile.main(apply=True, expect_duplicates=2)

    assert (await db_session.get(ResidentGoal, duplicate_active.id)).status == "active"


async def test_apply_marks_duplicate_arcs_superseded_and_persists_manifest(
    db_session, monkeypatch, tmp_path,
):
    canonical, duplicate_active, duplicate_abandoned = await _seed_arc_replays(db_session)
    monkeypatch.setattr(reconcile, "async_session", _sessions(db_session))
    manifest = tmp_path / "preset-arcs-apply.json"

    report = await reconcile.main(
        apply=True,
        expect_duplicates=2,
        manifest_path=manifest,
    )

    assert report["mode"] == "apply"
    assert report["duplicate_count"] == 2
    assert report["applied_duplicate_ids"] == [
        duplicate_abandoned.id,
        duplicate_active.id,
    ]
    assert report["manifest_path"] == str(manifest)
    rows = dict((await db_session.execute(
        select(ResidentGoal.id, ResidentGoal.status)
    )).all())
    assert rows == {
        canonical.id: "achieved",
        duplicate_active.id: "superseded",
        duplicate_abandoned.id: "superseded",
    }

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["mode"] == "apply"
    assert [item["status"] for item in payload["groups"][0]["duplicates"]] == [
        "active",
        "abandoned",
    ]


async def test_apply_manifest_failure_happens_before_database_writes(
    db_session, monkeypatch, tmp_path,
):
    _, duplicate_active, duplicate_abandoned = await _seed_arc_replays(db_session)
    monkeypatch.setattr(reconcile, "async_session", _sessions(db_session))

    def fail_manifest(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(reconcile, "_write_manifest", fail_manifest)
    with pytest.raises(OSError, match="disk full"):
        await reconcile.main(
            apply=True,
            expect_duplicates=2,
            manifest_path=tmp_path / "unwritable.json",
        )

    assert (await db_session.get(ResidentGoal, duplicate_active.id)).status == "active"
    assert (
        await db_session.get(ResidentGoal, duplicate_abandoned.id)
    ).status == "abandoned"


async def test_rollback_manifest_restores_previous_statuses(
    db_session, monkeypatch, tmp_path,
):
    canonical, duplicate_active, duplicate_abandoned = await _seed_arc_replays(db_session)
    monkeypatch.setattr(reconcile, "async_session", _sessions(db_session))
    manifest = tmp_path / "preset-arcs-rollback.json"

    await reconcile.main(
        apply=True,
        expect_duplicates=2,
        manifest_path=manifest,
    )
    result = await reconcile.main(rollback_manifest=manifest)

    assert result["mode"] == "rollback"
    assert result["restored_count"] == 2
    rows = dict((await db_session.execute(
        select(ResidentGoal.id, ResidentGoal.status)
    )).all())
    assert rows == {
        canonical.id: "achieved",
        duplicate_active.id: "active",
        duplicate_abandoned.id: "abandoned",
    }
