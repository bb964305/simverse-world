from __future__ import annotations

import asyncio
import os
import stat
import time
from dataclasses import replace

import jwt
import pytest
from pydantic import ValidationError

from app.lab.runtime_ref.service_auth import (
    RequestSchemaError,
    ServiceAuthenticationError,
    ServiceAuthConfig,
    ServiceAuthorizationError,
    ServiceBinding,
    ServiceTokenValidator,
    StrictRequestModel,
    canonical_request_digest,
    extract_bearer_token,
)
from app.lab.runtime_ref.store import (
    CommandBinding,
    CrossBindingReplay,
    RuntimeStore,
    RuntimeStoreConflict,
)


ISSUER = "simverse-gateway"
AUDIENCE = "lab-runtime"
CURRENT_KID = "runtime-current"
CURRENT_KEY = "runtime-current-secret-at-least-32-bytes"
NEXT_KID = "runtime-next"
NEXT_KEY = "runtime-next-secret-at-least-32-bytes"


def _token(
    *,
    kid: str = CURRENT_KID,
    key: str = CURRENT_KEY,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    action: str = "tool_result.submit",
    run_id: str = "run-1",
    session_id: str = "session-1",
    epoch: int = 7,
    jti: str = "jti-1",
    not_before: int | None = None,
    expires_at: int | None = None,
    actions: list[str] | None = None,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "run_id": run_id,
            "session_id": session_id,
            "epoch": epoch,
            "actions": [action] if actions is None else actions,
            "jti": jti,
            "nbf": now - 1 if not_before is None else not_before,
            "exp": now + 300 if expires_at is None else expires_at,
        },
        key,
        algorithm="HS256",
        headers={"kid": kid},
    )


def _validator() -> ServiceTokenValidator:
    return ServiceTokenValidator(
        {
            "issuer": ISSUER,
            "audience": AUDIENCE,
            "keys": {CURRENT_KID: CURRENT_KEY, NEXT_KID: NEXT_KEY},
        }
    )


