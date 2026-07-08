"""P1-1 §6/§7: history double-inject fix (E-02) + player chat window (E-08)."""
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.user import User
from app.models.conversation import Conversation

pytestmark = pytest.mark.anyio


# ── E-02: history is no longer double-injected ───────────────────────

def test_chat_reply_system_has_no_history_slot():
    from app.agent.prompts import CHAT_REPLY_SYSTEM
    assert "{history}" not in CHAT_REPLY_SYSTEM


def test_build_chat_system_does_not_inject_history():
    """_build_chat_system must not put the dialog history in the system prompt —
    it is supplied as the user message instead (E-02)."""
    from app.agent.chat import _build_chat_system

    class _R:
        name = "Bob"
        persona_md = "outgoing"
        meta_json = {"sbti": {"type": "GOGO", "type_name": "行者"}}

    class _O:
        name = "Alice"

    sys = _build_chat_system(_R(), _O(), "REL", is_initiator=False, history="UNIQUE_HISTORY_MARKER")
    assert "UNIQUE_HISTORY_MARKER" not in sys
    assert "Bob" in sys and "Alice" in sys  # prompt still assembled


# ── E-08: player-NPC chat only sends the last N turns ────────────────

class _FakeManager:
    def __init__(self):
        self.sent = []

    async def send(self, user_id, data):
        self.sent.append(data)


async def test_player_chat_sends_windowed_history():
    from app.ws.handlers import chat as chat_handler
    from app.ws.handlers.context import ConnectionContext

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id="u1", name="U", email="u@t.co", soul_coin_balance=100))
        s.add(Conversation(id="c-1", user_id="u1", resident_id="r-1"))
        await s.commit()

    resident = type("R", (), {
        "id": "r-1", "slug": "r", "name": "R", "token_cost_per_turn": 1,
        "status": "chatting", "creator_id": "u1",
        "ability_md": "a", "persona_md": "p", "soul_md": "s", "meta_json": {},
    })()
    ctx = ConnectionContext(user_id="u1", user_name="U", conversation_id="c-1", resident=resident)
    ctx.memory_context = ""
    # Pre-fill 15 prior turns; the window should keep only the last 10 sent.
    ctx.chat_messages = [{"role": "user", "content": f"m{i}"} for i in range(15)]

    captured = {}

    class _FakeRouter:
        async def chat_with_media(self, *, system_prompt, messages, media_url, media_type, meter=None):
            captured["messages"] = list(messages)
            yield "ok"

    fake = _FakeManager()
    with patch.object(chat_handler, "async_session", factory), \
         patch.object(chat_handler, "manager", fake), \
         patch.object(chat_handler, "ModelRouter", _FakeRouter), \
         patch.object(chat_handler, "assemble_system_prompt", lambda *a, **k: "sys"), \
         patch.object(chat_handler, "reward_creator_passive", new=AsyncMock(return_value=None)):
        await chat_handler.ws_limiter.reset()
        await chat_handler.handle_chat_msg(ctx, {"type": "chat_msg", "text": "brand new"})

    await engine.dispose()

    sent = captured["messages"]
    assert len(sent) == chat_handler.CHAT_HISTORY_WINDOW == 10
    assert sent[-1]["content"] == "brand new"      # newest user turn included
    assert sent[0]["content"] == "m6"              # oldest 6 turns dropped
