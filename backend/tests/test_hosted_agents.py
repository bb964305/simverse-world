import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.agent_player import (
    AgentActionReceipt,
    AgentNpcChatTurnReceipt,
    AgentPlayer,
)
from app.models.hosted_agent import (
    HostedAgentController,
    HostedAgentDailyUsage,
    HostedAgentTurn,
)
from app.models.resident import Resident
from app.models.user import User
from app.models.web3_agent_passport import Web3AgentPassport
from app.routers.agent_players import _terminalize_hosted_npc_failure
from app.services.auth_service import create_token
from app.services.agent_player_service import (
    _agent_is_online,
    agent_presence_ttl_seconds,
)
from app.services.hosted_agent_provider import (
    HostedGeneratedIdentity,
    HostedModelDecision,
    HostedOpenAIClient,
    HostedProviderError,
    HostedProviderResult,
    HostedProviderUsage,
    derive_hosted_identity,
    deterministic_public_action_summary,
    hosted_decision_token_reservation,
    normalize_hosted_provider_token_usage,
)
from app.services.hosted_agent_service import (
    HOSTED_DECISION_FIELD,
    HOSTED_RESULT_FIELD,
    HostedAgentError,
    adopt_recoverable_turn,
    begin_provider_stage_call,
    completed_provider_stage_result,
    complete_provider_stage_call,
    controller_public,
    controller_state,
    create_controller,
    create_hosted_identity,
    decrypt_secret_bundle,
    decrypt_turn_value,
    encrypt_secret_bundle,
    encrypt_turn_value,
    reserve_daily_budget,
    reserve_turn_provider_budget,
    reconcile_private_journal,
    set_desired_status,
    settle_daily_budget,
)


def _keyring_json() -> str:
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    return json.dumps({"test-key": key})


@pytest.fixture
def hosted_settings(monkeypatch):
    monkeypatch.setattr(settings, "hosted_agent_runner_enabled", True)
    monkeypatch.setattr(settings, "hosted_agent_runner_active_key_id", "test-key")
    monkeypatch.setattr(
        settings, "hosted_agent_runner_keyring", SecretStr(_keyring_json())
    )


def _generated_identity(**overrides) -> HostedGeneratedIdentity:
    payload = {
        "resident": {
            "age": 31,
            "occupation": "木匠",
            "background": "在山脚村落学习修缮家具和木屋。",
            "arrival_story": "听说港湾需要修缮工，于初秋乘船来到这里。",
            "appearance": "常穿深绿色围裙，袖口沾着少许木屑。",
            "home_aspiration": "希望在河边租一间带小工作台的屋子。",
        },
        "personality": {
            "traits": ["耐心", "务实", "好奇"],
            "speaking_style": "说话简洁温和，习惯先听完再回答。",
        },
        "life": {
            "values": ["可靠", "互助"],
            "routines": ["清晨整理工具", "傍晚散步"],
            "interests": ["木工", "观鸟"],
            "social_instinct": "遇到邻居会主动问候，但不打断别人的工作。",
            "relationship_approach": "通过守约和小范围互助逐渐建立信任。",
            "seed_memories": [
                "小时候曾修好一只被雨淋坏的木箱。",
                "启程前在旧码头看过一群向南飞的鸟。",
            ],
        },
        "private_goal": "悄悄练习雕刻一枚能送给未来朋友的木叶书签。",
        "introduction": "新来的木匠，愿意从修一把椅子开始认识这里。",
    }
    payload.update(overrides)
    return HostedGeneratedIdentity.model_validate(payload)


def _assert_write_only_hosted_projection(payload, secret: str) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    assert secret not in encoded
    assert '"api_key"' not in encoded
    assert '"secret_envelope"' not in encoded


@pytest.mark.anyio
async def test_confirmed_passport_owner_can_access_personal_hosted_runtime(
    client, db_session, hosted_settings
):
    user = User(
        id=str(uuid.uuid4()), name="Wallet owner",
        email=f"wallet-owner-{uuid.uuid4()}@test.com",
        wallet_address="0x1111111111111111111111111111111111111111",
        is_admin=False, is_banned=False,
    )
    db_session.add(user)
    await db_session.flush()
    resident = Resident(
        id=str(uuid.uuid4()), slug=f"wallet-resident-{uuid.uuid4()}",
        name="Wallet resident", creator_id=user.id,
    )
    db_session.add(resident)
    await db_session.flush()
    user.player_resident_id = resident.id
    db_session.add(Web3AgentPassport(
        user_id=user.id,
        resident_id=resident.id,
        chain_id=4663,
        registry_address="0x2222222222222222222222222222222222222222",
        agent_id="1",
        resident_key="0x" + "33" * 32,
        registration_tx_hash="0x" + "44" * 32,
        metadata_uri="https://simverse.example/passport/1",
        metadata_hash="0x" + "55" * 32,
    ))
    await db_session.commit()

    response = await client.get(
        "/admin/hosted-agents",
        headers={"Authorization": f"Bearer {create_token(user.id)}"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0}


async def _register_test_agent(client, name: str):
    application = await client.post(
        "/api/v1/agent-applications",
        json={
            "display_name": name,
            "sprite_key": "埃迪",
            "model_label": "hosted-test-model",
            "role_card": {"goals": {"public": "认识邻居"}},
            "client": {"name": "hosted-test", "version": "1"},
        },
    )
    assert application.status_code == 201, application.text
    created = application.json()
    redeemed = await client.post(
        "/api/v1/agent-pairings/redeem",
        json={
            "application_id": created["application_id"],
            "pairing_code": created["pairing_code"],
        },
    )
    assert redeemed.status_code == 200, redeemed.text
    play_token = redeemed.json()["agent_token"]
    session = await client.post(
        "/api/v1/agent-sessions",
        headers={"Authorization": f"Bearer {play_token}"},
        json={"client": {"name": "hosted-test", "version": "1"}},
    )
    assert session.status_code == 200, session.text
    return created, session.json()["session_token"]


async def _seed_hosted_fence(
    db_session,
    *,
    profile: AgentPlayer,
    action_type: str,
    observation_seq: int,
    event_cursor: int,
):
    owner = User(
        id=str(uuid.uuid4()),
        name="Hosted owner",
        email=f"hosted-owner-{uuid.uuid4()}@test.com",
        is_admin=True,
    )
    controller_id = str(uuid.uuid4())
    action_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    controller = HostedAgentController(
        id=controller_id,
        owner_user_id=owner.id,
        request_id=str(uuid.uuid4()),
        create_request_hash=uuid.uuid4().hex * 2,
        agent_player_id=profile.id,
        desired_status="running",
        runtime_status="claimed",
        provider_host="api.example.com",
        model="model",
        provider_validation_required=False,
        secret_envelope=encrypt_secret_bundle(
            controller_id,
            {
                "api_key": "arbitraryCredentialXYZ",
                "base_url": "https://api.example.com/v1",
                "private_identity": {},
                "journal": [],
                "disclosed_player_slugs": [],
            },
        ),
        identity_json={"display_name": "Hosted", "goals": {"public": "认识邻居"}},
        policy_json={},
        lease_owner="hosted-test-worker",
        lease_token="hosted-test-lease",
        lease_epoch=1,
        lease_expires_at=now + timedelta(minutes=3),
    )
    turn = HostedAgentTurn(
        id=turn_id,
        controller_id=controller_id,
        sequence=1,
        state="committing",
        lease_epoch=1,
        control_version=1,
        observation_seq=observation_seq,
        event_cursor=event_cursor,
        observation_envelope=encrypt_turn_value(
            turn_id=turn_id,
            field_name="observation",
            value={
                "observation_seq": observation_seq,
                "event_cursor": event_cursor,
                "recent_events": [],
            },
        ),
        decision_version=1,
        decision_envelope=encrypt_turn_value(
            turn_id=turn_id,
            field_name=HOSTED_DECISION_FIELD,
            value={"action": action_type},
        ),
        action_id=action_id,
        action_type=action_type,
        budget_date=now.date(),
    )
    usage = HostedAgentDailyUsage(
        controller_id=controller_id,
        usage_date=now.date(),
    )
    profile.control_kind = "hosted_agent"
    db_session.add_all([owner, controller, turn, usage])
    await db_session.commit()
    fence_headers = {
        "X-Simverse-Hosted-Controller-ID": controller_id,
        "X-Simverse-Hosted-Lease-Token": controller.lease_token,
        "X-Simverse-Hosted-Lease-Epoch": str(controller.lease_epoch),
        "X-Simverse-Hosted-Control-Version": str(controller.control_version),
        "X-Simverse-Hosted-Turn-ID": turn_id,
        "X-Simverse-Hosted-Event-Cursor": str(event_cursor),
    }
    return controller, turn, usage, action_id, fence_headers


def test_private_identity_never_duplicates_public_identity():
    generated = _generated_identity()
    public_identity, role_card, private_identity = derive_hosted_identity(
        generated=generated,
        display_name="林澄",
        model_label="town-model",
        sprite_key="埃迪",
        public_goal="帮助修缮公共空间并认识邻居",
    )
    public_json = json.dumps(
        {"identity": public_identity, "role_card": role_card}, ensure_ascii=False
    )
    assert private_identity["private_goal"] not in public_json
    for memory in private_identity["seed_memories"]:
        assert memory not in public_json

    payload = generated.model_dump()
    payload["private_goal"] = payload["resident"]["background"]
    overlapping = HostedGeneratedIdentity.model_validate(payload)
    with pytest.raises(ValueError, match="overlaps public identity"):
        derive_hosted_identity(
            generated=overlapping,
            display_name="林澄",
            model_label="town-model",
            sprite_key="埃迪",
            public_goal="帮助修缮公共空间并认识邻居",
        )


@pytest.mark.parametrize(
    "unsafe_text",
    ["I'm a real human, not an AI.", "我是现实中的真人，不是人工智能。"],
)
@pytest.mark.anyio
async def test_provider_rejects_deceptive_outbound_identity_claims(
    monkeypatch, unsafe_text
):
    usage = HostedProviderUsage(
        calls=1,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        latency_ms=1,
    )

    async def fake_completion(*_args, **_kwargs):
        return HostedProviderResult(
            content=json.dumps(
                {
                    "action": "message_player",
                    "player_slug": "nearby-player",
                    "text": unsafe_text,
                    "summary": "打招呼",
                }
            ),
            usage=usage,
        )

    provider = HostedOpenAIClient(
        base_url="https://api.example.com/v1",
        api_key=SecretStr("arbitraryCredentialXYZ"),
        model="test-model",
    )
    monkeypatch.setattr(provider, "_completion", fake_completion)
    try:
        with pytest.raises(HostedProviderError) as caught:
            await provider.decide(
                observation={},
                public_identity={},
                private_identity={},
                max_tokens=100,
            )
        assert caught.value.code == "provider_message_unsafe"
    finally:
        await provider.aclose()


def test_public_action_summary_ignores_provider_summary_and_message():
    private_marker = "provider-private-summary-marker"
    decision = HostedModelDecision.model_validate(
        {
            "action": "message_player",
            "player_slug": "nearby-player",
            "text": "私密正文也不能进公开日志",
            "summary": private_marker,
        }
    )
    summary = deterministic_public_action_summary(decision)
    assert summary == "与玩家 nearby-player 交谈"
    assert private_marker not in summary
    assert decision.text not in summary


@pytest.mark.parametrize(
    ("usage", "minimum_total", "expected_components"),
    [
        ({"total_tokens": 77}, 77, None),
        (
            {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 20},
            20,
            (17, 3),
        ),
        (
            {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 5},
            18,
            (10, 8),
        ),
    ],
)
def test_provider_usage_components_always_cover_reported_total(
    usage, minimum_total, expected_components
):
    input_tokens, output_tokens, total_tokens = normalize_hosted_provider_token_usage(
        usage=usage,
        system="system",
        user="user",
        content="content",
    )
    assert input_tokens + output_tokens == total_tokens
    assert total_tokens >= minimum_total
    if expected_components is not None:
        assert (input_tokens, output_tokens) == expected_components


@pytest.mark.anyio
async def test_provider_read_timeout_never_replays_on_another_ip(monkeypatch):
    requests: list[httpx.Request] = []

    def ambiguous_read_timeout(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("response timed out", request=request)

    provider = HostedOpenAIClient(
        base_url="https://api.example.com/v1",
        api_key=SecretStr("arbitraryCredentialXYZ"),
        model="test-model",
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(ambiguous_read_timeout),
        trust_env=False,
    )
    monkeypatch.setattr(
        provider,
        "_request_targets",
        AsyncMock(
            return_value=[
                (
                    "https://203.0.113.10/v1/chat/completions",
                    {"Host": "api.example.com"},
                    {"sni_hostname": "api.example.com"},
                ),
                (
                    "https://203.0.113.11/v1/chat/completions",
                    {"Host": "api.example.com"},
                    {"sni_hostname": "api.example.com"},
                ),
            ]
        ),
    )
    try:
        with pytest.raises(HostedProviderError) as caught:
            await provider._completion(system="system", user="user", max_tokens=32)
        assert caught.value.code == "provider_unavailable"
        assert caught.value.outcome_unknown is True
        assert caught.value.definitively_unbilled is False
        assert len(requests) == 1
        assert requests[0].url.host == "203.0.113.10"
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_provider_rate_limit_is_marked_definitively_unbilled(monkeypatch):
    provider = HostedOpenAIClient(
        base_url="https://api.example.com/v1",
        api_key=SecretStr("arbitraryCredentialXYZ"),
        model="test-model",
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, request=request)
        ),
        trust_env=False,
    )
    monkeypatch.setattr(
        provider,
        "_request_targets",
        AsyncMock(
            return_value=[
                (
                    "https://203.0.113.10/v1/chat/completions",
                    {"Host": "api.example.com"},
                    {"sni_hostname": "api.example.com"},
                )
            ]
        ),
    )
    try:
        with pytest.raises(HostedProviderError) as caught:
            await provider._completion(system="system", user="user", max_tokens=32)
        assert caught.value.code == "provider_rate_limited"
        assert caught.value.definitively_unbilled is True
        assert caught.value.outcome_unknown is False
    finally:
        await provider.aclose()


