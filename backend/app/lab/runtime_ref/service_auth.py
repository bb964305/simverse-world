"""Scoped service authentication helpers for the protocol-v2 Runtime.

The Runtime has its own audience and symmetric key ring.  A token is useful only
for the run/session/epoch/action tuple carried by its claims; callers must still
bind those claims to the validated request before looking up a session.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


MAX_REQUEST_BYTES = 256 * 1024
MAX_TOKEN_BYTES = 16 * 1024
_ALGORITHM = "HS256"


class ServiceAuthError(ValueError):
    """Base error with an HTTP-compatible status and a content-free reason."""

    status_code = 401

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ServiceAuthenticationError(ServiceAuthError):
    """The token itself is absent, malformed, expired, or untrusted."""


class ServiceAuthorizationError(ServiceAuthError):
    """A valid token is not authorized for the request binding."""

    status_code = 403


class RequestSchemaError(ValueError):
    """A canonical request cannot be represented within the wire limit."""


class StrictRequestModel(BaseModel):
    """Base class for versioned Runtime commands with no silent extra fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ServiceClaims(BaseModel):
    """Approved-v10 claim set accepted by the Runtime audience."""

    model_config = ConfigDict(extra="forbid", strict=True)

    iss: str = Field(min_length=1)
    aud: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    epoch: int = Field(ge=0)
    actions: list[str] = Field(min_length=1)
    jti: str = Field(min_length=1)
    nbf: int
    exp: int

    @field_validator("actions")
    @classmethod
    def _validate_actions(cls, actions: list[str]) -> list[str]:
        if any(not action for action in actions) or len(set(actions)) != len(actions):
            raise ValueError("actions must be non-empty and unique")
        return actions

    @model_validator(mode="after")
    def _validate_lifetime(self) -> "ServiceClaims":
        if self.exp <= self.nbf:
            raise ValueError("exp must be greater than nbf")
        return self


@dataclass(frozen=True)
class ServiceAuthConfig:
    issuer: str
    audience: str
    keys: Mapping[str, str]
    leeway_seconds: int = 0
    max_lifetime_seconds: int = 900

    def __post_init__(self) -> None:
        if (
            not isinstance(self.issuer, str)
            or not self.issuer
            or not isinstance(self.audience, str)
            or not self.audience
        ):
            raise ValueError("service auth issuer and audience are required")
        if not isinstance(self.keys, Mapping) or not self.keys or any(
            not isinstance(kid, str)
            or not kid
            or not isinstance(key, str)
            or not key
            or len(key.encode("utf-8")) < 32
            for kid, key in self.keys.items()
        ):
            raise ValueError(
                "service auth key ring must contain named keys of at least 32 bytes"
            )
        if (
            isinstance(self.leeway_seconds, bool)
            or not isinstance(self.leeway_seconds, int)
            or self.leeway_seconds < 0
        ):
            raise ValueError("service auth leeway must be non-negative")
        if (
            isinstance(self.max_lifetime_seconds, bool)
            or not isinstance(self.max_lifetime_seconds, int)
            or self.max_lifetime_seconds <= 0
        ):
            raise ValueError("service auth max lifetime must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ServiceAuthConfig":
        keys = value.get("keys")
        if not isinstance(keys, Mapping):
            raise ValueError("service auth keys must be a mapping")
        return cls(
            issuer=value.get("issuer"),
            audience=value.get("audience"),
            keys=dict(keys),
            leeway_seconds=value.get("leeway_seconds", 0),
            max_lifetime_seconds=value.get("max_lifetime_seconds", 900),
        )


@dataclass(frozen=True)
class ServiceTokenIssuerConfig:
    """The one active signing key for a dedicated service audience."""

    issuer: str
    audience: str
    current_kid: str
    current_key: str
    token_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (self.issuer, self.audience, self.current_kid)
        ):
            raise ValueError("service token issuer, audience, and kid are required")
        if (
            not isinstance(self.current_key, str)
            or len(self.current_key.encode("utf-8")) < 32
        ):
            raise ValueError("service token signing key must be at least 32 bytes")
        if (
            isinstance(self.token_ttl_seconds, bool)
            or not isinstance(self.token_ttl_seconds, int)
            or not 1 <= self.token_ttl_seconds <= 900
        ):
            raise ValueError("service token TTL must be between 1 and 900 seconds")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ServiceTokenIssuerConfig":
        return cls(
            issuer=value.get("issuer"),
            audience=value.get("audience"),
            current_kid=value.get("current_kid"),
            current_key=value.get("current_key"),
            token_ttl_seconds=value.get("token_ttl_seconds", 300),
        )


