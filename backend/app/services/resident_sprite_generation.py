"""Frozen validation and data contracts for resident sprite generation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


PROMPT_VERSION = "resident-sprite-v1"
ALGORITHM_VERSION = "resident-atlas-v2"
ADAPTER_VERSION = "resident-image-provider-v2"

Gender = Literal["male", "female", "neutral"]
AgeGroup = Literal["young", "adult", "elder"]
DirectionPolicy = Literal["mirror_right", "generate_right"]
RunState = Literal[
    "requested", "anchor_ready", "strips_ready", "processed",
    "auto_qc_passed", "candidate_ready", "phaser_reviewed",
    "human_approved", "published", "retrying", "interrupted", "failed",
    "quarantined", "rolled_back", "publishing",
]

_ASSET_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.ASCII)
_UUID4_HEX = re.compile(r"^[0-9a-f]{32}$", re.ASCII)
_HEX_16 = re.compile(r"^[0-9a-f]{16}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ResidentSpriteContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _trimmed_text(value: str, field_name: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if _CONTROL.search(normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


class ResidentSpriteRequest(StrictContractModel):
    asset_key: str = Field(pattern=_ASSET_KEY.pattern)
    display_name: str = Field(min_length=1, max_length=80)
    appearance: str = Field(min_length=1, max_length=1200)
    gender: Gender
    age_group: AgeGroup
    vibe: str = Field(min_length=1, max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=8)
    direction_policy: DirectionPolicy = "mirror_right"
    model: str = Field(min_length=1, max_length=200)
    anchor_quality: Literal["medium"] = "medium"
    strip_quality: Literal["high"] = "high"
    palette_colors: Literal[32] = 32
    prompt_version: Literal["resident-sprite-v1"] = PROMPT_VERSION
    algorithm_version: Literal["resident-atlas-v2"] = ALGORITHM_VERSION
    max_strip_generations: Literal[3] = 3

    @field_validator("display_name", "appearance", "vibe")
    @classmethod
    def normalize_text(cls, value: str, info) -> str:
        normalized = _trimmed_text(value, info.field_name)
        max_length = {
            "display_name": 80,
            "appearance": 1200,
            "vibe": 40,
        }[info.field_name]
        if not 1 <= len(normalized) <= max_length:
            raise ValueError(
                f"{info.field_name} must contain 1-{max_length} characters after normalization"
            )
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized = [_trimmed_text(value, "tag") for value in values]
        if any(not 1 <= len(value) <= 32 for value in normalized):
            raise ValueError("each tag must contain 1-32 characters")
        folded = [value.casefold() for value in normalized]
        if len(folded) != len(set(folded)):
            raise ValueError("tags must be unique after casefolding")
        return normalized

    @property
    def sprite_key(self) -> str:
        return f"generated/{self.asset_key}"

    def require_allowed_model(self, allowed_models: set[str] | frozenset[str]) -> None:
        if self.model not in allowed_models:
            raise ResidentSpriteContractError("MODEL_NOT_ALLOWED", "model is not allowlisted")


def display_name_collision_key(value: str) -> str:
    normalized = _trimmed_text(value, "display_name")
    if not 1 <= len(normalized) <= 80:
        raise ResidentSpriteContractError(
            "DISPLAY_NAME_INVALID", "display_name must contain 1-80 characters"
        )
    return normalized.casefold()


def ensure_display_name_available(display_name: str, existing_names: list[str]) -> None:
    key = display_name_collision_key(display_name)
    if any(display_name_collision_key(existing) == key for existing in existing_names):
        raise ResidentSpriteContractError(
            "DISPLAY_NAME_COLLISION", "display_name collides with an existing resident"
        )


def validate_non_symlink_path(path: Path, *, must_exist: bool = False) -> Path:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if must_exist:
                raise ResidentSpriteContractError("PATH_MISSING", "path does not exist")
            continue
        if stat.S_ISLNK(mode):
            raise ResidentSpriteContractError("PATH_SYMLINK", "symlink paths are not allowed")
    return absolute


def new_run_id() -> str:
    return uuid.uuid4().hex


def validate_run_id(value: str) -> str:
    if not _UUID4_HEX.fullmatch(value):
        raise ResidentSpriteContractError("RUN_ID_INVALID", "run_id must be lowercase UUID4 hex")
    parsed = uuid.UUID(hex=value)
    if parsed.version != 4 or parsed.hex != value:
        raise ResidentSpriteContractError("RUN_ID_INVALID", "run_id must be lowercase UUID4 hex")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    value = _canonical_value(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResidentSpriteContractError(
            "CANONICAL_JSON_INVALID", "value is not canonical JSON"
        ) from exc


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        rendered = value.isoformat()
        return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def content_id(value: BaseModel | dict[str, Any], own_field: str) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    if not isinstance(payload, dict):
        raise ResidentSpriteContractError("CONTENT_ID_INVALID", "content ID requires an object")
    without_id = {key: item for key, item in payload.items() if key != own_field}
    return hashlib.sha256(canonical_json_bytes(without_id)).hexdigest()


class SanitizedError(StrictContractModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    provider_request_id: str | None = Field(default=None, max_length=200)
    http_status: StrictInt | None = Field(default=None, ge=100, le=599)


class ProviderImageResult(StrictContractModel):
    image_bytes: bytes = Field(repr=False)
    provider_request_id: str | None = Field(default=None, max_length=200)
    latency_ms: StrictInt = Field(ge=0)
    submitted_request_count: StrictInt = Field(ge=1)


class QCFinding(StrictContractModel):
    code: str = Field(min_length=1, max_length=100)
    detail: str = Field(min_length=1, max_length=500)


class ResidentSpriteRunResult(StrictContractModel):
    run_id: str
    state: RunState
    staged_artifact_paths: list[str] = Field(default_factory=list)
    manifest_path: str | None = None
    qc_findings: list[QCFinding] = Field(default_factory=list)
    provider_request_ids: list[str] = Field(default_factory=list)
    error: SanitizedError | None = None

    @field_validator("run_id")
    @classmethod
    def valid_run_id(cls, value: str) -> str:
        return validate_run_id(value)


class RequestBudget(StrictContractModel):
    direction_policy: DirectionPolicy = "mirror_right"
    submitted_image_request_count: StrictInt = Field(default=0, ge=0)
    stage_counts: dict[str, StrictInt] = Field(default_factory=dict)

    @property
    def global_ceiling(self) -> int:
        return 11 if self.direction_policy == "mirror_right" else 14

    @staticmethod
    def stage_ceiling(stage: str) -> int:
        if stage == "anchor":
            return 2
        if stage in {"down", "left", "right", "up"}:
            return 3
        raise ResidentSpriteContractError("REQUEST_STAGE_INVALID", "unknown request stage")

    def consume_before_post(self, stage: str) -> None:
        stage_count = self.stage_counts.get(stage, 0)
        if (
            stage_count >= self.stage_ceiling(stage)
            or self.submitted_image_request_count >= self.global_ceiling
        ):
            raise ResidentSpriteContractError(
                "REQUEST_BUDGET_EXHAUSTED", "submitted image request budget is exhausted"
            )
        self.stage_counts[stage] = stage_count + 1
        self.submitted_image_request_count += 1


class WireRequestShape(StrictContractModel):
    endpoint: Literal["/images/edits"] = "/images/edits"
    fields: tuple[str, ...] = ("model", "prompt", "n", "size", "quality")
    size: Literal["1536x1024"] = "1536x1024"
    quality: Literal["high"] = "high"
    response_mode: Literal["b64_json_or_public_https_url"] = (
        "b64_json_or_public_https_url"
    )
    dimension_policy: Literal["bounded-center-fit-v1"] = "bounded-center-fit-v1"
    provider_evidence_policy: Literal["header-body-id-or-url-sha256-v1"] = (
        "header-body-id-or-url-sha256-v1"
    )


class CapabilityCostAuthorization(StrictContractModel):
    currency: Literal["USD"] = "USD"
    max_requests: Literal[7] = 7
    price_per_request_upper_bound_usd: str
    max_cost_usd: str
    cost_source: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_money(self) -> "CapabilityCostAuthorization":
        parsed: list[Decimal] = []
        for value in (self.price_per_request_upper_bound_usd, self.max_cost_usd):
            try:
                amount = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("capability price values must be decimal strings") from exc
            if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -6:
                raise ValueError("capability prices must be positive with at most six decimals")
            parsed.append(amount)
        if parsed[1] < parsed[0] * self.max_requests:
            raise ValueError("capability max cost is below its request ceiling")
        if self.cost_source != self.cost_source.strip():
            raise ValueError("capability cost source must be canonical text")
        return self


class WireReceipt(StrictContractModel):
    schema_version: Literal[1] = 1
    normalized_origin: str
    model_alias: str
    transport_security: Literal["https_or_loopback", "insecure_http_test"]
    adapter_version: Literal["resident-image-provider-v2"] = ADAPTER_VERSION
    endpoint: Literal["/images/edits"] = "/images/edits"
    multipart_field: Literal["image[]", "image"]
    request_shape: WireRequestShape
    output_dimensions: tuple[int, int] = (1536, 1024)
    calibration_source_sha256: str = Field(pattern=_SHA256.pattern)
    calibration_output_sha256: str = Field(pattern=_SHA256.pattern)
    provider_request_ids: list[str]
    submitted_request_count: StrictInt = Field(ge=1, le=2)
    reconciled_prior_failure_code: Literal[
        "PROVIDER_HTTP_ERROR",
        "PROVIDER_IMAGE_URL_INVALID",
        "PROVIDER_DIMENSIONS",
        "PROVIDER_EVIDENCE_MISSING",
    ] | None = None
    cost_authorization: CapabilityCostAuthorization
    operator: str = Field(min_length=1, max_length=80)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    wire_receipt_id: str = Field(pattern=_SHA256.pattern)

    @field_validator("operator")
    @classmethod
    def canonical_operator(cls, value: str) -> str:
        normalized = _trimmed_text(value, "operator")
        if normalized != value:
            raise ValueError("operator must already be trimmed NFC text")
        return normalized

    @model_validator(mode="after")
    def validate_receipt(self) -> "WireReceipt":
        if self.wire_receipt_id != content_id(self, "wire_receipt_id"):
            raise ValueError("wire_receipt_id does not match canonical content")
        if self.expires_at - self.observed_at != timedelta(hours=24):
            raise ValueError("wire receipt must expire exactly 24 hours after observation")
        if self.reconciled_prior_failure_code is not None and self.submitted_request_count != 2:
            raise ValueError("a reconciled prior probe requires exactly two submitted requests")
        return self


class BlindCandidateScore(StrictContractModel):
    candidate_id: str = Field(pattern=_HEX_16.pattern)
    identity_consistency: StrictInt = Field(ge=1, le=5)
    layout_correctness: StrictInt = Field(ge=1, le=5)
    movement_readability: StrictInt = Field(ge=1, le=5)
    visual_fit: StrictInt = Field(ge=1, le=5)


class BlindScoreFile(StrictContractModel):
    schema_version: Literal[1] = 1
    qualification_id: str
    scores: list[BlindCandidateScore] = Field(min_length=2, max_length=2)
    notes: str = Field(default="", max_length=1000)

    @field_validator("qualification_id")
    @classmethod
    def qualification_uuid(cls, value: str) -> str:
        return validate_run_id(value)

    @model_validator(mode="after")
    def unique_candidates(self) -> "BlindScoreFile":
        if len({score.candidate_id for score in self.scores}) != 2:
            raise ValueError("scores must contain two distinct candidate IDs")
        return self


class CapabilityContract(StrictContractModel):
    normalized_origin: str
    model_alias: str
    transport_security: Literal["https_or_loopback", "insecure_http_test"] = (
        "https_or_loopback"
    )
    adapter_version: Literal["resident-image-provider-v2"] = ADAPTER_VERSION
    prompt_version: Literal["resident-sprite-v1"] = PROMPT_VERSION
    algorithm_version: Literal["resident-atlas-v2"] = ALGORITHM_VERSION
    generation_endpoint: Literal["/images/generations"] = "/images/generations"
    edit_endpoint: Literal["/images/edits"] = "/images/edits"
    multipart_field: Literal["image[]", "image"]
    anchor_fields: tuple[str, ...] = ("model", "prompt", "n", "size", "quality")
    edit_fields: tuple[str, ...] = ("model", "prompt", "n", "size", "quality")
    oneshot_fields: tuple[str, ...] = ("model", "prompt", "n", "size", "quality")
    response_mode: Literal["b64_json_or_public_https_url"] = (
        "b64_json_or_public_https_url"
    )
    dimension_policy: Literal["bounded-center-fit-v1"] = "bounded-center-fit-v1"
    provider_evidence_policy: Literal["header-body-id-or-url-sha256-v1"] = (
        "header-body-id-or-url-sha256-v1"
    )
    anchor_quality: Literal["medium"] = "medium"
    strip_quality: Literal["high"] = "high"
    oneshot_quality: Literal["high"] = "high"
    anchor_dimensions: tuple[int, int] = (1024, 1024)
    strip_dimensions: tuple[int, int] = (1536, 1024)
    sheet_dimensions: tuple[int, int] = (1024, 1536)
    output_dimensions: tuple[int, int] = (96, 128)


class CapabilityReceipt(CapabilityContract):
    schema_version: Literal[1] = 1
    wire_receipt_id: str = Field(pattern=_SHA256.pattern)
    probe_id: str = Field(min_length=1, max_length=200)
    qualification_id: str
    operator: str = Field(min_length=1, max_length=80)
    reviewer: str = Field(min_length=1, max_length=80)
    qualified_at: AwareDatetime
    expires_at: AwareDatetime
    evidence_sha256: list[str]
    provider_request_ids: list[str]
    blind_scores: list[BlindCandidateScore]
    latency_ms: list[StrictInt] = Field(min_length=5, max_length=5)
    capability_request_count: StrictInt = Field(ge=6, le=7)
    capability_cost_upper_bound_usd: str = Field(
        pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"
    )
    cost_source: str = Field(min_length=1, max_length=200)
    receipt_id: str = Field(pattern=_SHA256.pattern)

    @field_validator("qualification_id")
    @classmethod
    def qualification_uuid(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("operator", "reviewer")
    @classmethod
    def canonical_people(cls, value: str, info) -> str:
        normalized = _trimmed_text(value, info.field_name)
        if normalized != value:
            raise ValueError(f"{info.field_name} must already be trimmed NFC text")
        return normalized

    @field_validator("evidence_sha256")
    @classmethod
    def valid_evidence_hashes(cls, values: list[str]) -> list[str]:
        if not values or any(not _SHA256.fullmatch(value) for value in values):
            raise ValueError("evidence hashes must be nonempty SHA-256 values")
        return values

    @field_validator("latency_ms")
    @classmethod
    def nonnegative_latencies(cls, values: list[int]) -> list[int]:
        if any(value < 0 for value in values):
            raise ValueError("latency_ms values must be nonnegative")
        return values

    @model_validator(mode="after")
    def validate_receipt(self) -> "CapabilityReceipt":
        try:
            cost_upper_bound = Decimal(self.capability_cost_upper_bound_usd)
        except InvalidOperation as exc:
            raise ValueError("capability cost upper bound must be a decimal string") from exc
        if (
            not cost_upper_bound.is_finite()
            or cost_upper_bound <= 0
            or cost_upper_bound.as_tuple().exponent < -6
            or self.cost_source != self.cost_source.strip()
        ):
            raise ValueError("capability cost evidence is invalid")
        if self.operator.strip() == self.reviewer.strip():
            raise ValueError("reviewer must differ from operator")
        if self.expires_at - self.qualified_at != timedelta(days=30):
            raise ValueError("capability must expire exactly 30 days after qualification")
        if self.receipt_id != content_id(self, "receipt_id"):
            raise ValueError("receipt_id does not match canonical content")
        return self


class QualifiedSpriteCapability(StrictContractModel):
    """Frozen evidence and runtime inputs required for every provider POST."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    receipt: CapabilityReceipt
    contract: CapabilityContract
    revocation_path: Path
    clock: Callable[[], datetime]


