import pytest
from app.config import Settings


def test_media_config_defaults():
    s = Settings()
    # P1 fix: uploads must live under the served static root (see app.main
    # /static mount) — the old backend/static/uploads default 404'd in Docker.
    assert s.static_dir == "static"
    assert s.media_upload_dir == "static/uploads"
    assert s.media_max_image_size == 5 * 1024 * 1024  # 5 MB
    assert s.media_max_video_size == 50 * 1024 * 1024  # 50 MB
    assert s.video_llm_model == "kimi-k2.5"


def test_media_upload_dir_is_string():
    s = Settings()
    assert isinstance(s.media_upload_dir, str)
