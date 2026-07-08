"""P1-1 LLM usage metering: pricing, record_usage, and client instrumentation.

Metering writes go to their own short-lived session, so these tests bind
``metering`` to a shared in-memory sqlite (StaticPool keeps one connection so
the ``create_all`` table is visible to the separate metering session).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select, func
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.database import Base
from app.models.llm_usage import LLMUsage
from app.llm import metering
from app.llm.metering import Meter, record_usage, estimate_tokens, usage_from_response
from app.llm.pricing import compute_cost
import app.llm.client as llm_client

pytestmark = pytest.mark.anyio


# ---------- fakes ----------

class _FakeUsage:
    def __init__(self, i=100, o=50, cr=0, cc=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cc


class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, text="", usage=None):
        self.content = [_FakeBlock(text)]
        self.usage = usage


class _FakeStream:
    def __init__(self, chunks, final=None):
        self._chunks = chunks
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()

    async def get_final_message(self):
        if self._final is None:
            raise RuntimeError("no final message")
        return self._final


@pytest.fixture
async def metered():
    """Enable metering against a shared in-memory sqlite; yield its session factory."""
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


async def _rows(factory):
    async with factory() as s:
        return (await s.execute(select(LLMUsage))).scalars().all()


# ---------- pricing ----------

def test_cost_haiku_input_output():
    # 1M input @ $1 + 1M output @ $5 = $6
    assert compute_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000) == 6.0


def test_cost_sonnet_higher_than_haiku():
    h = compute_cost("claude-haiku-4-5", 1000, 500)
    s = compute_cost("claude-sonnet-4-5", 1000, 500)
    assert s > h > 0


def test_cost_unknown_model_falls_back_to_haiku():
    assert compute_cost("kimi-k2.5", 1000, 500) == compute_cost("claude-haiku-4-5", 1000, 500)


def test_cost_includes_cache_tokens():
    base = compute_cost("claude-haiku-4-5", 1000, 0)
    withcache = compute_cost("claude-haiku-4-5", 1000, 0, cache_read_tokens=1_000_000)
    assert withcache > base


# ---------- estimate / usage extraction ----------

def test_estimate_tokens_nonzero_and_cjk_denser():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("你好世界你好世界") > estimate_tokens("abcdefgh")


def test_usage_from_response():
    assert usage_from_response(_FakeResp(usage=None)) is None
    got = usage_from_response(_FakeResp(usage=_FakeUsage(100, 50, 3, 7)))
    assert got == {
        "input_tokens": 100, "output_tokens": 50,
        "cache_read_tokens": 3, "cache_creation_tokens": 7,
    }


# ---------- record_usage ----------

async def test_record_from_real_usage(metered):
    await record_usage(
        "decide", model="claude-haiku-4-5", owner="system",
        response=_FakeResp(usage=_FakeUsage(200, 40)),
        resident_id="r1", parse_ok=True, latency_ms=123,
    )
    rows = await _rows(metered)
    assert len(rows) == 1
    row = rows[0]
    assert row.scenario == "decide"
    assert row.owner == "system"
    assert row.resident_id == "r1"
    assert row.source == "usage"
    assert row.input_tokens == 200 and row.output_tokens == 40
    assert row.parse_ok is True
    assert row.latency_ms == 123
    assert row.cost_usd == compute_cost("claude-haiku-4-5", 200, 40)


async def test_record_estimated_when_usage_missing(metered):
    await record_usage(
        "plan", model="claude-haiku-4-5", owner="system",
        response=_FakeResp(usage=None),
        est_input_tokens=300, est_output_tokens=80,
    )
    rows = await _rows(metered)
    assert rows[0].source == "estimated"
    assert rows[0].input_tokens == 300 and rows[0].output_tokens == 80
    assert rows[0].cost_usd == compute_cost("claude-haiku-4-5", 300, 80)


async def test_record_no_response_uses_estimate(metered):
    await record_usage("player_chat", model="claude-haiku-4-5", owner="user",
                       est_input_tokens=10, est_output_tokens=5)
    rows = await _rows(metered)
    assert rows[0].source == "estimated" and rows[0].owner == "user"


async def test_metering_disabled_writes_nothing(metered):
    settings.llm_metering_enabled = False
    await record_usage("decide", model="claude-haiku-4-5", response=_FakeResp(usage=_FakeUsage()))
    assert await _rows(metered) == []


async def test_record_never_raises_on_bad_factory():
    # Point at a broken factory and confirm the error is swallowed, not raised.
    settings.llm_metering_enabled = True
    metering.set_session_factory(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        await record_usage("decide", model="claude-haiku-4-5", response=_FakeResp(usage=_FakeUsage()))
    finally:
        settings.llm_metering_enabled = False
        metering.set_session_factory(None)


# ---------- client instrumentation ----------

async def test_chat_records_with_parse_ok(metered, monkeypatch):
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_FakeResp(text='{"action": "wander"}', usage=_FakeUsage(150, 20))
    )
    monkeypatch.setattr(llm_client, "get_client", lambda owner="system", **k: fake_client)

    out = await llm_client.chat(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=200,
        meter=Meter(scenario="decide", resident_id="r9"), expects_json=True,
    )
    assert out == '{"action": "wander"}'
    rows = await _rows(metered)
    assert len(rows) == 1
    assert rows[0].scenario == "decide" and rows[0].resident_id == "r9"
    assert rows[0].parse_ok is True
    assert rows[0].input_tokens == 150 and rows[0].source == "usage"


async def test_chat_parse_ok_false_on_garbage(metered, monkeypatch):
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_FakeResp(text="not json at all", usage=_FakeUsage())
    )
    monkeypatch.setattr(llm_client, "get_client", lambda owner="system", **k: fake_client)
    await llm_client.chat("sys", [{"role": "user", "content": "hi"}],
                          meter=Meter(scenario="decide"), expects_json=True)
    rows = await _rows(metered)
    assert rows[0].parse_ok is False


async def test_chat_without_meter_records_nothing(metered, monkeypatch):
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_FakeResp(text="hello", usage=_FakeUsage()))
    monkeypatch.setattr(llm_client, "get_client", lambda owner="system", **k: fake_client)
    await llm_client.chat("sys", [{"role": "user", "content": "hi"}])
    assert await _rows(metered) == []


async def test_stream_chat_records_from_final_message(metered, monkeypatch):
    final = _FakeResp(text="", usage=_FakeUsage(90, 30))
    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock(return_value=_FakeStream(["a", "b", "c"], final=final))
    monkeypatch.setattr(llm_client, "get_client", lambda owner="user", **k: fake_client)

    chunks = []
    async for c in llm_client.stream_chat("sys", [{"role": "user", "content": "hi"}],
                                          meter=Meter(scenario="player_chat", resident_id="r5")):
        chunks.append(c)
    assert chunks == ["a", "b", "c"]
    rows = await _rows(metered)
    assert len(rows) == 1
    assert rows[0].scenario == "player_chat" and rows[0].owner == "user"
    assert rows[0].input_tokens == 90 and rows[0].source == "usage"


async def test_stream_chat_estimates_when_no_final_usage(metered, monkeypatch):
    fake_client = MagicMock()
    # final message raises -> fall back to estimate from streamed text
    fake_client.messages.stream = MagicMock(return_value=_FakeStream(["hello ", "world"], final=None))
    monkeypatch.setattr(llm_client, "get_client", lambda owner="user", **k: fake_client)
    async for _ in llm_client.stream_chat("sys", [{"role": "user", "content": "hi"}],
                                          meter=Meter(scenario="player_chat")):
        pass
    rows = await _rows(metered)
    assert len(rows) == 1
    assert rows[0].source == "estimated" and rows[0].output_tokens > 0


async def test_attempt_recorded_even_when_parse_fails(metered, monkeypatch):
    """E-19: charging is per-attempt — a parse-failure fallback still costs money
    and must appear in telemetry with parse_ok=False."""
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_FakeResp(text="junk", usage=_FakeUsage(500, 10)))
    monkeypatch.setattr(llm_client, "get_client", lambda owner="system", **k: fake_client)
    await llm_client.chat("sys", [{"role": "user", "content": "x"}],
                          meter=Meter(scenario="extract"), expects_json=True)
    async with metered() as s:
        total = (await s.execute(select(func.sum(LLMUsage.cost_usd)))).scalar_one()
    assert total > 0
