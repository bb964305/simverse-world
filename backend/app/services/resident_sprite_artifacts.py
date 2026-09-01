"""Crash-safe artifact storage for resident sprite generation runs."""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal

from pydantic import AwareDatetime, Field, StrictInt, field_validator, model_validator

from app.services.resident_sprite_generation import (
    ResidentSpriteContractError,
    ResidentSpriteRequest,
    RequestBudget,
    RunState,
    SanitizedError,
    StrictContractModel,
    canonical_json_bytes,
    validate_non_symlink_path,
    validate_run_id,
)
from app.services.file_lock import exclusive_file_lock


MANIFEST_NAME = "manifest.json"
_LOCK_NAME = ".manifest.lock"
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_PROVIDER_STAGES = frozenset({"anchor", "down", "left", "right", "up"})
_TERMINAL_STATES = frozenset({"published", "rolled_back"})
_INTERNAL_STAGE_TARGETS: dict[str, RunState] = {
    "strips": "strips_ready",
    "postprocess": "processed",
    "auto_qc": "auto_qc_passed",
    "candidate": "candidate_ready",
    "phaser_review": "phaser_reviewed",
    "human_approval": "human_approved",
    "publish_start": "publishing",
    "publish": "published",
    "rollback": "rolled_back",
    "failure": "failed",
    "interruption": "interrupted",
    "quarantine": "quarantined",
}
_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    "requested": frozenset({"failed", "interrupted", "quarantined"}),
    "anchor_ready": frozenset({"strips_ready", "failed", "interrupted", "quarantined"}),
    "strips_ready": frozenset({"processed", "failed", "interrupted", "quarantined"}),
    "processed": frozenset({"auto_qc_passed", "failed", "interrupted", "quarantined"}),
    "auto_qc_passed": frozenset({"candidate_ready", "failed", "interrupted", "quarantined"}),
    "candidate_ready": frozenset({"phaser_reviewed", "failed", "interrupted", "quarantined"}),
    "phaser_reviewed": frozenset({"human_approved", "failed", "interrupted", "quarantined"}),
    "human_approved": frozenset({"publishing", "failed", "interrupted"}),
    "publishing": frozenset({"published", "rolled_back", "failed", "interrupted"}),
    "retrying": frozenset({"failed", "interrupted"}),
    "failed": frozenset(),
    "interrupted": frozenset(),
    "quarantined": frozenset(),
    "published": frozenset(),
    "rolled_back": frozenset(),
}

ProviderStage = Literal["anchor", "down", "left", "right", "up"]


class ProviderStageClaim(StrictContractModel):
    stage: ProviderStage
    attempt_id: str
    owner: str = Field(min_length=1, max_length=100)
    expected_artifact_path: str = Field(min_length=1, max_length=500)
    claimed_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("attempt_id")
    @classmethod
    def valid_attempt_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("owner")
    @classmethod
    def valid_owner(cls, value: str) -> str:
        if value != value.strip() or _CONTROL.search(value):
            raise ValueError("claim owner must be trimmed printable text")
        return value

    @field_validator("expected_artifact_path")
    @classmethod
    def valid_expected_artifact_path(cls, value: str) -> str:
        return validate_artifact_relative_path(value)

    @field_validator("claimed_at", "expires_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("claim timestamps must use UTC")
        return value

    @model_validator(mode="after")
    def positive_lease(self) -> "ProviderStageClaim":
        if self.expires_at <= self.claimed_at:
            raise ValueError("claim expiry must follow claim time")
        return self


class SpriteArtifact(StrictContractModel):
    relative_path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=_SHA256.pattern)
    size: StrictInt = Field(ge=0, le=_MAX_ARTIFACT_BYTES)

    @field_validator("relative_path")
    @classmethod
    def valid_relative_path(cls, value: str) -> str:
        return validate_artifact_relative_path(value)


