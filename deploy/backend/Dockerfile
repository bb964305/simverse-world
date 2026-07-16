FROM python:3.12-slim

WORKDIR /app

# Install system deps for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY . .
# Install the app and its dependencies straight from pyproject.toml
# so the image can never drift from the declared dependency list
RUN pip install --no-cache-dir .

EXPOSE 8000

# Run migrations, then start the API (schema is Alembic-managed, P0-6).
# Multiple workers are safe now that P0-3b moved all shared state to Redis and
# realtime broadcasts to Redis pub/sub; UVICORN_WORKERS defaults to 1 for
# single-node dev and is set to 2+ by the deploy compose.
# --proxy-headers + --forwarded-allow-ips=* trust X-Forwarded-* from any
# peer: safe here because the ONLY ingress is the Cloudflare tunnel, so the
# only peer that can ever dial uvicorn IS the trusted proxy. This corrects
# request.client.host from forwarded headers; app.rate_limit.client_ip_key
# additionally prefers CF-Connecting-IP for REST rate-limit bucketing.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-1} --proxy-headers --forwarded-allow-ips=*"]