RevocationReason = Literal[
    "SCHEMA_MISMATCH", "EDIT_UNSUPPORTED", "RESPONSE_MODE_CHANGED", "DIMENSIONS_CHANGED"
]


class CapabilityRevocation(StrictContractModel):
    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=_SHA256.pattern)
    reason_code: RevocationReason
    observed_at: AwareDatetime
    provider_request_id: str | None = Field(default=None, max_length=200)
    actor: str = Field(min_length=1, max_length=80)
    revocation_id: str = Field(pattern=_SHA256.pattern)

    @field_validator("actor")
    @classmethod
    def canonical_actor(cls, value: str) -> str:
        normalized = _trimmed_text(value, "actor")
        if normalized != value:
            raise ValueError("actor must already be trimmed NFC text")
        return normalized

    @model_validator(mode="after")
    def validate_revocation(self) -> "CapabilityRevocation":
        if self.revocation_id != content_id(self, "revocation_id"):
            raise ValueError("revocation_id does not match canonical content")
        return self


def validate_wire_receipt(
    receipt: WireReceipt, now: datetime, contract: CapabilityContract
) -> None:
    _validate_clock(now)
    if now > receipt.expires_at:
        raise ResidentSpriteContractError("WIRE_RECEIPT_EXPIRED", "wire receipt has expired")
    if (
        receipt.normalized_origin != contract.normalized_origin
        or receipt.model_alias != contract.model_alias
        or receipt.transport_security != contract.transport_security
        or receipt.adapter_version != contract.adapter_version
        or receipt.endpoint != contract.edit_endpoint
        or receipt.multipart_field != contract.multipart_field
        or receipt.output_dimensions != contract.strip_dimensions
        or receipt.request_shape.fields != contract.edit_fields
        or receipt.request_shape.response_mode != contract.response_mode
        or receipt.request_shape.dimension_policy != contract.dimension_policy
        or receipt.request_shape.provider_evidence_policy
        != contract.provider_evidence_policy
    ):
        raise ResidentSpriteContractError("WIRE_RECEIPT_INCOMPATIBLE", "wire receipt is incompatible")