@pytest.mark.anyio
async def test_store_state_survives_reopen_and_replays_events(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = RuntimeStore(path)
    session = await first.create_or_get_session(
        run_id="run-1", client_run_id="client-1", epoch=7,
        scopes=["web_search", "web_search"], budget_usd=0.75,
        egress_allowlist=["example.test"],
    )
    await first.transition_session(
        session.session_id,
        expected_states="created",
        new_state="intent_pending",
        checkpoint={"messages": [{"role": "user", "content": "brief"}]},
    )
    intent = await first.record_intent(
        session.session_id, turn_id="turn-1", intent_id="intent-1",
        tool="web.search", args={"query": "sentinel"},
    )
    event = await first.append_event(
        session.session_id,
        event_kind="tool_intent",
        turn_id=intent.turn_id,
        intent_id=intent.intent_id,
        payload={"tool": intent.tool, "args": intent.args},
        dedupe_key="intent:intent-1",
    )
    repeated = await first.append_event(
        session.session_id,
        event_kind="tool_intent",
        turn_id=intent.turn_id,
        intent_id=intent.intent_id,
        payload={"tool": intent.tool, "args": intent.args},
        dedupe_key="intent:intent-1",
    )
    assert repeated == event

    second = RuntimeStore(path)
    restored = await second.get_session(session.session_id)
    assert restored is not None
    assert restored.state == "intent_pending"
    assert restored.scopes == ("web_search",)
    assert restored.budget_usd == 0.75
    assert restored.egress_allowlist == ("example.test",)
    assert restored.checkpoint["messages"][0]["content"] == "brief"
    assert await second.create_or_get_session(
        run_id="run-1", client_run_id="client-1", epoch=7,
        scopes=["web_search"], budget_usd=0.75,
        egress_allowlist=["example.test"],
    ) == restored
    assert await second.get_intent(session.session_id, "intent-1") == intent
    assert await second.list_events(session.session_id, after=0) == [event]
    assert await second.list_events(session.session_id, after=event.cursor) == []


@pytest.mark.anyio
async def test_intent_result_and_artifact_are_idempotent_and_durable(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    store = RuntimeStore(path)
    session = await store.create_or_get_session(
        run_id="run-1", client_run_id="client-1", epoch=1, scopes=["web_search"]
    )
    await store.record_intent(
        session.session_id, turn_id="turn-1", intent_id="intent-1",
        tool="web.search", args={"query": "x"},
    )
    payload = {"sentinel": "BROKER-1"}
    digest = canonical_request_digest(payload)
    result = await store.resolve_intent(
        session.session_id, intent_id="intent-1", result_digest=digest,
        outcome="succeeded", payload=payload,
    )
    assert result.state == "result_recorded"
    assert await store.count_active_intents(session.session_id) == 1
    assert await store.resolve_intent(
        session.session_id, intent_id="intent-1", result_digest=digest,
        outcome="succeeded", payload=payload,
    ) == result
    with pytest.raises(RuntimeStoreConflict, match="digest"):
        await store.resolve_intent(
            session.session_id, intent_id="intent-1", result_digest=digest,
            outcome="succeeded", payload={"sentinel": "DIFFERENT"},
        )
    with pytest.raises(RuntimeStoreConflict, match="pending intent"):
        await store.record_intent(
            session.session_id, turn_id="turn-2", intent_id="intent-2",
            tool="web.search", args={"query": "next"},
        )
    applied = await store.mark_intent_applied(session.session_id, "intent-1")
    assert applied.state == "applied"
    assert await store.count_active_intents(session.session_id) == 0
    await store.record_intent(
        session.session_id, turn_id="turn-2", intent_id="intent-2",
        tool="web.search", args={"query": "next"},
    )
    artifact = await store.put_artifact(
        session.session_id, artifact_id="artifact-1", kind="text",
        title="report", text_md="BROKER-1",
        meta={"broker_result_digest": digest},
    )

    reopened = RuntimeStore(path)
    assert await reopened.get_intent(session.session_id, "intent-1") == applied
    assert await reopened.list_artifacts(session.session_id) == [artifact]
    with pytest.raises(RuntimeStoreConflict):
        await reopened.put_artifact(
            session.session_id, artifact_id="artifact-1", kind="text",
            title="report", text_md="different", meta={},
        )


@pytest.mark.anyio
async def test_exact_command_retry_returns_persisted_receipt_across_restart(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    body = {
        "command_id": "command-1", "run_id": "run-1",
        "session_id": "session-1", "epoch": 7,
    }
    binding = CommandBinding(
        audience=AUDIENCE,
        command_id="command-1",
        jti="jti-1",
        request_digest=canonical_request_digest(body),
        run_id="run-1",
        session_id="session-1",
        epoch=7,
        action="tool_result.submit",
    )
    first_store = RuntimeStore(path)
    first = await first_store.claim_command(binding)
    assert first.is_retry is False
    response = {
        "receipt_id": first.receipt.receipt_id,
        "request_digest": binding.request_digest,
    }
    completed = await first_store.complete_command(binding, response=response)

    reopened = RuntimeStore(path)
    retry = await reopened.claim_command(binding)
    assert retry.is_retry is True
    assert retry.receipt == completed
    assert retry.receipt.response == response

    mutations = (
        replace(binding, audience="other-audience"),
        replace(binding, command_id="other-command"),
        replace(binding, request_digest="0" * 64),
        replace(binding, run_id="other-run"),
        replace(binding, session_id="other-session"),
        replace(binding, epoch=8),
        replace(binding, action="runtime.control"),
    )
    for changed in mutations:
        with pytest.raises(CrossBindingReplay):
            await reopened.claim_command(changed)

    with pytest.raises(CrossBindingReplay):
        await reopened.claim_command(replace(binding, jti="different-jti"))


@pytest.mark.anyio
async def test_concurrent_exact_command_claim_has_one_receipt(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    binding = CommandBinding(
        audience=AUDIENCE,
        command_id="command-race",
        jti="jti-race",
        request_digest=canonical_request_digest({"command_id": "command-race"}),
        run_id="run-race",
        session_id="session-race",
        epoch=3,
        action="tool_result.submit",
    )
    claims = await asyncio.gather(*(store.claim_command(binding) for _ in range(8)))
    assert sum(not claim.is_retry for claim in claims) == 1
    assert len({claim.receipt.receipt_id for claim in claims}) == 1


def test_service_auth_accepts_current_and_next_keys_with_exact_binding():
    validator = _validator()
    expected = ServiceBinding(run_id="run-1", session_id="session-1", epoch=7)
    current = validator.validate(
        _token(), required_action="tool_result.submit", expected_binding=expected
    )
    following = validator.validate(
        _token(kid=NEXT_KID, key=NEXT_KEY, jti="jti-next"),
        required_action="tool_result.submit",
        expected_binding=expected,
    )
    assert current.jti == "jti-1"
    assert following.jti == "jti-next"
    encoded = _token()
    assert extract_bearer_token(f"Bearer {encoded}") == encoded


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (_token(kid="unknown", key="unknown-secret-at-least-32-bytes-long"), "untrusted_key"),
        (_token(key="wrong-secret-at-least-32-bytes-long"), "invalid_token"),
        (_token(issuer="other-issuer"), "invalid_token"),
        (_token(audience="lab-executor"), "invalid_token"),
        (_token(expires_at=int(time.time()) - 10), "expired_token"),
        (_token(not_before=int(time.time()) + 60), "token_not_yet_valid"),
    ],
)
def test_service_auth_rejects_untrusted_or_invalid_tokens(token, reason):
    with pytest.raises(ServiceAuthenticationError) as rejected:
        _validator().validate(token, required_action="tool_result.submit")
    assert rejected.value.reason == reason


def test_service_auth_rejects_action_and_request_binding_before_lookup():
    validator = _validator()
    with pytest.raises(ServiceAuthorizationError) as wrong_action:
        validator.validate(_token(), required_action="runtime.control")
    assert wrong_action.value.reason == "action_not_allowed"

    with pytest.raises(ServiceAuthorizationError) as multiple_actions:
        validator.validate(
            _token(actions=["tool_result.submit", "runtime.control"]),
            required_action="tool_result.submit",
        )
    assert multiple_actions.value.reason == "action_not_allowed"

    for expected in (
        ServiceBinding(run_id="other-run", session_id="session-1", epoch=7),
        ServiceBinding(run_id="run-1", session_id="other-session", epoch=7),
        ServiceBinding(run_id="run-1", session_id="session-1", epoch=8),
    ):
        with pytest.raises(ServiceAuthorizationError) as mismatch:
            validator.validate(
                _token(), required_action="tool_result.submit", expected_binding=expected
            )
        assert mismatch.value.reason == "binding_mismatch"

    for header in (None, "Basic abc", "Bearer", "Bearer a b"):
        with pytest.raises(ServiceAuthenticationError):
            extract_bearer_token(header)


def test_service_auth_rejects_long_lived_tokens_and_invalid_key_mappings():
    now = int(time.time())
    long_lived = _token(not_before=now, expires_at=now + 901)
    with pytest.raises(ServiceAuthenticationError) as rejected:
        _validator().validate(long_lived, required_action="tool_result.submit")
    assert rejected.value.reason == "invalid_token"

    configured = ServiceTokenValidator(
        {
            "issuer": ISSUER,
            "audience": AUDIENCE,
            "keys": {CURRENT_KID: CURRENT_KEY},
            "max_lifetime_seconds": 1200,
        }
    )
    assert configured.validate(
        long_lived, required_action="tool_result.submit"
    ).jti == "jti-1"

    for keys in (
        {None: CURRENT_KEY},
        {CURRENT_KID: None},
        {1: CURRENT_KEY},
        {CURRENT_KID: 123},
        {CURRENT_KID: "x" * 31},
    ):
        with pytest.raises(ValueError, match="key ring"):
            ServiceAuthConfig.from_mapping(
                {"issuer": ISSUER, "audience": AUDIENCE, "keys": keys}
            )

    with pytest.raises(ValueError, match="max lifetime"):
        ServiceAuthConfig.from_mapping(
            {
                "issuer": ISSUER,
                "audience": AUDIENCE,
                "keys": {CURRENT_KID: CURRENT_KEY},
                "max_lifetime_seconds": 0,
            }
        )

    for field, value, message in (
        ("leeway_seconds", True, "leeway"),
        ("leeway_seconds", "0", "leeway"),
        ("max_lifetime_seconds", True, "max lifetime"),
        ("max_lifetime_seconds", "900", "max lifetime"),
    ):
        with pytest.raises(ValueError, match=message):
            ServiceAuthConfig.from_mapping(
                {
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                    "keys": {CURRENT_KID: CURRENT_KEY},
                    field: value,
                }
            )


@pytest.mark.anyio
async def test_store_rejects_coerced_numbers_and_hardens_sqlite_files(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    store = RuntimeStore(path)
    for epoch in (True, "7", 7.0):
        with pytest.raises(ValueError, match="epoch"):
            await store.create_or_get_session(
                run_id="run-bad", client_run_id="client-bad",
                epoch=epoch, scopes=[],
            )

    session = await store.create_or_get_session(
        run_id="run-good", client_run_id="client-good", epoch=7, scopes=[]
    )
    for after in (True, "0", 0.0):
        with pytest.raises(ValueError, match="replay window"):
            await store.list_events(session.session_id, after=after)
    for limit in (True, "1", 1.0):
        with pytest.raises(ValueError, match="replay window"):
            await store.list_events(session.session_id, limit=limit)

    body = {"command_id": "bad-epoch"}
    for epoch in (True, "7", 7.0):
        with pytest.raises(ValueError, match="epoch"):
            await store.claim_command(
                CommandBinding(
                    audience=AUDIENCE,
                    command_id="bad-epoch",
                    jti=f"bad-epoch-{epoch!r}",
                    request_digest=canonical_request_digest(body),
                    run_id="run-good",
                    session_id=session.session_id,
                    epoch=epoch,
                    action="tool_result.submit",
                )
            )

    if os.name == "posix":
        os.chmod(path, 0o666)
        reopened = RuntimeStore(path)
        await reopened.initialize()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        await reopened.append_event(
            session.session_id, event_kind="session_started", payload={}
        )
        candidates = (
            path,
            path.with_name(f"{path.name}-wal"),
            path.with_name(f"{path.name}-shm"),
        )
        for candidate in candidates:
            if candidate.exists():
                assert stat.S_IMODE(os.stat(candidate).st_mode) == 0o600


def test_canonical_digest_and_strict_schema_are_stable_and_bounded():
    assert canonical_request_digest({"b": 2, "a": "值"}) == canonical_request_digest(
        {"a": "值", "b": 2}
    )
    with pytest.raises(RequestSchemaError, match="request_too_large"):
        canonical_request_digest({"payload": "x" * 100}, max_bytes=32)

    class Command(StrictRequestModel):
        command_id: str

    assert Command(command_id="one").command_id == "one"
    with pytest.raises(ValidationError):
        Command(command_id="one", ignored=True)
