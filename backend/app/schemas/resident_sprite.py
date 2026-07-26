"""Admin API contracts for the resident sprite publication workflow."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CHECKLIST_KEYS = (
    "identity_consistency",
    "down_direction",
    "left_direction",
    "right_direction",
    "up_direction",
    "walk_animation",
    "transparent_background",
    "limited_palette",
    "phaser_preview",
)


class ResidentSpriteRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resident_id: str
    appearance: str | None = Field(default=None, min_length=1, max_length=1200)
    gender: Literal["male", "female", "neutral"] = "neutral"
    age_group: Literal["young", "adult", "elder"] = "adult"
    vibe: str | None = Field(default=None, min_length=1, max_length=40)
    tags: list[str] | None = Field(default=None, max_length=8)
    direction_policy: Literal["mirror_right", "generate_right"] = "mirror_right"

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(not 1 <= len(value) <= 32 for value in normalized):
            raise ValueError("each tag must contain 1-32 characters")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("tags must be unique after casefolding")
        return normalized


class ResidentSpriteProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["resume", "retry"]
    expected_version: int = Field(ge=1)


class ResidentSpriteReviewRequest(BaseModel):
    expected_version: int = Field(ge=1)
    evidence: dict = Field(default_factory=dict)
    checklist: dict[str, bool]
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("checklist")
    @classmethod
    def exact_checklist(cls, value: dict[str, bool]) -> dict[str, bool]:
        if set(value) != set(CHECKLIST_KEYS):
            raise ValueError(f"checklist must contain exactly: {', '.join(CHECKLIST_KEYS)}")
        return value


class VersionedSpriteAction(BaseModel):
    expected_version: int = Field(ge=1)


class ResidentSpriteRejectRequest(VersionedSpriteAction):
    reason: str = Field(min_length=1, max_length=4000)


class ResidentSpriteRollbackRequest(VersionedSpriteAction):
    reason: str = Field(min_length=1, max_length=4000)


class ResidentSpriteRunResponse(BaseModel):
    id: str
    resident_id: str
    run_id: str
    status: str
    direction_policy: str
    generation_request_json: dict
    retry_of_run_id: str | None
    capability_receipt_id: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempts: int
    manifest_path: str | None
    candidate_texture_path: str | None
    candidate_portrait_path: str | None
    candidate_texture_url: str | None = None
    candidate_portrait_url: str | None = None
    candidate_texture_sha256: str | None
    candidate_portrait_sha256: str | None
    published_texture_sha256: str | None
    published_portrait_sha256: str | None
    request_count: int
    # Conservative request-count-based upper bound; null when pricing is not configured.
    estimated_cost_usd: float | None
    review_evidence_json: dict | None
    review_checklist_json: dict | None
    review_notes: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    rejection_reason: str | None
    published_by: str | None
    published_at: datetime | None
    rolled_back_by: str | None
    rolled_back_at: datetime | None
    rollback_reason: str | None
    error_code: str | None
    error_message: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ResidentSpriteRunListResponse(BaseModel):
    items: list[ResidentSpriteRunResponse]
    total: int
    page: int
    per_page: int
