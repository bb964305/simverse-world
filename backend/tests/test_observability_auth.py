"""GET /metrics bearer-token gate (PLAN_P3 清尾)."""
import pytest
from unittest.mock import patch

from app.config import settings


@pytest.mark.anyio
async def test_metrics_open_when_no_token(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_metrics_401_without_token_when_configured(client):
    with patch.object(settings, "metrics_token", "s3cret"):
        assert (await client.get("/metrics")).status_code == 401
        assert (await client.get(
            "/metrics", headers={"Authorization": "Bearer wrong"}
        )).status_code == 401
        assert (await client.get(
            "/metrics", headers={"Authorization": "Bearer s3cret"}
        )).status_code == 200
        # other routes unaffected
        assert (await client.get("/health")).status_code == 200
