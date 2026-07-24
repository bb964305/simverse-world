"""Regression tests for the 2026-07-23 production-test fixes.

P1-1  /static mount        — uploaded media must be retrievable via its URL
P1-2  deep-forge staleness — a dead pipeline session must reach a terminal state
P1-3  account deletion     — DELETE /settings/account must clean up FK children
P2-2  seasons leaderboard  — no active season is 200 + empty board, not 404
"""
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.conversation import Conversation, Message
from app.models.forge_session import ForgeSession
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.transaction import Transaction
from app.models.user import User

pytestmark = pytest.mark.anyio


# 1x1 valid PNG (magic bytes pass the media sniffer)
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


async def _register(client, tag: str) -> tuple[str, str]:
    """Register a fresh user; returns (user_id, token)."""
    resp = await client.post("/auth/register", json={
        "name": f"fixtest-{tag}",
        "email": f"fixtest-{tag}-{uuid.uuid4().hex[:8]}@test.dev",
        "password": "FixTest123!",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["user"]["id"], body["access_token"]


# ─── P1-1: /static mount ─────────────────────────────────────────

async def test_uploaded_media_is_served_via_static(client):
    _, token = await _register(client, "media")
    resp = await client.post(
        "/api/media/upload?media_type=image",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("pixel.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    media_url = resp.json()["media_url"]
    assert media_url.startswith("/static/uploads/")

    try:
        fetched = await client.get(media_url)
        assert fetched.status_code == 200, f"{media_url} not served: {fetched.status_code}"
        assert fetched.content == PNG_BYTES
    finally:
        # keep the repo working tree clean
        from app.config import settings
        leftover = Path(settings.media_upload_dir) / media_url.removeprefix("/static/uploads/")
        leftover.unlink(missing_ok=True)


async def test_portrait_dir_lives_under_static_root():
    from app.config import settings
    from app.services.portrait_service import _portrait_dir

    assert _portrait_dir() == Path(settings.static_dir) / "portraits"
    assert Path(settings.media_upload_dir).resolve().is_relative_to(
        Path(settings.static_dir).resolve()
    ), "media uploads must live under the served static root"


# ─── P1-2: deep-forge staleness sweep ────────────────────────────

async def test_stale_deep_forge_session_is_swept_to_error(client, db_session):
    user_id, token = await _register(client, "forge")
    stale = ForgeSession(
        user_id=user_id, character_name="卡住的角色", mode="quick",
        status="building", current_stage="build",
        updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(stale)
    await db_session.commit()

    resp = await client.get(
        f"/forge/deep-status/{stale.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "error"


async def test_fresh_deep_forge_session_is_not_swept(client, db_session):
    user_id, token = await _register(client, "forge2")
    fresh = ForgeSession(
        user_id=user_id, character_name="进行中的角色", mode="deep",
        status="building", current_stage="build",
        updated_at=datetime.now(UTC),
    )
    db_session.add(fresh)
    await db_session.commit()

    resp = await client.get(
        f"/forge/deep-status/{fresh.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "building"


# ─── P1-3: account deletion ──────────────────────────────────────

async def test_delete_account_cleans_up_and_orphans_residents(client, db_session):
    user_id, token = await _register(client, "del")

    resident = Resident(slug=f"fixtest-npc-{uuid.uuid4().hex[:6]}", name="删号NPC",
                        creator_id=user_id)
    db_session.add(resident)
    await db_session.flush()

    conv = Conversation(user_id=user_id, resident_id=resident.id)
    db_session.add(conv)
    await db_session.flush()
    db_session.add_all([
        Message(conversation_id=conv.id, role="user", content="hi"),
        Transaction(user_id=user_id, amount=-5, reason="test"),
        ForgeSession(user_id=user_id, character_name="x", mode="quick", status="done"),
        Memory(resident_id=resident.id, type="event", content="met the user",
               source="chat_player", related_user_id=user_id),
    ])
    await db_session.commit()
    resident_id, conv_id = resident.id, conv.id

    email = (await client.get(
        "/users/me", headers={"Authorization": f"Bearer {token}"}
    )).json()["email"]

    # wrong confirmation email → 400, nothing deleted
    resp = await client.request(
        "DELETE", "/settings/account",
        headers={"Authorization": f"Bearer {token}"},
        json={"confirm_email": "wrong@test.dev"},
    )
    assert resp.status_code == 400

    resp = await client.request(
        "DELETE", "/settings/account",
        headers={"Authorization": f"Bearer {token}"},
        json={"confirm_email": email},
    )
    assert resp.status_code in (200, 204), resp.text

    # the API used its own DB session — drop this session's cached instances
    db_session.expire_all()

    assert (await db_session.execute(
        select(User).where(User.id == user_id))).scalar_one_or_none() is None
    assert (await db_session.execute(
        select(Conversation).where(Conversation.id == conv_id))).scalar_one_or_none() is None
    assert (await db_session.execute(
        select(Message).where(Message.conversation_id == conv_id))).scalars().first() is None
    assert (await db_session.execute(
        select(Transaction).where(Transaction.user_id == user_id))).scalars().first() is None
    assert (await db_session.execute(
        select(ForgeSession).where(ForgeSession.user_id == user_id))).scalars().first() is None

    orphan = (await db_session.execute(
        select(Resident).where(Resident.id == resident_id))).scalar_one()
    assert orphan.creator_id is None  # resident stays in the world, ownerless

    mem = (await db_session.execute(
        select(Memory).where(Memory.resident_id == resident_id))).scalars().first()
    assert mem is not None and mem.related_user_id is None

    # token no longer usable
    resp = await client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


# ─── P2-2: leaderboard without an active season ──────────────────

async def test_leaderboard_without_active_season_returns_empty(client):
    resp = await client.get("/seasons/current/leaderboard")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"top": [], "season": None}


# ─── P2-1: lab flag exposed to the frontend ──────────────────────

async def test_users_me_exposes_lab_enabled_flag(client):
    from app.config import settings
    _, token = await _register(client, "lab")
    resp = await client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["lab_enabled"] == settings.lab_enabled
