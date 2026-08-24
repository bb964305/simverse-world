"""P3 visitor/beta readiness and safe Lab-to-market promotion tests."""

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.lab_artifact import LabArtifact
from app.models.lab_task import LabTask
from app.models.market import LabMarketCandidate
from app.models.user import User
from app.services import lab_market_candidate_service, lab_readiness_service
from app.services.lab_market_candidate_service import CandidateError

pytestmark = pytest.mark.anyio


async def test_lab_visitor_stays_open_while_beta_blocks_publish(monkeypatch):
    monkeypatch.setattr(settings, "lab_enabled", True)
    monkeypatch.setattr(settings, "lab_adapter", "mock")
    monkeypatch.setattr(settings, "lab_beta_user_ids", ["allowed-user"])

    visitor = await lab_readiness_service.snapshot(user_id="visitor-user")
    assert visitor["visitor_open"] is True
    assert visitor["publish_allowed"] is False
    assert visitor["blockers"] == ["beta_access_required"]

    admitted = await lab_readiness_service.snapshot(user_id="allowed-user")
    assert admitted["visitor_open"] is True
    assert admitted["publish_allowed"] is True
    assert admitted["beta_mode"] is True


async def test_only_clean_verified_completed_artifact_can_be_nominated(
    db_session,
):
    owner = User(
        id="lab-market-owner",
        name="owner",
        email="lab-market-owner@test.local",
    )
    reviewer = User(
        id="lab-market-admin",
        name="admin",
        email="lab-market-admin@test.local",
        is_admin=True,
    )
    task = LabTask(
        id="lab-market-task",
        issuer_user_id=owner.id,
        title="路线调研",
        brief_md="调研商路",
        scopes_json=["web_search"],
        status="completed",
    )
    clean = LabArtifact(
        id="lab-market-clean",
        run_id="lab-market-run",
        task_id=task.id,
        kind="text",
        title="商路报告",
        text_md="报告正文",
        scan_status="clean",
        verification_status="verified",
        storage_status="legacy",
    )
    unsafe = LabArtifact(
        id="lab-market-unsafe",
        run_id="lab-market-run",
        task_id=task.id,
        kind="text",
        title="未扫描报告",
        text_md="报告正文",
        scan_status="pending",
        verification_status="verified",
        storage_status="legacy",
    )
    db_session.add_all([owner, reviewer, task, clean, unsafe])
    await db_session.commit()

    with pytest.raises(CandidateError, match="scan-clean"):
        await lab_market_candidate_service.nominate(
            db_session,
            artifact_id=unsafe.id,
            user_id=owner.id,
            title="",
            summary="",
            offer_type="service",
            suggested_price_sc=5,
        )

    candidate = await lab_market_candidate_service.nominate(
        db_session,
        artifact_id=clean.id,
        user_id=owner.id,
        title="商路咨询",
        summary="把已验证调研包装为人工审核服务",
        offer_type="service",
        suggested_price_sc=5,
    )
    duplicate = await lab_market_candidate_service.nominate(
        db_session,
        artifact_id=clean.id,
        user_id=owner.id,
        title="不会覆盖",
        summary="",
        offer_type="good",
        suggested_price_sc=99,
    )
    assert duplicate.id == candidate.id
    assert duplicate.title == "商路咨询"

    approved = await lab_market_candidate_service.review(
        db_session,
        candidate_id=candidate.id,
        reviewer_id=reviewer.id,
        decision="approve",
        note="仅作为人工服务说明，不执行产物代码",
    )
    assert approved.status == "approved"
    assert approved.reviewed_by_user_id == reviewer.id
    rows = (
        await db_session.execute(select(LabMarketCandidate))
    ).scalars().all()
    assert len(rows) == 1
