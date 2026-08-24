"""Versioned AES-256-GCM helper for durable hosted-runner secrets.

Env/config shape:

```
HOSTED_AGENT_RUNNER_ACTIVE_KEY_ID=<kid>
HOSTED_AGENT_RUNNER_KEYRING={"<kid>":"<base64url-32-byte-key>", ...}
```

The active key id selects which entry encrypts *new* rows; older ids stay in
the keyring so historical rows remain decryptable during rotation. Each
ciphertext envelope is versioned and compact:

```
svhr1:<kid>:<nonce_b64url>:<ciphertext_b64url>
```

AAD binds the field to a concrete storage slot so ciphertext cannot be moved
between rows/columns without detection:

* envelope version (`svhr1`)
* key id (`kid`)
* row reference (`table-or-model:id`)
* field name (`provider_api_key`, `play_token`, ...)

This module does not log or repr raw key material.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import dataclass
from typing import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

_ENVELOPE_VERSION = "svhr1"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class HostedRunnerSecretError(ValueError):
    """Hosted-runner secret configuration or envelope is invalid."""


def _secret_text(value: SecretStr | str, *, field_name: str) -> str:
    text = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not isinstance(text, str):
        raise HostedRunnerSecretError(f"{field_name} must be a string")
    return text.strip()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str, *, field_name: str) -> bytes:
    normalized = value.strip()
    if not normalized:
        raise HostedRunnerSecretError(f"{field_name} must not be empty")
    padding = "=" * (-len(normalized) % 4)
    try:
        return base64.urlsafe_b64decode(normalized + padding)
    except Exception as exc:  # pragma: no cover - backend varies by codec impl
        raise HostedRunnerSecretError(f"{field_name} is not valid base64url") from exc


def _build_aad(*, version: str, key_id: str, row_ref: str, field_name: str) -> bytes:
    if not row_ref or not field_name:
        raise HostedRunnerSecretError("row_ref and field_name must not be empty")
    return json.dumps(
        {
            "scope": "hosted-agent-runner",
            "v": version,
            "kid": key_id,
            "row": row_ref,
            "field": field_name,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _decode_key_material(encoded: str, *, key_id: str) -> bytes:
    raw = _b64url_decode(encoded, field_name=f"HOSTED_AGENT_RUNNER_KEYRING[{key_id!r}]")
    if len(raw) != _KEY_BYTES:
        raise HostedRunnerSecretError(
            f"HOSTED_AGENT_RUNNER_KEYRING[{key_id!r}] must decode to exactly "
            f"{_KEY_BYTES} bytes for AES-256-GCM"
        )
    return raw


@dataclass(frozen=True)
class HostedRunnerKeyring:
    active_key_id: str
    _keys: Mapping[str, bytes]

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    def __repr__(self) -> str:
        return (
            "HostedRunnerKeyring("
            f"active_key_id={self.active_key_id!r}, "
            f"key_ids={list(self.key_ids)!r}, "
            f"version={_ENVELOPE_VERSION!r})"
        )

    def encrypt_secret(
        self,
        plaintext: SecretStr | str,
        *,
        row_ref: str,
        field_name: str,
    ) -> str:
        key = self._keys[self.active_key_id]
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(
            nonce,
            _secret_text(plaintext, field_name="plaintext").encode("utf-8"),
            _build_aad(
                version=_ENVELOPE_VERSION,
                key_id=self.active_key_id,
                row_ref=row_ref,
                field_name=field_name,
            ),
        )
        return (
            f"{_ENVELOPE_VERSION}:{self.active_key_id}:"
            f"{_b64url_encode(nonce)}:{_b64url_encode(ciphertext)}"
        )

    def decrypt_secret(
        self,
        envelope: str,
        *,
        row_ref: str,
        field_name: str,
    ) -> SecretStr:
        parts = envelope.split(":")
        if len(parts) != 4:
            raise HostedRunnerSecretError("secret envelope format is invalid")
        version, key_id, nonce_b64, ciphertext_b64 = parts
        if version != _ENVELOPE_VERSION:
            raise HostedRunnerSecretError(f"unsupported secret envelope version: {version}")
        key = self._keys.get(key_id)
        if key is None:
            raise HostedRunnerSecretError(f"no decryption key configured for kid={key_id!r}")
        nonce = _b64url_decode(nonce_b64, field_name="secret nonce")
        if len(nonce) != _NONCE_BYTES:
            raise HostedRunnerSecretError("secret nonce must decode to 12 bytes")
        ciphertext = _b64url_decode(ciphertext_b64, field_name="secret ciphertext")
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                _build_aad(
                    version=version,
                    key_id=key_id,
                    row_ref=row_ref,
                    field_name=field_name,
                ),
            )
        except InvalidTag as exc:
            raise HostedRunnerSecretError(
                "secret authentication failed; check row_ref / field_name / keyring"
            ) from exc
        return SecretStr(plaintext.decode("utf-8"))


def load_hosted_runner_keyring(
    *,
    active_key_id: str,
    keyring_json: SecretStr | str,
) -> HostedRunnerKeyring:
    active = active_key_id.strip()
    if not active:
        raise HostedRunnerSecretError("HOSTED_AGENT_RUNNER_ACTIVE_KEY_ID must not be empty")
    if not _KEY_ID_RE.fullmatch(active):
        raise HostedRunnerSecretError("HOSTED_AGENT_RUNNER_ACTIVE_KEY_ID has an invalid format")

    raw_keyring = _secret_text(
        keyring_json, field_name="HOSTED_AGENT_RUNNER_KEYRING"
    )
    if not raw_keyring:
        raise HostedRunnerSecretError("HOSTED_AGENT_RUNNER_KEYRING must not be empty")
    try:
        parsed = json.loads(raw_keyring)
    except json.JSONDecodeError as exc:
        raise HostedRunnerSecretError(
            "HOSTED_AGENT_RUNNER_KEYRING must be a JSON object of kid->base64url key"
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise HostedRunnerSecretError(
            "HOSTED_AGENT_RUNNER_KEYRING must be a non-empty JSON object"
        )

    decoded: dict[str, bytes] = {}
    for key_id, encoded in parsed.items():
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            raise HostedRunnerSecretError("HOSTED_AGENT_RUNNER_KEYRING contains an invalid key id")
        if not isinstance(encoded, str):
            raise HostedRunnerSecretError(
                f"HOSTED_AGENT_RUNNER_KEYRING[{key_id!r}] must be a base64url string"
            )
        decoded[key_id] = _decode_key_material(encoded, key_id=key_id)

    if active not in decoded:
        raise HostedRunnerSecretError(
            "HOSTED_AGENT_RUNNER_ACTIVE_KEY_ID is not present in HOSTED_AGENT_RUNNER_KEYRING"
        )
    return HostedRunnerKeyring(active_key_id=active, _keys=decoded)
