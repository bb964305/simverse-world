"""Shared slowapi limiter instance.

Defined in its own module so routers can import the decorator without a
circular dependency on ``app.main`` (which imports the routers).
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

# Keys on client IP: register/login are hit before login so no user_id is
# available; IP is the only stable key. Behind a proxy, X-Forwarded-For must
# be trusted for get_remote_address to see the real client.
limiter = Limiter(key_func=get_remote_address, default_limits=[])