class ServiceTokenIssuer:
    """Issue short-lived, single-action JWTs bound to one runtime session."""

    def __init__(
        self, config: ServiceTokenIssuerConfig | Mapping[str, Any]
    ) -> None:
        self.config = (
            config
            if isinstance(config, ServiceTokenIssuerConfig)
            else ServiceTokenIssuerConfig.from_mapping(config)
        )

    def issue(
        self,
        *,
        run_id: str,
        session_id: str,
        epoch: int,
        action: str,
        command_id: str | None = None,
        jti: str | None = None,
        issued_at: int | None = None,
    ) -> str:
        """Sign one action token; command retries derive the same stable jti."""

        if command_id is not None and jti is not None:
            raise ValueError("provide command_id or jti, not both")
        if command_id is not None:
            if not isinstance(command_id, str) or not command_id:
                raise ValueError("command_id must be a non-empty string")
            jti = "cmd-" + hashlib.sha256(canonical_json_bytes({
                "issuer": self.config.issuer,
                "audience": self.config.audience,
                "run_id": run_id,
                "session_id": session_id,
                "epoch": epoch,
                "action": action,
                "command_id": command_id,
            })).hexdigest()
        elif jti is None:
            jti = str(uuid.uuid4())

        now = int(time.time()) if issued_at is None else issued_at
        claims = ServiceClaims.model_validate({
            "iss": self.config.issuer,
            "aud": self.config.audience,
            "run_id": run_id,
            "session_id": session_id,
            "epoch": epoch,
            "actions": [action],
            "jti": jti,
            "nbf": now,
            "exp": now + self.config.token_ttl_seconds,
        })
        return jwt.encode(
            claims.model_dump(),
            self.config.current_key,
            algorithm=_ALGORITHM,
            headers={"kid": self.config.current_kid},
        )


@dataclass(frozen=True)
class ServiceBinding:
    run_id: str
    session_id: str
    epoch: int


def extract_bearer_token(authorization: str | None) -> str:
    """Extract one Bearer token without accepting alternate auth schemes."""

    if not authorization:
        raise ServiceAuthenticationError("missing_token")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise ServiceAuthenticationError("malformed_token")
    return token


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_json_bytes(value: Any, *, max_bytes: int = MAX_REQUEST_BYTES) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        encoded = json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RequestSchemaError("request_not_canonical_json") from exc
    if len(encoded) > max_bytes:
        raise RequestSchemaError("request_too_large")
    return encoded


def canonical_request_digest(value: Any, *, max_bytes: int = MAX_REQUEST_BYTES) -> str:
    """Hash the validated command body, excluding transport/auth headers."""

    return hashlib.sha256(canonical_json_bytes(value, max_bytes=max_bytes)).hexdigest()


class ServiceTokenValidator:
    """Validate a per-audience JWT and its request binding fail closed."""

    def __init__(self, config: ServiceAuthConfig | Mapping[str, Any]) -> None:
        self.config = (
            config if isinstance(config, ServiceAuthConfig)
            else ServiceAuthConfig.from_mapping(config)
        )

    def validate(
        self,
        token: str,
        *,
        required_action: str,
        expected_binding: ServiceBinding | None = None,
    ) -> ServiceClaims:
        if not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise ServiceAuthenticationError("malformed_token")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise ServiceAuthenticationError("malformed_token") from exc
        kid = header.get("kid")
        if header.get("alg") != _ALGORITHM or not isinstance(kid, str):
            raise ServiceAuthenticationError("untrusted_key")
        key = self.config.keys.get(kid)
        if not key:
            raise ServiceAuthenticationError("untrusted_key")

        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=[_ALGORITHM],
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=self.config.leeway_seconds,
                options={
                    "require": [
                        "iss", "aud", "run_id", "session_id", "epoch",
                        "actions", "jti", "nbf", "exp",
                    ]
                },
            )
            claims = ServiceClaims.model_validate(payload)
        except jwt.ExpiredSignatureError as exc:
            raise ServiceAuthenticationError("expired_token") from exc
        except jwt.ImmatureSignatureError as exc:
            raise ServiceAuthenticationError("token_not_yet_valid") from exc
        except (jwt.PyJWTError, ValidationError) as exc:
            raise ServiceAuthenticationError("invalid_token") from exc

        if claims.exp - claims.nbf > self.config.max_lifetime_seconds:
            raise ServiceAuthenticationError("invalid_token")
        if claims.actions != [required_action]:
            raise ServiceAuthorizationError("action_not_allowed")
        if expected_binding is not None and (
            claims.run_id != expected_binding.run_id
            or claims.session_id != expected_binding.session_id
            or claims.epoch != expected_binding.epoch
        ):
            raise ServiceAuthorizationError("binding_mismatch")
        return claims
