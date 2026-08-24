import base64
import json

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.services.hosted_agent_runner_crypto import (
    HostedRunnerSecretError,
    load_hosted_runner_keyring,
)


def _encoded_key(seed: bytes) -> str:
    assert len(seed) == 32
    return base64.urlsafe_b64encode(seed).decode("ascii").rstrip("=")


_KID_OLD = "2026-08-14"
_KID_NEW = "2026-09-01"
_KEY_OLD = _encoded_key(bytes(range(32)))
_KEY_NEW = _encoded_key(bytes(range(31, -1, -1)))


def _keyring_json() -> str:
    return json.dumps({_KID_OLD: _KEY_OLD, _KID_NEW: _KEY_NEW})


def test_disabled_hosted_runner_can_boot_without_keys():
    settings = Settings(
        debug=True,
        hosted_agent_runner_enabled=False,
        hosted_agent_runner_active_key_id="",
        hosted_agent_runner_keyring="",
        _env_file=None,
    )
    assert settings.hosted_agent_runner_secret_keyring is None


def test_enabled_hosted_runner_rejects_missing_keyring():
    with pytest.raises(ValidationError, match="HOSTED_AGENT_RUNNER"):
        Settings(
            debug=True,
            hosted_agent_runner_enabled=True,
            hosted_agent_runner_active_key_id=_KID_OLD,
            hosted_agent_runner_keyring="",
            _env_file=None,
        )


def test_enabled_hosted_runner_rejects_invalid_keyring_shape():
    with pytest.raises(ValidationError, match="HOSTED_AGENT_RUNNER"):
        Settings(
            debug=True,
            hosted_agent_runner_enabled=True,
            hosted_agent_runner_active_key_id=_KID_OLD,
            hosted_agent_runner_keyring='{"bad kid":"abc"}',
            _env_file=None,
        )


def test_enabled_production_runner_accepts_only_private_internal_api_service():
    strong_jwt = "hosted-runner-test-jwt-secret-that-is-long-enough"
    settings = Settings(
        debug=False,
        jwt_secret=strong_jwt,
        hosted_agent_runner_enabled=True,
        hosted_agent_runner_active_key_id=_KID_OLD,
        hosted_agent_runner_keyring=_keyring_json(),
        hosted_agent_runner_allowed_hosts=["api.openai.com"],
        hosted_agent_runner_internal_api_base="http://api:8000",
        _env_file=None,
    )
    assert settings.hosted_agent_runner_internal_api_base == "http://api:8000"

    for unsafe_base in (
        "https://example.com",
        "http://example.com:8000",
        "http://user:password@api:8000",
        "http://api:8000/forward",
        "http://api:8000?next=https://example.com",
    ):
        with pytest.raises(ValidationError, match="private http://api:8000"):
            Settings(
                debug=False,
                jwt_secret=strong_jwt,
                hosted_agent_runner_enabled=True,
                hosted_agent_runner_active_key_id=_KID_OLD,
                hosted_agent_runner_keyring=_keyring_json(),
                hosted_agent_runner_allowed_hosts=["api.openai.com"],
                hosted_agent_runner_internal_api_base=unsafe_base,
                _env_file=None,
            )


def test_enabled_debug_runner_may_use_loopback_internal_api():
    settings = Settings(
        debug=True,
        hosted_agent_runner_enabled=True,
        hosted_agent_runner_active_key_id=_KID_OLD,
        hosted_agent_runner_keyring=_keyring_json(),
        hosted_agent_runner_internal_api_base="http://127.0.0.1:8000",
        _env_file=None,
    )
    assert settings.hosted_agent_runner_internal_api_base == "http://127.0.0.1:8000"


def test_secret_round_trip_uses_active_key_and_redacts_repr():
    keyring = load_hosted_runner_keyring(
        active_key_id=_KID_NEW,
        keyring_json=SecretStr(_keyring_json()),
    )

    ciphertext = keyring.encrypt_secret(
        SecretStr("sk-hosted-runner-secret"),
        row_ref="agent_hosted_runners:42",
        field_name="provider_api_key",
    )
    assert ciphertext.startswith(f"svhr1:{_KID_NEW}:")
    assert "sk-hosted-runner-secret" not in ciphertext

    decrypted = keyring.decrypt_secret(
        ciphertext,
        row_ref="agent_hosted_runners:42",
        field_name="provider_api_key",
    )
    assert decrypted.get_secret_value() == "sk-hosted-runner-secret"

    keyring_repr = repr(keyring)
    assert _KEY_OLD not in keyring_repr
    assert _KEY_NEW not in keyring_repr


def test_decrypt_rejects_row_or_field_rebinding():
    keyring = load_hosted_runner_keyring(
        active_key_id=_KID_OLD,
        keyring_json=_keyring_json(),
    )
    ciphertext = keyring.encrypt_secret(
        "sv-play-token",
        row_ref="agent_hosted_runners:7",
        field_name="play_token",
    )

    with pytest.raises(HostedRunnerSecretError, match="authentication failed"):
        keyring.decrypt_secret(
            ciphertext,
            row_ref="agent_hosted_runners:8",
            field_name="play_token",
        )

    with pytest.raises(HostedRunnerSecretError, match="authentication failed"):
        keyring.decrypt_secret(
            ciphertext,
            row_ref="agent_hosted_runners:7",
            field_name="provider_api_key",
        )


def test_settings_repr_redacts_keyring_secret():
    settings = Settings(
        debug=True,
        hosted_agent_runner_enabled=False,
        hosted_agent_runner_active_key_id=_KID_OLD,
        hosted_agent_runner_keyring=_keyring_json(),
        _env_file=None,
    )
    settings_repr = repr(settings)
    assert _KEY_OLD not in settings_repr
    assert _KEY_NEW not in settings_repr