@pytest.mark.parametrize("action", ["message_player", "npc_chat_turn"])
@pytest.mark.anyio
async def test_provider_cannot_echo_private_identity_into_outbound_text(
    monkeypatch, action
):
    private_memory = "启程前在旧码头看过一群向南飞的鸟"
    payload = {
        "action": action,
        "text": f"我记得{private_memory}。",
        "summary": "聊聊往事",
    }
    if action == "message_player":
        payload["player_slug"] = "nearby-player"
    else:
        payload["resident_slug"] = "nearby-npc"
    usage = HostedProviderUsage(
        calls=1,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        latency_ms=1,
    )

    async def fake_completion(*_args, **_kwargs):
        return HostedProviderResult(content=json.dumps(payload), usage=usage)

    provider = HostedOpenAIClient(
        base_url="https://api.example.com/v1",
        api_key=SecretStr("arbitraryCredentialXYZ"),
        model="test-model",
    )
    monkeypatch.setattr(provider, "_completion", fake_completion)
    try:
        with pytest.raises(HostedProviderError) as caught:
            await provider.decide(
                observation={},
                public_identity={},
                private_identity={
                    "identity": {
                        "private_goal": "雕刻一枚木叶书签",
                        "seed_memories": [private_memory],
                    }
                },
                max_tokens=100,
            )
        assert caught.value.code == "provider_private_identity_leak"
    finally:
        await provider.aclose()


