"""External Agent player API: scoped auth, safe movement and spectators."""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import jwt
import pytest
from sqlalchemy import select, update

from app.config import settings
from app.models.agent_player import (
    AgentCredential,
    AgentEvent,
    AgentNpcChatTurnReceipt,
    AgentPlayer,
)
from app.models.conversation import Conversation, Message
from app.models.resident import Resident
from app.models.user import User
from app.services.player_npc_chat_service import recover_expired_npc_chat_turns


async def _register_and_session(client, name: str = "测试旅者"):
    application = await client.post(
        "/api/v1/agent-applications",
        json={
            "display_name": name,
            "sprite_key": "埃迪",
            "model_label": "codex-test",
            "role_card": {"goals": {"public": "去图书馆"}},
            "client": {"name": "pytest", "version": "1"},
        },
    )
    assert application.status_code == 201, application.text
    created = application.json()
    assert created["next_step"] == "redeem_pairing_code"

    redeemed = await client.post(
        "/api/v1/agent-pairings/redeem",
        json={
            "application_id": created["application_id"],
            "pairing_code": created["pairing_code"],
        },
    )
    assert redeemed.status_code == 200, redeemed.text
    credentials = redeemed.json()
    session = await client.post(
        "/api/v1/agent-sessions",
        headers={"Authorization": f"Bearer {credentials['agent_token']}"},
        json={"client": {"name": "pytest", "version": "1"}},
    )
    assert session.status_code == 200, session.text
    return created, credentials, session.json()


async def _set_agent_position(db_session, agent_player_id: str, tile_x: int, tile_y: int):
    profile = await db_session.get(AgentPlayer, agent_player_id)
    assert profile is not None
    user = await db_session.get(User, profile.user_id)
    resident = await db_session.get(Resident, profile.resident_id)
    assert user is not None and resident is not None
    resident.tile_x = tile_x
    resident.tile_y = tile_y
    user.last_x = tile_x * 32 + 16
    user.last_y = tile_y * 32 + 16
    await db_session.commit()


async def _seed_chat_npc(
    db_session,
    *,
    slug: str = "chat-npc",
    tile_x: int = 75,
    tile_y: int = 56,
    status: str = "idle",
):
    creator = User(
        id=f"creator-{slug}",
        name="NPC Creator",
        email=f"{slug}@creator.test",
        soul_coin_balance=0,
    )
    npc = Resident(
        id=f"resident-{slug}",
        slug=slug,
        name="对话居民",
        resident_type="npc",
        status=status,
        creator_id=creator.id,
        tile_x=tile_x,
        tile_y=tile_y,
        token_cost_per_turn=1,
        ability_md="会认真回答问题",
        persona_md="温和而简洁",
        soul_md="珍视诚实",
    )
    db_session.add_all([creator, npc])
    await db_session.commit()
    return npc


@pytest.mark.anyio
async def test_registration_issues_scoped_hashed_credentials(client, db_session):
    created, credentials, session = await _register_and_session(client)

    assert created["pairing_code"].startswith("sv_pair_")
    assert credentials["agent_token"].startswith("sv_play_")
    assert credentials["viewer_token"].startswith("sv_view_")
    assert session["session_token"].count(".") == 2

    users = (await db_session.execute(select(User))).scalars().all()
    assert len(users) == 1
    assert users[0].hashed_password is None
    assert users[0].soul_coin_balance == 0
    resident = (await db_session.execute(select(Resident))).scalar_one()
    assert resident.resident_type == "player"
    assert resident.reply_mode == "manual"
    assert resident.meta_json["agent_controlled"] is True
    profile = (await db_session.execute(select(AgentPlayer))).scalar_one()
    assert profile.client_json == {"name": "pytest", "version": "1"}

    credentials_in_db = (await db_session.execute(select(AgentCredential))).scalars().all()
    stored_values = {c.token_hash for c in credentials_in_db}
    assert all(len(value) == 64 for value in stored_values)
    assert created["pairing_code"] not in stored_values
    assert credentials["agent_token"] not in stored_values
    assert credentials["viewer_token"] not in stored_values

    # The browser's public /residents collection is the NPC sprite layer.
    # Agent-controlled player avatars travel over player presence and must not
    # appear a second time as a static/chat-capable NPC.
    public_roster = await client.get("/residents")
    assert public_roster.status_code == 200
    assert all(item["id"] != resident.id for item in public_roster.json())


