"""Nomination and admin review for safe Lab-to-market candidate artifacts."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lab_artifact import LabArtifact
from app.models.lab_task import LabTask
from app.models.market import LabMarketCandidate
from app.services.lab_artifact_service import is_releasable


class CandidateError(Exception):
    pass


def serialize(candidate: LabMarketCandidate) -> dict:
    return {
        "id": candidate.id,
        "artifact_id": candidate.artifact_id,
        "task_id": candidate.task_id,
        "title": candidate.title,
        "summary": candidate.summary,
        "offer_type": candidate.offer_type,
        "suggested_price_sc": int(candidate.suggested_price_sc),
        "status": candidate.status,
        "review_note": candidate.review_note,
        "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
        "updated_at": candidate.updated_at.isoformat() if candidate.updated_at else None,
    }


async def nominate(
    db: AsyncSession,
    *,
    artifact_id: str,
    user_id: str,
    title: str,
    summary: str,
    offer_type: str,
    suggested_price_sc: int,
) -> LabMarketCandidate:
    artifact = await db.get(LabArtifact, artifact_id)
    if artifact is None:
        raise CandidateError("artifact not found")
    task = await db.get(LabTask, artifact.task_id)
    if task is None or task.issuer_user_id != user_id:
        raise CandidateError("artifact not found")
    if task.status != "completed":
        raise CandidateError("only completed task artifacts can be nominated")
    if not is_releasable(artifact):
        raise CandidateError("artifact is not scan-clean and verified")
    existing = (
        await db.execute(
            select(LabMarketCandidate).where(
                LabMarketCandidate.artifact_id == artifact_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    candidate = LabMarketCandidate(
        artifact_id=artifact.id,
        task_id=task.id,
        proposed_by_user_id=user_id,
        title=title.strip() or artifact.title or task.title,
        summary=summary.strip(),
        offer_type=offer_type,
        suggested_price_sc=suggested_price_sc,
        status="pending",
    )
    db.add(candidate)
    await db.commit()
    await db.refresh(candidate)
    return candidate


async def list_for_user(db: AsyncSession, *, user_id: str) -> list[LabMarketCandidate]:
    return list(
        (
            await db.execute(
                select(LabMarketCandidate)
                .where(LabMarketCandidate.proposed_by_user_id == user_id)
                .order_by(LabMarketCandidate.created_at.desc())
            )
        ).scalars()
    )


async def review(
    db: AsyncSession,
    *,
    candidate_id: str,
    reviewer_id: str,
    decision: str,
    note: str,
) -> LabMarketCandidate:
    candidate = (
        await db.execute(
            select(LabMarketCandidate)
            .where(LabMarketCandidate.id == candidate_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise CandidateError("candidate not found")
    if candidate.status != "pending":
        raise CandidateError("candidate is not pending")
    candidate.status = "approved" if decision == "approve" else "rejected"
    candidate.reviewed_by_user_id = reviewer_id
    candidate.review_note = note.strip()
    candidate.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(candidate)
    return candidate