@pytest.mark.parametrize("action", ["message_player", "npc_chat_turn"])
@pytest.mark.parametrize(
    "private_source",
    ["current_inbox", "journal_inbox", "journal_npc_reply"],
)
@pytest.mark.anyio
async def test_provider_cannot_echo_private_continuity_or_inbox_text(
    monkeypatch, action, private_source
):
    private_text = "只给收件人的蓝鲸七号暗语，不能转告镇上其他人"
    private_context = {
        "identity": {
            "private_goal": "练习雕刻一枚木叶书签",
            "seed_memories": ["小时候曾在山脚下照看过一间木工棚"],
        },
        "journal": [],
    }
    observation = {
        "recent_events": [],
        "nearby": {
            "players": [{"slug": "nearby-player", "name": "邻居"}],
            "residents": [{"slug": "nearby-npc", "name": "木匠"}],
        },
    }
    if private_source == "current_inbox":
        observation["recent_events"] = [
            {
                "seq": 8,
                "kind": "player_message",
                "from": {"slug": "sender"},
                "text": private_text,
            }
        ]
    elif private_source == "journal_inbox":
        private_context["journal"] = [
            {
                "turn": 3,
                "events": [
                    {"kind": "player_message", "text": private_text}
                ],
                "summary": "完成了一次小镇行动",
                "result": {},
            }
        ]
    else:
        private_context["journal"] = [
            {
                "turn": 4,
                "events": [],
                "summary": "与居民交谈",
                "result": {"reply": private_text},
            }
        ]

    payload = {
        "action": action,
        "text": f"我把这段话告诉你：{private_text}。",
        "summary": "继续交谈",
    }
    if action == "message_player":
        payload["player_slug"] = "nearby-player"
    else:
        payload["resident_slug"] = "nearby-npc"
    usage = HostedProviderUsage(
        calls=1,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        latency_ms=1,
    )

    async def fake_completion(*_args, **_kwargs):
        return HostedProviderResult(content=json.dumps(payload), usage=usage)

    provider = HostedOpenAIClient(
        base_url="https://api.example.com/v1",
        api_key=SecretStr("arbitraryCredentialXYZ"),
        model="test-model",
    )
    monkeypatch.setattr(provider, "_completion", fake_completion)
    try:
        with pytest.raises(HostedProviderError) as caught:
            await provider.decide(
                observation=observation,
                public_identity={},
                private_identity=private_context,
                max_tokens=100,
            )
        assert caught.value.code == "provider_private_identity_leak"
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_provider_rejects_long_private_fragment_with_changed_edges(monkeypatch):
    private_text = "启程之前约定的暗号是蓝鲸七号并且只能在雨夜使用"
    leaked_fragment = "我只记得约定的暗号是蓝鲸七号并且只能在晴天改用"
    assert private_text not in leaked_fragment
    assert leaked_fragment not in private_text
    usage = HostedProviderUsage(
        calls=1,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        latency_ms=1,
    )

    async def fake_completion(*_args, **_kwargs):
        return HostedProviderResult(
            content=json.dumps(
                {
                    "action": "message_player",
                    "player_slug": "nearby-player",
                    "text": leaked_fragment,
                    "summary": "提起一个约定",
                }
            ),
            usage=usage,
        )

    provider = HostedOpenAIClient(
        base_url="https://api.example.com/v1",
        api_key=SecretStr("arbitraryCredentialXYZ"),
        model="test-model",
    )
    monkeypatch.setattr(provider, "_completion", fake_completion)
    try:
        with pytest.raises(HostedProviderError) as caught:
            await provider.decide(
                observation={"recent_events": []},
                public_identity={},
                private_identity={
                    "identity": {},
                    "journal": [{"events": [{"text": private_text}]}],
                },
                max_tokens=100,
            )
        assert caught.value.code == "provider_private_identity_leak"
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_public_town_observation_and_safe_journal_summary_are_not_private(
    monkeypatch,
):
    public_text = "中央广场的喷泉今天在阳光下显得格外明亮"
    usage = HostedProviderUsage(
        calls=1,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        latency_ms=1,
    )

    async def fake_completion(*_args, **_kwargs):
        return HostedProviderResult(
            content=json.dumps(
                {
                    "action": "message_player",
                    "player_slug": "nearby-player",
                    "text": public_text,
                    "summary": "谈论广场景色",
                }
            ),
            usage=usage,
        )

    provider = HostedOpenAIClient(
        base_url="https://api.example.com/v1",
        api_key=SecretStr("arbitraryCredentialXYZ"),
        model="test-model",
    )
    monkeypatch.setattr(provider, "_completion", fake_completion)
    try:
        decision, _ = await provider.decide(
            observation={
                "recent_events": [],
                "nearby": {
                    "players": [
                        {
                            "slug": "nearby-player",
                            "name": public_text,
                        }
                    ]
                },
            },
            public_identity={},
            private_identity={
                "identity": {},
                "journal": [
                    {
                        "summary": public_text,
                        "result": {
                            "recipient": {"name": public_text},
                        },
                    }
                ],
            },
            max_tokens=100,
        )
        assert decision.text == public_text
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_create_rejects_request_marker_as_key_and_all_projections_stay_write_only(
    client, db_session, monkeypatch, hosted_settings
):
    async def safe_provider_url(_value):
        return "https://api.example.com/v1", "api.example.com"

    monkeypatch.setattr(
        "app.routers.admin.hosted_agents.validate_hosted_provider_base_url",
        safe_provider_url,
    )
    admin = User(
        id=str(uuid.uuid4()),
        name="Admin",
        email="hosted-request-marker-admin@test.com",
        is_admin=True,
        is_banned=False,
    )
    db_session.add(admin)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(admin.id)}"}
    request_marker = str(uuid.uuid4())
    base_payload = {
        "request_id": request_marker,
        "base_url": "https://api.example.com/v1",
        "model": "hosted-model",
        "display_name": "林澄",
        "goal": "在集市帮邻居修理家具",
    }

    reflected = await client.post(
        "/admin/hosted-agents",
        headers=headers,
        json={**base_payload, "api_key": request_marker},
    )
    assert reflected.status_code == 422
    assert reflected.json() == {
        "detail": {
            "code": "invalid_hosted_agent_request",
            "message": "Invalid Hosted Agent request",
        }
    }
    assert request_marker not in reflected.text
    assert (
        await db_session.execute(select(HostedAgentController))
    ).scalars().all() == []

    # The same frontend recovery marker remains usable when paired with a
    # distinct credential. Every admin projection may expose the marker but
    # must keep the actual key write-only.
    actual_key = "safeCredentialForProjectionTest"
    created = await client.post(
        "/admin/hosted-agents",
        headers=headers,
        json={**base_payload, "api_key": actual_key},
    )
    assert created.status_code == 202, created.text
    created_payload = created.json()
    assert created_payload["request_id"] == request_marker
    _assert_write_only_hosted_projection(created_payload, actual_key)

    controller = (
        await db_session.execute(
            select(HostedAgentController).where(
                HostedAgentController.owner_user_id == admin.id
            )
        )
    ).scalar_one()
    assert decrypt_secret_bundle(controller)["api_key"] == actual_key
    assert decrypt_secret_bundle(controller)["api_key"] != request_marker

    listed = await client.get("/admin/hosted-agents", headers=headers)
    detailed = await client.get(
        f"/admin/hosted-agents/{controller.id}", headers=headers
    )
    state = await client.get(
        f"/admin/hosted-agents/{controller.id}/state", headers=headers
    )
    assert listed.status_code == detailed.status_code == state.status_code == 200
    for payload in (
        listed.json(),
        detailed.json(),
        state.json(),
        controller_public(controller),
    ):
        _assert_write_only_hosted_projection(payload, actual_key)
    assert listed.json()["items"][0]["request_id"] == request_marker
    assert detailed.json()["request_id"] == request_marker
    assert state.json()["request_id"] == request_marker


@pytest.mark.anyio
async def test_patch_rejects_old_key_reflection_and_updates_public_projections(
    client, db_session, hosted_settings
):
    admin = User(
        id=str(uuid.uuid4()),
        name="Admin",
        email="hosted-admin@test.com",
        is_admin=True,
        is_banned=False,
    )
    avatar_user = User(
        id=str(uuid.uuid4()),
        name="林澄",
        email="hosted-avatar@test.com",
        settings_json={},
    )
    resident = Resident(
        id=str(uuid.uuid4()),
        slug="lin-cheng",
        name="林澄",
        resident_type="player",
        ability_md="Occupation: 木匠.",
        persona_md="A patient town resident.",
        soul_md="Values: reliability. Present public goal: 旧目标",
    )
    profile = AgentPlayer(
        id=str(uuid.uuid4()),
        user_id=avatar_user.id,
        resident_id=resident.id,
        control_kind="hosted_agent",
        model_label="old-model",
        client_json={"name": "simverse-hosted-agent"},
        role_json={
            "goals": {"public": "旧目标"},
            "ability_md": resident.ability_md,
            "persona_md": resident.persona_md,
            "soul_md": resident.soul_md,
        },
    )
    controller_id = str(uuid.uuid4())
    old_key = "arbitraryCredentialXYZ"
    controller = HostedAgentController(
        id=controller_id,
        owner_user_id=admin.id,
        request_id=str(uuid.uuid4()),
        create_request_hash="a" * 64,
        agent_player_id=profile.id,
        desired_status="running",
        runtime_status="idle",
        control_version=1,
        provider_host="api.example.com",
        model="old-model",
        provider_validation_required=False,
        secret_version=1,
        secret_envelope=encrypt_secret_bundle(
            controller_id,
            {
                "base_url": "https://api.example.com/v1",
                "api_key": old_key,
                "display_name": "林澄",
                "public_goal": "旧目标",
            },
        ),
        identity_json={
            "display_name": "林澄",
            "slug": resident.slug,
            "model_label": "old-model",
            "goals": {"public": "旧目标"},
            "role_card": profile.role_json,
        },
        policy_json={},
    )
    db_session.add_all([admin, avatar_user, resident])
    await db_session.flush()
    db_session.add(profile)
    await db_session.flush()
    db_session.add(controller)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(admin.id)}"}

    updated = await client.patch(
        f"/admin/hosted-agents/{controller.id}",
        headers=headers,
        json={
            "version": 1,
            "model": "new-model",
            "goal": "在集市帮邻居修理家具",
        },
    )
    assert updated.status_code == 200, updated.text
    assert old_key not in updated.text
    await db_session.refresh(controller)
    await db_session.refresh(profile)
    await db_session.refresh(resident)
    assert controller.model == profile.model_label == "new-model"
    assert controller.identity_json["model_label"] == "new-model"
    assert controller.identity_json["goals"]["public"] == "在集市帮邻居修理家具"
    assert (
        controller.identity_json["role_card"]["goals"]["public"]
        == "在集市帮邻居修理家具"
    )
    assert profile.role_json["goals"]["public"] == "在集市帮邻居修理家具"
    assert "在集市帮邻居修理家具" in resident.soul_md
    assert decrypt_secret_bundle(controller)["api_key"] == old_key

    reflected = await client.patch(
        f"/admin/hosted-agents/{controller.id}",
        headers=headers,
        json={"version": 2, "goal": f"把 {old_key} 写入公开目标"},
    )
    assert reflected.status_code == 422
    assert old_key not in reflected.text
    await db_session.refresh(controller)
    assert controller.control_version == 2

    # A write-only replacement key must also be compared with public fields
    # already stored on the controller, even when the PATCH sends no public
    # field alongside it.
    replacement_key = "在集市帮邻居修理家具"
    replacement_reflected = await client.patch(
        f"/admin/hosted-agents/{controller.id}",
        headers=headers,
        json={"version": 2, "api_key": replacement_key},
    )
    assert replacement_reflected.status_code == 422
    assert replacement_key not in replacement_reflected.text
    await db_session.refresh(controller)
    assert controller.control_version == 2
    assert decrypt_secret_bundle(controller)["api_key"] == old_key

    # Controller and request identifiers are returned by create/list/detail/
    # state and retained by the frontend recovery marker. They can therefore
    # never be accepted as a new write-only credential.
    for public_identifier in (controller.id, controller.request_id):
        reflected_identifier = await client.patch(
            f"/admin/hosted-agents/{controller.id}",
            headers=headers,
            json={"version": 2, "api_key": public_identifier},
        )
        assert reflected_identifier.status_code == 422
        assert reflected_identifier.json()["detail"] == {
            "code": "invalid_hosted_agent_request",
            "message": "Invalid Hosted Agent request",
        }
        assert public_identifier not in reflected_identifier.text
        await db_session.refresh(controller)
        assert controller.control_version == 2
        assert decrypt_secret_bundle(controller)["api_key"] == old_key