def _validate_clock(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ResidentSpriteContractError("CLOCK_INVALID", "clock must be timezone-aware")


def validate_capability_receipt(
    receipt: CapabilityReceipt,
    now: datetime,
    expected: CapabilityContract,
    revocation_path: Path | None = None,
) -> None:
    _validate_clock(now)
    if now > receipt.expires_at:
        raise ResidentSpriteContractError("CAPABILITY_EXPIRED", "capability receipt has expired")
    for field_name in CapabilityContract.model_fields:
        if getattr(receipt, field_name) != getattr(expected, field_name):
            raise ResidentSpriteContractError(
                "CAPABILITY_INCOMPATIBLE", f"capability field is incompatible: {field_name}"
            )
    if revocation_path is not None and revocation_path.exists():
        try:
            revocation = CapabilityRevocation.model_validate_json(revocation_path.read_bytes())
        except Exception as exc:
            raise ResidentSpriteContractError(
                "REVOCATION_INVALID", "capability revocation tombstone is invalid"
            ) from exc
        if revocation.receipt_id != receipt.receipt_id:
            raise ResidentSpriteContractError(
                "REVOCATION_INVALID", "capability revocation receipt does not match"
            )
        raise ResidentSpriteContractError("CAPABILITY_REVOKED", "capability receipt is revoked")


def create_revocation_tombstone(directory: Path, revocation: CapabilityRevocation) -> Path:
    directory = validate_non_symlink_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{revocation.receipt_id}.json"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            file_fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            existing_fd = os.open(filename, os.O_RDONLY | nofollow, dir_fd=directory_fd)
            try:
                existing_bytes = b""
                while chunk := os.read(existing_fd, 64 * 1024):
                    existing_bytes += chunk
                    if len(existing_bytes) > 1024 * 1024:
                        raise ResidentSpriteContractError(
                            "REVOCATION_INVALID", "existing revocation is oversized"
                        )
            finally:
                os.close(existing_fd)
            try:
                existing = CapabilityRevocation.model_validate_json(existing_bytes)
            except Exception as exc:
                raise ResidentSpriteContractError(
                    "REVOCATION_INVALID", "existing revocation is invalid"
                ) from exc
            if existing.receipt_id != revocation.receipt_id:
                raise ResidentSpriteContractError(
                    "REVOCATION_INVALID", "existing revocation receipt does not match"
                )
            return directory / filename
        try:
            os.write(file_fd, canonical_json_bytes(revocation))
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return directory / filename


class _PersistentRequestBudget:
    """RequestBudget-compatible proxy that persists each reservation first."""

    def __init__(
        self,
        budget: RequestBudget,
        consume: Callable[[str], RequestBudget],
    ) -> None:
        self._budget = budget
        self._consume = consume

    @property
    def direction_policy(self) -> DirectionPolicy:
        return self._budget.direction_policy

    @property
    def submitted_image_request_count(self) -> int:
        return self._budget.submitted_image_request_count

    @property
    def stage_counts(self) -> dict[str, int]:
        return dict(self._budget.stage_counts)

    @property
    def global_ceiling(self) -> int:
        return self._budget.global_ceiling

    def consume_before_post(self, stage: str) -> None:
        self._budget = self._consume(stage)


async def generate_resident_sprite(
    request: ResidentSpriteRequest,
    *,
    client: Any,
    artifact_root: Path,
    run_id: str | None = None,
    capability: QualifiedSpriteCapability | None = None,
    retry_failed: bool = False,
) -> ResidentSpriteRunResult:
    """Generate or resume a crash-safe resident sprite candidate run."""
    if capability is None or not isinstance(capability, QualifiedSpriteCapability):
        raise ResidentSpriteContractError(
            "CAPABILITY_REQUIRED",
            "a qualified sprite capability context is required",
        )

    def verify_capability() -> None:
        validate_capability_receipt(
            capability.receipt,
            capability.clock(),
            capability.contract,
            capability.revocation_path,
        )

    # Verify before importing artifact storage or creating a run directory.
    verify_capability()
    multipart_field = capability.receipt.multipart_field
    qualified_model = capability.contract.model_alias
    model_alias = getattr(client, "model_alias", None)
    contract_origin = getattr(client, "contract_origin", None)
    if not isinstance(model_alias, str) or not model_alias:
        raise ResidentSpriteContractError(
            "MODEL_BINDING_REQUIRED",
            "provider client must expose a nonempty model alias",
        )
    if not isinstance(contract_origin, str) or not contract_origin:
        raise ResidentSpriteContractError(
            "PROVIDER_BINDING_REQUIRED",
            "provider client must expose its normalized contract origin",
        )
    if model_alias != qualified_model or request.model != qualified_model:
        raise ResidentSpriteContractError(
            "MODEL_MISMATCH",
            "provider, capability, and request models must match",
        )
    if contract_origin != capability.contract.normalized_origin:
        raise ResidentSpriteContractError(
            "PROVIDER_ORIGIN_MISMATCH",
            "provider origin does not match the qualified capability",
        )

    # These modules import this contract module, so they must remain lazy to
    # avoid a generation -> artifacts/QC -> generation import cycle.
    from app.services.resident_sprite_artifacts import (
        MANIFEST_NAME,
        advance_stage,
        claim_stage,
        complete_stage,
        consume_request_budget,
        create_run,
        fail_stage_claim,
        load_run,
        read_artifact,
        release_stage_claim,
        retry_run,
        write_artifact,
        write_canonical_json_artifact,
    )
    from app.services.resident_sprite_postprocess import (
        ResidentSpritePostprocessError,
        build_resident_sprite_atlas,
        derive_resident_portrait,
    )
    from app.services.resident_sprite_prompts import (
        render_anchor_prompt,
        render_direction_prompt,
    )
    from app.services.resident_sprite_provider import ProviderError
    from app.services.resident_sprite_qc import inspect_resident_sprite_atlas

    artifact_root = validate_non_symlink_path(Path(artifact_root))
    active_run_id = new_run_id() if run_id is None else validate_run_id(run_id)
    manifest = create_run(artifact_root, active_run_id, request)
    run_directory = artifact_root / active_run_id
    write_canonical_json_artifact(
        artifact_root,
        active_run_id,
        "evidence/capability.json",
        {
            "schema_version": 1,
            "receipt": capability.receipt.model_dump(mode="json"),
            "contract": capability.contract.model_dump(mode="json"),
        },
    )

    def result_for(
        current_manifest: Any,
        *,
        findings: list[QCFinding] | None = None,
    ) -> ResidentSpriteRunResult:
        return ResidentSpriteRunResult(
            run_id=active_run_id,
            state=current_manifest.state,
            staged_artifact_paths=[
                str(run_directory / artifact.relative_path)
                for artifact in current_manifest.artifacts
            ],
            manifest_path=str(run_directory / MANIFEST_NAME),
            qc_findings=[] if findings is None else findings,
            provider_request_ids=list(current_manifest.provider_request_ids),
            error=current_manifest.error,
        )

    def qc_findings_from_artifact() -> list[QCFinding]:
        try:
            payload = json.loads(
                read_artifact(
                    artifact_root, active_run_id, "candidate/qc.json"
                )
            )
            if not isinstance(payload, dict) or set(payload) != {
                "direction_policy",
                "findings",
                "passed",
            }:
                raise ValueError("unexpected QC artifact shape")
            findings = [QCFinding.model_validate(item) for item in payload["findings"]]
            if (
                payload["direction_policy"] != request.direction_policy
                or payload["passed"] is not (not findings)
            ):
                raise ValueError("QC artifact does not match the run")
            return findings
        except ResidentSpriteContractError:
            raise
        except Exception as exc:
            raise ResidentSpriteContractError(
                "QC_ARTIFACT_INVALID", "stored QC evidence is invalid"
            ) from exc

    if manifest.state == "failed":
        if not retry_failed:
            return result_for(manifest)
        manifest = retry_run(artifact_root, active_run_id)
    if manifest.state == "quarantined" or "auto_qc" in manifest.completed_stages:
        return result_for(manifest, findings=qc_findings_from_artifact())

    budget = _PersistentRequestBudget(
        manifest.request_budget,
        lambda stage: consume_request_budget(
            artifact_root, active_run_id, stage
        ),
    )
    claim_owner = f"resident-sprite-pipeline:{new_run_id()}"

    async def provider_artifact(
        *,
        completed_stage: str,
        relative_path: str,
        invoke: Callable[[], Any],
    ) -> bytes:
        current = load_run(artifact_root, active_run_id)
        if completed_stage in current.completed_stages:
            return read_artifact(artifact_root, active_run_id, relative_path)

        claim_now = capability.clock().astimezone(timezone.utc)
        claim = claim_stage(
            artifact_root,
            active_run_id,
            completed_stage.removeprefix("strip_"),
            claim_owner,
            claim_now,
            timedelta(hours=1),
            expected_artifact_path=relative_path,
        )
        declared = any(
            artifact.relative_path == relative_path for artifact in current.artifacts
        )
        if declared:
            data = read_artifact(artifact_root, active_run_id, relative_path)
            complete_stage(
                artifact_root,
                active_run_id,
                stage=claim.stage,
                owner=claim.owner,
                attempt_id=claim.attempt_id,
                now=capability.clock().astimezone(timezone.utc),
            )
            return data

        request_count_before = current.request_budget.submitted_image_request_count
        try:
            provider_result = await invoke()
            write_artifact(
                artifact_root,
                active_run_id,
                relative_path,
                provider_result.image_bytes,
            )
            request_ids = (
                ()
                if provider_result.provider_request_id is None
                else (provider_result.provider_request_id,)
            )
            complete_stage(
                artifact_root,
                active_run_id,
                stage=claim.stage,
                owner=claim.owner,
                attempt_id=claim.attempt_id,
                now=capability.clock().astimezone(timezone.utc),
                provider_request_ids=request_ids,
            )
        except asyncio.CancelledError:
            interrupted = load_run(artifact_root, active_run_id)
            active = interrupted.active_claim
            request_count_after = (
                interrupted.request_budget.submitted_image_request_count
            )
            if (
                request_count_after == request_count_before
                and active is not None
                and active.attempt_id == claim.attempt_id
            ):
                release_stage_claim(
                    artifact_root,
                    active_run_id,
                    stage=claim.stage,
                    owner=claim.owner,
                    attempt_id=claim.attempt_id,
                )
            raise
        except ProviderError as exc:
            fail_stage_claim(
                artifact_root,
                active_run_id,
                stage=claim.stage,
                owner=claim.owner,
                attempt_id=claim.attempt_id,
                error=exc.error,
            )
            raise
        except Exception:
            interrupted = load_run(artifact_root, active_run_id)
            active = interrupted.active_claim
            request_count_after = (
                interrupted.request_budget.submitted_image_request_count
            )
            if (
                request_count_after == request_count_before
                and active is not None
                and active.attempt_id == claim.attempt_id
            ):
                release_stage_claim(
                    artifact_root,
                    active_run_id,
                    stage=claim.stage,
                    owner=claim.owner,
                    attempt_id=claim.attempt_id,
                )
            raise
        return provider_result.image_bytes

    try:
        anchor = await provider_artifact(
            completed_stage="anchor",
            relative_path="anchor.png",
            invoke=lambda: client.generate_anchor(
                render_anchor_prompt(request.appearance),
                run_id=active_run_id,
                budget=budget,
                logical_job="anchor",
                gate=verify_capability,
            ),
        )

        strip_bytes: dict[str, bytes] = {}
        directions = ["down", "left", "up"]
        if request.direction_policy == "generate_right":
            directions.append("right")
        for direction in directions:
            strip_bytes[direction] = await provider_artifact(
                completed_stage=f"strip_{direction}",
                relative_path=f"strips/{direction}.png",
                invoke=lambda direction=direction: client.edit_strip(
                    anchor,
                    render_direction_prompt(direction.upper()),
                    multipart_field=multipart_field,
                    run_id=active_run_id,
                    stage=direction,
                    logical_job=f"{direction}-strip",
                    budget=budget,
                    gate=verify_capability,
                ),
            )
    except ProviderError as exc:
        del exc
        failed = load_run(artifact_root, active_run_id)
        return result_for(failed)

    manifest = load_run(artifact_root, active_run_id)
    if "strips" not in manifest.completed_stages:
        manifest = advance_stage(
            artifact_root,
            active_run_id,
            stage="strips",
            state="strips_ready",
        )

    if "postprocess" in manifest.completed_stages:
        texture = read_artifact(
            artifact_root, active_run_id, "candidate/texture.png"
        )
        read_artifact(artifact_root, active_run_id, "candidate/portrait.png")
    else:
        try:
            texture = build_resident_sprite_atlas(
                strip_bytes["down"],
                strip_bytes["left"],
                strip_bytes["up"],
                right_strip_png=strip_bytes.get("right"),
            )
            portrait = derive_resident_portrait(texture)
        except ResidentSpritePostprocessError as exc:
            error = SanitizedError(code=exc.code, message=exc.message)
            findings = [QCFinding(code=exc.code, detail=exc.message)]
            write_canonical_json_artifact(
                artifact_root,
                active_run_id,
                "candidate/qc.json",
                {
                    "direction_policy": request.direction_policy,
                    "findings": [
                        finding.model_dump(mode="json") for finding in findings
                    ],
                    "passed": False,
                },
            )
            manifest = advance_stage(
                artifact_root,
                active_run_id,
                stage="quarantine",
                state="quarantined",
                error=error,
            )
            return result_for(manifest, findings=findings)
        write_artifact(
            artifact_root,
            active_run_id,
            "candidate/texture.png",
            texture,
        )
        write_artifact(
            artifact_root,
            active_run_id,
            "candidate/portrait.png",
            portrait,
        )
        manifest = advance_stage(
            artifact_root,
            active_run_id,
            stage="postprocess",
            state="processed",
        )

    if manifest.state == "quarantined" or "auto_qc" in manifest.completed_stages:
        findings = qc_findings_from_artifact()
        return result_for(manifest, findings=findings)

    findings = inspect_resident_sprite_atlas(
        texture,
        direction_policy=request.direction_policy,
    )
    write_canonical_json_artifact(
        artifact_root,
        active_run_id,
        "candidate/qc.json",
        {
            "direction_policy": request.direction_policy,
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "passed": not findings,
        },
    )
    manifest = advance_stage(
        artifact_root,
        active_run_id,
        stage="auto_qc" if not findings else "quarantine",
        state="auto_qc_passed" if not findings else "quarantined",
    )
    return result_for(manifest, findings=findings)
