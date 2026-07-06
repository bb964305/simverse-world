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

# Run migrations, then start the API (schema is Alembic-managed, P0-6)
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