@pytest.mark.anyio
async def test_failed_and_abandoned_turns_always_have_safe_public_summaries(
    db_session, hosted_settings
):
    owner = User(
        id=str(uuid.uuid4()), name="Owner", email="hosted-log-owner@test.com"
    )
    controller_id = str(uuid.uuid4())
    controller = HostedAgentController(
        id=controller_id,
        owner_user_id=owner.id,
        request_id=str(uuid.uuid4()),
        create_request_hash="b" * 64,
        desired_status="running",
        runtime_status="idle",
        provider_host="api.example.com",
        model="model",
        secret_envelope=encrypt_secret_bundle(
            controller_id,
            {"api_key": "arbitraryCredentialXYZ", "base_url": "https://api.example.com/v1"},
        ),
        identity_json={"display_name": "林澄", "goals": {"public": "认识邻居"}},
        policy_json={},
    )
    failed = HostedAgentTurn(
        id=str(uuid.uuid4()),
        controller_id=controller_id,
        sequence=1,
        state="failed",
        lease_epoch=0,
        control_version=1,
        observation_envelope="opaque",
        public_summary=None,
        error_code="unsafe_provider_detail_must_not_be_summary",
    )
    abandoned = HostedAgentTurn(
        id=str(uuid.uuid4()),
        controller_id=controller_id,
        sequence=2,
        state="abandoned",
        lease_epoch=0,
        control_version=1,
        observation_envelope="opaque",
        public_summary=None,
    )
    db_session.add_all([owner, controller, failed, abandoned])
    await db_session.commit()

    state = await controller_state(db_session, controller=controller)
    assert [item["summary"] for item in state["logs"]] == [
        "本轮行动未能完成",
        "本轮行动已取消",
    ]
    assert all(item["summary"] for item in state["logs"])


@pytest.mark.anyio
async def test_create_hosted_identity_concurrent_same_name_uses_stable_controller_slugs(
    tmp_path, hosted_settings
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'hosted-identities.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    owner_id = str(uuid.uuid4())
    controller_ids: list[str] = []
    try:
        async with factory() as db:
            owner = User(
                id=owner_id,
                name="Hosted Owner",
                email="hosted-owner@test.com",
                is_admin=True,
            )
            now = datetime.now(UTC)
            controllers = [
                HostedAgentController(
                    id=str(uuid.uuid4()),
                    owner_user_id=owner_id,
                    request_id=str(uuid.uuid4()),
                    create_request_hash=("a" if idx == 0 else "b") * 64,
                    desired_status="running",
                    runtime_status="claimed",
                    control_version=1,
                    provider_host="api.example.com",
                    model="hosted-model",
                    provider_validation_required=False,
                    secret_envelope="opaque",
                    identity_json={},
                    policy_json={},
                    lease_owner=f"worker-{idx}",
                    lease_token=f"lease-token-{idx}",
                    lease_epoch=1,
                    lease_expires_at=now + timedelta(minutes=3),
                )
                for idx in range(2)
            ]
            db.add(owner)
            db.add_all(controllers)
            await db.commit()
            controller_ids = [controller.id for controller in controllers]

        async def _create_identity(controller_id: str) -> tuple[str, str]:
            async with factory() as db:
                controller = await db.get(HostedAgentController, controller_id)
                profile = await create_hosted_identity(
                    db,
                    controller=controller,
                    display_name="林澄",
                    sprite_key="埃迪",
                    public_role={"goals": {"public": "认识邻居"}},
                    public_identity={"goals": {"public": "认识邻居"}},
                    play_secret_bundle={
                        "base_url": "https://api.example.com/v1",
                        "api_key": "sk-hosted-test",
                    },
                )
                resident = await db.get(Resident, profile.resident_id)
                assert resident is not None
                return controller_id, resident.slug

        created = await asyncio.gather(
            *(_create_identity(controller_id) for controller_id in controller_ids)
        )
        slugs = {slug for _controller_id, slug in created}
        assert slugs == {
            f"p-hosted-{uuid.UUID(controller_id).hex}"
            for controller_id in controller_ids
        }
        assert len(slugs) == 2
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_npc_terminal_failure_releases_controller_in_same_commit(
    db_session, hosted_settings
):
    owner = User(id=str(uuid.uuid4()), name="Owner", email="npc-fence-owner@test.com")
    controller_id = str(uuid.uuid4())
    controller = HostedAgentController(
        id=controller_id,
        owner_user_id=owner.id,
        request_id=str(uuid.uuid4()),
        create_request_hash="c" * 64,
        desired_status="running",
        runtime_status="claimed",
        provider_host="api.example.com",
        model="model",
        secret_envelope=encrypt_secret_bundle(
            controller_id,
            {"api_key": "arbitraryCredentialXYZ", "base_url": "https://api.example.com/v1"},
        ),
        identity_json={},
        policy_json={},
        lease_owner="worker",
        lease_token="lease-token",
        lease_epoch=1,
        lease_expires_at=datetime.now(UTC),
    )
    turn = HostedAgentTurn(
        id=str(uuid.uuid4()),
        controller_id=controller_id,
        sequence=1,
        state="committing",
        lease_epoch=1,
        control_version=1,
        observation_envelope="opaque",
    )
    db_session.add_all([owner, controller, turn])
    await db_session.commit()

    _terminalize_hosted_npc_failure(
        controller, turn, error_code="observation_changed_during_turn"
    )
    await db_session.commit()
    await db_session.refresh(controller)
    await db_session.refresh(turn)
    assert turn.state == "failed"
    assert turn.error_code == "observation_changed_during_turn"
    assert controller.runtime_status == "idle"
    assert controller.lease_owner is None
    assert controller.lease_token is None
    assert controller.lease_expires_at is None


@pytest.mark.anyio
async def test_large_context_exceeding_daily_limit_never_calls_provider(
    db_engine, monkeypatch
):
    from app.hosted_agents import worker as worker_module
    from app.hosted_agents.worker import HostedAgentWorker

    factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(worker_module, "async_session", factory)
    async with factory() as db:
        owner = User(
            id=str(uuid.uuid4()), name="Owner", email="budget-owner@test.com"
        )
        controller = HostedAgentController(
            id=str(uuid.uuid4()),
            owner_user_id=owner.id,
            request_id=str(uuid.uuid4()),
            create_request_hash="d" * 64,
            desired_status="running",
            runtime_status="claimed",
            provider_host="api.example.com",
            model="model",
            secret_envelope="opaque",
            identity_json={},
            policy_json={},
            max_tokens_per_day=1000,
            lease_owner="budget-worker",
            lease_token="budget-lease",
            lease_epoch=1,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
        )
        db.add_all([owner, controller])
        await db.commit()

    reservation = hosted_decision_token_reservation(
        observation={"scene": "海" * 4000},
        public_identity={},
        private_identity={},
        max_output_tokens=600,
    )
    assert reservation > controller.max_tokens_per_day
    provider_calls = 0

    async def provider_operation():
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("provider must not be called")

    worker = HostedAgentWorker()
    try:
        with pytest.raises(HostedAgentError) as caught:
            await worker._provider_call(
                controller,
                reserve_tokens=reservation,
                operation=provider_operation,
                scenario="hosted_agent_test",
            )
        assert caught.value.code == "token_budget_exhausted"
        assert provider_calls == 0
    finally:
        await worker.aclose()


@pytest.mark.anyio
async def test_provider_usage_above_reservation_saturates_hard_daily_limit(
    db_session,
):
    owner = User(
        id=str(uuid.uuid4()), name="Owner", email="usage-overrun-owner@test.com"
    )
    controller = HostedAgentController(
        id=str(uuid.uuid4()),
        owner_user_id=owner.id,
        request_id=str(uuid.uuid4()),
        create_request_hash="e" * 64,
        desired_status="running",
        runtime_status="claimed",
        provider_host="api.example.com",
        model="model",
        secret_envelope="opaque",
        identity_json={},
        policy_json={},
        max_tokens_per_day=1000,
    )
    db_session.add_all([owner, controller])
    await db_session.commit()

    reservation = await reserve_daily_budget(
        db_session, controller=controller, reserve_tokens=100
    )
    within = await settle_daily_budget(
        db_session,
        controller_id=controller.id,
        usage_date=reservation.usage_date,
        reserve_tokens=100,
        input_tokens=120,
        output_tokens=30,
    )
    assert within is False
    usage = await db_session.get(
        HostedAgentDailyUsage, (controller.id, reservation.usage_date)
    )
    assert usage.tokens_reserved == 0
    assert usage.tokens_charged == controller.max_tokens_per_day
    assert usage.input_tokens + usage.output_tokens == 100
    with pytest.raises(HostedAgentError) as caught:
        await reserve_daily_budget(
            db_session, controller=controller, reserve_tokens=1
        )
    assert caught.value.code == "token_budget_exhausted"


@pytest.mark.anyio
async def test_internal_api_429_preserves_same_durable_turn_for_retry(
    db_engine, monkeypatch, hosted_settings
):
    from app.hosted_agents import worker as worker_module
    from app.hosted_agents.worker import HostedAgentWorker
    from app.services.hosted_agent_api_client import HostedAgentApiError

    factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(worker_module, "async_session", factory)
    controller_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    async with factory() as db:
        owner = User(
            id=str(uuid.uuid4()), name="Owner", email="retry-owner@test.com"
        )
        controller = HostedAgentController(
            id=controller_id,
            owner_user_id=owner.id,
            request_id=str(uuid.uuid4()),
            create_request_hash="f" * 64,
            desired_status="running",
            runtime_status="claimed",
            provider_host="api.example.com",
            model="model",
            secret_envelope="opaque",
            identity_json={},
            policy_json={},
            lease_owner="worker",
            lease_token="lease-token",
            lease_epoch=3,
            lease_expires_at=datetime.now(UTC).replace(microsecond=0)
            + timedelta(minutes=3),
        )
        decision = {"action": "wait", "seconds": 1, "summary": "provider text"}
        turn = HostedAgentTurn(
            id=turn_id,
            controller_id=controller_id,
            sequence=1,
            state="decision_ready",
            lease_epoch=3,
            control_version=1,
            observation_seq=1,
            event_cursor=0,
            observation_envelope="opaque",
            decision_version=1,
            decision_envelope=encrypt_turn_value(
                turn_id=turn_id,
                field_name=HOSTED_DECISION_FIELD,
                value=decision,
            ),
            action_id="durable-action-id",
            action_type="wait",
        )
        db.add_all([owner, controller, turn])
        await db.commit()

    worker = HostedAgentWorker()

    async def rate_limited(**_kwargs):
        raise HostedAgentApiError("agent_api_http_429", 429, retryable=True)

    monkeypatch.setattr(worker, "_session_call", rate_limited)
    try:
        with pytest.raises(HostedAgentApiError) as caught:
            await worker._submit_turn(
                controller=controller,
                turn=turn,
                play_token=SecretStr("sv_play_test"),
            )
        assert caught.value.status_code == 429
    finally:
        await worker.aclose()
    async with factory() as db:
        persisted_turn = await db.get(HostedAgentTurn, turn_id)
        persisted_controller = await db.get(HostedAgentController, controller_id)
        assert persisted_turn.state == "committing"
        assert persisted_turn.action_id == "durable-action-id"
        assert persisted_controller.runtime_status == "claimed"
        assert persisted_controller.lease_token == "lease-token"


@pytest.mark.anyio
async def test_hosted_wait_uses_real_receipt_and_atomically_releases_controller(
    client, db_session, hosted_settings
):
    created, session_token = await _register_test_agent(client, "长驻行动员")
    profile = await db_session.get(AgentPlayer, created["application_id"])
    headers = {"Authorization": f"Bearer {session_token}"}
    observation = (
        await client.get("/api/v1/agent/observation", headers=headers)
    ).json()
    controller, turn, usage, action_id, fence_headers = await _seed_hosted_fence(
        db_session,
        profile=profile,
        action_type="wait",
        observation_seq=observation["observation_seq"],
        event_cursor=observation["event_cursor"],
    )
    decision_day = datetime.now(UTC).date() - timedelta(days=1)
    turn.budget_date = decision_day
    usage.usage_date = decision_day
    await db_session.commit()

    response = await client.post(
        "/api/v1/agent/actions",
        headers={**headers, **fence_headers},
        json={
            "action_id": action_id,
            "observation_seq": observation["observation_seq"],
            "type": "wait",
            "params": {"seconds": 1},
        },
    )
    assert response.status_code == 200, response.text
    receipts = (
        await db_session.execute(
            select(AgentActionReceipt).where(
                AgentActionReceipt.agent_player_id == profile.id,
                AgentActionReceipt.action_id == action_id,
            )
        )
    ).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].action_type == "wait"
    assert receipts[0].result_json["status"] == "completed"
    await db_session.refresh(controller)
    await db_session.refresh(turn)
    await db_session.refresh(usage)
    commit_day_usage = await db_session.get(
        HostedAgentDailyUsage, (controller.id, datetime.now(UTC).date())
    )
    assert turn.state == "completed"
    assert decrypt_turn_value(turn, HOSTED_RESULT_FIELD)["status"] == "completed"
    assert usage.actions == 0
    assert commit_day_usage.actions == 1
    assert controller.runtime_status == "idle"
    assert controller.lease_token is None
    assert controller.last_action_at is not None


