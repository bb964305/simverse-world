"""Shared slowapi limiter instance.

Defined in its own module so routers can import the decorator without a
circular dependency on ``app.main`` (which imports the routers).
"""
from slowapi import Limiter


def client_ip_key(request) -> str:
    """Real client IP behind the CF tunnel.

    Trust boundary: the ONLY ingress into this API is the Cloudflare tunnel
    (simverse-api.proxypool.eu.org -> vm212), so forwarded headers set by
    Cloudflare are trusted here. slowapi's default key_func
    (get_remote_address) reads only ``request.client.host`` -- the socket
    peer -- which behind the tunnel is the tunnel/edge IP and is IDENTICAL
    for every real client, collapsing all clients into one shared
    rate-limit bucket (limits ineffective; one abuser throttles everyone).

    Order: CF-Connecting-IP (set by Cloudflare, most reliable) -> first hop
    of X-Forwarded-For -> request.client.host (uvicorn --proxy-headers
    already corrects this when the above headers are absent, so it's a
    safe final fallback) -> "127.0.0.1" if there is no client at all.
    """
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


# Keys on the real client IP behind the CF tunnel (see client_ip_key above).
# register/login are hit before login so no user_id is available; IP is the
# only stable key.
limiter = Limiter(key_func=client_ip_key, default_limits=[])
