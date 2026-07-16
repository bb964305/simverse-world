"""P0-4b: refuse to start with the default JWT secret outside debug mode."""
import pytest
from pydantic import ValidationError

from app.config import Settings, _DEFAULT_JWT_SECRET


def test_default_secret_rejected_when_not_debug():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(debug=False, jwt_secret=_DEFAULT_JWT_SECRET, _env_file=None)


def test_default_secret_allowed_in_debug():
    s = Settings(debug=True, jwt_secret=_DEFAULT_JWT_SECRET, _env_file=None)
    assert s.jwt_secret == _DEFAULT_JWT_SECRET


def test_real_secret_allowed_when_not_debug():
    # >=32 chars: the P0-4b hardening (test_config_jwt_guard.py) also floors
    # secret length, so the "real secret" fixture must clear that bar.
    s = Settings(
        debug=False,
        jwt_secret="a-long-random-production-secret-value",
        _env_file=None,
    )
    assert s.debug is False


def test_debug_defaults_to_false():
    assert Settings.model_fields["debug"].default is False
