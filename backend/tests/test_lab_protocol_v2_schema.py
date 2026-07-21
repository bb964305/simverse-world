"""Strict protocol-v2 schema contracts fixed before Runtime/Gateway wiring."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.lab.protocol import (
    MAX_COMMAND_BYTES,
    ControlCommand,
    RuntimeEvent,
    RuntimeV2Handshake,
    ToolResultCommand,
    args_digest,
    content_digest,
)


def _event_body(**overrides):
    body = {
        "schema_version": 2,
        "event_id": "event-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "cursor": 1,
        "epoch": 4,
        "event_kind": "session_started",
        "occurred_at": datetime.now(UTC),
    }
    body.update(overrides)
    return body


def _result_body(**overrides):
    payload = {"records": [1, 2], "status": "ok"}
    body = {
        "schema_version": 2,
        "command_id": "result-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "intent_id": "intent-1",
        "action_id": "action-1",
        "outcome": "succeeded",
        "payload": payload,
        "result_digest": content_digest(payload),
        "epoch": 4,
    }
    body.update(overrides)
    return body


def _control_body(**overrides):
    body = {
        "schema_version": 2,
        "command_id": "control-1",
        "request_id": "request-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "target_kind": "runtime",
        "target_id": "session-1",
        "action": "kill",
        "epoch": 8,
        "deadline_at": datetime.now(UTC),
    }
    body.update(overrides)
    return body


def _handshake_body(**overrides):
    body = {
        "schema_version": 2,
        "protocol_version": 2,
        "provider_name": "runtime-ref",
        "durability_class": "session_affine",
        "reattach_capability": "client_run_id",
        "effect_mode": "broker_only",
        "capabilities": ["broker_mediation", "cursor_replay", "control"],
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize("bad_version", [True, False, "2", 2.0])
@pytest.mark.parametrize(
    ("model", "body_factory"),
    [
        (RuntimeEvent, _event_body),
        (ToolResultCommand, _result_body),
        (ControlCommand, _control_body),
        (RuntimeV2Handshake, _handshake_body),
    ],
)
def test_v2_schema_version_is_exact_strict_integer(
    model, body_factory, bad_version
):
    with pytest.raises(ValidationError):
        model.model_validate(body_factory(schema_version=bad_version))


@pytest.mark.parametrize("bad_version", [True, False, "2", 2.0, 1, 3])
def test_v2_handshake_protocol_version_is_exact_strict_integer(bad_version):
    with pytest.raises(ValidationError):
        RuntimeV2Handshake.model_validate(
            _handshake_body(protocol_version=bad_version)
        )


def test_tool_result_binds_payload_digest_and_rejects_unknown_fields():
    body = _result_body()
    parsed = ToolResultCommand.model_validate(body)
    assert parsed.payload == body["payload"]

    with pytest.raises(ValidationError, match="result_digest"):
        ToolResultCommand.model_validate({**body, "result_digest": "0" * 64})
    with pytest.raises(ValidationError, match="extra"):
        ToolResultCommand.model_validate({**body, "transport_token": "secret"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_protocol_v2_rejects_nonfinite_json_values(value):
    result = _result_body(payload={"value": value}, result_digest="0" * 64)
    with pytest.raises(ValidationError):
        ToolResultCommand.model_validate(result)
    with pytest.raises(ValidationError):
        RuntimeEvent.model_validate(_event_body(payload={"value": value}))


def test_protocol_v2_rejects_oversize_canonical_json():
    oversized = "x" * (MAX_COMMAND_BYTES + 1)
    with pytest.raises(ValidationError, match="MAX_COMMAND_BYTES"):
        RuntimeEvent.model_validate(_event_body(payload={"value": oversized}))

    payload = {"value": oversized}
    with pytest.raises(ValidationError, match="MAX_COMMAND_BYTES"):
        ToolResultCommand.model_validate(
            _result_body(payload=payload, result_digest=content_digest(payload))
        )


def test_runtime_event_requires_turn_intent_and_result_outcome_binding():
    with pytest.raises(ValidationError, match="turn_id and intent_id"):
        RuntimeEvent.model_validate(_event_body(event_kind="tool_result"))

    event = RuntimeEvent.model_validate(_event_body(
        event_kind="tool_result",
        turn_id="turn-1",
        intent_id="intent-1",
        outcome="denied",
        payload={"reason": "policy"},
    ))
    assert event.outcome == "denied"

    with pytest.raises(ValidationError, match="outcome is only valid"):
        RuntimeEvent.model_validate(_event_body(outcome="failed"))


def test_tool_intent_freezes_structured_tool_binding_and_digest():
    tool_args = {"filters": {"limit": 3}, "query": "sentinel"}
    event = RuntimeEvent.model_validate(_event_body(
        event_kind="tool_intent",
        turn_id="turn-1",
        intent_id="intent-1",
        tool_name="web.search",
        tool_args=tool_args,
        tool_args_digest=args_digest(tool_args),
    ))
    assert event.tool_name == "web.search"
    assert event.tool_args == tool_args
    assert event.tool_args_digest == args_digest({
        "query": "sentinel", "filters": {"limit": 3},
    })

    with pytest.raises(ValidationError, match="tool_args_digest"):
        RuntimeEvent.model_validate(_event_body(
            event_kind="tool_intent",
            turn_id="turn-1",
            intent_id="intent-1",
            tool_name="web.search",
            tool_args=tool_args,
            tool_args_digest="0" * 64,
        ))


@pytest.mark.parametrize("missing", ["tool_name", "tool_args", "tool_args_digest"])
def test_tool_intent_rejects_each_missing_structured_field(missing):
    tool_args = {"query": "sentinel"}
    body = _event_body(
        event_kind="tool_intent",
        turn_id="turn-1",
        intent_id="intent-1",
        tool_name="web.search",
        tool_args=tool_args,
        tool_args_digest=args_digest(tool_args),
    )
    body.pop(missing)
    with pytest.raises(ValidationError, match=missing):
        RuntimeEvent.model_validate(body)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool_name", "web.search"),
        ("tool_args", {"query": "sentinel"}),
        ("tool_args_digest", "0" * 64),
    ],
)
def test_non_intent_events_reject_tool_binding_field_mixing(field, value):
    with pytest.raises(ValidationError, match="only valid on tool_intent"):
        RuntimeEvent.model_validate(_event_body(**{field: value}))


def test_tool_intent_cannot_hide_binding_inside_payload():
    with pytest.raises(ValidationError, match="tool_name"):
        RuntimeEvent.model_validate(_event_body(
            event_kind="tool_intent",
            turn_id="turn-1",
            intent_id="intent-1",
            payload={
                "tool_name": "web.search",
                "tool_args": {"query": "sentinel"},
                "tool_args_digest": "0" * 64,
            },
        ))


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (RuntimeEvent, _event_body(occurred_at=datetime.now())),
        (ControlCommand, _control_body(deadline_at=datetime.now())),
    ],
)
def test_protocol_v2_timestamps_must_be_timezone_aware(model, body):
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_control_command_has_one_fenced_target_and_transport_owned_auth():
    command = ControlCommand.model_validate(_control_body())
    assert command.schema_version == 2
    assert "authorization_claims" not in ControlCommand.model_fields
    assert "transport JWT" in (ControlCommand.__doc__ or "")

    with pytest.raises(ValidationError):
        ControlCommand.model_validate(_control_body(action="restart"))
    with pytest.raises(ValidationError, match="extra"):
        ControlCommand.model_validate(_control_body(
            authorization_claims={"actions": ["kill"], "epoch": 8}
        ))


def test_runtime_v2_handshake_proves_broker_only_effects_and_recovery():
    parsed = RuntimeV2Handshake.model_validate(_handshake_body())
    assert parsed.provider_name == "runtime-ref"
    assert parsed.effect_mode == "broker_only"
    assert parsed.capabilities == [
        "broker_mediation", "cursor_replay", "control",
    ]

    missing_effect_mode = _handshake_body()
    missing_effect_mode.pop("effect_mode")
    with pytest.raises(ValidationError, match="effect_mode"):
        RuntimeV2Handshake.model_validate(missing_effect_mode)

    for field, value in (
        ("effect_mode", "direct"),
        ("durability_class", "ephemeral"),
        ("reattach_capability", "none"),
        ("capabilities", ["cursor_replay", "control"]),
    ):
        with pytest.raises(ValidationError):
            RuntimeV2Handshake.model_validate(_handshake_body(**{field: value}))


@pytest.mark.parametrize(
    "dangerous_capability",
    [
        "direct_tool_execution",
        "direct_tools",
        "docker_socket",
        "world_write",
        "db_credentials",
        "database_credentials",
        "executor_credentials",
    ],
)
def test_runtime_v2_handshake_rejects_direct_effect_capabilities(
    dangerous_capability,
):
    capabilities = ["broker_mediation", dangerous_capability]
    with pytest.raises(ValidationError, match="effect capability"):
        RuntimeV2Handshake.model_validate(
            _handshake_body(capabilities=capabilities)
        )