@pytest.mark.anyio
async def test_worker_restart_adopts_exact_same_decision_and_action_id(
    db_session, hosted_settings
):
    owner = User(
        id=str(uuid.uuid4()), name="Owner", email="restart-owner@test.com"
    )
    controller = HostedAgentController(
        id=str(uuid.uuid4()),
        owner_user_id=owner.id,
        request_id=str(uuid.uuid4()),
        create_request_hash="1" * 64,
        desired_status="running",
        runtime_status="claimed",
        provider_host="api.example.com",
        model="model",
        secret_envelope="opaque",
        identity_json={},
        policy_json={},
        lease_owner="replacement-worker",
        lease_token="replacement-lease",
        lease_epoch=8,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
    )
    durable_action_id = str(uuid.uuid4())
    recoverable = HostedAgentTurn(
        id=str(uuid.uuid4()),
        controller_id=controller.id,
        sequence=2,
        state="committing",
        lease_epoch=7,
        control_version=1,
        observation_envelope="opaque",
        decision_envelope="opaque-decision",
        action_id=durable_action_id,
        action_type="wait",
    )
    incomplete = HostedAgentTurn(
        id=str(uuid.uuid4()),
        controller_id=controller.id,
        sequence=1,
        state="observed",
        lease_epoch=7,
        control_version=1,
        observation_envelope="opaque",
    )
    db_session.add_all([owner, controller, recoverable, incomplete])
    await db_session.commit()

    adopted = await adopt_recoverable_turn(db_session, controller=controller)
    assert adopted is not None
    assert adopted.id == recoverable.id
    assert adopted.state == "decision_ready"
    assert adopted.action_id == durable_action_id
    assert adopted.lease_epoch == 8
    await db_session.refresh(incomplete)
    assert incomplete.state == "abandoned"
    assert incomplete.error_code == "worker_restarted_before_decision"


@pytest.mark.anyio
async def test_hosted_npc_observation_race_terminalizes_both_receipts(
    client, db_session, monkeypatch, hosted_settings
):
    created, session_token = await _register_test_agent(client, "长驻访谈员")
    profile = await db_session.get(AgentPlayer, created["application_id"])
    avatar_user = await db_session.get(User, profile.user_id)
    avatar = await db_session.get(Resident, profile.resident_id)
    avatar_user.soul_coin_balance = 5
    avatar_user.last_x = 75 * 32 + 16
    avatar_user.last_y = 56 * 32 + 16
    avatar.tile_x = 75
    avatar.tile_y = 56
    npc_owner = User(
        id=str(uuid.uuid4()), name="NPC Owner", email="hosted-npc-owner@test.com"
    )
    npc = Resident(
        id=str(uuid.uuid4()),
        slug="hosted-race-npc",
        name="港口木匠",
        resident_type="npc",
        creator_id=npc_owner.id,
        tile_x=76,
        tile_y=56,
        status="idle",
        token_cost_per_turn=1,
        ability_md="会修理木器",
        persona_md="耐心",
        soul_md="珍视可靠",
    )
    db_session.add_all([npc_owner, npc])
    await db_session.commit()
    headers = {"Authorization": f"Bearer {session_token}"}
    observation = (
        await client.get("/api/v1/agent/observation", headers=headers)
    ).json()
    controller, turn, _usage, action_id, fence_headers = await _seed_hosted_fence(
        db_session,
        profile=profile,
        action_type="npc_chat_turn",
        observation_seq=observation["observation_seq"],
        event_cursor=observation["event_cursor"],
    )

    async def reply_after_external_observation_change(**_kwargs):
        await db_session.execute(
            update(AgentPlayer)
            .where(AgentPlayer.id == profile.id)
            .values(observation_seq=AgentPlayer.observation_seq + 1)
        )
        await db_session.commit()
        return "这条回复不能跨越新的观察提交。"

    monkeypatch.setattr(
        "app.routers.agent_players.generate_single_turn_reply",
        reply_after_external_observation_change,
    )
    response = await client.post(
        "/api/v1/agent/npc-chat-turns",
        headers={**headers, **fence_headers},
        json={
            "turn_id": action_id,
            "observation_seq": observation["observation_seq"],
            "resident_slug": npc.slug,
            "text": "你好。",
        },
    )
    assert response.status_code == 409, response.text
    receipt = (
        await db_session.execute(
            select(AgentNpcChatTurnReceipt).where(
                AgentNpcChatTurnReceipt.agent_player_id == profile.id,
                AgentNpcChatTurnReceipt.turn_id == action_id,
            )
        )
    ).scalar_one()
    await db_session.refresh(controller)
    await db_session.refresh(turn)
    assert receipt.status == "failed"
    assert receipt.http_status == 409
    assert turn.state == "failed"
    assert turn.error_code == "observation_changed_during_turn"
    assert controller.runtime_status == "idle"
    assert controller.lease_token is None


