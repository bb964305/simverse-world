from unittest.mock import AsyncMock, patch

import pytest

from app.services.skill_import_service import IMPORT_MAX_MULTIPART_BODY_BYTES


@pytest.mark.anyio
async def test_import_rejects_oversize_declared_body_before_route(client):
    auth = AsyncMock()
    with patch("app.routers.residents._require_user_auth", auth):
        response = await client.post(
            "/residents/import",
            content=b"not parsed",
            headers={
                "content-type": "multipart/form-data; boundary=x",
                "content-length": str(IMPORT_MAX_MULTIPART_BODY_BYTES + 1),
            },
        )

    assert response.status_code == 413
    assert "multipart body size" in response.json()["detail"]
    auth.assert_not_awaited()


@pytest.mark.anyio
async def test_import_counts_chunked_body_before_multipart_parser(client):
    boundary = b"skill-import-boundary"
    prefix = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="large.md"\r\n'
        b"Content-Type: text/markdown\r\n\r\n"
    )

    async def chunks():
        yield prefix
        remaining = IMPORT_MAX_MULTIPART_BODY_BYTES + 1
        block = b"x" * 64_000
        while remaining:
            piece = block[:remaining]
            remaining -= len(piece)
            yield piece

    auth = AsyncMock()
    with patch("app.routers.residents._require_user_auth", auth):
        response = await client.post(
            "/residents/import",
            content=chunks(),
            headers={
                "content-type": f"multipart/form-data; boundary={boundary.decode()}",
                "transfer-encoding": "chunked",
            },
        )

    assert response.status_code == 413
    assert "multipart body size" in response.json()["detail"]
    auth.assert_not_awaited()