@pytest.mark.anyio
async def test_message_player_repeats_until_explicitly_acknowledged(client, db_session):
    created_a, _credentials_a, session_a = await _register_and_session(client, "信使甲")
    created_b, _credentials_b, session_b = await _register_and_session(client, "信使乙")
    await _set_agent_position(db_session, created_a["application_id"], 75, 56)
    await _set_agent_position(db_session, created_b["application_id"], 76, 56)

    sender_headers = {"Authorization": f"Bearer {session_a['session_token']}"}
    target_headers = {"Authorization": f"Bearer {session_b['session_token']}"}
    sender_observation = (
        await client.get("/api/v1/agent/observation", headers=sender_headers)
    ).json()

    message = await client.post(
        "/api/v1/agent/actions",
        headers=sender_headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": sender_observation["observation_seq"],
            "type": "message_player",
            "params": {
                "player_slug": created_b["agent"]["slug"],
                "text": "中央广场东侧集合。",
            },
        },
    )
    assert message.status_code == 200, message.text
    result = message.json()["result"]
    assert result["action"] == "message_player"
    assert result["recipient"]["slug"] == created_b["agent"]["slug"]
    assert "text" not in result

    first = await client.get("/api/v1/agent/observation", headers=target_headers)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["event_cursor"] == 1
    assert payload["has_more_events"] is False
    assert len(payload["recent_events"]) == 1
    event = payload["recent_events"][0]
    assert event["kind"] == "player_message"
    assert event["from"]["slug"] == created_a["agent"]["slug"]
    assert event["text"] == "中央广场东侧集合。"

    second = await client.get("/api/v1/agent/observation", headers=target_headers)
    assert second.status_code == 200, second.text
    assert second.json()["recent_events"] == payload["recent_events"]
    assert second.json()["event_cursor"] == 1

    acknowledged = await client.post(
        "/api/v1/agent/events/ack",
        headers=target_headers,
        json={"event_cursor": 1},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json() == {"event_cursor": 1, "acknowledged": 1}

    third = await client.get("/api/v1/agent/observation", headers=target_headers)
    assert third.status_code == 200, third.text
    assert third.json()["recent_events"] == []
    assert third.json()["event_cursor"] == 1

    stored = (
        await db_session.execute(
            select(AgentEvent).where(AgentEvent.agent_player_id == created_b["application_id"])
        )
    ).scalars().all()
    assert len(stored) == 1
    profile_b = await db_session.get(AgentPlayer, created_b["application_id"])
    assert profile_b is not None
    await db_session.refresh(profile_b)
    assert profile_b.last_seen_event_seq == 1

    ahead = await client.post(
        "/api/v1/agent/events/ack",
        headers=target_headers,
        json={"event_cursor": 2},
    )
    assert ahead.status_code == 422


@pytest.mark.anyio
async def test_agent_npc_single_turn_persists_and_exact_retry_replays(
    client, db_session, monkeypatch
):
    created, _credentials, session = await _register_and_session(client, "访谈员")
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    npc = await _seed_chat_npc(db_session, tile_x=76, tile_y=56)
    profile = await db_session.get(AgentPlayer, created["application_id"])
    user = await db_session.get(User, profile.user_id)
    user.soul_coin_balance = 5
    await db_session.commit()

    calls = 0

    async def fake_reply(**_kwargs):
        nonlocal calls
        calls += 1
        return "欢迎来到小镇。"

    monkeypatch.setattr(
        "app.routers.agent_players.generate_single_turn_reply", fake_reply
    )
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    body = {
        "turn_id": str(uuid.uuid4()),
        "observation_seq": observation["observation_seq"],
        "resident_slug": npc.slug,
        "text": "你好，我刚到这里。",
        "context": "中央广场相遇",
    }
    first = await client.post(
        "/api/v1/agent/npc-chat-turns", headers=headers, json=body
    )
    assert first.status_code == 200, first.text
    assert first.json()["reply"] == "欢迎来到小镇。"
    assert first.json()["charged_sc"] == 1
    assert first.json()["balance"] == 4

    retry = await client.post(
        "/api/v1/agent/npc-chat-turns", headers=headers, json=body
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == first.json()
    assert calls == 1

    conversations = (await db_session.execute(select(Conversation))).scalars().all()
    messages = (await db_session.execute(select(Message))).scalars().all()
    receipts = (
        await db_session.execute(select(AgentNpcChatTurnReceipt))
    ).scalars().all()
    assert len(conversations) == len(receipts) == 1
    assert [message.role for message in messages] == ["user", "assistant"]
    await db_session.refresh(user)
    await db_session.refresh(npc)
    assert user.soul_coin_balance == 4
    assert npc.status == "idle"
    assert npc.total_conversations == 1
    await db_session.refresh(profile)
    assert profile.operation_token is None
    assert profile.operation_expires_at is None


@pytest.mark.anyio
async def test_agent_npc_turn_id_cannot_be_reused_for_different_text(
    client, db_session, monkeypatch
):
    created, _credentials, session = await _register_and_session(client, "幂等访谈员")
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    npc = await _seed_chat_npc(
        db_session, slug="idempotent-npc", tile_x=76, tile_y=56
    )
    profile = await db_session.get(AgentPlayer, created["application_id"])
    user = await db_session.get(User, profile.user_id)
    user.soul_coin_balance = 5
    await db_session.commit()

    monkeypatch.setattr(
        "app.routers.agent_players.generate_single_turn_reply",
        AsyncMock(return_value="第一次回答。"),
    )
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    turn_id = str(uuid.uuid4())
    base = {
        "turn_id": turn_id,
        "observation_seq": observation["observation_seq"],
        "resident_slug": npc.slug,
    }
    first = await client.post(
        "/api/v1/agent/npc-chat-turns",
        headers=headers,
        json={**base, "text": "第一句话"},
    )
    assert first.status_code == 200, first.text
    conflict = await client.post(
        "/api/v1/agent/npc-chat-turns",
        headers=headers,
        json={**base, "text": "偷换后的话"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


@pytest.mark.anyio
async def test_agent_npc_chat_respects_human_shared_lock(
    client, db_session, monkeypatch
):
    from app.ws.manager import manager

    created, _credentials, session = await _register_and_session(client, "锁测试员")
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    npc = await _seed_chat_npc(db_session, slug="locked-npc", tile_x=76, tile_y=56)
    profile = await db_session.get(AgentPlayer, created["application_id"])
    user = await db_session.get(User, profile.user_id)
    user.soul_coin_balance = 5
    await db_session.commit()
    assert await manager.lock_resident(npc.id, "human-user") is True

    model = AsyncMock(return_value="不应调用")
    monkeypatch.setattr("app.routers.agent_players.generate_single_turn_reply", model)
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    response = await client.post(
        "/api/v1/agent/npc-chat-turns",
        headers=headers,
        json={
            "turn_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "resident_slug": npc.slug,
            "text": "你好",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "resident_unavailable"
    model.assert_not_awaited()
    assert await manager.resident_lock_owner(npc.id) == "human-user"


@pytest.mark.anyio
async def test_expired_agent_receipt_never_resets_a_human_owned_chat(
    client, db_session
):
    from app.ws.manager import manager

    created, _credentials, _session = await _register_and_session(client, "回收测试员")
    npc = await _seed_chat_npc(
        db_session, slug="human-owned-npc", tile_x=76, tile_y=56, status="chatting"
    )
    profile = await db_session.get(AgentPlayer, created["application_id"])
    conversation = Conversation(user_id=profile.user_id, resident_id=npc.id, turns=1)
    db_session.add(conversation)
    await db_session.flush()
    old_token = "expired-agent-lease"
    profile.operation_kind = "npc_chat_turn"
    profile.operation_token = old_token
    profile.operation_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    receipt = AgentNpcChatTurnReceipt(
        agent_player_id=profile.id,
        resident_id=npc.id,
        conversation_id=conversation.id,
        turn_id=str(uuid.uuid4()),
        status="pending",
        http_status=202,
        observation_seq=0,
        request_hash="a" * 64,
        response_json={"status": "pending"},
        recovery_json={},
        lease_token=old_token,
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(receipt)
    await db_session.commit()
    assert await manager.lock_resident(npc.id, "human-user") is True

    assert await recover_expired_npc_chat_turns(db_session) == 1
    await db_session.refresh(npc)
    await db_session.refresh(receipt)
    assert npc.status == "chatting"
    assert receipt.lease_token is None
    assert await manager.resident_lock_owner(npc.id) == "human-user"


@pytest.mark.anyio
async def test_npc_turn_reaper_reloads_a_lease_renewed_after_candidate_lookup(
    client, db_session, monkeypatch
):
    created, _credentials, _session = await _register_and_session(
        client, "续租回收测试员"
    )
    npc = await _seed_chat_npc(
        db_session, slug="renewed-lease-npc", tile_x=76, tile_y=56, status="chatting"
    )
    profile = await db_session.get(AgentPlayer, created["application_id"])
    conversation = Conversation(user_id=profile.user_id, resident_id=npc.id, turns=1)
    db_session.add(conversation)
    await db_session.flush()
    expired_token = "expired-candidate-token"
    renewed_token = "renewed-after-candidate-token"
    profile.operation_kind = "npc_chat_turn"
    profile.operation_token = expired_token
    profile.operation_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    receipt = AgentNpcChatTurnReceipt(
        agent_player_id=profile.id,
        resident_id=npc.id,
        conversation_id=conversation.id,
        turn_id=str(uuid.uuid4()),
        status="pending",
        http_status=202,
        observation_seq=0,
        request_hash="b" * 64,
        response_json={"status": "pending"},
        recovery_json={},
        lease_token=expired_token,
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(receipt)
    await db_session.commit()

    original_refresh = db_session.refresh
    renewal_injected = False

    async def refresh_and_renew(instance, *args, **kwargs):
        nonlocal renewal_injected
        await original_refresh(instance, *args, **kwargs)
        if instance is profile and not renewal_injected:
            renewal_injected = True
            await db_session.execute(
                update(AgentNpcChatTurnReceipt)
                .where(AgentNpcChatTurnReceipt.id == receipt.id)
                .values(
                    lease_token=renewed_token,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
                )
                .execution_options(synchronize_session=False)
            )

    monkeypatch.setattr(db_session, "refresh", refresh_and_renew)

    assert await recover_expired_npc_chat_turns(db_session) == 0
    assert renewal_injected is True
    assert receipt.lease_token == renewed_token
    renewed_until = receipt.lease_expires_at
    if renewed_until.tzinfo is None:
        renewed_until = renewed_until.replace(tzinfo=UTC)
    assert renewed_until > datetime.now(UTC)
    assert npc.status == "chatting"


@pytest.mark.anyio
async def test_active_billable_operation_blocks_normal_agent_action(
    client, db_session
):
    created, _credentials, session = await _register_and_session(client, "串行测试员")
    profile = await db_session.get(AgentPlayer, created["application_id"])
    profile.operation_kind = "npc_chat_turn"
    profile.operation_token = "active-operation"
    profile.operation_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    response = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "wait",
            "params": {"seconds": 1},
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "agent_operation_in_progress"


@pytest.mark.anyio
async def test_agent_npc_chat_rejects_far_busy_player_and_insufficient_balance(
    client, db_session, monkeypatch
):
    created, _credentials, session = await _register_and_session(client, "边界访谈员")
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    far = await _seed_chat_npc(db_session, slug="far-npc", tile_x=80, tile_y=56)
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()

    def body(slug: str):
        return {
            "turn_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "resident_slug": slug,
            "text": "你好",
        }

    assert (
        await client.post(
            "/api/v1/agent/npc-chat-turns", headers=headers, json=body(far.slug)
        )
    ).status_code == 422

    near = await _seed_chat_npc(db_session, slug="near-npc", tile_x=76, tile_y=56)
    insufficient = await client.post(
        "/api/v1/agent/npc-chat-turns", headers=headers, json=body(near.slug)
    )
    assert insufficient.status_code == 402

    profile = await db_session.get(AgentPlayer, created["application_id"])
    avatar = await db_session.get(Resident, profile.resident_id)
    player_target = await client.post(
        "/api/v1/agent/npc-chat-turns", headers=headers, json=body(avatar.slug)
    )
    assert player_target.status_code == 404

    user = await db_session.get(User, profile.user_id)
    user.soul_coin_balance = 5
    near.status = "sleeping"
    await db_session.commit()
    busy = await client.post(
        "/api/v1/agent/npc-chat-turns", headers=headers, json=body(near.slug)
    )
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "resident_unavailable"


@pytest.mark.anyio
async def test_agent_npc_chat_model_failure_is_recoverable_and_not_charged(
    client, db_session, monkeypatch
):
    created, _credentials, session = await _register_and_session(client, "恢复访谈员")
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    npc = await _seed_chat_npc(db_session, slug="retry-npc", tile_x=76, tile_y=56)
    profile = await db_session.get(AgentPlayer, created["application_id"])
    user = await db_session.get(User, profile.user_id)
    user.soul_coin_balance = 5
    await db_session.commit()

    async def failing_reply(**_kwargs):
        raise RuntimeError("fake upstream outage")

    monkeypatch.setattr(
        "app.routers.agent_players.generate_single_turn_reply", failing_reply
    )
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    body = {
        "turn_id": str(uuid.uuid4()),
        "observation_seq": observation["observation_seq"],
        "resident_slug": npc.slug,
        "text": "这次会失败",
    }
    failure = await client.post(
        "/api/v1/agent/npc-chat-turns", headers=headers, json=body
    )
    assert failure.status_code == 503
    receipt = (
        await db_session.execute(select(AgentNpcChatTurnReceipt))
    ).scalar_one()
    await db_session.refresh(user)
    assert receipt.status == "pending"
    assert user.soul_coin_balance == 5
    assert (
        await db_session.execute(select(Message).where(Message.role == "assistant"))
    ).scalars().all() == []

    # A handled upstream failure releases its lease immediately, so the exact
    # same turn can resume without creating a second conversation.
    async def recovered_reply(**_kwargs):
        return "现在恢复了。"

    monkeypatch.setattr(
        "app.routers.agent_players.generate_single_turn_reply", recovered_reply
    )
    recovered = await client.post(
        "/api/v1/agent/npc-chat-turns", headers=headers, json=body
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["reply"] == "现在恢复了。"
    assert len((await db_session.execute(select(Conversation))).scalars().all()) == 1


@pytest.mark.anyio
async def test_agent_npc_chat_bounds_sdk_retries_inside_the_durable_lease(
    client, db_session, monkeypatch
):
    created, _credentials, session = await _register_and_session(client, "超时访谈员")
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    npc = await _seed_chat_npc(db_session, slug="timeout-npc", tile_x=76, tile_y=56)
    profile = await db_session.get(AgentPlayer, created["application_id"])
    user = await db_session.get(User, profile.user_id)
    user.soul_coin_balance = 5
    await db_session.commit()

    async def never_finishes_inside_budget(**_kwargs):
        await asyncio.sleep(1)
        return "不应返回"

    monkeypatch.setattr(
        "app.routers.agent_players.generate_single_turn_reply",
        never_finishes_inside_budget,
    )
    monkeypatch.setattr(
        "app.routers.agent_players.NPC_CHAT_CALL_TIMEOUT_SECONDS", 0.01
    )
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    response = await client.post(
        "/api/v1/agent/npc-chat-turns",
        headers=headers,
        json={
            "turn_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "resident_slug": npc.slug,
            "text": "这次会超时",
        },
    )
    assert response.status_code == 503
    await db_session.refresh(profile)
    await db_session.refresh(user)
    await db_session.refresh(npc)
    receipt = (
        await db_session.execute(select(AgentNpcChatTurnReceipt))
    ).scalar_one()
    assert profile.operation_token is None
    assert receipt.lease_token is None
    assert npc.status == "idle"
    assert user.soul_coin_balance == 5


@pytest.mark.anyio
async def test_agent_can_claim_daily_budget_then_chat(
    client, db_session, monkeypatch
):
    created, _credentials, session = await _register_and_session(client, "日常访谈员")
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    npc = await _seed_chat_npc(db_session, slug="daily-npc", tile_x=76, tile_y=56)
    headers = {"Authorization": f"Bearer {session['session_token']}"}

    reward = await client.post("/api/v1/agent/daily-reward", headers=headers)
    assert reward.status_code == 200, reward.text
    assert reward.json()["claimed"] is True
    assert reward.json()["new_balance"] >= 10

    async def fake_reply(**_kwargs):
        return "今天也很高兴见到你。"

    monkeypatch.setattr(
        "app.routers.agent_players.generate_single_turn_reply", fake_reply
    )
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    chatted = await client.post(
        "/api/v1/agent/npc-chat-turns",
        headers=headers,
        json={
            "turn_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "resident_slug": npc.slug,
            "text": "你好",
        },
    )
    assert chatted.status_code == 200, chatted.text


@pytest.mark.anyio
async def test_pairing_code_is_one_time(client):
    application = (
        await client.post(
            "/api/v1/agent-applications",
            json={"display_name": "一次性", "client": {"name": "pytest", "version": "1"}},
        )
    ).json()
    body = {
        "application_id": application["application_id"],
        "pairing_code": application["pairing_code"],
    }
    assert (await client.post("/api/v1/agent-pairings/redeem", json=body)).status_code == 200
    assert (await client.post("/api/v1/agent-pairings/redeem", json=body)).status_code == 409


@pytest.mark.anyio
async def test_unredeemed_application_never_enters_running_public_town(
    client, db_session
):
    response = await client.post(
        "/api/v1/agent-applications",
        json={"display_name": "待配对", "client": {"name": "pytest", "version": "1"}},
    )
    assert response.status_code == 201
    created = response.json()
    assert created["agent_status"] == "pending_pairing"
    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None and profile.status == "pending_pairing"

    town = (await client.get("/api/v1/public/town/snapshot")).json()
    assert town["counts"]["agents"] == 0
    assert town["residents"] == []


@pytest.mark.anyio
async def test_message_player_requires_active_nearby_agent_target(client, db_session):
    created_a, _credentials_a, session_a = await _register_and_session(client, "联络员甲")
    created_b, _credentials_b, _session_b = await _register_and_session(client, "联络员乙")
    await _set_agent_position(db_session, created_a["application_id"], 75, 56)
    await _set_agent_position(db_session, created_b["application_id"], 80, 56)

    headers = {"Authorization": f"Bearer {session_a['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    too_far = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "message_player",
            "params": {
                "player_slug": created_b["agent"]["slug"],
                "text": "你现在太远了。",
            },
        },
    )
    assert too_far.status_code == 422

    target_profile = await db_session.get(AgentPlayer, created_b["application_id"])
    assert target_profile is not None
    target_profile.status = "inactive"
    await db_session.commit()

    unavailable = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "message_player",
            "params": {
                "player_slug": created_b["agent"]["slug"],
                "text": "你已离线。",
            },
        },
    )
    assert unavailable.status_code == 404


@pytest.mark.anyio
async def test_wrong_application_id_does_not_consume_pairing_code(client):
    application = (
        await client.post(
            "/api/v1/agent-applications",
            json={"display_name": "配对", "client": {"name": "pytest", "version": "1"}},
        )
    ).json()
    wrong = await client.post(
        "/api/v1/agent-pairings/redeem",
        json={"application_id": str(uuid.uuid4()), "pairing_code": application["pairing_code"]},
    )
    assert wrong.status_code == 401
    good = await client.post(
        "/api/v1/agent-pairings/redeem",
        json={
            "application_id": application["application_id"],
            "pairing_code": application["pairing_code"],
        },
    )
    assert good.status_code == 200


@pytest.mark.anyio
async def test_private_messages_do_not_leak_to_viewer_or_public_town(client, db_session):
    created_a, _credentials_a, session_a = await _register_and_session(client, "观察者甲")
    created_b, credentials_b, session_b = await _register_and_session(client, "观察者乙")
    await _set_agent_position(db_session, created_a["application_id"], 75, 56)
    await _set_agent_position(db_session, created_b["application_id"], 76, 56)

    sender_headers = {"Authorization": f"Bearer {session_a['session_token']}"}
    target_headers = {"Authorization": f"Bearer {session_b['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=sender_headers)).json()
    sent = await client.post(
        "/api/v1/agent/actions",
        headers=sender_headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "message_player",
            "params": {
                "player_slug": created_b["agent"]["slug"],
                "text": "仅目标可见的私聊内容。",
            },
        },
    )
    assert sent.status_code == 200, sent.text

    viewer = await client.post(
        "/api/v1/viewer/sessions", json={"view_token": credentials_b["viewer_token"]}
    )
    assert viewer.status_code == 200
    snapshot = await client.get("/api/v1/viewer/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["recent_events"] == []
    assert "仅目标可见的私聊内容。" not in snapshot.text

    target_observation = await client.get("/api/v1/agent/observation", headers=target_headers)
    assert target_observation.status_code == 200, target_observation.text
    assert target_observation.json()["recent_events"][0]["text"] == "仅目标可见的私聊内容。"

    town = await client.get("/api/v1/public/town/snapshot")
    assert town.status_code == 200, town.text
    assert town.json()["activity"] == []
    assert "仅目标可见的私聊内容。" not in town.text


@pytest.mark.anyio
async def test_ordinary_login_jwt_cannot_access_agent_api(client):
    ordinary = jwt.encode(
        {"sub": "someone"}, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    response = await client.get(
        "/api/v1/agent/me", headers={"Authorization": f"Bearer {ordinary}"}
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_banned_agent_principal_cannot_use_existing_sessions(
    client, db_session
):
    created, credentials, session = await _register_and_session(client)
    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None
    user = await db_session.get(User, profile.user_id)
    assert user is not None
    user.is_banned = True
    await db_session.commit()

    denied = await client.get(
        "/api/v1/agent/me",
        headers={"Authorization": f"Bearer {session['session_token']}"},
    )
    assert denied.status_code == 403
    denied_refresh = await client.post(
        "/api/v1/agent-sessions",
        headers={"Authorization": f"Bearer {credentials['agent_token']}"},
        json={"client": {"name": "pytest", "version": "1"}},
    )
    assert denied_refresh.status_code == 403
    public = await client.get("/api/v1/public/town/snapshot")
    assert public.status_code == 200
    assert public.json()["counts"]["agents"] == 0
    assert "测试旅者" not in public.text


@pytest.mark.anyio
async def test_action_retry_is_idempotent_and_position_is_atomic(client, db_session):
    _created, _credentials, session = await _register_and_session(client)
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    start = (observation["self"]["tile_x"], observation["self"]["tile_y"])

    # Pick a walkable adjacent tile exposed by the authoritative pathfinder.
    from app.agent.pathfinder import get_walkable_tiles

    target = next(
        (start[0] + dx, start[1] + dy)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        if (start[0] + dx, start[1] + dy) in get_walkable_tiles()
    )
    action_id = str(uuid.uuid4())
    body = {
        "action_id": action_id,
        "observation_seq": observation["observation_seq"],
        "type": "move",
        "params": {"tile_x": target[0], "tile_y": target[1]},
    }
    first = await client.post("/api/v1/agent/actions", headers=headers, json=body)
    second = await client.post("/api/v1/agent/actions", headers=headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    profile = (await db_session.execute(select(AgentPlayer))).scalar_one()
    user = await db_session.get(User, profile.user_id)
    resident = await db_session.get(Resident, profile.resident_id)
    assert (user.last_x // 32, user.last_y // 32) == target
    assert (resident.tile_x, resident.tile_y) == target
    assert profile.observation_seq == 1


@pytest.mark.anyio
async def test_action_id_cannot_be_reused_for_a_different_request(client):
    _created, _credentials, session = await _register_and_session(client)
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    action_id = str(uuid.uuid4())
    first = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": action_id,
            "observation_seq": observation["observation_seq"],
            "type": "wait",
            "params": {"seconds": 1},
        },
    )
    assert first.status_code == 200

    conflict = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": action_id,
            "observation_seq": observation["observation_seq"],
            "type": "wait",
            "params": {"seconds": 2},
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


@pytest.mark.anyio
async def test_move_rejects_collision_and_stale_observation(client):
    _created, _credentials, session = await _register_and_session(client)
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    bad = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "move",
            "params": {"tile_x": 0, "tile_y": 0},
        },
    )
    assert bad.status_code == 422

    wait = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "wait",
            "params": {"seconds": 0},
        },
    )
    assert wait.status_code == 200
    stale = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "wait",
            "params": {},
        },
    )
    assert stale.status_code == 409


@pytest.mark.anyio
async def test_durable_move_succeeds_when_realtime_projection_is_down(
    client, db_session, monkeypatch
):
    created, _credentials, session = await _register_and_session(client)
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    start = (observation["self"]["tile_x"], observation["self"]["tile_y"])

    from app.agent.pathfinder import get_walkable_tiles
    from app.ws.manager import manager

    target = next(
        (start[0] + dx, start[1] + dy)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        if (start[0] + dx, start[1] + dy) in get_walkable_tiles()
    )
    monkeypatch.setattr(
        manager,
        "update_agent_position",
        AsyncMock(side_effect=RuntimeError("redis unavailable")),
    )
    moved = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "move",
            "params": {"tile_x": target[0], "tile_y": target[1]},
        },
    )
    assert moved.status_code == 200, moved.text

    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None
    user = await db_session.get(User, profile.user_id)
    resident = await db_session.get(Resident, profile.resident_id)
    assert user is not None and resident is not None
    await db_session.refresh(user)
    await db_session.refresh(resident)
    assert (user.last_x // 32, user.last_y // 32) == target
    assert (resident.tile_x, resident.tile_y) == target


@pytest.mark.anyio
async def test_view_token_creates_read_only_cookie_and_public_projection(client):
    _created, credentials, _session = await _register_and_session(client)
    viewer = await client.post(
        "/api/v1/viewer/sessions", json={"view_token": credentials["viewer_token"]}
    )
    assert viewer.status_code == 200
    assert "httponly" in viewer.headers["set-cookie"].lower()
    snapshot = await client.get("/api/v1/viewer/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["agent"]["name"] == "测试旅者"
    assert "nearby" in snapshot.json()
    assert "balance" not in snapshot.json()["self"]
    assert "resident_id" not in snapshot.json()["self"]

    town = await client.get("/api/v1/public/town/snapshot")
    assert town.status_code == 200
    data = town.json()
    assert data["counts"]["agents"] == 1
    assert data["counts"]["humans"] == 0
    assert data["residents"][0]["kind"] == "agent"

    ended = await client.delete("/api/v1/viewer/sessions")
    assert ended.status_code == 200
    assert "max-age=0" in ended.headers["set-cookie"].lower()


@pytest.mark.anyio
async def test_viewer_snapshot_sets_private_no_store_headers(client):
    _created, credentials, _session = await _register_and_session(client)
    assert (
        await client.post(
            "/api/v1/viewer/sessions",
            json={"view_token": credentials["viewer_token"]},
        )
    ).status_code == 200

    snapshot = await client.get("/api/v1/viewer/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.headers["cache-control"] == "private, no-store"
    assert snapshot.headers["vary"] == "Cookie"


@pytest.mark.anyio
async def test_viewer_projection_safely_normalizes_malformed_role_goal(
    client, db_session
):
    created, credentials, _session = await _register_and_session(client)
    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None
    profile.role_json = {"goals": "not-an-object", "goal": {"private": "secret"}}
    await db_session.commit()

    assert (
        await client.post(
            "/api/v1/viewer/sessions",
            json={"view_token": credentials["viewer_token"]},
        )
    ).status_code == 200
    snapshot = await client.get("/api/v1/viewer/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["agent"]["current_goal"] is None


@pytest.mark.anyio
async def test_observation_and_viewer_omit_players_without_live_presence(
    client, db_session, monkeypatch
):
    from app.ws.manager import manager

    created, credentials, session = await _register_and_session(client, "观测员甲")
    created_other, _other_credentials, _other_session = await _register_and_session(
        client, "观测员乙"
    )
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    await _set_agent_position(db_session, created_other["application_id"], 76, 56)

    human_user = User(
        id="human-viewer-user",
        name="线下真人",
        email="human-viewer@example.com",
        soul_coin_balance=0,
        last_x=77 * 32 + 16,
        last_y=56 * 32 + 16,
    )
    human_resident = Resident(
        id="human-viewer-resident",
        slug="human-viewer",
        name="线下真人",
        resident_type="player",
        status="idle",
        creator_id=human_user.id,
        tile_x=77,
        tile_y=56,
    )
    db_session.add_all([human_user, human_resident])

    pending = await client.post(
        "/api/v1/agent-applications",
        json={
            "display_name": "待配对",
            "sprite_key": "埃迪",
            "client": {"name": "pytest", "version": "1"},
        },
    )
    assert pending.status_code == 201, pending.text
    pending_profile = await db_session.get(
        AgentPlayer, pending.json()["application_id"]
    )
    assert pending_profile is not None
    pending_user = await db_session.get(User, pending_profile.user_id)
    pending_resident = await db_session.get(Resident, pending_profile.resident_id)
    assert pending_user is not None and pending_resident is not None
    pending_user.last_x = 78 * 32 + 16
    pending_user.last_y = 56 * 32 + 16
    pending_resident.tile_x = 78
    pending_resident.tile_y = 56
    await db_session.commit()

    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None
    monkeypatch.setattr(
        manager,
        "get_online_players",
        AsyncMock(
            return_value=[
                {
                    "player_id": profile.user_id,
                    "x": 75 * 32 + 16,
                    "y": 56 * 32 + 16,
                    "direction": "down",
                    "name": "观测员甲",
                }
            ]
        ),
    )

    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = await client.get("/api/v1/agent/observation", headers=headers)
    assert observation.status_code == 200, observation.text
    observed_players = observation.json()["nearby"]["players"]
    assert observed_players == []

    assert (
        await client.post(
            "/api/v1/viewer/sessions",
            json={"view_token": credentials["viewer_token"]},
        )
    ).status_code == 200
    snapshot = await client.get("/api/v1/viewer/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    payload = snapshot.json()
    assert payload["nearby"]["players"] == []
    assert payload["self"]["tile_x"] == 75
    assert payload["self"]["tile_y"] == 56
    assert payload["location"] == {"slug": "central_plaza", "name": "中央广场"}


@pytest.mark.anyio
async def test_production_viewer_cookie_supports_cross_site_frontend(
    client, monkeypatch
):
    _created, credentials, _session = await _register_and_session(client)
    monkeypatch.setattr(settings, "debug", False)
    response = await client.post(
        "/api/v1/viewer/sessions",
        json={"view_token": credentials["viewer_token"]},
    )
    cookie = response.headers["set-cookie"].lower()
    assert "samesite=none" in cookie
    assert "secure" in cookie


@pytest.mark.anyio
async def test_private_agent_is_omitted_without_becoming_a_human_count(client):
    application = await client.post(
        "/api/v1/agent-applications",
        json={
            "display_name": "隐身旅者",
            "public_visible": False,
            "client": {"name": "pytest", "version": "1"},
        },
    )
    assert application.status_code == 201

    town = await client.get("/api/v1/public/town/snapshot")
    assert town.status_code == 200
    payload = town.json()
    assert payload["counts"]["agents"] == 0
    assert payload["counts"]["humans"] == 0
    assert payload["counts"]["residents"] == 0
    assert payload["residents"] == []


@pytest.mark.anyio
async def test_agent_presence_expires_without_a_rest_heartbeat(
    client, db_session, monkeypatch
):
    from app.services import agent_player_service

    # 本测同一测试内两次取快照断言不同结果,关掉 3s TTL 缓存(F3)以取实时值
    monkeypatch.setattr(agent_player_service, "_SNAPSHOT_CACHE_TTL_SECONDS", 0.0)
    created, credentials, _session = await _register_and_session(client)
    live = (await client.get("/api/v1/public/town/snapshot")).json()
    assert live["counts"]["online"] == 1

    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None
    profile.last_seen_at = datetime.now(UTC) - timedelta(
        seconds=settings.agent_presence_ttl_seconds + 1
    )
    await db_session.commit()

    stale = (await client.get("/api/v1/public/town/snapshot")).json()
    assert stale["counts"]["online"] == 0
    viewer = await client.post(
        "/api/v1/viewer/sessions", json={"view_token": credentials["viewer_token"]}
    )
    assert viewer.status_code == 200
    view = (await client.get("/api/v1/viewer/snapshot")).json()
    assert view["agent"]["is_online"] is False


@pytest.mark.anyio
async def test_snapshot_cached_within_ttl(db_session, monkeypatch):
    """F3: 公共快照 3s TTL 内复用,第二次调用零 DB 查询。"""
    from app.services import agent_player_service

    calls = {"n": 0}
    original_execute = db_session.execute

    async def counting_execute(*args, **kwargs):
        calls["n"] += 1
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(db_session, "execute", counting_execute)

    first = await agent_player_service.public_town_snapshot(db_session)
    queries_first = calls["n"]
    assert queries_first > 0

    second = await agent_player_service.public_town_snapshot(db_session)
    assert calls["n"] == queries_first
    assert second == first


@pytest.mark.anyio
async def test_snapshot_online_bulk(db_session, monkeypatch):
    """F3: 真人在线判定走一次批查集合,不逐人 await is_online。"""
    import json

    from app.redis_client import get_redis
    from app.services import agent_player_service
    from app.ws.manager import POSITIONS_KEY, manager

    online_human = User(
        id="human-bulk-online",
        name="真人甲",
        email="human-bulk-online@example.com",
        soul_coin_balance=0,
    )
    online_avatar = Resident(
        id="resident-human-bulk-online",
        slug="human-bulk-online",
        name="真人化身",
        resident_type="player",
        status="idle",
        creator_id=online_human.id,
        tile_x=75,
        tile_y=56,
    )
    offline_human = User(
        id="human-bulk-offline",
        name="真人乙",
        email="human-bulk-offline@example.com",
        soul_coin_balance=0,
    )
    offline_avatar = Resident(
        id="resident-human-bulk-offline",
        slug="human-bulk-offline",
        name="离线化身",
        resident_type="player",
        status="idle",
        creator_id=offline_human.id,
        tile_x=60,
        tile_y=50,
    )
    db_session.add_all([online_human, online_avatar, offline_human, offline_avatar])
    await db_session.commit()

    await get_redis().hset(
        POSITIONS_KEY,
        online_human.id,
        json.dumps({"x": 75 * 32 + 16, "y": 56 * 32 + 16}),
    )

    async def _forbidden(user_id: str) -> bool:
        raise AssertionError("public_town_snapshot 不得逐人调用 is_online")

    monkeypatch.setattr(manager, "is_online", _forbidden)

    snapshot = await agent_player_service.public_town_snapshot(db_session)
    assert snapshot["counts"]["humans"] == 2
    assert snapshot["counts"]["online"] == 1


@pytest.mark.anyio
async def test_observation_bbox_keeps_players(client, db_session, monkeypatch):
    """F3: observation 的 residents 查询在 SQL 层按 bbox 裁剪 npc,
    但 player 行豁免(其坐标来自 presence 覆盖,DB tile 可能陈旧)。"""
    from sqlalchemy.sql import Select

    from app.services import agent_player_service
    from app.ws.manager import manager

    created, _credentials, _session = await _register_and_session(client, "边界观察员")
    await _set_agent_position(db_session, created["application_id"], 75, 56)
    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None

    near_npc = Resident(
        id="resident-bbox-near-npc",
        slug="bbox-near-npc",
        name="邻座居民",
        resident_type="npc",
        status="idle",
        tile_x=76,
        tile_y=56,
    )
    far_npc = Resident(
        id="resident-bbox-far-npc",
        slug="bbox-far-npc",
        name="远方居民",
        resident_type="npc",
        status="idle",
        tile_x=10,
        tile_y=10,
    )
    stale_owner = User(
        id="user-bbox-stale-player",
        name="漂移真人",
        email="bbox-stale-player@example.com",
        soul_coin_balance=0,
    )
    stale_player = Resident(
        id="resident-bbox-stale-player",
        slug="bbox-stale-player",
        name="漂移化身",
        resident_type="player",
        status="idle",
        creator_id=stale_owner.id,
        # DB tile 早已陈旧,远在 bbox 之外;presence 会把它放回 agent 身边
        tile_x=5,
        tile_y=5,
    )
    db_session.add_all([near_npc, far_npc, stale_owner, stale_player])
    await db_session.commit()

    monkeypatch.setattr(
        manager,
        "get_online_players",
        AsyncMock(
            return_value=[
                {
                    "player_id": stale_owner.id,
                    "x": 76 * 32 + 16,
                    "y": 56 * 32 + 16,
                }
            ]
        ),
    )

    captured: list[list[Resident]] = []
    original_execute = db_session.execute

    async def spying_execute(statement, *args, **kwargs):
        if isinstance(statement, Select):
            descriptions = statement.column_descriptions
            if len(descriptions) == 1 and descriptions[0].get("entity") is Resident:
                probe = await original_execute(statement, *args, **kwargs)
                captured.append(list(probe.scalars().all()))
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", spying_execute)

    data = await agent_player_service.observation(
        db_session, profile, include_private_events=False
    )

    assert len(captured) == 1
    loaded_ids = {row.id for row in captured[0]}
    assert far_npc.id not in loaded_ids  # 半径外 npc 在 SQL 层被裁掉
    assert near_npc.id in loaded_ids
    assert stale_player.id in loaded_ids  # player 行豁免 bbox

    nearby_resident_ids = {item["id"] for item in data["nearby"]["residents"]}
    assert near_npc.id in nearby_resident_ids
    assert far_npc.id not in nearby_resident_ids
    nearby_player_ids = {item["id"] for item in data["nearby"]["players"]}
    assert stale_player.id in nearby_player_ids


@pytest.mark.anyio
async def test_registration_gate_defaults_closed_outside_debug(client, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "agent_self_registration_enabled", False)
    response = await client.post(
        "/api/v1/agent-applications",
        json={"display_name": "不应创建", "client": {"name": "pytest", "version": "1"}},
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_agent_payload_sizes_are_bounded(client):
    oversized_role = await client.post(
        "/api/v1/agent-applications",
        json={
            "display_name": "过大角色卡",
            "role_card": {"notes": "x" * (33 * 1024)},
            "client": {"name": "pytest", "version": "1"},
        },
    )
    assert oversized_role.status_code == 413

    _created, _credentials, session = await _register_and_session(client)
    headers = {"Authorization": f"Bearer {session['session_token']}"}
    observation = (await client.get("/api/v1/agent/observation", headers=headers)).json()
    oversized_action = await client.post(
        "/api/v1/agent/actions",
        headers=headers,
        json={
            "action_id": str(uuid.uuid4()),
            "observation_seq": observation["observation_seq"],
            "type": "wait",
            "params": {"padding": "x" * (9 * 1024)},
        },
    )
    assert oversized_action.status_code == 413