@pytest.mark.anyio
async def test_npc_reply_survives_restart_only_in_encrypted_private_journal(
    client, db_session, monkeypatch, hosted_settings
):
    created, session_token = await _register_test_agent(client, "连续访谈员")
    profile = await db_session.get(AgentPlayer, created["application_id"])
    avatar_user = await db_session.get(User, profile.user_id)
    avatar = await db_session.get(Resident, profile.resident_id)
    avatar_user.soul_coin_balance = 5
    avatar_user.last_x = 75 * 32 + 16
    avatar_user.last_y = 56 * 32 + 16
    avatar.tile_x = 75
    avatar.tile_y = 56
    npc_owner = User(
        id=str(uuid.uuid4()), name="NPC Owner", email="continuity-npc-owner@test.com"
    )
    npc = Resident(
        id=str(uuid.uuid4()),
        slug="continuity-npc",
        name="旧书店主",
        resident_type="npc",
        creator_id=npc_owner.id,
        tile_x=76,
        tile_y=56,
        status="idle",
        token_cost_per_turn=1,
        ability_md="熟悉旧书",
        persona_md="说话温和",
        soul_md="珍视承诺",
    )
    db_session.add_all([npc_owner, npc])
    await db_session.commit()
    headers = {"Authorization": f"Bearer {session_token}"}
    observation = (
        await client.get("/api/v1/agent/observation", headers=headers)
    ).json()
    controller, turn, _usage, action_id, fence_headers = await _seed_hosted_fence(
        db_session,
        profile=profile,
        action_type="npc_chat_turn",
        observation_seq=observation["observation_seq"],
        event_cursor=observation["event_cursor"],
    )
    private_reply = "明天下午我会在旧书店门口留一本蓝色封面的航海日志。"
    monkeypatch.setattr(
        "app.routers.agent_players.generate_single_turn_reply",
        AsyncMock(return_value=private_reply),
    )
    response = await client.post(
        "/api/v1/agent/npc-chat-turns",
        headers={**headers, **fence_headers},
        json={
            "turn_id": action_id,
            "observation_seq": observation["observation_seq"],
            "resident_slug": npc.slug,
            "text": "我们明天还能继续聊吗？",
        },
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(turn)
    assert private_reply not in (turn.result_envelope or "")
    assert decrypt_turn_value(turn, HOSTED_RESULT_FIELD)["reply"] == private_reply

    # Simulate a worker restart/new lease before folding the terminal turn.
    await db_session.refresh(controller)
    controller.runtime_status = "claimed"
    controller.lease_owner = "replacement-worker"
    controller.lease_token = "replacement-lease"
    controller.lease_epoch += 1
    controller.lease_expires_at = datetime.now(UTC) + timedelta(minutes=3)
    await db_session.commit()
    bundle = await reconcile_private_journal(db_session, controller=controller)
    assert bundle["journal"][-1]["result"]["reply"] == private_reply
    await db_session.refresh(controller)
    assert private_reply not in controller.secret_envelope
    public_state = await controller_state(db_session, controller=controller)
    assert private_reply not in json.dumps(public_state, ensure_ascii=False, default=str)


def test_hosted_presence_ttl_covers_slowest_legal_operation(monkeypatch):
    monkeypatch.setattr(settings, "agent_presence_ttl_seconds", 90)
    monkeypatch.setattr(settings, "hosted_agent_runner_llm_timeout_seconds", 60.0)
    monkeypatch.setattr(settings, "user_llm_timeout", 120)
    hosted = AgentPlayer(
        control_kind="hosted_agent",
        last_seen_at=datetime.now(UTC) - timedelta(seconds=100),
    )
    external = AgentPlayer(
        control_kind="external_agent",
        last_seen_at=hosted.last_seen_at,
    )
    assert agent_presence_ttl_seconds(hosted) >= 60 + 155 + 60 + 30
    assert agent_presence_ttl_seconds(external) == 90
    assert _agent_is_online(hosted) is True
    assert _agent_is_online(external) is False


@pytest.mark.anyio
async def test_capacity_slots_bound_all_running_controllers(
    db_session, monkeypatch, hosted_settings
):
    monkeypatch.setattr(settings, "hosted_agent_runner_max_concurrent", 1)
    first_owner = User(
        id=str(uuid.uuid4()), name="First", email="capacity-first@test.com"
    )
    second_owner = User(
        id=str(uuid.uuid4()), name="Second", email="capacity-second@test.com"
    )
    db_session.add_all([first_owner, second_owner])
    await db_session.commit()
    first_owner_id = first_owner.id
    second_owner_id = second_owner.id
    policy = {
        "heartbeat_seconds": 30,
        "action_interval_seconds": 30,
        "daily_action_limit": 200,
        "daily_token_limit": 200_000,
        "max_output_tokens": 600,
    }
    bundle = {
        "base_url": "https://api.example.com/v1",
        "api_key": "arbitraryCredentialXYZ",
        "display_name": "林澄",
        "sprite_key": "埃迪",
        "public_goal": "认识邻居",
    }
    first, _ = await create_controller(
        db_session,
        owner_user_id=first_owner_id,
        request_id=str(uuid.uuid4()),
        request_hash="a" * 64,
        provider_host="api.example.com",
        model="model",
        bundle=bundle,
        policy=policy,
    )
    assert first.capacity_slot == 0
    first_id = first.id
    with pytest.raises(HostedAgentError) as caught:
        await create_controller(
            db_session,
            owner_user_id=second_owner_id,
            request_id=str(uuid.uuid4()),
            request_hash="b" * 64,
            provider_host="api.example.com",
            model="model",
            bundle={**bundle, "display_name": "周岚"},
            policy=policy,
        )
    assert caught.value.code == "hosted_agent_capacity_exhausted"
    paused = await set_desired_status(
        db_session,
        controller_id=first_id,
        owner_user_id=first_owner_id,
        desired_status="paused",
    )
    assert paused.capacity_slot is None
    second, _ = await create_controller(
        db_session,
        owner_user_id=second_owner_id,
        request_id=str(uuid.uuid4()),
        request_hash="c" * 64,
        provider_host="api.example.com",
        model="model",
        bundle={**bundle, "display_name": "周岚"},
        policy=policy,
    )
    assert second.capacity_slot == 0


@pytest.mark.anyio
async def test_low_daily_limit_is_rejected_before_provisioning(
    client, db_session, monkeypatch, hosted_settings
):
    async def safe_provider_url(_value):
        return "https://api.example.com/v1", "api.example.com"

    monkeypatch.setattr(
        "app.routers.admin.hosted_agents.validate_hosted_provider_base_url",
        safe_provider_url,
    )
    admin = User(
        id=str(uuid.uuid4()),
        name="Admin",
        email="provision-limit-admin@test.com",
        is_admin=True,
    )
    db_session.add(admin)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(admin.id)}"}
    payload = {
        "request_id": str(uuid.uuid4()),
        "base_url": "https://api.example.com/v1",
        "api_key": "arbitraryCredentialXYZ",
        "model": "model",
        "display_name": "林澄",
        "goal": "认识邻居",
        "daily_token_limit": 1000,
    }
    rejected = await client.post(
        "/admin/hosted-agents", headers=headers, json=payload
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "daily_token_limit_too_low_for_provisioning"
    assert (
        await db_session.execute(select(HostedAgentController))
    ).scalars().all() == []

    policy = {
        "heartbeat_seconds": 30,
        "action_interval_seconds": 30,
        "daily_action_limit": 200,
        "daily_token_limit": 200_000,
        "max_output_tokens": 600,
    }
    controller, _ = await create_controller(
        db_session,
        owner_user_id=admin.id,
        request_id=str(uuid.uuid4()),
        request_hash="9" * 64,
        provider_host="api.example.com",
        model="model",
        bundle={
            "base_url": "https://api.example.com/v1",
            "api_key": "arbitraryCredentialXYZ",
            "display_name": "林澄",
            "sprite_key": "埃迪",
            "public_goal": "认识邻居",
        },
        policy=policy,
    )
    patched = await client.patch(
        f"/admin/hosted-agents/{controller.id}",
        headers=headers,
        json={"version": 1, "daily_token_limit": 1000},
    )
    assert patched.status_code == 422
    assert patched.json()["detail"]["code"] == "daily_token_limit_too_low_for_provisioning"


@pytest.mark.anyio
async def test_aggregate_metering_failure_does_not_replay_paid_provider_call(
    db_engine, monkeypatch, hosted_settings
):
    from app.hosted_agents import worker as worker_module
    from app.hosted_agents.worker import HostedAgentWorker

    factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(worker_module, "async_session", factory)

    async def broken_metering(*_args, **_kwargs):
        raise RuntimeError("provider-controlled text must not be logged")

    monkeypatch.setattr(worker_module, "record_usage", broken_metering)
    async with factory() as db:
        owner = User(
            id=str(uuid.uuid4()), name="Owner", email="meter-owner@test.com"
        )
        controller = HostedAgentController(
            id=str(uuid.uuid4()),
            owner_user_id=owner.id,
            request_id=str(uuid.uuid4()),
            create_request_hash="7" * 64,
            desired_status="running",
            runtime_status="claimed",
            provider_host="api.example.com",
            model="model",
            secret_envelope="opaque",
            identity_json={},
            policy_json={},
            max_tokens_per_day=1000,
            lease_owner="meter-worker",
            lease_token="meter-lease",
            lease_epoch=1,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
        )
        db.add_all([owner, controller])
        await db.commit()
    calls = 0
    provider_usage = HostedProviderUsage(
        calls=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        latency_ms=1,
    )

    async def provider_operation():
        nonlocal calls
        calls += 1
        return provider_usage

    worker = HostedAgentWorker()
    try:
        returned = await worker._provider_call(
            controller,
            reserve_tokens=100,
            operation=provider_operation,
            scenario="hosted_agent_test",
        )
        assert returned is provider_usage
        assert calls == 1
    finally:
        await worker.aclose()
    async with factory() as db:
        usage = (
            await db.execute(
                select(HostedAgentDailyUsage).where(
                    HostedAgentDailyUsage.controller_id == controller.id
                )
            )
        ).scalar_one()
        assert usage.calls_charged == 1
        assert usage.tokens_charged == 15


@pytest.mark.anyio
async def test_provider_429_without_usage_releases_exact_stage_reservation(
    db_engine, monkeypatch, hosted_settings
):
    from app.hosted_agents import worker as worker_module
    from app.hosted_agents.worker import HostedAgentWorker

    factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(worker_module, "async_session", factory)
    controller_id = str(uuid.uuid4())
    async with factory() as db:
        owner = User(
            id=str(uuid.uuid4()), name="Owner", email="provider-429-owner@test.com"
        )
        controller = HostedAgentController(
            id=controller_id,
            owner_user_id=owner.id,
            request_id=str(uuid.uuid4()),
            create_request_hash="8" * 64,
            desired_status="running",
            runtime_status="claimed",
            capacity_slot=0,
            provider_host="api.example.com",
            model="model",
            secret_envelope="opaque",
            identity_json={},
            policy_json={},
            max_tokens_per_day=1_000,
            lease_owner="rate-limit-worker",
            lease_token="rate-limit-lease",
            lease_epoch=1,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
        )
        db.add_all([owner, controller])
        await db.commit()

    sends = 0

    async def rejected_before_inference():
        nonlocal sends
        sends += 1
        raise HostedProviderError(
            "provider_rate_limited",
            "remote detail",
            429,
            definitively_unbilled=True,
        )

    worker = HostedAgentWorker()
    try:
        with pytest.raises(HostedProviderError) as caught:
            await worker._provider_call(
                controller,
                reserve_tokens=400,
                operation=rejected_before_inference,
                scenario="hosted_agent_preflight",
                stage="preflight",
            )
        assert caught.value.code == "provider_rate_limited"
        assert sends == 1
    finally:
        await worker.aclose()

    async with factory() as db:
        persisted = await db.get(HostedAgentController, controller_id)
        turn = (
            await db.execute(
                select(HostedAgentTurn).where(
                    HostedAgentTurn.controller_id == controller_id
                )
            )
        ).scalar_one()
        usage = await db.get(
            HostedAgentDailyUsage, (controller_id, datetime.now(UTC).date())
        )
        assert turn.state == "failed"
        assert turn.error_code == "provider_rate_limited"
        assert turn.reserved_tokens == 0
        assert usage.calls_reserved == 0
        assert usage.tokens_reserved == 0
        assert usage.calls_charged == 0
        assert usage.tokens_charged == 0
        assert persisted.desired_status == "running"
        assert persisted.runtime_status == "backoff"
        assert persisted.provider_retry_at is not None
        assert persisted.lease_token is None


@pytest.mark.anyio
async def test_ambiguous_provider_error_blocks_without_replay_or_releasing_budget(
    db_engine, monkeypatch, hosted_settings
):
    from app.hosted_agents import worker as worker_module
    from app.hosted_agents.worker import HostedAgentWorker
    from app.services.hosted_agent_service import claim_due_controller

    factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(worker_module, "async_session", factory)
    controller_id = str(uuid.uuid4())
    async with factory() as db:
        owner = User(
            id=str(uuid.uuid4()), name="Owner", email="unknown-owner@test.com"
        )
        controller = HostedAgentController(
            id=controller_id,
            owner_user_id=owner.id,
            request_id=str(uuid.uuid4()),
            create_request_hash="9" * 64,
            desired_status="running",
            runtime_status="claimed",
            capacity_slot=0,
            provider_host="api.example.com",
            model="model",
            secret_envelope="opaque",
            identity_json={},
            policy_json={},
            max_tokens_per_day=1_000,
            lease_owner="unknown-worker",
            lease_token="unknown-lease",
            lease_epoch=1,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
        )
        db.add_all([owner, controller])
        await db.commit()

    sends = 0

    async def timed_out_after_send():
        nonlocal sends
        sends += 1
        raise HostedProviderError(
            "provider_unavailable",
            "remote detail",
            502,
            outcome_unknown=True,
        )

    worker = HostedAgentWorker()
    try:
        with pytest.raises(HostedProviderError):
            await worker._provider_call(
                controller,
                reserve_tokens=400,
                operation=timed_out_after_send,
                scenario="hosted_agent_preflight",
                stage="preflight",
            )
    finally:
        await worker.aclose()
    assert sends == 1

    async with factory() as db:
        persisted = await db.get(HostedAgentController, controller_id)
        turn = (
            await db.execute(
                select(HostedAgentTurn).where(
                    HostedAgentTurn.controller_id == controller_id
                )
            )
        ).scalar_one()
        usage = await db.get(
            HostedAgentDailyUsage, (controller_id, datetime.now(UTC).date())
        )
        assert persisted.desired_status == "paused"
        assert persisted.runtime_status == "error"
        assert persisted.last_error_code == "provider_outcome_unknown"
        assert persisted.capacity_slot is None
        assert turn.state == "failed"
        assert turn.error_code == "provider_outcome_unknown"
        assert turn.reserved_tokens == 400
        assert usage.calls_reserved == 1
        assert usage.tokens_reserved == 400
        assert usage.calls_charged == 0
        assert await claim_due_controller(db, worker_id="tick-two") is None
        assert await claim_due_controller(db, worker_id="tick-three") is None
    assert sends == 1


@pytest.mark.anyio
async def test_restart_finds_calling_provider_stage_and_never_sends_again(
    db_engine, monkeypatch, hosted_settings
):
    from app.hosted_agents import worker as worker_module
    from app.hosted_agents.worker import HostedAgentWorker
    from app.services.hosted_agent_service import claim_due_controller

    factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(worker_module, "async_session", factory)
    controller_id = str(uuid.uuid4())
    async with factory() as db:
        owner = User(
            id=str(uuid.uuid4()), name="Owner", email="crashed-call-owner@test.com"
        )
        original = HostedAgentController(
            id=controller_id,
            owner_user_id=owner.id,
            request_id=str(uuid.uuid4()),
            create_request_hash="a" * 64,
            desired_status="running",
            runtime_status="claimed",
            capacity_slot=0,
            provider_host="api.example.com",
            model="model",
            secret_envelope="opaque",
            identity_json={},
            policy_json={},
            max_tokens_per_day=1_000,
            lease_owner="crashed-worker",
            lease_token="crashed-lease",
            lease_epoch=1,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
        )
        db.add_all([owner, original])
        await db.commit()
        marker = await begin_provider_stage_call(
            db,
            controller=original,
            stage="identity",
            reserve_tokens=400,
        )
        # The marker is committed before the HTTP send. Simulate that send and
        # a process crash before any response/result can be durably recorded.
        original.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        original.next_tick_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        marker_id = marker.id

    sends = 1
    async with factory() as db:
        replacement = await claim_due_controller(db, worker_id="replacement-worker")
        assert replacement is not None

    async def forbidden_replay():
        nonlocal sends
        sends += 1
        raise AssertionError("ambiguous provider stage must never be replayed")

    worker = HostedAgentWorker()
    try:
        with pytest.raises(HostedAgentError) as caught:
            await worker._provider_call(
                replacement,
                reserve_tokens=400,
                operation=forbidden_replay,
                scenario="hosted_agent_identity",
                stage="identity",
            )
        assert caught.value.code == "provider_outcome_unknown"
    finally:
        await worker.aclose()
    assert sends == 1

    async with factory() as db:
        persisted = await db.get(HostedAgentController, controller_id)
        marker = await db.get(HostedAgentTurn, marker_id)
        usage = await db.get(
            HostedAgentDailyUsage, (controller_id, datetime.now(UTC).date())
        )
        assert persisted.desired_status == "paused"
        assert persisted.last_error_code == "provider_outcome_unknown"
        assert marker.state == "failed"
        assert marker.error_code == "provider_outcome_unknown"
        assert usage.calls_reserved == 1
        assert usage.tokens_reserved == 400


@pytest.mark.anyio
async def test_provider_stage_success_result_and_metering_are_one_durable_transition(
    db_session, hosted_settings
):
    owner = User(
        id=str(uuid.uuid4()), name="Owner", email="stage-result-owner@test.com"
    )
    controller = HostedAgentController(
        id=str(uuid.uuid4()),
        owner_user_id=owner.id,
        request_id=str(uuid.uuid4()),
        create_request_hash="b" * 64,
        desired_status="running",
        runtime_status="claimed",
        capacity_slot=0,
        provider_host="api.example.com",
        model="model",
        secret_envelope="opaque",
        identity_json={},
        policy_json={},
        max_tokens_per_day=2_000,
        lease_owner="stage-worker",
        lease_token="stage-lease",
        lease_epoch=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
    )
    db_session.add_all([owner, controller])
    await db_session.commit()

    marker = await begin_provider_stage_call(
        db_session,
        controller=controller,
        stage="identity",
        reserve_tokens=500,
    )
    identity = _generated_identity().model_dump()
    completed = await complete_provider_stage_call(
        db_session,
        controller=controller,
        turn_id=marker.id,
        result_value={"stage": "identity", "identity": identity},
        usage={"input_tokens": 120, "output_tokens": 80},
    )
    assert completed is True

    durable = await completed_provider_stage_result(
        db_session, controller=controller, stage="identity"
    )
    usage = await db_session.get(
        HostedAgentDailyUsage, (controller.id, datetime.now(UTC).date())
    )
    await db_session.refresh(marker)
    assert durable == {"stage": "identity", "identity": identity}
    assert marker.state == "completed"
    assert marker.result_envelope is not None
    assert marker.reserved_tokens == 500
    assert usage.calls_reserved == 0
    assert usage.tokens_reserved == 0
    assert usage.calls_charged == 1
    assert usage.tokens_charged == 200


@pytest.mark.anyio
async def test_decision_budget_reservation_and_calling_marker_are_atomic(
    db_session, hosted_settings
):
    owner = User(
        id=str(uuid.uuid4()), name="Owner", email="atomic-call-owner@test.com"
    )
    controller = HostedAgentController(
        id=str(uuid.uuid4()),
        owner_user_id=owner.id,
        request_id=str(uuid.uuid4()),
        create_request_hash="c" * 64,
        desired_status="running",
        runtime_status="claimed",
        capacity_slot=0,
        provider_host="api.example.com",
        model="model",
        secret_envelope="opaque",
        identity_json={},
        policy_json={},
        max_tokens_per_day=1_000,
        lease_owner="atomic-worker",
        lease_token="atomic-lease",
        lease_epoch=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
    )
    turn = HostedAgentTurn(
        id=str(uuid.uuid4()),
        controller_id=controller.id,
        sequence=1,
        state="observed",
        lease_epoch=1,
        control_version=1,
        observation_envelope="opaque",
    )
    db_session.add_all([owner, controller, turn])
    await db_session.commit()

    reserved = await reserve_turn_provider_budget(
        db_session,
        controller=controller,
        turn_id=turn.id,
        reserve_tokens=350,
    )
    usage = await db_session.get(
        HostedAgentDailyUsage, (controller.id, datetime.now(UTC).date())
    )
    assert reserved.state == "calling"
    assert reserved.budget_date == datetime.now(UTC).date()
    assert reserved.reserved_tokens == 350
    assert usage.calls_reserved == 1
    assert usage.tokens_reserved == 350


@pytest.mark.anyio
async def test_pause_revokes_presence_and_old_credentials_or_fences_cannot_revive_it(
    client, db_session, hosted_settings
):
    from app.ws.manager import manager

    application = await client.post(
        "/api/v1/agent-applications",
        json={
            "display_name": "暂停测试员",
            "sprite_key": "埃迪",
            "model_label": "hosted-test-model",
            "role_card": {"goals": {"public": "认识邻居"}},
            "client": {"name": "hosted-test", "version": "1"},
        },
    )
    created = application.json()
    redeemed = await client.post(
        "/api/v1/agent-pairings/redeem",
        json={
            "application_id": created["application_id"],
            "pairing_code": created["pairing_code"],
        },
    )
    play_token = redeemed.json()["agent_token"]
    session = await client.post(
        "/api/v1/agent-sessions",
        headers={"Authorization": f"Bearer {play_token}"},
        json={"client": {"name": "hosted-test", "version": "1"}},
    )
    session_token = session.json()["session_token"]
    session_headers = {"Authorization": f"Bearer {session_token}"}
    assert (
        await client.get("/api/v1/agent/observation", headers=session_headers)
    ).status_code == 200

    profile = await db_session.get(AgentPlayer, created["application_id"])
    assert profile is not None
    assert await manager.get_visible_position(profile.user_id) is not None
    owner = User(
        id=str(uuid.uuid4()), name="Owner", email="pause-presence-owner@test.com"
    )
    controller = HostedAgentController(
        id=str(uuid.uuid4()),
        owner_user_id=owner.id,
        request_id=str(uuid.uuid4()),
        create_request_hash="d" * 64,
        agent_player_id=profile.id,
        desired_status="running",
        runtime_status="idle",
        capacity_slot=0,
        provider_host="api.example.com",
        model="model",
        secret_envelope="opaque",
        identity_json={},
        policy_json={},
    )
    profile.control_kind = "hosted_agent"
    db_session.add_all([owner, controller])
    await db_session.commit()

    paused = await set_desired_status(
        db_session,
        controller_id=controller.id,
        owner_user_id=owner.id,
        desired_status="paused",
    )
    await db_session.refresh(profile)
    assert paused.capacity_slot is None
    assert profile.last_seen_at is None
    assert _agent_is_online(profile) is False
    assert await manager.get_visible_position(profile.user_id) is None

    # Neither a still-valid play credential nor an old session may refresh a
    # paused Hosted identity before the controller gate.
    old_session = await client.get(
        "/api/v1/agent/observation", headers=session_headers
    )
    assert old_session.status_code == 409
    assert old_session.json()["detail"]["code"] == "hosted_controller_not_running"
    old_play = await client.post(
        "/api/v1/agent-sessions",
        headers={"Authorization": f"Bearer {play_token}"},
        json={"client": {"name": "stale-client", "version": "1"}},
    )
    assert old_play.status_code == 409
    assert old_play.json()["detail"]["code"] == "hosted_controller_not_running"
    await db_session.refresh(profile)
    assert profile.last_seen_at is None
    assert await manager.get_visible_position(profile.user_id) is None

    # Resume explicitly, but use a stale controller fence. Actions and NPC
    # turns must fail before receipt creation and before any presence touch.
    started = await set_desired_status(
        db_session,
        controller_id=controller.id,
        owner_user_id=owner.id,
        desired_status="running",
    )
    started.runtime_status = "claimed"
    started.lease_owner = "current-worker"
    started.lease_token = "current-lease"
    started.lease_epoch += 1
    started.lease_expires_at = datetime.now(UTC) + timedelta(minutes=3)
    await db_session.commit()
    action_id = str(uuid.uuid4())
    stale_turn_id = str(uuid.uuid4())
    stale_headers = {
        **session_headers,
        "X-Simverse-Hosted-Controller-ID": controller.id,
        "X-Simverse-Hosted-Lease-Token": "stale-lease",
        "X-Simverse-Hosted-Lease-Epoch": str(started.lease_epoch),
        "X-Simverse-Hosted-Control-Version": str(started.control_version),
        "X-Simverse-Hosted-Turn-ID": stale_turn_id,
        "X-Simverse-Hosted-Event-Cursor": "0",
    }
    stale_action = await client.post(
        "/api/v1/agent/actions",
        headers=stale_headers,
        json={
            "action_id": action_id,
            "observation_seq": profile.observation_seq,
            "type": "wait",
            "params": {"seconds": 1},
        },
    )
    assert stale_action.status_code == 409
    assert stale_action.json()["detail"]["code"] == "hosted_controller_fence_lost"

    npc_turn_id = str(uuid.uuid4())
    stale_npc = await client.post(
        "/api/v1/agent/npc-chat-turns",
        headers={
            **stale_headers,
            "X-Simverse-Hosted-Turn-ID": npc_turn_id,
        },
        json={
            "turn_id": npc_turn_id,
            "observation_seq": profile.observation_seq,
            "resident_slug": "any-resident",
            "text": "你好",
        },
    )
    assert stale_npc.status_code == 409
    assert stale_npc.json()["detail"]["code"] == "hosted_controller_fence_lost"

    await db_session.refresh(profile)
    assert profile.last_seen_at is None
    assert await manager.get_visible_position(profile.user_id) is None
    action_receipts = (
        await db_session.execute(
            select(AgentActionReceipt).where(
                AgentActionReceipt.agent_player_id == profile.id
            )
        )
    ).scalars().all()
    npc_receipts = (
        await db_session.execute(
            select(AgentNpcChatTurnReceipt).where(
                AgentNpcChatTurnReceipt.agent_player_id == profile.id
            )
        )
    ).scalars().all()
    assert action_receipts == []
    assert npc_receipts == []
