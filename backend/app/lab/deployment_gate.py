"""Runtime validation of the externally produced Lab D0 release receipt."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping


_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_EXPECTED_SERVICES = frozenset(
    {
        "lab-runtime",
        "lab-executor",
        "artifact-ingest",
        "artifact-scanner",
        "artifact-cleanup",
    }
)
_MAX_RECEIPT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ApprovedDeployment:
    source_sha: str
    request_hash: str
    expires_at: datetime
    service_image_digests: Mapping[str, str]


def _aware_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("D0 release receipt expiry is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("D0 release receipt expiry is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("D0 release receipt expiry must be timezone-aware")
    return parsed.astimezone(UTC)


def require_d0_release_receipt(
    *,
    path_value: str,
    expected_receipt_sha256: str,
    expected_request_hash: str,
    expected_source_sha: str,
) -> ApprovedDeployment:
    if _SHA256.fullmatch(expected_receipt_sha256) is None:
        raise ValueError("LAB_D0_RELEASE_RECEIPT_SHA256 must be a SHA-256 digest")
    if _SHA256.fullmatch(expected_request_hash) is None:
        raise ValueError("LAB_D0_REQUEST_HASH must be a SHA-256 digest")
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", expected_source_sha) is None:
        raise ValueError("LAB_SERVICE_SHA must be a full source digest")
    path = Path(path_value)
    if not path_value or not path.is_file():
        raise ValueError("D0 release-check receipt is missing")
    mode = path.stat().st_mode
    if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("D0 release-check receipt must be mounted read-only")
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_RECEIPT_BYTES:
        raise ValueError("D0 release-check receipt size is invalid")
    actual_digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_receipt_sha256):
        raise ValueError("D0 release-check receipt digest mismatch")
    try:
        receipt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("D0 release-check receipt is not valid JSON") from exc
    if not isinstance(receipt, dict) or receipt.get("ok") is not True:
        raise ValueError("D0 release-check receipt does not approve this deployment")
    if receipt.get("sha") != expected_source_sha:
        raise ValueError("D0 release-check receipt source SHA mismatch")
    d0 = receipt.get("d0")
    if not isinstance(d0, dict) or d0.get("request_hash") != expected_request_hash:
        raise ValueError("D0 release-check receipt request binding mismatch")
    expires_at = _aware_time(d0.get("expires_at"))
    if expires_at <= datetime.now(UTC):
        raise ValueError("D0 release-check receipt has expired")
    services = d0.get("services")
    if (
        not isinstance(services, dict)
        or set(services) != _EXPECTED_SERVICES
        or any(
            not isinstance(value, str) or _IMAGE_DIGEST.fullmatch(value) is None
            for value in services.values()
        )
    ):
        raise ValueError("D0 release-check receipt service digest set is invalid")
    return ApprovedDeployment(
        source_sha=expected_source_sha,
        request_hash=expected_request_hash,
        expires_at=expires_at,
        service_image_digests=dict(services),
    )
