"""E5 TTS: SBTI voice mapping, file cache (no re-call), daily quota 429."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.resident import Resident


def _mock_client(content=b"audio-bytes"):
    client = MagicMock()
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=resp)
    return client


def _resident(slug="klaus", meta=None):
    return Resident(slug=slug, name=slug, creator_id="system", district="cafe", status="idle",
                    tile_x=1, tile_y=1, meta_json=meta or {"sbti": {"type": "OJBK"}})


@pytest.fixture
def tts_env(tmp_path):
    from app.services import tts_service as ts
    from app.config import settings as s
    with patch.object(ts, "TTS_DIR", tmp_path), \
         patch.object(s, "tts_base_url", "http://tts.local"), \
         patch.object(s, "tts_api_key", "k"):
        yield ts


def test_voice_differs_by_sbti_and_override():
    from app.services.tts_service import voice_for
    a = _resident(meta={"sbti": {"type": "OJBK"}})
    b = _resident(meta={"sbti": {"type": "WXYZ"}})
    assert voice_for(a) != voice_for(b)
    c = _resident(meta={"voice": "custom_voice"})
    assert voice_for(c) == "custom_voice"


@pytest.mark.anyio
async def test_cache_hit_no_recall(tts_env):
    ts = tts_env
    res = _resident()
    client = _mock_client()
    with patch.object(ts, "get_client", return_value=client):
        r1 = await ts.synthesize("u1", res, "你好呀")
        r2 = await ts.synthesize("u1", res, "你好呀")  # identical → cache
    assert r1["cached"] is False and r2["cached"] is True
    assert r1["url"] == r2["url"]
    assert client.post.await_count == 1  # only synthesized once


@pytest.mark.anyio
async def test_quota_429(tts_env):
    ts = tts_env
    res = _resident()
    from app.config import settings as s
    with patch.object(ts, "get_client", return_value=_mock_client()), \
         patch.object(s, "tts_daily_free_quota", 1):
        await ts.synthesize("u1", res, "文本一")  # count 1, ok
        with pytest.raises(ts.TTSQuotaError):
            await ts.synthesize("u1", res, "文本二")  # count 2 > 1


@pytest.mark.anyio
async def test_text_too_long(tts_env):
    ts = tts_env
    with patch.object(ts, "get_client", return_value=_mock_client()):
        with pytest.raises(ts.TTSError):
            await ts.synthesize("u1", _resident(), "字" * 301)