class SpriteRunManifest(StrictContractModel):
    schema_version: Literal[1] = 1
    run_id: str
    request: ResidentSpriteRequest
    request_budget: RequestBudget
    state: RunState = "requested"
    completed_stages: list[str] = Field(default_factory=list, max_length=32)
    artifacts: list[SpriteArtifact] = Field(default_factory=list, max_length=128)
    provider_request_ids: list[str] = Field(default_factory=list, max_length=64)
    error: SanitizedError | None = None
    active_claim: ProviderStageClaim | None = None
    completed_claims: list[ProviderStageClaim] = Field(default_factory=list, max_length=5)

    @field_validator("run_id")
    @classmethod
    def valid_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("completed_stages")
    @classmethod
    def valid_stages(cls, values: list[str]) -> list[str]:
        if any(not _STAGE.fullmatch(value) for value in values):
            raise ValueError("completed stages must be lowercase stage identifiers")
        if len(values) != len(set(values)):
            raise ValueError("completed stages must be unique")
        return values

    @field_validator("provider_request_ids")
    @classmethod
    def valid_provider_request_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 200 or _CONTROL.search(value) for value in values):
            raise ValueError("provider request IDs must contain 1-200 printable characters")
        if len(values) != len(set(values)):
            raise ValueError("provider request IDs must be unique")
        return values

    @model_validator(mode="after")
    def unique_artifact_paths(self) -> "SpriteRunManifest":
        paths = [artifact.relative_path for artifact in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact relative paths must be unique")
        if self.request_budget.direction_policy != self.request.direction_policy:
            raise ValueError("request budget direction policy must match the request")
        completed_attempts = [claim.attempt_id for claim in self.completed_claims]
        completed_claim_stages = [claim.stage for claim in self.completed_claims]
        if len(completed_attempts) != len(set(completed_attempts)):
            raise ValueError("completed claim attempt IDs must be unique")
        if len(completed_claim_stages) != len(set(completed_claim_stages)):
            raise ValueError("completed provider stages must be unique")
        if self.active_claim is not None:
            mapped_stage = _completed_provider_stage(self.active_claim.stage)
            if mapped_stage in self.completed_stages:
                raise ValueError("active claim stage is already completed")
            if self.state in _TERMINAL_STATES:
                raise ValueError("terminal runs cannot retain an active claim")
        for claim in self.completed_claims:
            if _completed_provider_stage(claim.stage) not in self.completed_stages:
                raise ValueError("completed claim must map to a completed stage")
        return self


ExpiredClaimRecoveryAction = Literal[
    "expired_claim_released",
    "orphan_reconciliation_required",
    "external_request_status_uncertain",
]


class ExpiredClaimRecoveryOutcome(StrictContractModel):
    action: ExpiredClaimRecoveryAction
    manifest: SpriteRunManifest
    stage: ProviderStage
    expected_artifact_path: str
    stage_request_count: StrictInt = Field(ge=0)


def validate_artifact_relative_path(value: str) -> str:
    """Return a canonical POSIX artifact path or reject boundary ambiguity."""
    if not value or "\\" in value or _CONTROL.search(value):
        raise ValueError("artifact path must be a nonempty printable POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] in {MANIFEST_NAME, _LOCK_NAME}
    ):
        raise ValueError("artifact path must remain inside the run directory")
    return value


def create_run(
    artifact_root: Path,
    run_id: str,
    request: ResidentSpriteRequest,
) -> SpriteRunManifest:
    """Create the initial manifest, or return an identical existing run."""
    run_dir = _run_directory(artifact_root, run_id, create=True)
    with _run_lock(run_dir):
        manifest_path = run_dir / MANIFEST_NAME
        if manifest_path.exists():
            manifest, _ = _load_manifest(run_dir, run_id, verify_artifacts=True)
            if manifest.request != request:
                raise ResidentSpriteContractError(
                    "RUN_REQUEST_CONFLICT", "run_id already belongs to a different request"
                )
            return manifest

        unexpected = [
            path.name for path in run_dir.iterdir() if path.name != _LOCK_NAME
        ]
        if unexpected:
            raise ResidentSpriteContractError(
                "RUN_DIRECTORY_NOT_EMPTY", "run directory contains untracked files"
            )
        manifest = SpriteRunManifest(
            run_id=run_id,
            request=request,
            request_budget=RequestBudget(direction_policy=request.direction_policy),
        )
        _atomic_create_bytes(manifest_path, canonical_json_bytes(manifest))
        return manifest


def load_run(artifact_root: Path, run_id: str) -> SpriteRunManifest:
    """Load a resumable run and verify every declared artifact."""
    run_dir = _run_directory(artifact_root, run_id, create=False)
    manifest, _ = _load_manifest(run_dir, run_id, verify_artifacts=True)
    return manifest


def claim_stage(
    artifact_root: Path,
    run_id: str,
    stage: ProviderStage,
    owner: str,
    now: datetime,
    ttl: timedelta,
    *,
    attempt_id: str | None = None,
    expected_artifact_path: str | None = None,
) -> ProviderStageClaim:
    """Atomically acquire a provider-stage lease before doing paid work."""
    _validate_provider_stage(stage)
    _validate_utc_now(now)
    _validate_claim_ttl(ttl)
    expected_path = validate_artifact_relative_path(
        expected_artifact_path or _default_provider_artifact_path(stage)
    )
    if attempt_id is not None:
        validate_run_id(attempt_id)

    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        _ensure_mutable_state(manifest)
        _validate_provider_stage_precondition(manifest, stage)
        active = manifest.active_claim
        if active is not None and now < active.expires_at:
            if (
                active.stage == stage
                and active.owner == owner
                and active.expected_artifact_path == expected_path
                and attempt_id == active.attempt_id
            ):
                return active
            raise ResidentSpriteContractError(
                "STAGE_CLAIM_HELD", "provider stage is held by an unexpired claim"
            )

        if active is not None and attempt_id == active.attempt_id:
            raise ResidentSpriteContractError(
                "CLAIM_ATTEMPT_REUSED", "expired claim requires a new attempt ID"
            )
        if (
            active is not None
            and manifest.request_budget.stage_counts.get(active.stage, 0) > 0
            and not any(
                artifact.relative_path == active.expected_artifact_path
                for artifact in manifest.artifacts
            )
        ):
            raise ResidentSpriteContractError(
                "EXTERNAL_REQUEST_STATUS_UNCERTAIN",
                "expired paid claim requires explicit cost acknowledgement",
            )
        _reject_unregistered_provider_artifact(run_dir, manifest, expected_path)
        claim = ProviderStageClaim(
            stage=stage,
            attempt_id=attempt_id or uuid.uuid4().hex,
            owner=owner,
            expected_artifact_path=expected_path,
            claimed_at=now,
            expires_at=now + ttl,
        )
        updated = _validated_manifest_copy(manifest, active_claim=claim)
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return claim


def release_stage_claim(
    artifact_root: Path,
    run_id: str,
    *,
    stage: ProviderStage,
    owner: str,
    attempt_id: str,
) -> SpriteRunManifest:
    """Release a lease only when its complete ownership tuple matches."""
    _validate_provider_stage(stage)
    validate_run_id(attempt_id)
    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        _require_matching_claim(manifest, stage, owner, attempt_id)
        updated = _validated_manifest_copy(manifest, active_claim=None)
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return updated


def release_expired_claim_if_safe(
    artifact_root: Path,
    run_id: str,
    *,
    stage: ProviderStage,
    owner: str,
    attempt_id: str,
    now: datetime,
) -> ExpiredClaimRecoveryOutcome:
    """Release an expired provider claim only when retrying cannot duplicate work."""
    _validate_provider_stage(stage)
    validate_run_id(attempt_id)
    _validate_utc_now(now)
    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        claim = _require_matching_claim(manifest, stage, owner, attempt_id)
        if now < claim.expires_at:
            raise ResidentSpriteContractError(
                "STAGE_CLAIM_ACTIVE", "provider stage claim has not expired"
            )

        expected_path = _artifact_path(run_dir, claim.expected_artifact_path)
        artifact_declared = any(
            artifact.relative_path == claim.expected_artifact_path
            for artifact in manifest.artifacts
        )
        unsafe_path = False
        try:
            validate_non_symlink_path(expected_path.parent)
            output_exists = _path_exists_without_symlink(expected_path)
        except ResidentSpriteContractError:
            unsafe_path = True
            output_exists = True
        stage_request_count = manifest.request_budget.stage_counts.get(stage, 0)

        if not artifact_declared and (unsafe_path or output_exists):
            return ExpiredClaimRecoveryOutcome(
                action="orphan_reconciliation_required",
                manifest=manifest,
                stage=stage,
                expected_artifact_path=claim.expected_artifact_path,
                stage_request_count=stage_request_count,
            )
        if not artifact_declared and stage_request_count > 0:
            return ExpiredClaimRecoveryOutcome(
                action="external_request_status_uncertain",
                manifest=manifest,
                stage=stage,
                expected_artifact_path=claim.expected_artifact_path,
                stage_request_count=stage_request_count,
            )

        updated = _validated_manifest_copy(manifest, active_claim=None)
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return ExpiredClaimRecoveryOutcome(
            action="expired_claim_released",
            manifest=updated,
            stage=stage,
            expected_artifact_path=claim.expected_artifact_path,
            stage_request_count=stage_request_count,
        )


def acknowledge_uncertain_request_cost(
    artifact_root: Path,
    run_id: str,
    *,
    stage: ProviderStage,
    owner: str,
    attempt_id: str,
    expected_stage_request_count: int,
    reviewer: str,
    now: datetime,
) -> SpriteRunManifest:
    """Record accepted uncertain spend before allowing a replacement request."""
    _validate_provider_stage(stage)
    validate_run_id(attempt_id)
    _validate_utc_now(now)
    if (
        reviewer != reviewer.strip()
        or not reviewer
        or len(reviewer) > 80
        or _CONTROL.search(reviewer)
    ):
        raise ResidentSpriteContractError(
            "RECOVERY_REVIEWER_INVALID", "recovery reviewer must be canonical text"
        )
    if (
        not isinstance(expected_stage_request_count, int)
        or isinstance(expected_stage_request_count, bool)
        or expected_stage_request_count <= 0
    ):
        raise ResidentSpriteContractError(
            "RECOVERY_REQUEST_COUNT_INVALID", "recovery request count must be positive"
        )

    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        claim = _require_matching_claim(manifest, stage, owner, attempt_id)
        if now < claim.expires_at:
            raise ResidentSpriteContractError(
                "STAGE_CLAIM_ACTIVE", "provider stage claim has not expired"
            )
        stage_request_count = manifest.request_budget.stage_counts.get(stage, 0)
        if stage_request_count != expected_stage_request_count:
            raise ResidentSpriteContractError(
                "RECOVERY_REQUEST_COUNT_MISMATCH",
                "confirmed request count does not match the manifest",
            )
        if any(
            artifact.relative_path == claim.expected_artifact_path
            for artifact in manifest.artifacts
        ):
            raise ResidentSpriteContractError(
                "RECOVERY_ARTIFACT_EXISTS", "provider artifact is already registered"
            )
        expected_path = _artifact_path(run_dir, claim.expected_artifact_path)
        if _path_exists_without_symlink(expected_path):
            raise ResidentSpriteContractError(
                "RECOVERY_ORPHAN_EXISTS", "unregistered provider artifact requires reconciliation"
            )

        relative_path = f"recovery/uncertain-{attempt_id}.json"
        target = _artifact_path(run_dir, relative_path)
        _ensure_parent_directories(run_dir, target.parent)
        payload = canonical_json_bytes(
            {
                "schema_version": 1,
                "action": "uncertain_cost_accepted_for_retry",
                "run_id": run_id,
                "stage": stage,
                "attempt_id": attempt_id,
                "stage_request_count": stage_request_count,
                "reviewer": reviewer,
                "acknowledged_at": now,
            }
        )
        _atomic_create_bytes(target, payload)
        record = SpriteArtifact(
            relative_path=relative_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )
        updated = _validated_manifest_copy(
            manifest,
            active_claim=None,
            artifacts=[*manifest.artifacts, record],
        )
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return updated


def complete_stage(
    artifact_root: Path,
    run_id: str,
    *,
    stage: ProviderStage,
    owner: str,
    attempt_id: str,
    now: datetime,
    provider_request_ids: tuple[str, ...] = (),
) -> SpriteRunManifest:
    """Complete one leased provider stage after its artifact is durable."""
    _validate_provider_stage(stage)
    validate_run_id(attempt_id)
    _validate_utc_now(now)
    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        completed_stage = _completed_provider_stage(stage)
        if manifest.active_claim is None and completed_stage in manifest.completed_stages:
            completed_claim = next(
                (claim for claim in manifest.completed_claims if claim.stage == stage),
                None,
            )
            if (
                completed_claim is not None
                and completed_claim.owner == owner
                and completed_claim.attempt_id == attempt_id
            ):
                return manifest
            raise ResidentSpriteContractError(
                "STAGE_ALREADY_COMPLETED", "provider stage was completed by another claim"
            )

        claim = _require_matching_claim(manifest, stage, owner, attempt_id)
        if now >= claim.expires_at:
            raise ResidentSpriteContractError("STAGE_CLAIM_EXPIRED", "provider stage claim expired")
        if not any(
            artifact.relative_path == claim.expected_artifact_path
            for artifact in manifest.artifacts
        ):
            raise ResidentSpriteContractError(
                "ARTIFACT_NOT_READY", "expected provider artifact is not in the manifest"
            )
        new_request_ids = _merged_provider_request_ids(manifest, provider_request_ids)
        target_state: RunState = "anchor_ready"
        updated = _validated_manifest_copy(
            manifest,
            state=target_state,
            completed_stages=[*manifest.completed_stages, completed_stage],
            provider_request_ids=new_request_ids,
            error=None,
            active_claim=None,
            completed_claims=[*manifest.completed_claims, claim],
        )
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return updated


def fail_stage_claim(
    artifact_root: Path,
    run_id: str,
    *,
    stage: ProviderStage,
    owner: str,
    attempt_id: str,
    error: SanitizedError,
    provider_request_ids: tuple[str, ...] = (),
) -> SpriteRunManifest:
    """Atomically record a provider failure and relinquish its stage claim."""
    _validate_provider_stage(stage)
    validate_run_id(attempt_id)
    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        _ensure_mutable_state(manifest)
        _require_matching_claim(manifest, stage, owner, attempt_id)
        request_ids = provider_request_ids
        if (
            error.provider_request_id is not None
            and error.provider_request_id not in request_ids
        ):
            request_ids = (*request_ids, error.provider_request_id)
        updated = _validated_manifest_copy(
            manifest,
            state="failed",
            provider_request_ids=_merged_provider_request_ids(manifest, request_ids),
            error=error,
            active_claim=None,
        )
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return updated


def consume_request_budget(
    artifact_root: Path,
    run_id: str,
    stage: str,
) -> RequestBudget:
    """Durably reserve one provider request before its external POST begins."""
    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        _ensure_mutable_state(manifest)
        if manifest.active_claim is None or manifest.active_claim.stage != stage:
            raise ResidentSpriteContractError(
                "STAGE_CLAIM_REQUIRED", "request budget requires an active stage claim"
            )
        budget = RequestBudget.model_validate(
            manifest.request_budget.model_dump(mode="python")
        )
        budget.consume_before_post(stage)
        updated = manifest.model_copy(update={"request_budget": budget}, deep=True)
        updated = SpriteRunManifest.model_validate(updated.model_dump(mode="python"))
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return RequestBudget.model_validate(budget.model_dump(mode="python"))


def write_artifact(
    artifact_root: Path,
    run_id: str,
    relative_path: str,
    data: bytes,
) -> SpriteArtifact:
    """Persist immutable bytes and register them in the run manifest."""
    relative_path = validate_artifact_relative_path(relative_path)
    if not isinstance(data, bytes):
        raise ResidentSpriteContractError("ARTIFACT_INVALID", "artifact data must be bytes")
    if len(data) > _MAX_ARTIFACT_BYTES:
        raise ResidentSpriteContractError("ARTIFACT_TOO_LARGE", "artifact exceeds size limit")

    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        _ensure_mutable_state(manifest)
        target = _artifact_path(run_dir, relative_path)
        _ensure_parent_directories(run_dir, target.parent)
        _atomic_create_bytes(target, data)

        record = SpriteArtifact(
            relative_path=relative_path,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
        existing = next(
            (
                artifact
                for artifact in manifest.artifacts
                if artifact.relative_path == relative_path
            ),
            None,
        )
        if existing is not None:
            if existing != record:
                raise ResidentSpriteContractError(
                    "ARTIFACT_CONFLICT", "artifact metadata conflicts with the manifest"
                )
            return existing

        updated = manifest.model_copy(
            update={"artifacts": [*manifest.artifacts, record]}, deep=True
        )
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return record


def read_artifact(
    artifact_root: Path,
    run_id: str,
    relative_path: str,
) -> bytes:
    """Read a manifest-declared artifact after a fresh integrity check."""
    relative_path = validate_artifact_relative_path(relative_path)
    run_dir = _run_directory(artifact_root, run_id, create=False)
    manifest, _ = _load_manifest(run_dir, run_id, verify_artifacts=True)
    artifact = next(
        (
            candidate
            for candidate in manifest.artifacts
            if candidate.relative_path == relative_path
        ),
        None,
    )
    if artifact is None:
        raise ResidentSpriteContractError(
            "ARTIFACT_NOT_DECLARED", "artifact is not declared by the run manifest"
        )
    target = _artifact_path(run_dir, relative_path)
    data = _read_regular_file(target, max_bytes=_MAX_ARTIFACT_BYTES)
    if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ResidentSpriteContractError(
            "ARTIFACT_CORRUPT", f"artifact integrity check failed: {relative_path}"
        )
    return data


def write_canonical_json_artifact(
    artifact_root: Path,
    run_id: str,
    relative_path: str,
    value: Any,
) -> SpriteArtifact:
    """Canonicalize a JSON value before using the immutable artifact writer."""
    return write_artifact(
        artifact_root,
        run_id,
        relative_path,
        canonical_json_bytes(value),
    )


def advance_stage(
    artifact_root: Path,
    run_id: str,
    *,
    stage: str,
    state: RunState,
    provider_request_ids: tuple[str, ...] = (),
    error: SanitizedError | None = None,
) -> SpriteRunManifest:
    """Checkpoint one non-provider stage under the strict state graph."""
    if not isinstance(stage, str) or not _STAGE.fullmatch(stage):
        raise ResidentSpriteContractError("STAGE_INVALID", "stage identifier is invalid")
    if stage in _PROVIDER_STAGES or stage.startswith("strip_"):
        raise ResidentSpriteContractError(
            "STAGE_CLAIM_REQUIRED", "provider stages must use claim_stage and complete_stage"
        )
    if _INTERNAL_STAGE_TARGETS.get(stage) != state:
        raise ResidentSpriteContractError(
            "STATE_TRANSITION_INVALID", "stage does not map to the requested state"
        )
    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        new_request_ids = _merged_provider_request_ids(manifest, provider_request_ids)

        if stage in manifest.completed_stages:
            if (
                manifest.state == state
                and new_request_ids == manifest.provider_request_ids
                and manifest.error == error
            ):
                return manifest
            raise ResidentSpriteContractError(
                "STAGE_CONFLICT", "completed stage cannot be rewritten"
            )

        _ensure_mutable_state(manifest)
        if manifest.active_claim is not None:
            raise ResidentSpriteContractError(
                "STAGE_CLAIM_HELD", "internal transition cannot bypass an active claim"
            )
        _validate_state_transition(manifest.state, state)
        if state == "strips_ready":
            _require_all_direction_strips(manifest)
        if state == "failed" and error is None:
            raise ResidentSpriteContractError(
                "STATE_TRANSITION_INVALID", "failed transition requires a sanitized error"
            )

        updated = _validated_manifest_copy(
            manifest,
            state=state,
            completed_stages=[*manifest.completed_stages, stage],
            provider_request_ids=new_request_ids,
            error=error,
        )
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return updated


def retry_run(artifact_root: Path, run_id: str) -> SpriteRunManifest:
    """Explicitly move a failed or interrupted provider run into retrying."""
    return _explicit_recovery_transition(
        artifact_root, run_id, allowed={"failed", "interrupted"}, target="retrying"
    )


def review_quarantined_run(artifact_root: Path, run_id: str) -> SpriteRunManifest:
    """Explicitly return a quarantined candidate to post-processing review."""
    return _explicit_recovery_transition(
        artifact_root, run_id, allowed={"quarantined"}, target="processed"
    )


def _completed_provider_stage(stage: ProviderStage) -> str:
    return "anchor" if stage == "anchor" else f"strip_{stage}"


def _default_provider_artifact_path(stage: ProviderStage) -> str:
    return "anchor/identity.png" if stage == "anchor" else f"strips/{stage}.png"


def _validate_provider_stage(stage: str) -> None:
    if stage not in _PROVIDER_STAGES:
        raise ResidentSpriteContractError("REQUEST_STAGE_INVALID", "unknown provider stage")


def _validate_utc_now(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ResidentSpriteContractError("CLOCK_INVALID", "claim clock must be UTC-aware")


def _validate_claim_ttl(ttl: timedelta) -> None:
    if not isinstance(ttl, timedelta) or not timedelta(seconds=1) <= ttl <= timedelta(hours=1):
        raise ResidentSpriteContractError(
            "CLAIM_TTL_INVALID", "claim TTL must be between one second and one hour"
        )


def _ensure_mutable_state(manifest: SpriteRunManifest) -> None:
    if manifest.state in _TERMINAL_STATES:
        raise ResidentSpriteContractError(
            "RUN_TERMINAL", "published and rolled-back runs are immutable"
        )


def _validate_provider_stage_precondition(
    manifest: SpriteRunManifest,
    stage: ProviderStage,
) -> None:
    completed_stage = _completed_provider_stage(stage)
    if completed_stage in manifest.completed_stages:
        raise ResidentSpriteContractError("STAGE_ALREADY_COMPLETED", "provider stage is complete")
    if manifest.state in {"failed", "interrupted"}:
        raise ResidentSpriteContractError(
            "EXPLICIT_RETRY_REQUIRED", "failed or interrupted run requires retry_run"
        )
    if manifest.state == "quarantined":
        raise ResidentSpriteContractError(
            "EXPLICIT_REVIEW_REQUIRED", "quarantined run requires review_quarantined_run"
        )
    anchor_complete = "anchor" in manifest.completed_stages
    if stage == "anchor":
        allowed = manifest.state in {"requested", "retrying"} and not anchor_complete
    else:
        required_directions = _required_directions(manifest)
        allowed = (
            stage in required_directions
            and anchor_complete
            and manifest.state in {"anchor_ready", "retrying"}
        )
    if not allowed:
        raise ResidentSpriteContractError(
            "STAGE_PRECONDITION_FAILED", "provider stage prerequisites are not satisfied"
        )


def _required_directions(manifest: SpriteRunManifest) -> tuple[str, ...]:
    if manifest.request.direction_policy == "generate_right":
        return ("down", "left", "up", "right")
    return ("down", "left", "up")


def _require_all_direction_strips(manifest: SpriteRunManifest) -> None:
    missing = [
        direction
        for direction in _required_directions(manifest)
        if f"strip_{direction}" not in manifest.completed_stages
    ]
    if missing:
        raise ResidentSpriteContractError(
            "STAGE_PRECONDITION_FAILED", "all required direction strips must be complete"
        )


def _reject_unregistered_provider_artifact(
    run_dir: Path,
    manifest: SpriteRunManifest,
    relative_path: str,
) -> None:
    target = _artifact_path(run_dir, relative_path)
    if not _path_exists_without_symlink(target):
        return
    if not any(artifact.relative_path == relative_path for artifact in manifest.artifacts):
        raise ResidentSpriteContractError(
            "ORPHAN_RECONCILIATION_REQUIRED",
            "provider artifact exists without manifest registration",
        )


def _require_matching_claim(
    manifest: SpriteRunManifest,
    stage: ProviderStage,
    owner: str,
    attempt_id: str,
) -> ProviderStageClaim:
    claim = manifest.active_claim
    if claim is None:
        raise ResidentSpriteContractError("STAGE_CLAIM_REQUIRED", "no active stage claim")
    if claim.stage != stage or claim.owner != owner or claim.attempt_id != attempt_id:
        raise ResidentSpriteContractError(
            "STAGE_CLAIM_MISMATCH", "stage claim ownership tuple does not match"
        )
    return claim


def _merged_provider_request_ids(
    manifest: SpriteRunManifest,
    provider_request_ids: tuple[str, ...],
) -> list[str]:
    merged = list(manifest.provider_request_ids)
    for request_id in provider_request_ids:
        if request_id not in merged:
            merged.append(request_id)
    return merged


def _validate_state_transition(current: RunState, target: RunState) -> None:
    if current in {"failed", "interrupted"}:
        raise ResidentSpriteContractError(
            "EXPLICIT_RETRY_REQUIRED", "failed or interrupted run requires retry_run"
        )
    if current == "quarantined":
        raise ResidentSpriteContractError(
            "EXPLICIT_REVIEW_REQUIRED", "quarantined run requires review_quarantined_run"
        )
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ResidentSpriteContractError(
            "STATE_TRANSITION_INVALID", f"state cannot transition from {current} to {target}"
        )


def _validated_manifest_copy(
    manifest: SpriteRunManifest,
    **updates: Any,
) -> SpriteRunManifest:
    copied = manifest.model_copy(update=updates, deep=True)
    return SpriteRunManifest.model_validate(copied.model_dump(mode="python"))


def _explicit_recovery_transition(
    artifact_root: Path,
    run_id: str,
    *,
    allowed: set[RunState],
    target: RunState,
) -> SpriteRunManifest:
    run_dir = _run_directory(artifact_root, run_id, create=False)
    with _run_lock(run_dir):
        manifest, old_manifest_bytes = _load_manifest(
            run_dir, run_id, verify_artifacts=True
        )
        if manifest.state not in allowed:
            raise ResidentSpriteContractError(
                "STATE_TRANSITION_INVALID", "explicit recovery is not allowed from this state"
            )
        if manifest.active_claim is not None:
            raise ResidentSpriteContractError(
                "STAGE_CLAIM_HELD", "explicit recovery requires the claim to be released"
            )
        updated = _validated_manifest_copy(manifest, state=target, error=None)
        _commit_manifest(run_dir, updated, old_manifest_bytes)
        return updated


def _run_directory(artifact_root: Path, run_id: str, *, create: bool) -> Path:
    validate_run_id(run_id)
    root = validate_non_symlink_path(Path(artifact_root), must_exist=not create)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    root = validate_non_symlink_path(root, must_exist=True)
    if not root.is_dir():
        raise ResidentSpriteContractError("ARTIFACT_ROOT_INVALID", "artifact root is not a directory")
    run_dir = root / run_id
    if create:
        run_dir.mkdir(mode=0o700, exist_ok=True)
    run_dir = validate_non_symlink_path(run_dir, must_exist=True)
    if not run_dir.is_dir():
        raise ResidentSpriteContractError("RUN_DIRECTORY_INVALID", "run path is not a directory")
    return run_dir


def _artifact_path(run_dir: Path, relative_path: str) -> Path:
    validate_artifact_relative_path(relative_path)
    target = run_dir.joinpath(*PurePosixPath(relative_path).parts)
    try:
        target.relative_to(run_dir)
    except ValueError as exc:
        raise ResidentSpriteContractError(
            "ARTIFACT_PATH_INVALID", "artifact path escapes the run directory"
        ) from exc
    return target


def _ensure_parent_directories(run_dir: Path, parent: Path) -> None:
    current = run_dir
    for part in parent.relative_to(run_dir).parts:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ResidentSpriteContractError(
                "ARTIFACT_PATH_INVALID", "artifact parent must be a real directory"
            )


@contextmanager
def _run_lock(run_dir: Path) -> Iterator[None]:
    lock_path = run_dir / _LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ResidentSpriteContractError("RUN_LOCK_INVALID", "run lock cannot be opened") from exc
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ResidentSpriteContractError("RUN_LOCK_INVALID", "run lock is not a file")
        with exclusive_file_lock(lock_fd):
            yield
    finally:
        os.close(lock_fd)


def _load_manifest(
    run_dir: Path,
    run_id: str,
    *,
    verify_artifacts: bool,
) -> tuple[SpriteRunManifest, bytes]:
    manifest_path = run_dir / MANIFEST_NAME
    try:
        raw = _read_regular_file(manifest_path, max_bytes=_MAX_MANIFEST_BYTES)
        manifest = SpriteRunManifest.model_validate_json(raw)
    except ResidentSpriteContractError:
        raise
    except Exception as exc:
        raise ResidentSpriteContractError(
            "MANIFEST_INVALID", "manifest is missing or invalid"
        ) from exc
    if manifest.run_id != run_id:
        raise ResidentSpriteContractError("MANIFEST_INVALID", "manifest run_id does not match")
    if raw != canonical_json_bytes(manifest):
        raise ResidentSpriteContractError("MANIFEST_INVALID", "manifest is not canonical JSON")
    if verify_artifacts:
        for artifact in manifest.artifacts:
            _verify_artifact(run_dir, artifact)
    return manifest, raw


def _verify_artifact(run_dir: Path, artifact: SpriteArtifact) -> None:
    target = _artifact_path(run_dir, artifact.relative_path)
    try:
        data = _read_regular_file(target, max_bytes=_MAX_ARTIFACT_BYTES)
    except ResidentSpriteContractError as exc:
        raise ResidentSpriteContractError(
            "ARTIFACT_CORRUPT", f"artifact cannot be read: {artifact.relative_path}"
        ) from exc
    if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ResidentSpriteContractError(
            "ARTIFACT_CORRUPT", f"artifact integrity check failed: {artifact.relative_path}"
        )


def _read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    validate_non_symlink_path(path, must_exist=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(path, flags)
    except OSError as exc:
        raise ResidentSpriteContractError("ARTIFACT_READ_FAILED", "file cannot be opened") from exc
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise ResidentSpriteContractError("ARTIFACT_READ_FAILED", "file is invalid or oversized")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(file_fd, min(64 * 1024, max_bytes + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ResidentSpriteContractError("ARTIFACT_READ_FAILED", "file is oversized")
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _atomic_create_bytes(path: Path, data: bytes) -> None:
    if _path_exists_without_symlink(path):
        try:
            existing = _read_regular_file(path, max_bytes=max(len(data), _MAX_MANIFEST_BYTES))
        except ResidentSpriteContractError as exc:
            raise ResidentSpriteContractError("ARTIFACT_CONFLICT", "existing file is invalid") from exc
        if existing == data:
            return
        raise ResidentSpriteContractError("ARTIFACT_CONFLICT", "refusing to overwrite different bytes")
    _atomic_replace_bytes(path, data, expected_current=None)


def _commit_manifest(
    run_dir: Path,
    manifest: SpriteRunManifest,
    expected_current: bytes,
) -> None:
    _atomic_replace_bytes(
        run_dir / MANIFEST_NAME,
        canonical_json_bytes(manifest),
        expected_current=expected_current,
    )


def _atomic_replace_bytes(
    path: Path,
    data: bytes,
    *,
    expected_current: bytes | None,
) -> None:
    parent = validate_non_symlink_path(path.parent, must_exist=True)
    temp_path = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    file_fd: int | None = None
    try:
        file_fd = os.open(temp_path, flags, 0o600)
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(file_fd, view[written:])
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None

        if expected_current is None:
            if _path_exists_without_symlink(path):
                existing = _read_regular_file(
                    path, max_bytes=max(len(data), _MAX_MANIFEST_BYTES)
                )
                if existing == data:
                    return
                raise ResidentSpriteContractError(
                    "ARTIFACT_CONFLICT", "refusing to overwrite different bytes"
                )
        else:
            current = _read_regular_file(path, max_bytes=_MAX_MANIFEST_BYTES)
            if current == data:
                return
            if current != expected_current:
                raise ResidentSpriteContractError(
                    "MANIFEST_CONFLICT", "manifest changed during update"
                )

        os.replace(temp_path, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ResidentSpriteContractError("ARTIFACT_PATH_INVALID", "symlink is not allowed") from exc
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _path_exists_without_symlink(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise ResidentSpriteContractError("ARTIFACT_PATH_INVALID", "symlink is not allowed")
    return True
