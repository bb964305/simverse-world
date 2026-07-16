"""Tests for the CF-tunnel-aware rate-limit key_func (security audit fix).

Production runs behind a Cloudflare tunnel (simverse-api.proxypool.eu.org ->
vm212) with multiple uvicorn workers. slowapi's default key_func
(get_remote_address) reads only request.client.host -- the socket peer --
which behind the tunnel is the tunnel/edge IP, identical for every real
client. That collapses all clients into one shared rate-limit bucket: limits
become ineffective and one abuser can throttle everyone.

app.rate_limit.client_ip_key fixes this by reading, in order:
CF-Connecting-IP -> first hop of X-Forwarded-For -> request.client.host.
"""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.rate_limit import client_ip_key


# ---------------------------------------------------------------------------
# Unit: client_ip_key on fake request objects (covers all fallbacks)
# ---------------------------------------------------------------------------

def _fake_request(headers=None, client_host="10.0.0.1"):
    return SimpleNamespace(
        headers=headers or {},
        client=SimpleNamespace(host=client_host),
    )


def test_prefers_cf_connecting_ip_over_everything():
    req = _fake_request(
        headers={
            "CF-Connecting-IP": "1.2.3.4",
            "X-Forwarded-For": "5.6.7.8, 9.9.9.9",
        },
        client_host="127.0.0.1",
    )
    assert client_ip_key(req) == "1.2.3.4"


def test_falls_back_to_xff_first_hop_when_no_cf_header():
    req = _fake_request(
        headers={"X-Forwarded-For": "5.6.7.8, 9.9.9.9"},
        client_host="127.0.0.1",
    )
    assert client_ip_key(req) == "5.6.7.8"


def test_falls_back_to_client_host_when_no_proxy_headers():
    req = _fake_request(headers={}, client_host="127.0.0.1")
    assert client_ip_key(req) == "127.0.0.1"


def test_falls_back_to_default_when_no_client_at_all():
    req = SimpleNamespace(headers={}, client=None)
    assert client_ip_key(req) == "127.0.0.1"


# ---------------------------------------------------------------------------
# Behavioral: two CF-Connecting-IPs must not share a slowapi bucket
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_rest_rate_limit_buckets_by_cf_connecting_ip_not_socket_peer(client):
    """Under the ASGI test transport every request has the SAME
    request.client.host (127.0.0.1) -- exactly like every real request
    behind the CF tunnel sharing the tunnel's edge IP. Distinguishing
    callers by CF-Connecting-IP is the whole point of the fix.

    Caller A exhausts its own bucket and gets 429; caller B, using a
    DIFFERENT CF-Connecting-IP, must still be allowed through (401, not
    429) because it has its own bucket.
    """
    limit = settings.rest_rate_limit_forge_per_minute

    codes_a = []
    for i in range(limit + 1):
        r = await client.post(
            "/forge/quick",
            json={"name": f"a{i}", "raw_text": "x"},
            headers={"CF-Connecting-IP": "1.1.1.1"},
        )
        codes_a.append(r.status_code)
    assert codes_a[:limit] == [401] * limit
    assert codes_a[limit] == 429

    r_b = await client.post(
        "/forge/quick",
        json={"name": "b0", "raw_text": "x"},
        headers={"CF-Connecting-IP": "2.2.2.2"},
    )
    assert r_b.status_code == 401  # allowed through the limiter, own bucket

    # caller A's bucket is still exhausted (sanity: buckets are per-IP, not
    # reset globally by B's traffic)
    r_a_again = await client.post(
        "/forge/quick",
        json={"name": "a-again", "raw_text": "x"},
        headers={"CF-Connecting-IP": "1.1.1.1"},
    )
    assert r_a_again.status_code == 429
