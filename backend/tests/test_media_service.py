import pytest
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import UploadFile
from app.media.service import MediaService, MediaValidationError


def _make_upload_file(filename: str, content: bytes, content_type: str) -> UploadFile:
    file_obj = io.BytesIO(content)
    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = filename
    mock_file.content_type = content_type
    mock_file.read = AsyncMock(return_value=content)
    mock_file.size = len(content)
    return mock_file


@pytest.mark.anyio
async def test_save_image_returns_media_url(tmp_path):
    svc = MediaService(upload_base=str(tmp_path))
    content = b"\xff\xd8\xff" + b"0" * 100  # minimal JPEG-like bytes
    f = _make_upload_file("photo.jpg", content, "image/jpeg")

    result = await svc.save_upload(f, media_type="image")

    assert result["media_type"] == "image"
    assert result["media_url"].startswith("/static/uploads/images/")
    assert result["media_url"].endswith(".jpg")
    # File should exist on disk
    file_path = svc.get_file_path(result["media_url"])
    assert file_path.exists()
    assert file_path.read_bytes() == content


@pytest.mark.anyio
async def test_save_video_returns_media_url(tmp_path):
    svc = MediaService(upload_base=str(tmp_path))
    content = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100  # minimal MP4 ftyp box
    f = _make_upload_file("clip.mp4", content, "video/mp4")

    result = await svc.save_upload(f, media_type="video")

    assert result["media_type"] == "video"
    assert result["media_url"].startswith("/static/uploads/videos/")
    assert result["media_url"].endswith(".mp4")


@pytest.mark.anyio
async def test_image_too_large_raises_error(tmp_path):
    svc = MediaService(upload_base=str(tmp_path), max_image_size=100)
    content = b"x" * 200
    f = _make_upload_file("big.jpg", content, "image/jpeg")

    with pytest.raises(MediaValidationError, match="Image too large"):
        await svc.save_upload(f, media_type="image")


@pytest.mark.anyio
async def test_video_too_large_raises_error(tmp_path):
    svc = MediaService(upload_base=str(tmp_path), max_video_size=100)
    content = b"x" * 200
    f = _make_upload_file("big.mp4", content, "video/mp4")

    with pytest.raises(MediaValidationError, match="Video too large"):
        await svc.save_upload(f, media_type="video")


@pytest.mark.anyio
async def test_unsupported_image_type_raises_error(tmp_path):
    svc = MediaService(upload_base=str(tmp_path))
    content = b"fakecontent"
    f = _make_upload_file("file.bmp", content, "image/bmp")

    with pytest.raises(MediaValidationError, match="Unsupported image type"):
        await svc.save_upload(f, media_type="image")


@pytest.mark.anyio
async def test_unsupported_video_type_raises_error(tmp_path):
    svc = MediaService(upload_base=str(tmp_path))
    content = b"fakecontent"
    f = _make_upload_file("file.avi", content, "video/avi")

    with pytest.raises(MediaValidationError, match="Unsupported video type"):
        await svc.save_upload(f, media_type="video")


@pytest.mark.anyio
async def test_spoofed_image_content_rejected(tmp_path):
    """Declared image/jpeg but non-image bytes must be rejected (magic bytes, P0-4d)."""
    svc = MediaService(upload_base=str(tmp_path))
    content = b"<?php system($_GET['cmd']); ?>"
    f = _make_upload_file("evil.jpg", content, "image/jpeg")

    with pytest.raises(MediaValidationError, match="does not match a supported image"):
        await svc.save_upload(f, media_type="image")


@pytest.mark.anyio
async def test_spoofed_video_content_rejected(tmp_path):
    """Declared video/mp4 but non-video bytes must be rejected (magic bytes, P0-4d)."""
    svc = MediaService(upload_base=str(tmp_path))
    content = b"#!/bin/sh\nrm -rf /\n" + b"x" * 100
    f = _make_upload_file("evil.mp4", content, "video/mp4")

    with pytest.raises(MediaValidationError, match="does not match a supported video"):
        await svc.save_upload(f, media_type="video")


@pytest.mark.anyio
async def test_extension_follows_sniffed_type_not_declared(tmp_path):
    """A PNG uploaded as image/jpeg is saved with .png (sniffed type wins)."""
    svc = MediaService(upload_base=str(tmp_path))
    content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    f = _make_upload_file("actually-png.jpg", content, "image/jpeg")

    result = await svc.save_upload(f, media_type="image")
    assert result["media_url"].endswith(".png")


def test_get_file_path_resolves_url(tmp_path):
    svc = MediaService(upload_base=str(tmp_path))
    url = "/static/uploads/images/abc123.jpg"
    path = svc.get_file_path(url)
    assert path == tmp_path / "images" / "abc123.jpg"


def test_get_file_path_video(tmp_path):
    svc = MediaService(upload_base=str(tmp_path))
    url = "/static/uploads/videos/abc123.mp4"
    path = svc.get_file_path(url)
    assert path == tmp_path / "videos" / "abc123.mp4"
