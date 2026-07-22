"""Detached receipt signing with a replaceable signing boundary."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, TypeVar

from app.lab.artifact_services.canonical import canonical_json_bytes
from app.lab.artifact_services.schemas import ReceiptBase, receipt_signing_payload


ReceiptT = TypeVar("ReceiptT", bound=ReceiptBase)


class ReceiptSignatureError(ValueError):
    pass


class ReceiptSigner(Protocol):
    algorithm: str

    def sign(self, receipt_type: type[ReceiptT], payload: dict) -> ReceiptT: ...


class ReceiptVerifier(Protocol):
    def verify(self, receipt: ReceiptT) -> ReceiptT: ...


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    try:
        if "=" in value:
            raise ValueError("padding is not canonical")
        encoded = value.encode("ascii")
        return base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ReceiptSignatureError("receipt signature is not base64url") from exc


def _validated_signing_payload(
    receipt_type: type[ReceiptT], payload: dict
) -> dict:
    """Normalize datetimes and nested models before producing signed bytes."""
    draft = receipt_type.model_validate(
        {**payload, "signature": _b64url(bytes(32))}
    )
    return receipt_signing_payload(draft)


def _openssl() -> str:
    executable = shutil.which("openssl")
    if executable is None:
        raise ValueError("OpenSSL is required for EdDSA receipt verification")
    return executable


def _readonly_key_path(value: str, *, private: bool) -> Path:
    path = Path(value)
    if not value or not path.is_absolute() or not path.is_file():
        raise ValueError("receipt key must be an absolute existing file")
    mode = path.stat().st_mode
    if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("receipt key must be mounted read-only")
    if private and mode & (stat.S_IRGRP | stat.S_IROTH):
        raise ValueError("receipt private key must not be group/world readable")
    return path


def _validate_openssl_key(path: Path, *, private: bool) -> None:
    command = [_openssl(), "pkey"]
    if not private:
        command.append("-pubin")
    command.extend(("-in", str(path), "-text_pub", "-noout"))
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("receipt key could not be validated") from exc
    if completed.returncode != 0 or b"ED25519" not in completed.stdout.upper():
        raise ValueError("receipt key must be a usable Ed25519 key")


@dataclass(frozen=True)
class HmacReceiptSigner:
    """Reference signer. Production may replace this boundary with KMS/HSM signing."""

    issuer: str
    current_kid: str
    current_key: str
    algorithm = "HS256"

    def __post_init__(self) -> None:
        if not self.issuer or not self.current_kid:
            raise ValueError("receipt issuer and kid are required")
        if len(self.current_key.encode("utf-8")) < 32:
            raise ValueError("receipt signing key must be at least 32 bytes")

    def sign(self, receipt_type: type[ReceiptT], payload: dict) -> ReceiptT:
        body = dict(payload)
        body["algorithm"] = "HS256"
        body["issuer"] = self.issuer
        body["kid"] = self.current_kid
        unsigned = _validated_signing_payload(receipt_type, body)
        signature = hmac.new(
            self.current_key.encode("utf-8"),
            canonical_json_bytes(unsigned),
            hashlib.sha256,
        ).digest()
        return receipt_type.model_validate(
            {**unsigned, "signature": _b64url(signature)}
        )


@dataclass(frozen=True)
class HmacReceiptVerifier:
    issuers: Mapping[str, Mapping[str, str]]

    def verify(self, receipt: ReceiptT) -> ReceiptT:
        if receipt.algorithm != "HS256":
            raise ReceiptSignatureError("unsupported receipt algorithm")
        keys = self.issuers.get(receipt.issuer)
        key = None if keys is None else keys.get(receipt.kid)
        if not key or len(key.encode("utf-8")) < 32:
            raise ReceiptSignatureError("untrusted receipt issuer or key")
        expected = _b64url(
            hmac.new(
                key.encode("utf-8"),
                canonical_json_bytes(receipt_signing_payload(receipt)),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(receipt.signature, expected):
            raise ReceiptSignatureError("receipt signature mismatch")
        return receipt


@dataclass(frozen=True)
class Ed25519ReceiptSigner:
    """Ed25519 signer backed by a read-only external private-key mount."""

    issuer: str
    current_kid: str
    private_key_path: str
    algorithm = "EdDSA"

    def __post_init__(self) -> None:
        if not self.issuer or not self.current_kid:
            raise ValueError("receipt issuer and kid are required")
        path = _readonly_key_path(self.private_key_path, private=True)
        _validate_openssl_key(path, private=True)

    def sign(self, receipt_type: type[ReceiptT], payload: dict) -> ReceiptT:
        body = dict(payload)
        body["algorithm"] = "EdDSA"
        body["issuer"] = self.issuer
        body["kid"] = self.current_kid
        unsigned = _validated_signing_payload(receipt_type, body)
        signing_payload = canonical_json_bytes(unsigned)
        try:
            completed = subprocess.run(
                [
                    _openssl(),
                    "pkeyutl",
                    "-sign",
                    "-rawin",
                    "-inkey",
                    self.private_key_path,
                ],
                input=signing_payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReceiptSignatureError("EdDSA receipt signing failed") from exc
        if completed.returncode != 0 or len(completed.stdout) != 64:
            raise ReceiptSignatureError("EdDSA receipt signing failed")
        return receipt_type.model_validate(
            {**unsigned, "signature": _b64url(completed.stdout)}
        )


@dataclass(frozen=True)
class Ed25519ReceiptVerifier:
    """Verifier whose keyring contains public-key paths, never signing keys."""

    issuers: Mapping[str, Mapping[str, str]]

    def __post_init__(self) -> None:
        for keys in self.issuers.values():
            for path_value in keys.values():
                path = _readonly_key_path(path_value, private=False)
                _validate_openssl_key(path, private=False)

    def verify(self, receipt: ReceiptT) -> ReceiptT:
        if receipt.algorithm != "EdDSA":
            raise ReceiptSignatureError("unsupported receipt algorithm")
        keys = self.issuers.get(receipt.issuer)
        key_path = None if keys is None else keys.get(receipt.kid)
        if not key_path:
            raise ReceiptSignatureError("untrusted receipt issuer or key")
        signature = _b64url_decode(receipt.signature)
        if len(signature) != 64:
            raise ReceiptSignatureError("EdDSA receipt signature length is invalid")
        descriptor, signature_name = tempfile.mkstemp(
            prefix="simverse-receipt-", suffix=".sig"
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(signature)
                target.flush()
                os.fsync(target.fileno())
            try:
                completed = subprocess.run(
                    [
                        _openssl(),
                        "pkeyutl",
                        "-verify",
                        "-pubin",
                        "-rawin",
                        "-inkey",
                        key_path,
                        "-sigfile",
                        signature_name,
                    ],
                    input=canonical_json_bytes(receipt_signing_payload(receipt)),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ReceiptSignatureError(
                    "EdDSA receipt verification failed"
                ) from exc
        finally:
            Path(signature_name).unlink(missing_ok=True)
        if completed.returncode != 0:
            raise ReceiptSignatureError("receipt signature mismatch")
        return receipt
