#!/usr/bin/env python3
"""Generate, qualify, review, and publish resident sprite candidates."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.resident_sprite_generation import (
    BlindScoreFile,
    CapabilityCostAuthorization,
    CapabilityContract,
    CapabilityReceipt,
    QualifiedSpriteCapability,
    ResidentSpriteContractError,
    ResidentSpriteRequest,
    SanitizedError,
    WireReceipt,
    WireRequestShape,
    canonical_json_bytes,
    content_id,
    generate_resident_sprite,
    new_run_id,
    validate_capability_receipt,
    validate_non_symlink_path,
    validate_run_id,
    validate_wire_receipt,
)
from app.services.resident_sprite_artifacts import (
    MANIFEST_NAME,
    acknowledge_uncertain_request_cost,
    advance_stage,
    load_run,
    read_artifact,
    release_expired_claim_if_safe,
    write_canonical_json_artifact,
)
from app.services.resident_sprite_postprocess import build_resident_sprite_atlas
from app.services.resident_sprite_prompts import (
    render_anchor_prompt,
    render_direction_prompt,
    render_qualification_oneshot_prompt,
)
from app.services.resident_sprite_provider import (
    ProviderConfig,
    ProviderError,
    QualificationBudget,
    ResidentSpriteProvider,
    calibration_anchor_png,
    wire_gate,
)


EXIT_SUCCESS = 0
EXIT_VALIDATION = 2
EXIT_PROVIDER = 3
EXIT_POSTPROCESS = 4
EXIT_APPROVAL = 5
EXIT_PUBLISH = 6

Handler = Callable[[argparse.Namespace, Any], Any]

ENV_PROVIDER_BASE_URL = "RESIDENT_SPRITE_PROVIDER_BASE_URL"
ENV_PROVIDER_API_KEY = "RESIDENT_SPRITE_PROVIDER_API_KEY"
ENV_PROVIDER_MODEL = "RESIDENT_SPRITE_PROVIDER_MODEL"
ENV_PROVIDER_TIMEOUT = "RESIDENT_SPRITE_PROVIDER_TIMEOUT"
ENV_ALLOW_INSECURE_HTTP_TEST = "RESIDENT_SPRITE_ALLOW_INSECURE_HTTP_TEST"
ENV_ARTIFACT_DIR = "RESIDENT_SPRITE_ARTIFACT_DIR"
ENV_ARTIFACT_ROOT = "RESIDENT_SPRITE_ARTIFACT_ROOT"  # Legacy CLI name.
ENV_QUALIFICATION_ROOT = "RESIDENT_SPRITE_QUALIFICATION_ROOT"
ENV_CAPABILITY_RECEIPT = "RESIDENT_SPRITE_CAPABILITY_RECEIPT"
ENV_REVOCATION_ROOT = "RESIDENT_SPRITE_REVOCATION_ROOT"
ENV_STATIC_ROOT = "RESIDENT_SPRITE_STATIC_ROOT"
ENV_STATIC_DIR = "STATIC_DIR"

_HEX_64 = frozenset("0123456789abcdef")


class CLIError(RuntimeError):
    def __init__(self, exit_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code


class _StrictCLIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ApprovalChecks(_StrictCLIModel):
    identity_consistent: bool
    directions_correct: bool
    gait_readable: bool
    scale_and_baseline_stable: bool
    anatomy_and_clipping_clean: bool
    background_clean: bool
    asymmetry_acceptable: bool
    originality_acceptable: bool
    gameplay_fit: bool


class ApprovalChecklist(_StrictCLIModel):
    schema_version: int = Field(ge=1, le=1)
    run_id: str
    decision: str = Field(pattern=r"^approve$")
    checks: ApprovalChecks
    notes: str = Field(max_length=1000)


# Some callers load this script with importlib without registering the module in
# sys.modules, so Pydantic cannot resolve postponed annotations by module name.
ApprovalChecklist.model_rebuild(_types_namespace={"ApprovalChecks": ApprovalChecks})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and review staged resident sprites",
        epilog=(
            "Configuration: provider commands require RESIDENT_SPRITE_PROVIDER_BASE_URL, "
            "RESIDENT_SPRITE_PROVIDER_API_KEY, and RESIDENT_SPRITE_PROVIDER_MODEL; "
            "qualification uses RESIDENT_SPRITE_QUALIFICATION_ROOT; generation/review uses "
            "RESIDENT_SPRITE_ARTIFACT_DIR and RESIDENT_SPRITE_CAPABILITY_RECEIPT; publication "
            "uses STATIC_DIR (default: static). Legacy RESIDENT_SPRITE_ARTIFACT_ROOT and "
            "RESIDENT_SPRITE_STATIC_ROOT remain accepted. Optional: RESIDENT_SPRITE_PROVIDER_TIMEOUT and "
            "RESIDENT_SPRITE_REVOCATION_ROOT."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe-wire")
    probe.add_argument("--spec", type=Path, required=True)
    probe.add_argument("--operator", required=True)
    probe.add_argument("--price-per-request-usd", required=True)
    probe.add_argument("--confirm-max-requests", type=int, required=True)
    probe.add_argument("--confirm-max-cost-usd", required=True)
    probe.add_argument("--cost-source", required=True)
    probe.add_argument(
        "--resume-after-response-format-rejection",
        action="store_true",
        help="Account one prior v2 image[] request rejected with PROVIDER_HTTP_ERROR.",
    )
    probe.add_argument(
        "--resume-after-image-url-rejection",
        action="store_true",
        help="Account one prior v2 image[] request rejected with PROVIDER_IMAGE_URL_INVALID.",
    )
    probe.add_argument(
        "--resume-after-dimension-rejection",
        action="store_true",
        help="Account one prior v2 image[] request rejected with PROVIDER_DIMENSIONS.",
    )
    probe.add_argument(
        "--resume-after-evidence-missing",
        action="store_true",
        help="Account one prior successful v2 image[] probe that lacked durable evidence.",
    )

    qualify_generate = commands.add_parser("qualify-generate")
    qualify_generate.add_argument("--spec", type=Path, required=True)
    qualify_generate.add_argument("--wire-receipt", required=True)
    qualify_generate.add_argument("--operator", required=True)
    qualify_generate.add_argument("--confirm-max-requests", type=int, required=True)
    qualify_generate.add_argument("--confirm-max-cost-usd", required=True)

    qualify_review = commands.add_parser("qualify-review")
    qualify_review.add_argument("--qualification-id", required=True)
    qualify_review.add_argument("--reviewer", required=True)
    qualify_review.add_argument("--scores", type=Path, required=True)

    generate = commands.add_parser("generate")
    generate.add_argument("--spec", type=Path, required=True)
    generate.add_argument("--run-id")

    resume = commands.add_parser("resume")
    resume.add_argument("--run-id", required=True)

    review = commands.add_parser("review-phaser")
    review.add_argument("--run-id", required=True)
    review.add_argument("--reviewer", required=True)

    approve = commands.add_parser("approve")
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--checklist", type=Path, required=True)

    reject = commands.add_parser("reject")
    reject.add_argument("--run-id", required=True)
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--reason", required=True)

    publish = commands.add_parser("publish")
    publish.add_argument("--run-id", required=True)

    recover = commands.add_parser("recover")
    recover.add_argument("--run-id")
    recover.add_argument("--accept-uncertain-cost", action="store_true")
    recover.add_argument("--confirm-stage", choices=("anchor", "down", "left", "right", "up"))
    recover.add_argument("--confirm-attempt-id")
    recover.add_argument("--confirm-stage-request-count", type=int)
    recover.add_argument("--reviewer")
    return parser


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise CLIError(EXIT_VALIDATION, "INPUT_TOO_LARGE", "input JSON exceeds 1 MiB")
        return json.loads(raw)
    except CLIError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(EXIT_VALIDATION, "INPUT_INVALID", "input JSON is unavailable or invalid") from exc


def _validated_payload(args: argparse.Namespace) -> Any:
    command = args.command
    for field_name in ("operator", "reviewer"):
        if hasattr(args, field_name):
            value = getattr(args, field_name)
            if value is None:
                continue
            if value != value.strip() or not 1 <= len(value) <= 80 or any(ord(char) < 32 for char in value):
                raise CLIError(
                    EXIT_VALIDATION, "OPERATOR_INVALID", f"{field_name} must be canonical text"
                )
    if command in {"probe-wire", "qualify-generate", "generate"}:
        if command == "probe-wire":
            resume_flags = (
                args.resume_after_response_format_rejection,
                args.resume_after_image_url_rejection,
                args.resume_after_dimension_rejection,
                args.resume_after_evidence_missing,
            )
            if sum(resume_flags) > 1:
                raise CLIError(
                    EXIT_VALIDATION,
                    "PROBE_RESUME_INVALID",
                    "only one prior probe failure may be reconciled",
                )
            _capability_cost_authorization(args)
        elif command == "qualify-generate":
            _parse_confirmed_cost(args.confirm_max_cost_usd)
        return ResidentSpriteRequest.model_validate(_read_json(args.spec))
    if command == "qualify-review":
        scores = BlindScoreFile.model_validate(_read_json(args.scores))
        validate_run_id(args.qualification_id)
        if scores.qualification_id != args.qualification_id:
            raise CLIError(
                EXIT_VALIDATION, "QUALIFICATION_ID_MISMATCH", "score qualification ID does not match"
            )
        return scores
    if hasattr(args, "run_id") and args.run_id is not None:
        validate_run_id(args.run_id)
    if command == "approve":
        checklist = ApprovalChecklist.model_validate(_read_json(args.checklist))
        if checklist.run_id != args.run_id or not all(checklist.checks.model_dump().values()):
            raise CLIError(
                EXIT_APPROVAL, "APPROVAL_INVALID", "approval checklist is incomplete or mismatched"
            )
        return checklist
    if command == "reject" and not 1 <= len(args.reason.strip()) <= 1000:
        raise CLIError(EXIT_VALIDATION, "REJECTION_REASON_INVALID", "rejection reason is invalid")
    return None


def _parse_confirmed_cost(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CLIError(
            EXIT_VALIDATION, "PAID_CONFIRMATION_INVALID", "confirmed capability cost is invalid"
        ) from exc
    if not parsed.is_finite() or parsed <= 0 or parsed.as_tuple().exponent < -6:
        raise CLIError(
            EXIT_VALIDATION, "PAID_CONFIRMATION_INVALID", "confirmed capability cost is invalid"
        )
    return parsed


def _capability_cost_authorization(args: argparse.Namespace) -> CapabilityCostAuthorization:
    try:
        return CapabilityCostAuthorization(
            max_requests=args.confirm_max_requests,
            price_per_request_upper_bound_usd=args.price_per_request_usd,
            max_cost_usd=args.confirm_max_cost_usd,
            cost_source=args.cost_source,
        )
    except ValidationError as exc:
        raise CLIError(
            EXIT_VALIDATION,
            "PAID_CONFIRMATION_INVALID",
            "capability request and cost authorization is invalid",
        ) from exc


def _confirm_qualification_paid(
    args: argparse.Namespace, authorization: CapabilityCostAuthorization
) -> None:
    confirmed_cost = _parse_confirmed_cost(args.confirm_max_cost_usd)
    if (
        args.confirm_max_requests != authorization.max_requests
        or confirmed_cost != Decimal(authorization.max_cost_usd)
    ):
        raise CLIError(
            EXIT_VALIDATION,
            "PAID_CONFIRMATION_MISMATCH",
            "qualification confirmation must match the frozen wire authorization",
        )


def _required_env(name: str, *, exit_code: int = EXIT_VALIDATION) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip() or value != value.strip():
        raise CLIError(exit_code, "CONFIG_REQUIRED", f"required configuration is missing: {name}")
    return value


def _configured_root(name: str, *, create: bool, exit_code: int) -> Path:
    raw = _required_env(name, exit_code=exit_code)
    path = Path(raw)
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        path = validate_non_symlink_path(path, must_exist=True)
    except (OSError, ResidentSpriteContractError) as exc:
        raise CLIError(exit_code, "CONFIG_PATH_INVALID", f"configured path is invalid: {name}") from exc
    if not path.is_dir():
        raise CLIError(exit_code, "CONFIG_PATH_INVALID", f"configured path is not a directory: {name}")
    return path


def _configured_root_with_legacy(
    name: str,
    legacy_name: str,
    *,
    create: bool,
    exit_code: int,
) -> Path:
    selected = name if name in os.environ else legacy_name
    return _configured_root(selected, create=create, exit_code=exit_code)


def _artifact_root(*, create: bool, exit_code: int) -> Path:
    return _configured_root_with_legacy(
        ENV_ARTIFACT_DIR, ENV_ARTIFACT_ROOT, create=create, exit_code=exit_code
    )


def _static_root(*, create: bool, exit_code: int) -> Path:
    if ENV_STATIC_DIR not in os.environ and ENV_STATIC_ROOT not in os.environ:
        path = Path("static")
        try:
            if create:
                path.mkdir(parents=True, exist_ok=True)
            path = validate_non_symlink_path(path, must_exist=True)
        except (OSError, ResidentSpriteContractError) as exc:
            raise CLIError(
                exit_code, "CONFIG_PATH_INVALID", "default static storage path is invalid"
            ) from exc
        if not path.is_dir():
            raise CLIError(
                exit_code, "CONFIG_PATH_INVALID", "default static storage path is not a directory"
            )
        return path
    return _configured_root_with_legacy(
        ENV_STATIC_DIR, ENV_STATIC_ROOT, create=create, exit_code=exit_code
    )


def _provider_config(expected_model: str) -> ProviderConfig:
    model = _required_env(ENV_PROVIDER_MODEL)
    if model != expected_model:
        raise CLIError(EXIT_VALIDATION, "MODEL_MISMATCH", "provider model does not match the request")
    timeout_raw = os.environ.get(ENV_PROVIDER_TIMEOUT, "180")
    try:
        timeout = float(timeout_raw)
    except ValueError as exc:
        raise CLIError(EXIT_VALIDATION, "CONFIG_INVALID", "provider timeout is invalid") from exc
    if not 1 <= timeout <= 600:
        raise CLIError(EXIT_VALIDATION, "CONFIG_INVALID", "provider timeout must be between 1 and 600 seconds")
    insecure_raw = os.environ.get(ENV_ALLOW_INSECURE_HTTP_TEST, "false").strip().lower()
    if insecure_raw not in {"true", "false"}:
        raise CLIError(
            EXIT_VALIDATION,
            "CONFIG_INVALID",
            "insecure HTTP test flag must be true or false",
        )
    return ProviderConfig(
        base_url=_required_env(ENV_PROVIDER_BASE_URL),
        api_key=_required_env(ENV_PROVIDER_API_KEY),
        model=model,
        timeout=timeout,
        allow_insecure_http_test=insecure_raw == "true",
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_hex_64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEX_64 for char in value)


def _atomic_create(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_non_symlink_path(path.parent, must_exist=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            if path.read_bytes() == data:
                return
        except OSError:
            pass
        raise CLIError(EXIT_VALIDATION, "EVIDENCE_CONFLICT", "immutable evidence already exists")
    write_failed = False
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("write made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
    except Exception:
        write_failed = True
        raise
    finally:
        os.close(fd)
        if write_failed:
            try:
                path.unlink()
            except OSError:
                pass
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _durable_staging_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("write made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, value: Any) -> None:
    _atomic_create(path, canonical_json_bytes(value))


def _load_model(path: Path, model: type[BaseModel], *, code: str) -> BaseModel:
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("stored evidence is oversized")
        return model.model_validate_json(raw)
    except (OSError, ValidationError, ResidentSpriteContractError, TypeError, ValueError) as exc:
        raise CLIError(EXIT_VALIDATION, code, "stored evidence failed strict validation") from exc


def _contract(config: ProviderConfig, multipart_field: str) -> CapabilityContract:
    return CapabilityContract(
        normalized_origin=config.normalized_origin,
        model_alias=config.model,
        transport_security=config.transport_security,
        multipart_field=multipart_field,
    )


def _wire_path(root: Path, receipt_id: str) -> Path:
    if not _is_hex_64(receipt_id):
        raise CLIError(EXIT_VALIDATION, "WIRE_RECEIPT_ID_INVALID", "wire receipt ID is invalid")
    return root / "wire" / f"{receipt_id}.json"


def _qualification_path(root: Path, qualification_id: str) -> Path:
    validate_run_id(qualification_id)
    return root / "qualifications" / qualification_id


def _capability_context() -> tuple[QualifiedSpriteCapability, ProviderConfig]:
    receipt_path = Path(_required_env(ENV_CAPABILITY_RECEIPT, exit_code=EXIT_POSTPROCESS))
    try:
        validate_non_symlink_path(receipt_path, must_exist=True)
    except ResidentSpriteContractError as exc:
        raise CLIError(EXIT_POSTPROCESS, "CAPABILITY_RECEIPT_INVALID", "capability receipt path is invalid") from exc
    receipt = _load_model(receipt_path, CapabilityReceipt, code="CAPABILITY_RECEIPT_INVALID")
    assert isinstance(receipt, CapabilityReceipt)
    config = _provider_config(receipt.model_alias)
    contract = CapabilityContract.model_validate(
        {name: getattr(receipt, name) for name in CapabilityContract.model_fields}
    )
    revocation_root_raw = os.environ.get(ENV_REVOCATION_ROOT)
    if revocation_root_raw:
        revocation_root = Path(revocation_root_raw)
    else:
        revocation_root = receipt_path.parent / "revocations"
    revocation_path = revocation_root / f"{receipt.receipt_id}.json"
    capability = QualifiedSpriteCapability(
        receipt=receipt,
        contract=contract,
        revocation_path=revocation_path,
        clock=lambda: datetime.now(timezone.utc),
    )
    validate_capability_receipt(receipt, datetime.now(timezone.utc), contract, revocation_path)
    return capability, config


async def _probe_wire(args: argparse.Namespace, request: ResidentSpriteRequest) -> dict[str, Any]:
    cost_authorization = _capability_cost_authorization(args)
    config = _provider_config(request.model)
    root = _configured_root(ENV_QUALIFICATION_ROOT, create=True, exit_code=EXIT_VALIDATION)
    async with httpx.AsyncClient() as http_client:
        provider = ResidentSpriteProvider(config, http_client)
        if (
            args.resume_after_response_format_rejection
            or args.resume_after_image_url_rejection
            or args.resume_after_dimension_rejection
            or args.resume_after_evidence_missing
        ):
            result, multipart_field = await provider.probe_wire(
                render_direction_prompt("DOWN"),
                prior_submitted_request_count=1,
            )
        else:
            result, multipart_field = await provider.probe_wire(
                render_direction_prompt("DOWN")
            )
    if result.provider_request_id is None:
        raise CLIError(
            EXIT_PROVIDER,
            "PROVIDER_EVIDENCE_MISSING",
            "provider returned no durable request evidence",
        )
    now = datetime.now(timezone.utc)
    source_sha = _sha256(calibration_anchor_png())
    payload = {
        "schema_version": 1,
        "normalized_origin": config.normalized_origin,
        "model_alias": config.model,
        "transport_security": config.transport_security,
        "multipart_field": multipart_field,
        "request_shape": WireRequestShape(),
        "calibration_source_sha256": source_sha,
        "calibration_output_sha256": _sha256(result.image_bytes),
        "provider_request_ids": [] if result.provider_request_id is None else [result.provider_request_id],
        "submitted_request_count": result.submitted_request_count,
        "reconciled_prior_failure_code": (
            "PROVIDER_HTTP_ERROR" if args.resume_after_response_format_rejection
            else "PROVIDER_IMAGE_URL_INVALID" if args.resume_after_image_url_rejection
            else "PROVIDER_DIMENSIONS" if args.resume_after_dimension_rejection
            else "PROVIDER_EVIDENCE_MISSING" if args.resume_after_evidence_missing
            else None
        ),
        "cost_authorization": cost_authorization,
        "operator": args.operator,
        "observed_at": now,
        "expires_at": now + timedelta(hours=24),
    }
    draft = WireReceipt.model_construct(**payload, wire_receipt_id="")
    receipt = WireReceipt(**payload, wire_receipt_id=content_id(draft, "wire_receipt_id"))
    _write_json(_wire_path(root, receipt.wire_receipt_id), receipt)
    _atomic_create(root / "wire" / f"{receipt.wire_receipt_id}.png", result.image_bytes)
    return {
        "state": "wire_probed",
        "wire_receipt_id": receipt.wire_receipt_id,
        "receipt_path": str(_wire_path(root, receipt.wire_receipt_id)),
        "expires_at": receipt.expires_at,
        "cost_authorization": cost_authorization.model_dump(mode="json"),
    }


async def _qualify_generate(args: argparse.Namespace, request: ResidentSpriteRequest) -> dict[str, Any]:
    config = _provider_config(request.model)
    root = _configured_root(ENV_QUALIFICATION_ROOT, create=True, exit_code=EXIT_VALIDATION)
    receipt = _load_model(_wire_path(root, args.wire_receipt), WireReceipt, code="WIRE_RECEIPT_INVALID")
    assert isinstance(receipt, WireReceipt)
    _confirm_qualification_paid(args, receipt.cost_authorization)
    if receipt.operator != args.operator:
        raise CLIError(EXIT_VALIDATION, "OPERATOR_MISMATCH", "qualification operator does not match the wire probe")
    contract = _contract(config, receipt.multipart_field)
    validate_wire_receipt(receipt, datetime.now(timezone.utc), contract)
    qualification_id = new_run_id()
    qualification_dir = _qualification_path(root, qualification_id)
    qualification_dir.mkdir(parents=True, exist_ok=False)
    budget = QualificationBudget()
    gate = wire_gate(receipt, contract, lambda: datetime.now(timezone.utc))
    try:
        async with httpx.AsyncClient() as http_client:
            provider = ResidentSpriteProvider(config, http_client)
            anchor = await provider.generate_anchor(
                render_anchor_prompt(request.appearance), run_id=qualification_id,
                budget=budget, logical_job="qualification-anchor", gate=gate, allow_retry=False,
            )
            strips = []
            for direction in ("DOWN", "LEFT", "UP"):
                strips.append(await provider.edit_strip(
                    anchor.image_bytes, render_direction_prompt(direction),
                    multipart_field=receipt.multipart_field, run_id=qualification_id,
                    stage=direction.lower(), logical_job=f"qualification-{direction.lower()}",
                    budget=budget, gate=gate, allow_retry=False,
                ))
            oneshot = await provider.generate_oneshot_draft(
                render_qualification_oneshot_prompt(request.appearance),
                run_id=qualification_id, budget=budget, gate=gate,
            )
        atlas = build_resident_sprite_atlas(
            strips[0].image_bytes, strips[1].image_bytes, strips[2].image_bytes
        )
        evidence = [anchor, *strips, oneshot]
        provider_request_ids = [item.provider_request_id for item in evidence]
        if any(request_id is None for request_id in provider_request_ids):
            raise CLIError(
                EXIT_PROVIDER,
                "PROVIDER_EVIDENCE_MISSING",
                "provider returned no durable request evidence",
            )
        files = {
            "anchor.png": anchor.image_bytes,
            "down.png": strips[0].image_bytes,
            "left.png": strips[1].image_bytes,
            "up.png": strips[2].image_bytes,
            "candidate-a.png": atlas,
            "candidate-b.png": oneshot.image_bytes,
        }
        for name, data in files.items():
            _atomic_create(qualification_dir / name, data)
        candidate_ids = [_sha256(atlas)[:16], _sha256(oneshot.image_bytes)[:16]]
        metadata = {
            "schema_version": 1,
            "qualification_id": qualification_id,
            "request": request.model_dump(mode="json"),
            "contract": contract.model_dump(mode="json"),
            "wire_receipt_id": receipt.wire_receipt_id,
            "operator": args.operator,
            "candidate_ids": candidate_ids,
            "evidence_sha256": [_sha256(item.image_bytes) for item in evidence],
            "provider_request_ids": provider_request_ids,
            "latency_ms": [item.latency_ms for item in evidence],
            "cost_authorization": receipt.cost_authorization.model_dump(mode="json"),
            "submitted_request_count": (
                receipt.submitted_request_count + budget.submitted_image_request_count
            ),
        }
        _write_json(qualification_dir / "qualification.json", metadata)
    except Exception:
        shutil.rmtree(qualification_dir, ignore_errors=True)
        raise
    return {
        "state": "qualification_pending_review",
        "qualification_id": qualification_id,
        "candidate_ids": candidate_ids,
        "candidate_paths": [str(qualification_dir / "candidate-a.png"), str(qualification_dir / "candidate-b.png")],
    }


def _qualify_review(args: argparse.Namespace, scores: BlindScoreFile) -> dict[str, Any]:
    root = _configured_root(ENV_QUALIFICATION_ROOT, create=False, exit_code=EXIT_VALIDATION)
    qualification_dir = _qualification_path(root, args.qualification_id)
    metadata = _read_json(qualification_dir / "qualification.json")
    if not isinstance(metadata, dict) or metadata.get("qualification_id") != args.qualification_id:
        raise CLIError(EXIT_VALIDATION, "QUALIFICATION_INVALID", "qualification evidence is invalid")
    expected_candidates = metadata.get("candidate_ids")
    if not isinstance(expected_candidates, list) or {item.candidate_id for item in scores.scores} != set(expected_candidates):
        raise CLIError(EXIT_VALIDATION, "CANDIDATE_ID_MISMATCH", "blind scores do not match qualification candidates")
    if any(
        value < 4
        for score in scores.scores
        for value in (
            score.identity_consistency, score.layout_correctness,
            score.movement_readability, score.visual_fit,
        )
    ):
        raise CLIError(EXIT_APPROVAL, "QUALIFICATION_SCORE_FAILED", "both blind candidates must score at least four in every category")
    try:
        contract = CapabilityContract.model_validate_json(
            json.dumps(metadata["contract"], separators=(",", ":"))
        )
        wire_receipt_id = metadata["wire_receipt_id"]
        operator = metadata["operator"]
        evidence_sha256 = metadata["evidence_sha256"]
        provider_request_ids = metadata["provider_request_ids"]
        latency_ms = metadata["latency_ms"]
        cost_authorization = CapabilityCostAuthorization.model_validate(
            metadata["cost_authorization"]
        )
        submitted_request_count = metadata["submitted_request_count"]
    except (KeyError, ValidationError, TypeError) as exc:
        raise CLIError(EXIT_VALIDATION, "QUALIFICATION_INVALID", "qualification evidence is invalid") from exc
    evidence_files = ("anchor.png", "down.png", "left.png", "up.png", "candidate-b.png")
    try:
        actual_evidence = [_sha256((qualification_dir / name).read_bytes()) for name in evidence_files]
        actual_candidates = [
            _sha256((qualification_dir / "candidate-a.png").read_bytes())[:16],
            _sha256((qualification_dir / "candidate-b.png").read_bytes())[:16],
        ]
    except OSError as exc:
        raise CLIError(EXIT_VALIDATION, "QUALIFICATION_EVIDENCE_MISSING", "qualification evidence is unavailable") from exc
    if actual_evidence != evidence_sha256 or actual_candidates != expected_candidates:
        raise CLIError(EXIT_VALIDATION, "QUALIFICATION_EVIDENCE_CORRUPT", "qualification evidence failed integrity validation")
    wire_receipt = _load_model(_wire_path(root, wire_receipt_id), WireReceipt, code="WIRE_RECEIPT_INVALID")
    assert isinstance(wire_receipt, WireReceipt)
    if wire_receipt.operator != operator:
        raise CLIError(EXIT_VALIDATION, "QUALIFICATION_INVALID", "qualification operator does not match wire evidence")
    if (
        cost_authorization != wire_receipt.cost_authorization
        or submitted_request_count != wire_receipt.submitted_request_count + 5
    ):
        raise CLIError(
            EXIT_VALIDATION,
            "QUALIFICATION_COST_EVIDENCE_INVALID",
            "qualification request count or cost evidence differs from wire authorization",
        )
    now = datetime.now(timezone.utc)
    payload = {
        **contract.model_dump(mode="python"),
        "schema_version": 1,
        "wire_receipt_id": wire_receipt_id,
        "probe_id": wire_receipt_id,
        "qualification_id": args.qualification_id,
        "operator": operator,
        "reviewer": args.reviewer,
        "qualified_at": now,
        "expires_at": now + timedelta(days=30),
        "evidence_sha256": evidence_sha256,
        "provider_request_ids": [
            *wire_receipt.provider_request_ids,
            *provider_request_ids,
        ],
        "blind_scores": scores.scores,
        "latency_ms": latency_ms,
        "capability_request_count": submitted_request_count,
        "capability_cost_upper_bound_usd": cost_authorization.max_cost_usd,
        "cost_source": cost_authorization.cost_source,
    }
    draft = CapabilityReceipt.model_construct(**payload, receipt_id="")
    receipt = CapabilityReceipt(**payload, receipt_id=content_id(draft, "receipt_id"))
    receipt_path = root / "capabilities" / f"{receipt.receipt_id}.json"
    _write_json(receipt_path, receipt)
    _write_json(qualification_dir / "scores.json", scores)
    return {
        "state": "qualified",
        "receipt_id": receipt.receipt_id,
        "receipt_path": str(receipt_path),
        "expires_at": receipt.expires_at,
    }


async def _generate(request: ResidentSpriteRequest, *, run_id: str | None, retry_failed: bool) -> dict[str, Any]:
    artifact_root = _artifact_root(create=True, exit_code=EXIT_POSTPROCESS)
    capability, config = _capability_context()
    async with httpx.AsyncClient() as http_client:
        provider = ResidentSpriteProvider(config, http_client)
        result = await generate_resident_sprite(
            request, client=provider, artifact_root=artifact_root, run_id=run_id,
            capability=capability, retry_failed=retry_failed,
        )
    return result.model_dump(mode="json")


async def _resume(args: argparse.Namespace) -> dict[str, Any]:
    artifact_root = _artifact_root(create=False, exit_code=EXIT_POSTPROCESS)
    manifest = load_run(artifact_root, args.run_id)
    return await _generate(manifest.request, run_id=args.run_id, retry_failed=True)


def _review_phaser(args: argparse.Namespace) -> dict[str, Any]:
    root = _artifact_root(create=False, exit_code=EXIT_APPROVAL)
    manifest = load_run(root, args.run_id)
    if manifest.state not in {"auto_qc_passed", "candidate_ready", "phaser_reviewed"}:
        raise CLIError(EXIT_APPROVAL, "REVIEW_STATE_INVALID", "candidate is not ready for Phaser review")
    read_artifact(root, args.run_id, "candidate/texture.png")
    read_artifact(root, args.run_id, "candidate/portrait.png")
    qc = json.loads(read_artifact(root, args.run_id, "candidate/qc.json"))
    if not isinstance(qc, dict) or qc.get("passed") is not True or qc.get("findings") != []:
        raise CLIError(EXIT_APPROVAL, "QC_NOT_PASSED", "candidate has not passed automatic QC")
    if manifest.state == "auto_qc_passed":
        manifest = advance_stage(root, args.run_id, stage="candidate", state="candidate_ready")
    evidence = {"schema_version": 1, "run_id": args.run_id, "reviewer": args.reviewer, "reviewed_at": datetime.now(timezone.utc)}
    write_canonical_json_artifact(root, args.run_id, "review/phaser.json", evidence)
    if manifest.state == "candidate_ready":
        manifest = advance_stage(root, args.run_id, stage="phaser_review", state="phaser_reviewed")
    if manifest.state != "phaser_reviewed":
        raise CLIError(EXIT_APPROVAL, "REVIEW_STATE_INVALID", "candidate is not ready for Phaser review")
    return {"run_id": args.run_id, "state": manifest.state, "reviewer": args.reviewer}


def _approve(args: argparse.Namespace, checklist: ApprovalChecklist) -> dict[str, Any]:
    root = _artifact_root(create=False, exit_code=EXIT_APPROVAL)
    manifest = load_run(root, args.run_id)
    if manifest.state == "human_approved":
        return {"run_id": args.run_id, "state": manifest.state, "reviewer": args.reviewer}
    if manifest.state != "phaser_reviewed":
        raise CLIError(EXIT_APPROVAL, "APPROVAL_STATE_INVALID", "candidate is not ready for approval")
    evidence = {**checklist.model_dump(mode="json"), "reviewer": args.reviewer, "approved_at": datetime.now(timezone.utc)}
    write_canonical_json_artifact(root, args.run_id, "review/approval.json", evidence)
    if manifest.state == "phaser_reviewed":
        manifest = advance_stage(root, args.run_id, stage="human_approval", state="human_approved")
    if manifest.state != "human_approved":
        raise CLIError(EXIT_APPROVAL, "APPROVAL_STATE_INVALID", "candidate is not ready for approval")
    return {"run_id": args.run_id, "state": manifest.state, "reviewer": args.reviewer}


def _reject(args: argparse.Namespace) -> dict[str, Any]:
    root = _artifact_root(create=False, exit_code=EXIT_APPROVAL)
    manifest = load_run(root, args.run_id)
    if manifest.state == "quarantined":
        return {"run_id": args.run_id, "state": manifest.state}
    if manifest.state not in {"auto_qc_passed", "candidate_ready", "phaser_reviewed"}:
        raise CLIError(EXIT_APPROVAL, "REJECTION_STATE_INVALID", "candidate is not ready for rejection")
    write_canonical_json_artifact(root, args.run_id, "review/rejection.json", {
        "schema_version": 1, "run_id": args.run_id, "reviewer": args.reviewer,
        "reason": args.reason.strip(), "rejected_at": datetime.now(timezone.utc),
    })
    manifest = advance_stage(
        root, args.run_id, stage="quarantine", state="quarantined",
        error=SanitizedError(code="HUMAN_REJECTED", message="candidate was rejected during human review"),
    )
    return {"run_id": args.run_id, "state": manifest.state}


def _publication_target(static_root: Path, asset_key: str, run_id: str) -> Path:
    validate_run_id(run_id)
    root = validate_non_symlink_path(static_root, must_exist=True)
    target = root / "resident-sprites" / asset_key / run_id
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise CLIError(EXIT_PUBLISH, "PUBLISH_PATH_INVALID", "publication path escapes static storage") from exc
    if relative.parts != ("resident-sprites", asset_key, run_id):
        raise CLIError(EXIT_PUBLISH, "PUBLISH_PATH_INVALID", "publication path is not canonical")
    return target


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _publication_files(
    metadata: dict[str, Any], texture: bytes, portrait: bytes
) -> dict[str, bytes]:
    return {
        "texture.png": texture,
        "portrait.png": portrait,
        "publication.json": canonical_json_bytes(metadata),
    }


def _validate_publication_directory(
    target: Path,
    expected_files: Mapping[str, bytes],
    *,
    error_code: str,
) -> None:
    try:
        validate_non_symlink_path(target, must_exist=True)
        if not stat.S_ISDIR(target.lstat().st_mode):
            raise ValueError("publication target is not a directory")
        entries = {entry.name: entry for entry in target.iterdir()}
        if set(entries) != set(expected_files):
            raise ValueError("publication target has unexpected entries")
        for name, expected in expected_files.items():
            entry = entries[name]
            if not stat.S_ISREG(entry.lstat().st_mode) or entry.read_bytes() != expected:
                raise ValueError("publication file does not match")
    except (OSError, ResidentSpriteContractError, ValueError) as exc:
        raise CLIError(EXIT_PUBLISH, error_code, "publication target conflicts with expected content") from exc


def _remove_publication_directory(
    static_root: Path,
    target: Path,
    expected_files: Mapping[str, bytes],
) -> None:
    canonical_target = _publication_target(
        static_root,
        target.parent.name,
        target.name,
    )
    if canonical_target != target:
        raise CLIError(EXIT_PUBLISH, "RECOVERY_PATH_INVALID", "recovery target is not canonical")
    _validate_publication_directory(
        target, expected_files, error_code="RECOVERY_TARGET_CONFLICT"
    )
    shutil.rmtree(target)
    _fsync_directory(target.parent)


def _publish(args: argparse.Namespace) -> dict[str, Any]:
    root = _artifact_root(create=False, exit_code=EXIT_PUBLISH)
    static_root = _static_root(create=True, exit_code=EXIT_PUBLISH)
    manifest = load_run(root, args.run_id)
    asset_key = manifest.request.asset_key
    target = _publication_target(static_root, asset_key, args.run_id)
    if manifest.state == "published":
        try:
            publication = json.loads(read_artifact(root, args.run_id, "publish/publication.json"))
        except (OSError, ValueError, ResidentSpriteContractError) as exc:
            raise CLIError(EXIT_PUBLISH, "PUBLISHED_ASSET_INVALID", "published assets failed integrity validation") from exc
        if not isinstance(publication, dict):
            raise CLIError(EXIT_PUBLISH, "PUBLISHED_ASSET_INVALID", "published assets failed integrity validation")
        texture = read_artifact(root, args.run_id, "candidate/texture.png")
        portrait = read_artifact(root, args.run_id, "candidate/portrait.png")
        expected_metadata = {
            "schema_version": 1, "run_id": args.run_id, "asset_key": asset_key,
            "texture_sha256": _sha256(texture), "portrait_sha256": _sha256(portrait),
        }
        if publication != expected_metadata:
            raise CLIError(EXIT_PUBLISH, "PUBLISHED_ASSET_INVALID", "published assets failed integrity validation")
        _validate_publication_directory(
            target,
            _publication_files(publication, texture, portrait),
            error_code="PUBLISHED_ASSET_INVALID",
        )
        return {"run_id": args.run_id, "state": "published", "publish_path": str(target)}
    if manifest.state != "human_approved":
        raise CLIError(EXIT_PUBLISH, "PUBLISH_STATE_INVALID", "candidate is not approved for publication")
    texture = read_artifact(root, args.run_id, "candidate/texture.png")
    portrait = read_artifact(root, args.run_id, "candidate/portrait.png")
    metadata = {
        "schema_version": 1, "run_id": args.run_id, "asset_key": asset_key,
        "texture_sha256": _sha256(texture), "portrait_sha256": _sha256(portrait),
    }
    expected_files = _publication_files(metadata, texture, portrait)
    target.parent.mkdir(parents=True, exist_ok=True)
    validate_non_symlink_path(target.parent, must_exist=True)
    target_preexisting = _path_exists(target)
    if target_preexisting:
        _validate_publication_directory(
            target, expected_files, error_code="PUBLISH_TARGET_CONFLICT"
        )
        temporary = None
    else:
        temporary = Path(tempfile.mkdtemp(prefix=f".{args.run_id}-", dir=target.parent))
    publishing = False
    target_installed = False
    try:
        if temporary is not None:
            for name, data in expected_files.items():
                _durable_staging_file(temporary / name, data)
            _fsync_directory(temporary)
        write_canonical_json_artifact(root, args.run_id, "publish/publication.json", metadata)
        advance_stage(root, args.run_id, stage="publish_start", state="publishing")
        publishing = True
        if temporary is not None:
            try:
                os.replace(temporary, target)
                target_installed = True
                _fsync_directory(target.parent)
            except OSError:
                if not _path_exists(target):
                    raise
                _validate_publication_directory(
                    target, expected_files, error_code="PUBLISH_TARGET_CONFLICT"
                )
                shutil.rmtree(temporary)
                temporary = None
                _fsync_directory(target.parent)
        manifest = advance_stage(root, args.run_id, stage="publish", state="published")
    except Exception as exc:
        if temporary is not None and _path_exists(temporary):
            shutil.rmtree(temporary, ignore_errors=True)
        cleanup_complete = True
        if target_installed and _path_exists(target):
            try:
                _remove_publication_directory(static_root, target, expected_files)
            except CLIError:
                cleanup_complete = False
        if publishing and cleanup_complete:
            advance_stage(root, args.run_id, stage="rollback", state="rolled_back")
        if isinstance(exc, (CLIError, ResidentSpriteContractError)):
            raise
        raise CLIError(EXIT_PUBLISH, "PUBLISH_FAILED", "publication failed") from exc
    return {"run_id": args.run_id, "state": manifest.state, "publish_path": str(target)}


def _recover_one(root: Path, run_id: str) -> dict[str, Any]:
    manifest = load_run(root, run_id)
    action = "none"
    if manifest.active_claim is not None:
        claim = manifest.active_claim
        if datetime.now(timezone.utc) >= claim.expires_at:
            outcome = release_expired_claim_if_safe(
                root,
                run_id,
                stage=claim.stage,
                owner=claim.owner,
                attempt_id=claim.attempt_id,
                now=datetime.now(timezone.utc),
            )
            manifest = outcome.manifest
            if outcome.action == "orphan_reconciliation_required":
                return {
                    "run_id": run_id,
                    "state": manifest.state,
                    "action": outcome.action,
                    "stage": outcome.stage,
                    "expected_artifact_path": outcome.expected_artifact_path,
                }
            if outcome.action == "external_request_status_uncertain":
                return {
                    "run_id": run_id,
                    "state": manifest.state,
                    "action": outcome.action,
                    "stage": outcome.stage,
                    "request_count": outcome.stage_request_count,
                }
            action = outcome.action
        else:
            action = "claim_active"
    if manifest.state == "publishing":
        static_root = _static_root(create=False, exit_code=EXIT_PUBLISH)
        target = _publication_target(static_root, manifest.request.asset_key, run_id)
        if _path_exists(target):
            metadata = json.loads(
                read_artifact(root, run_id, "publish/publication.json")
            )
            if not isinstance(metadata, dict):
                raise CLIError(EXIT_PUBLISH, "RECOVERY_EVIDENCE_INVALID", "publication recovery evidence is invalid")
            texture = read_artifact(root, run_id, "candidate/texture.png")
            portrait = read_artifact(root, run_id, "candidate/portrait.png")
            expected_metadata = {
                "schema_version": 1, "run_id": run_id,
                "asset_key": manifest.request.asset_key,
                "texture_sha256": _sha256(texture),
                "portrait_sha256": _sha256(portrait),
            }
            if metadata != expected_metadata:
                raise CLIError(EXIT_PUBLISH, "RECOVERY_EVIDENCE_INVALID", "publication recovery evidence is invalid")
            _remove_publication_directory(
                static_root,
                target,
                _publication_files(metadata, texture, portrait),
            )
        manifest = advance_stage(root, run_id, stage="rollback", state="rolled_back")
        action = "publishing_rolled_back"
    elif manifest.state in {"failed", "interrupted"}:
        action = "resume_required"
    elif manifest.state == "quarantined":
        action = "human_review_required"
    return {"run_id": run_id, "state": manifest.state, "action": action}


def _recover(args: argparse.Namespace) -> dict[str, Any]:
    root = _artifact_root(create=False, exit_code=EXIT_PUBLISH)
    if args.accept_uncertain_cost:
        if (
            args.run_id is None
            or args.confirm_stage is None
            or args.confirm_attempt_id is None
            or args.confirm_stage_request_count is None
            or args.reviewer is None
        ):
            raise CLIError(
                EXIT_VALIDATION,
                "UNCERTAIN_RECOVERY_CONFIRMATION_REQUIRED",
                "uncertain recovery requires exact run, stage, attempt, count, and reviewer",
            )
        manifest = load_run(root, args.run_id)
        claim = manifest.active_claim
        if claim is None:
            raise CLIError(
                EXIT_VALIDATION, "UNCERTAIN_RECOVERY_CLAIM_MISSING", "run has no active claim"
            )
        updated = acknowledge_uncertain_request_cost(
            root,
            args.run_id,
            stage=args.confirm_stage,
            owner=claim.owner,
            attempt_id=args.confirm_attempt_id,
            expected_stage_request_count=args.confirm_stage_request_count,
            reviewer=args.reviewer,
            now=datetime.now(timezone.utc),
        )
        return {
            "run_id": args.run_id,
            "state": updated.state,
            "action": "uncertain_cost_accepted_for_retry",
            "stage": args.confirm_stage,
            "stage_request_count": args.confirm_stage_request_count,
        }
    if args.run_id is not None:
        return _recover_one(root, args.run_id)
    runs = []
    for manifest_path in sorted(root.glob(f"*/{MANIFEST_NAME}")):
        try:
            runs.append(_recover_one(root, manifest_path.parent.name))
        except (ResidentSpriteContractError, OSError):
            runs.append({"run_id": manifest_path.parent.name, "state": "invalid", "action": "manual_recovery_required"})
    return {"runs": runs}


def _default_handler(command: str) -> Handler:
    handlers: dict[str, Handler] = {
        "probe-wire": _probe_wire,
        "qualify-generate": _qualify_generate,
        "qualify-review": _qualify_review,
        "generate": lambda args, request: _generate(
            request, run_id=args.run_id, retry_failed=False
        ),
        "resume": lambda args, payload: _resume(args),
        "review-phaser": lambda args, payload: _review_phaser(args),
        "approve": _approve,
        "reject": lambda args, payload: _reject(args),
        "publish": lambda args, payload: _publish(args),
        "recover": lambda args, payload: _recover(args),
    }
    return handlers[command]


def _emit_stdout(value: Any) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")


def _emit_error(code: str, message: str) -> None:
    payload = {"error": {"code": code[:100], "message": message[:500]}}
    sys.stderr.buffer.write(canonical_json_bytes(payload) + b"\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    handlers: Mapping[str, Handler] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _validated_payload(args)
        handler = None if handlers is None else handlers.get(args.command)
        if handler is None:
            handler = _default_handler(args.command)
        result = handler(args, payload)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        _emit_stdout(result)
        return EXIT_SUCCESS
    except ProviderError as exc:
        _emit_error(exc.error.code, exc.error.message)
        return EXIT_PROVIDER
    except CLIError as exc:
        _emit_error(exc.code, str(exc))
        return exc.exit_code
    except ResidentSpriteContractError as exc:
        exit_code = (
            EXIT_POSTPROCESS if args.command in {"generate", "resume"}
            else EXIT_APPROVAL if args.command in {"review-phaser", "approve", "reject"}
            else EXIT_PUBLISH if args.command in {"publish", "recover"}
            else EXIT_VALIDATION
        )
        _emit_error(exc.code, str(exc))
        return exit_code
    except (ValidationError, TypeError, ValueError):
        _emit_error("VALIDATION_FAILED", "input failed strict contract validation")
        return EXIT_VALIDATION
    except OSError:
        exit_code = (
            EXIT_POSTPROCESS if args.command in {"generate", "resume"}
            else EXIT_APPROVAL if args.command in {"review-phaser", "approve", "reject"}
            else EXIT_PUBLISH if args.command in {"publish", "recover"}
            else EXIT_VALIDATION
        )
        _emit_error("IO_FAILED", "configured storage operation failed")
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
