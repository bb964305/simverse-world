"""P0-4b hardening: the JWT secret guard must reject known placeholders and
too-short secrets in production, not just the single exact dev constant.

Diagnosis (security audit): the original guard did an exact-string match
against _DEFAULT_JWT_SECRET only. The deploy template
(deploy/backend/.env.example) ships a DIFFERENT placeholder
("generate-a-64-char-random-string-here") that is not equal to that
constant, so a copy-paste production deploy sailed through the guard with a
publicly-known weak secret -> forgeable JWTs (incl. admin).
"""
import pytest
from pydantic import ValidationError

from app.config import Settings

# Exact placeholder shipped in deploy/backend/.env.example — must be blocked
# even though it differs from the app's own _DEFAULT_JWT_SECRET constant.
_DEPLOY_TEMPLATE_PLACEHOLDER = "generate-a-64-char-random-string-here"

_STRONG_SECRET = "8f3e1c9a7b2d4f6e0a1c3b5d7e9f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d5f"  # 64 chars


def test_deploy_template_placeholder_rejected_when_not_debug():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(debug=False, jwt_secret=_DEPLOY_TEMPLATE_PLACEHOLDER, _env_file=None)


def test_short_secret_rejected_when_not_debug():
    short_secret = "x" * 31  # one under the 32-char floor
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(debug=False, jwt_secret=short_secret, _env_file=None)


def test_strong_secret_allowed_when_not_debug():
    s = Settings(debug=False, jwt_secret=_STRONG_SECRET, _env_file=None)
    assert s.jwt_secret == _STRONG_SECRET


def test_weak_secret_allowed_in_debug():
    s = Settings(debug=True, jwt_secret=_DEPLOY_TEMPLATE_PLACEHOLDER, _env_file=None)
    assert s.jwt_secret == _DEPLOY_TEMPLATE_PLACEHOLDER
