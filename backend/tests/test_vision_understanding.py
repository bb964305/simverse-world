"""Vision-understanding prepass for image chat (P1-1 fix: NPC 图片理解不可用).

The production model (qwen3.7-plus via 百炼中转) has no vision capability, so an
injected Anthropic image block was never consumed and the NPC answered "没有视觉
能力". These tests pin the fix: when SV_VISION_MODEL is configured, ModelRouter
runs a vision prepass, injects the text description into the conversation chain,
meters the call, and degrades gracefully (to the legacy text path) on any failure
or when the budget breaker is at PLAYER_ONLY.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.media.model_router import ModelRouter

pytestmark = pytest.mark.anyio


@pytest.fixture
async def metered():
    """Enable metering against a shared in-memory sqlite; yield its session factory.

    Mirrors the fixture in test_llm_usage.py (kept local so this file is
    self-contained and does not touch existing test modules)."""
    from app.config import settings
    from app.database import Base
    from app.llm import metering

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    metering.set_session_factory(factory)
    settings.llm_metering_enabled = True
    try:
        yield factory
    finally:
        settings.llm_metering_enabled = False
        metering.set_session_factory(None)
        await engine.dispose()


# ---------- fakes ----------

def _make_stream_mock(chunks: list[str]):
    """Async context manager yielding fixed text chunks (mirrors existing tests)."""
    class FakeStream:
        def __init__(self):
            self.text_stream = _async_gen(chunks)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_final_message(self):
            return None

    async def _async_gen(items):
        for item in items:
            yield item

    return FakeStream()


def _make_reactive_stream(reply_fn):
    """A stream whose chunks depend on the messages passed to stream(**kwargs).

    Used for the E2E: the fake "main model" reads the injected description and
    echoes the color/object back, proving the description reached the main chain.
    """
    class ReactiveClient:
        def __init__(self):
            self.messages = MagicMock()
            self.messages.stream = self._stream

        def _stream(self, **kwargs):
            reply = reply_fn(kwargs["messages"])
            return _make_stream_mock([reply])

    return ReactiveClient()


def _mock_vision_response(text: str):
    msg = MagicMock()
    block = MagicMock()
    block.text = text
    msg.content = [block]
    msg.usage = None  # force the estimated-tokens metering path
    return msg


# ---------- routing + description injection ----------

async def test_image_prepass_injects_description_and_keeps_image_block(monkeypatch):
    """With SV_VISION_MODEL set: vision model describes the image, the description
    is injected as text, and the (legacy) image block is still present."""
    monkeypatch.setenv("SV_VISION_MODEL", "qwen-vl-max")
    system_prompt = "You are a resident."
    messages = [{"role": "user", "content": "这是什么？"}]
    image_url = "https://example.com/photo.jpg"
    description = "图中有一只红色的苹果放在木桌上。"

    with patch("app.media.model_router.get_client") as mock_get_client:
        vision_client = MagicMock()
        vision_client.messages.create = AsyncMock(return_value=_mock_vision_response(description))
        main_client = MagicMock()
        main_client.messages.stream.return_value = _make_stream_mock(["看到了！"])
        mock_get_client.side_effect = [vision_client, main_client]

        router = ModelRouter()
        chunks = [c async for c in router.chat_with_media(
            system_prompt=system_prompt, messages=messages,
            media_url=image_url, media_type="image",
        )]

    assert chunks == ["看到了！"]
    # Vision model was called with the configured model id.
    assert vision_client.messages.create.call_args.kwargs["model"] == "qwen-vl-max"
    # Main model received the description in its last user message ...
    last_content = main_client.messages.stream.call_args.kwargs["messages"][-1]["content"]
    assert description in str(last_content)
    # ... and the image block is still attached (vision-capable relays benefit).
    image_blocks = [b for b in last_content if isinstance(b, dict) and b.get("type") == "image"]
    assert len(image_blocks) == 1


async def test_image_prepass_failure_falls_back_to_text(monkeypatch):
    """A vision-call failure must not crash the chat: the main model still streams
    (legacy behavior), just without a usable description."""
    monkeypatch.setenv("SV_VISION_MODEL", "qwen-vl-max")
    with patch("app.media.model_router.get_client") as mock_get_client:
        vision_client = MagicMock()
        vision_client.messages.create = AsyncMock(side_effect=RuntimeError("relay 500"))
        main_client = MagicMock()
        main_client.messages.stream.return_value = _make_stream_mock(["还在这~"])
        mock_get_client.side_effect = [vision_client, main_client]

        router = ModelRouter()
        chunks = [c async for c in router.chat_with_media(
            system_prompt="s", messages=[{"role": "user", "content": "hi"}],
            media_url="https://example.com/x.jpg", media_type="image",
        )]

    assert chunks == ["还在这~"]  # conversation survived
    # Image block preserved so a vision relay would still work.
    last_content = main_client.messages.stream.call_args.kwargs["messages"][-1]["content"]
    assert any(isinstance(b, dict) and b.get("type") == "image" for b in last_content)


async def test_no_vision_model_configured_uses_legacy_path(monkeypatch):
    """Without SV_VISION_MODEL the prepass is dormant: no vision call, image block
    only (identical to pre-fix behavior — keeps existing tests green)."""
    monkeypatch.delenv("SV_VISION_MODEL", raising=False)
    with patch("app.media.model_router.get_client") as mock_get_client:
        client = MagicMock()
        client.messages.stream.return_value = _make_stream_mock(["hi"])
        client.messages.create = AsyncMock(side_effect=AssertionError("vision must not be called"))
        mock_get_client.return_value = client

        router = ModelRouter()
        chunks = [c async for c in router.chat_with_media(
            system_prompt="s", messages=[{"role": "user", "content": "q"}],
            media_url="https://example.com/x.jpg", media_type="image",
        )]

    assert chunks == ["hi"]
    client.messages.create.assert_not_called()


# ---------- metering ----------

async def test_vision_call_is_metered(monkeypatch, metered):
    """The vision prepass records an llm_usage row (scenario='image') — it must
    not bypass the Meter."""
    from sqlalchemy import select
    from app.models.llm_usage import LLMUsage
    from app.llm.metering import Meter

    monkeypatch.setenv("SV_VISION_MODEL", "qwen-vl-max")
    with patch("app.media.model_router.get_client") as mock_get_client:
        vision_client = MagicMock()
        vision_client.messages.create = AsyncMock(
            return_value=_mock_vision_response("图中是一只蓝色的猫。")
        )
        main_client = MagicMock()
        main_client.messages.stream.return_value = _make_stream_mock(["嗯"])
        mock_get_client.side_effect = [vision_client, main_client]

        router = ModelRouter()
        _ = [c async for c in router.chat_with_media(
            system_prompt="s", messages=[{"role": "user", "content": "q"}],
            media_url="https://example.com/cat.jpg", media_type="image",
            meter=Meter(scenario="player_chat", resident_id="r1", user_id="u1"),
        )]

    async with metered() as s:
        rows = (await s.execute(select(LLMUsage).where(LLMUsage.scenario == "image"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].model == "qwen-vl-max"


async def test_malformed_vision_response_still_metered(monkeypatch, metered):
    """A billable create() that succeeds but returns a malformed body (content
    None) must NOT skip metering — extract runs defensively after record_usage's
    input is captured (禁止绕过 Meter). The chat still survives."""
    from sqlalchemy import select
    from app.models.llm_usage import LLMUsage
    from app.llm.metering import Meter

    monkeypatch.setenv("SV_VISION_MODEL", "qwen-vl-max")

    bad = MagicMock()
    bad.content = None  # extract_text -> `for block in None` -> TypeError
    bad.usage = None    # force estimated-token path

    with patch("app.media.model_router.get_client") as mock_get_client:
        vision_client = MagicMock()
        vision_client.messages.create = AsyncMock(return_value=bad)
        main_client = MagicMock()
        main_client.messages.stream.return_value = _make_stream_mock(["ok"])
        mock_get_client.side_effect = [vision_client, main_client]

        router = ModelRouter()
        chunks = [c async for c in router.chat_with_media(
            system_prompt="s", messages=[{"role": "user", "content": "q"}],
            media_url="https://example.com/x.jpg", media_type="image",
            meter=Meter(scenario="player_chat", user_id="u1"),
        )]

    assert chunks == ["ok"]  # conversation survived the malformed vision body
    async with metered() as s:
        rows = (await s.execute(select(LLMUsage).where(LLMUsage.scenario == "image"))).scalars().all()
    assert len(rows) == 1, "billable vision call was not metered"
    # Estimated-path row must include the image token estimate, not ~0.
    assert rows[0].input_tokens >= 1600


async def test_non_upload_local_path_refused(monkeypatch):
    """A local path outside /static/uploads/ is refused before any disk read
    (no arbitrary-file-read via the vision prepass)."""
    monkeypatch.setenv("SV_VISION_MODEL", "qwen-vl-max")
    router = ModelRouter()
    assert router._image_source("/etc/passwd") is None
    assert router._image_source("../../secret.png") is None


async def test_budget_player_only_skips_vision(monkeypatch, metered):
    """At PLAYER_ONLY the global budget is spent: the (optional) vision prepass is
    skipped to save cost, but the player-visible main chat still runs."""
    monkeypatch.setenv("SV_VISION_MODEL", "qwen-vl-max")
    from app.llm.budget import BudgetTier
    from app.llm.metering import Meter

    with patch("app.media.model_router.get_client") as mock_get_client, \
         patch("app.media.model_router.background_tier", AsyncMock(return_value=BudgetTier.PLAYER_ONLY)):
        vision_client = MagicMock()
        vision_client.messages.create = AsyncMock(side_effect=AssertionError("must skip vision"))
        main_client = MagicMock()
        main_client.messages.stream.return_value = _make_stream_mock(["还能聊"])
        mock_get_client.side_effect = [main_client]  # only the main model is fetched

        router = ModelRouter()
        chunks = [c async for c in router.chat_with_media(
            system_prompt="s", messages=[{"role": "user", "content": "q"}],
            media_url="https://example.com/x.jpg", media_type="image",
            meter=Meter(scenario="player_chat", user_id="u1"),
        )]

    assert chunks == ["还能聊"]
    vision_client.messages.create.assert_not_called()


# ---------- E2E: description actually reaches the NPC reply ----------

async def test_e2e_npc_reply_reflects_image_content(monkeypatch):
    """End-to-end: mock the vision model to return a known color+object; a reactive
    'main model' that answers from its input must produce a reply naming them,
    proving the image content flowed into the NPC's answer (not HTTP-200-only)."""
    monkeypatch.setenv("SV_VISION_MODEL", "qwen-vl-max")
    description = "图中有一个绿色的杯子。"

    def reply_from_messages(messages) -> str:
        blob = str(messages[-1]["content"])
        color = "绿色" if "绿色" in blob else "未知颜色"
        obj = "杯子" if "杯子" in blob else "物体"
        return f"我看到一个{color}的{obj}。"

    with patch("app.media.model_router.get_client") as mock_get_client:
        vision_client = MagicMock()
        vision_client.messages.create = AsyncMock(return_value=_mock_vision_response(description))
        main_client = _make_reactive_stream(reply_from_messages)
        mock_get_client.side_effect = [vision_client, main_client]

        router = ModelRouter()
        reply = "".join([c async for c in router.chat_with_media(
            system_prompt="你是居民", messages=[{"role": "user", "content": "看看这个"}],
            media_url="https://example.com/cup.jpg", media_type="image",
        )])

    assert "绿色" in reply
    assert "杯子" in reply
