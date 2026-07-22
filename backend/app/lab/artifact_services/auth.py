"""Per-audience service JWTs and one-object upload capabilities."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.lab.artifact_services.canonical import canonical_digest
from app.lab.artifact_services.schemas import CommandBase, UploadLeaseCommand


_ALGORITHM = "HS256"
MAX_TOKEN_BYTES = 16 * 1024


class ArtifactAuthError(ValueError):
    status_code = 401

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ArtifactAuthorizationError(ArtifactAuthError):
    status_code = 403


class ArtifactClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    iss: str = Field(min_length=1)
    aud: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    producer_action_id: str | None = None
    epoch: int = Field(ge=0)
    action: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    jti: str = Field(min_length=1)
    nbf: int
    exp: int

    @model_validator(mode="after")
    def valid_lifetime(self) -> "ArtifactClaims":
        if self.exp <= self.nbf:
            raise ValueError("exp must be greater than nbf")
        return self


class UploadCapabilityClaims(ArtifactClaims):
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_bytes: int = Field(gt=0)
    content_type: str = Field(min_length=1)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    declared_byte_size: int | None = Field(default=None, ge=0)


@dataclass(frozen=True)
class JwtKeyring:
    issuer: str
    audience: str
    keys: Mapping[str, str]
    leeway_seconds: int = 0
    max_lifetime_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience or not self.keys:
            raise ValueError("JWT issuer, audience, and keyring are required")
        if any(not kid or len(key.encode("utf-8")) < 32 for kid, key in self.keys.items()):
            raise ValueError("JWT keys must be named and at least 32 bytes")
        if self.leeway_seconds < 0 or self.max_lifetime_seconds <= 0:
            raise ValueError("invalid JWT lifetime configuration")


@dataclass(frozen=True)
class JwtIssuerConfig:
    issuer: str
    audience: str
    current_kid: str
    current_key: str
    ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience or not self.current_kid:
            raise ValueError("JWT issuer configuration is incomplete")
        if len(self.current_key.encode("utf-8")) < 32:
            raise ValueError("JWT signing key must be at least 32 bytes")
        if not 1 <= self.ttl_seconds <= 900:
            raise ValueError("JWT TTL must be between 1 and 900 seconds")


@dataclass(frozen=True)
class RequestBinding:
    tenant_id: str
    run_id: str
    session_id: str
    artifact_id: str
    producer_action_id: str | None
    epoch: int
    operation_id: str

    @classmethod
    def from_command(cls, command: CommandBase, *, operation_id: str) -> "RequestBinding":
        return cls(
            tenant_id=command.tenant_id,
            run_id=command.run_id,
            session_id=command.session_id,
            artifact_id=command.artifact_id,
            producer_action_id=command.producer_action_id,
            epoch=command.epoch,
            operation_id=operation_id,
        )


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise ArtifactAuthError("missing_token")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise ArtifactAuthError("malformed_token")
    return token


class ServiceTokenValidator:
    def __init__(self, keyring: JwtKeyring) -> None:
        self.keyring = keyring

    def validate(
        self,
        token: str,
        *,
        action: str,
        binding: RequestBinding | None = None,
        claims_type: type[ArtifactClaims] = ArtifactClaims,
        allow_expired: bool = False,
    ) -> ArtifactClaims:
        if not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise ArtifactAuthError("malformed_token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise ArtifactAuthError("malformed_token") from exc
        kid = header.get("kid")
        key = self.keyring.keys.get(kid) if isinstance(kid, str) else None
        if header.get("alg") != _ALGORITHM or not key:
            raise ArtifactAuthError("untrusted_key")
        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=[_ALGORITHM],
                issuer=self.keyring.issuer,
                audience=self.keyring.audience,
                leeway=self.keyring.leeway_seconds,
                options={
                    "verify_exp": not allow_expired,
                    "require": [
                        "iss", "aud", "tenant_id", "run_id", "session_id",
                        "artifact_id", "epoch", "action", "operation_id", "jti",
                        "nbf", "exp",
                    ]
                },
            )
            claims = claims_type.model_validate(payload)
        except jwt.ExpiredSignatureError as exc:
            raise ArtifactAuthError("expired_token") from exc
        except jwt.ImmatureSignatureError as exc:
            raise ArtifactAuthError("token_not_yet_valid") from exc
        except (jwt.PyJWTError, ValidationError) as exc:
            raise ArtifactAuthError("invalid_token") from exc
        if claims.exp - claims.nbf > self.keyring.max_lifetime_seconds:
            raise ArtifactAuthError("token_lifetime_exceeded")
        if claims.action != action:
            raise ArtifactAuthorizationError("action_mismatch")
        if binding is not None:
            expected = binding.__dict__
            if any(getattr(claims, name) != value for name, value in expected.items()):
                raise ArtifactAuthorizationError("binding_mismatch")
        return claims


class ServiceTokenIssuer:
    def __init__(self, config: JwtIssuerConfig) -> None:
        self.config = config

    def issue(
        self,
        *,
        action: str,
        binding: RequestBinding,
        now: int | None = None,
        extra_claims: Mapping[str, Any] | None = None,
    ) -> str:
        issued_at = int(time.time()) if now is None else now
        stable = {
            "issuer": self.config.issuer,
            "audience": self.config.audience,
            "action": action,
            **binding.__dict__,
        }
        claims = {
            "iss": self.config.issuer,
            "aud": self.config.audience,
            **binding.__dict__,
            "action": action,
            "jti": "op-" + canonical_digest(stable),
            "nbf": issued_at,
            "exp": issued_at + self.config.ttl_seconds,
            **dict(extra_claims or {}),
        }
        ArtifactClaims.model_validate(claims)
        return jwt.encode(
            claims,
            self.config.current_key,
            algorithm=_ALGORITHM,
            headers={"kid": self.config.current_kid},
        )


class UploadCapabilityIssuer:
    def __init__(self, config: JwtIssuerConfig) -> None:
        self.config = config

    def issue(self, command: UploadLeaseCommand, *, now: int | None = None) -> str:
        issued_at = int(time.time()) if now is None else now
        expiry = int(command.expires_at.timestamp())
        if expiry <= issued_at or expiry - issued_at > self.config.ttl_seconds:
            raise ValueError("upload lease expiry is outside the configured short TTL")
        binding = RequestBinding.from_command(command, operation_id=command.upload_id)
        payload = {
            "iss": self.config.issuer,
            "aud": self.config.audience,
            **binding.__dict__,
            "action": "artifact.upload",
            "jti": str(uuid.uuid5(uuid.NAMESPACE_URL, f"artifact-upload:{command.upload_id}")),
            "nbf": issued_at,
            "exp": expiry,
            "command_digest": canonical_digest(command),
            "max_bytes": command.max_bytes,
            "content_type": command.content_type,
            "expected_sha256": command.expected_sha256,
            "declared_byte_size": command.declared_byte_size,
        }
        UploadCapabilityClaims.model_validate(payload)
        return jwt.encode(
            payload,
            self.config.current_key,
            algorithm=_ALGORITHM,
            headers={"kid": self.config.current_kid},
        )
