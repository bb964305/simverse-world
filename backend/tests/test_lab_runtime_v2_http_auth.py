from __future__ import annotations

import time
from copy import deepcopy

import httpx
import jwt
import pytest

from app.lab.runtime_ref import server as runtime_server
from app.lab.runtime_ref.service_auth import canonical_request_digest
from app.lab.runtime_ref.server import create_app, create_entrypoint_app


ISSUER = "simverse-gateway"
AUDIENCE = "lab-runtime"
CURRENT_KID = "runtime-current"
CURRENT_KEY = "runtime-current-secret-at-least-32-bytes"
NEXT_KID = "runtime-next"
NEXT_KEY = "runtime-next-secret-at-least-32-bytes"


def _service_auth():
    return {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "keys": {CURRENT_KID: CURRENT_KEY, NEXT_KID: NEXT_KEY},
    }


def _app(path, *, completer_factory=None):
    kwargs = {}
    if completer_factory is not None:
        kwargs["completer_factory"] = completer_factory
    return create_app(
        protocol_version=2,
        runtime_store_path=str(path),
        service_auth=_service_auth(),
        **kwargs,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime.test"
    )


def _create_body(suffix: str = "one") -> dict:
    return {
        "schema_version": 2,
        "command_id": f"create-{suffix}",
        "run_id": f"run-{suffix}",
        "client_run_id": f"client-{suffix}",
        "epoch": 7,
        "scopes": ["web_search"],
        "budget_usd": 0.5,
        "egress_allowlist": [],
    }


def _token(
    *,
    run_id: str,
    session_id: str,
    epoch: int,
    action: str,
    jti: str,
    kid: str = CURRENT_KID,
    key: str = CURRENT_KEY,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    expires_in: int = 300,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "run_id": run_id,
            "session_id": session_id,
            "epoch": epoch,
            "actions": [action],
            "jti": jti,
            "nbf": now - 1,
            "exp": now + expires_in,
        },
        key,
        algorithm="HS256",
        headers={"kid": kid},
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_token(body: dict, **overrides) -> str:
    values = {
        "run_id": body["run_id"],
        "session_id": body["client_run_id"],
        "epoch": body["epoch"],
        "action": "session.create",
        "jti": f"jti-{body['command_id']}",
    }
    values.update(overrides)
    return _token(**values)


def test_v2_app_requires_durable_store_and_service_auth(tmp_path):
    with pytest.raises(ValueError, match="runtime_store_path"):
        create_app(protocol_version=2, service_auth=_service_auth())
    with pytest.raises(ValueError, match="service_auth"):
        create_app(protocol_version=2, runtime_store_path=str(tmp_path / "runtime.db"))
    with pytest.raises(ValueError, match="durable file"):
        create_app(
            protocol_version=2,
            runtime_store_path=":memory:",
            service_auth=_service_auth(),
        )
    with pytest.raises(ValueError, match="unsupported"):
        create_app(protocol_version=3)


def test_standalone_entrypoint_is_explicit_and_v2_fails_closed(tmp_path):
    assert "/runs" not in {
        route.path for route in runtime_server.app.routes if hasattr(route, "path")
    }
    with pytest.raises(ValueError, match="explicitly"):
        create_entrypoint_app({})
    with pytest.raises(ValueError, match="requires store path"):
        create_entrypoint_app({"LAB_RUNTIME_PROTOCOL_VERSION": "2"})
    with pytest.raises(ValueError, match="current and next"):
        create_entrypoint_app(
            {
                "LAB_RUNTIME_PROTOCOL_VERSION": "2",
                "LAB_RUNTIME_STORE_PATH": str(tmp_path / "runtime.db"),
                "LAB_RUNTIME_AUTH_ISSUER": ISSUER,
                "LAB_RUNTIME_AUTH_AUDIENCE": AUDIENCE,
                "LAB_RUNTIME_AUTH_KEYS_JSON": '{"only":"single-key-at-least-32-bytes-long"}',
            }
        )

    v1 = create_entrypoint_app({"LAB_RUNTIME_PROTOCOL_VERSION": "1"})
    assert v1.version == "1.0"

    v2 = create_entrypoint_app(
        {
            "LAB_RUNTIME_PROTOCOL_VERSION": "2",
            "LAB_RUNTIME_STORE_PATH": str(tmp_path / "runtime.db"),
            "LAB_RUNTIME_AUTH_ISSUER": ISSUER,
            "LAB_RUNTIME_AUTH_AUDIENCE": AUDIENCE,
            "LAB_RUNTIME_AUTH_KEYS_JSON": (
                '{"runtime-current":"runtime-current-secret-at-least-32-bytes",'
                '"runtime-next":"runtime-next-secret-at-least-32-bytes"}'
            ),
        }
    )
    assert v2.version == "2.0"
    assert "/runs/{sid}/events" in {
        route.path for route in v2.routes if hasattr(route, "path")
    }


@pytest.mark.anyio
async def test_create_is_fail_closed_and_accepts_current_and_next_keys(tmp_path):
    app = _app(tmp_path / "runtime.db")
    body = _create_body()
    async with _client(app) as client:
        livez = await client.get("/livez")
        assert livez.status_code == 200
        assert livez.json() == {"alive": True, "protocol_version": 2}

        missing = await client.post("/runs", json=body)
        assert missing.status_code == 401

        denied_tokens = (
            (_create_token(body, key="wrong-key-at-least-32-bytes-long"), 401),
            (
                _create_token(
                    body, kid="unknown-kid",
                    key="unknown-key-at-least-32-bytes-long",
                ),
                401,
            ),
            (_create_token(body, issuer="other-issuer"), 401),
            (_create_token(body, audience="lab-executor"), 401),
            (_create_token(body, expires_in=-10), 401),
            (_create_token(body, action="events.read"), 403),
            (_create_token(body, epoch=body["epoch"] + 1), 403),
            (_create_token(body, session_id="other-client"), 403),
        )
        for token, expected_status in denied_tokens:
            denied = await client.post("/runs", json=body, headers=_auth(token))
            assert denied.status_code == expected_status, denied.text
            assert denied.status_code != 404

        current = await client.post(
            "/runs", json=body, headers=_auth(_create_token(body))
        )
        assert current.status_code == 201, current.text
        assert current.json()["session_id"]
        assert current.json()["receipt_id"]
        assert current.json()["request_digest"] == canonical_request_digest(body)

        next_body = _create_body("next")
        following = await client.post(
            "/runs",
            json=next_body,
            headers=_auth(
                _create_token(next_body, kid=NEXT_KID, key=NEXT_KEY)
            ),
        )
        assert following.status_code == 201, following.text

        extra = {**_create_body("strict"), "ignored": True}
        strict = await client.post(
            "/runs", json=extra, headers=_auth(_create_token(extra))
        )
        assert strict.status_code == 422


@pytest.mark.anyio
async def test_create_exact_retry_survives_app_restart_and_rejects_cross_binding(tmp_path):
    path = tmp_path / "runtime.db"
    body = _create_body("restart")
    token = _create_token(body, jti="restart-jti")

    async with _client(_app(path)) as first_client:
        first = await first_client.post(
            "/runs", json=body, headers=_auth(token)
        )
    assert first.status_code == 201, first.text

    async with _client(_app(path)) as restarted_client:
        retry = await restarted_client.post(
            "/runs", json=body, headers=_auth(token)
        )
        assert retry.status_code == 201, retry.text
        assert retry.json() == first.json()

        expired_retry = await restarted_client.post(
            "/runs",
            json=body,
            headers=_auth(_create_token(body, jti="restart-jti", expires_in=-10)),
        )
        assert expired_retry.status_code == 401

        changed = deepcopy(body)
        changed["budget_usd"] = 0.75
        replay = await restarted_client.post(
            "/runs", json=changed, headers=_auth(token)
        )
        assert replay.status_code == 403
        assert "not found" not in replay.text.lower()

        other_jti = _create_token(body, jti="different-jti")
        command_reuse = await restarted_client.post(
            "/runs", json=body, headers=_auth(other_jti)
        )
        assert command_reuse.status_code == 403


@pytest.mark.anyio
async def test_run_routes_authenticate_and_bind_before_session_lookup(tmp_path):
    app = _app(tmp_path / "runtime.db")
    create_body = _create_body("routes")
    async with _client(app) as client:
        created = await client.post(
            "/runs",
            json=create_body,
            headers=_auth(_create_token(create_body)),
        )
        assert created.status_code == 201, created.text
        sid = created.json()["session_id"]
        run_id = create_body["run_id"]
        epoch = create_body["epoch"]

        event_token = _token(
            run_id=run_id, session_id=sid, epoch=epoch,
            action="events.read", jti="events-jti",
        )
        events = await client.get(
            f"/runs/{sid}/events", headers=_auth(event_token)
        )
        assert events.status_code == 200, events.text
        assert events.json() == {"events": [], "done": False}

        unauthenticated_bad_cursor = await client.get(
            f"/runs/{sid}/events", params={"after": "not-an-integer"}
        )
        assert unauthenticated_bad_cursor.status_code == 401
        authenticated_bad_cursor = await client.get(
            f"/runs/{sid}/events",
            params={"after": "not-an-integer"},
            headers=_auth(event_token),
        )
        assert authenticated_bad_cursor.status_code == 422

        missing_auth = await client.get(f"/runs/{sid}/events")
        assert missing_auth.status_code == 401

        wrong_action = await client.get(
            f"/runs/{sid}/events",
            headers=_auth(
                _token(
                    run_id=run_id, session_id=sid, epoch=epoch,
                    action="artifacts.read", jti="wrong-action-jti",
                )
            ),
        )
        assert wrong_action.status_code == 403

        wrong_path = await client.get(
            "/runs/session-does-not-exist/events", headers=_auth(event_token)
        )
        assert wrong_path.status_code == 403
        assert "not found" not in wrong_path.text.lower()

        expired_unknown = await client.get(
            "/runs/unknown/events",
            headers=_auth(
                _token(
                    run_id="unknown-run", session_id="unknown", epoch=epoch,
                    action="events.read", jti="expired-unknown", expires_in=-10,
                )
            ),
        )
        assert expired_unknown.status_code == 401
        assert "not found" not in expired_unknown.text.lower()

        valid_unknown = await client.get(
            "/runs/unknown/events",
            headers=_auth(
                _token(
                    run_id="unknown-run", session_id="unknown", epoch=epoch,
                    action="events.read", jti="valid-unknown",
                )
            ),
        )
        assert valid_unknown.status_code == 404

        wrong_epoch = await client.get(
            f"/runs/{sid}/events",
            headers=_auth(
                _token(
                    run_id=run_id, session_id=sid, epoch=epoch + 1,
                    action="events.read", jti="wrong-epoch",
                )
            ),
        )
        assert wrong_epoch.status_code == 403

        artifact_token = _token(
            run_id=run_id, session_id=sid, epoch=epoch,
            action="artifacts.read", jti="artifacts-jti",
        )
        artifacts = await client.get(
            f"/runs/{sid}/artifacts", headers=_auth(artifact_token)
        )
        assert artifacts.status_code == 409
        assert "pending" in artifacts.text.lower()


@pytest.mark.anyio
async def test_goal_and_result_scaffolds_validate_auth_and_binding_without_model(tmp_path):
    def forbidden_completer():
        raise AssertionError("Phase 2 scaffold must not construct a model completer")

    app = _app(
        tmp_path / "runtime.db", completer_factory=forbidden_completer
    )
    create_body = _create_body("scaffold")
    async with _client(app) as client:
        created = await client.post(
            "/runs",
            json=create_body,
            headers=_auth(_create_token(create_body)),
        )
        sid = created.json()["session_id"]
        run_id = create_body["run_id"]
        epoch = create_body["epoch"]

        goal_body = {
            "schema_version": 2,
            "command_id": "goal-scaffold",
            "run_id": run_id,
            "session_id": sid,
            "epoch": epoch,
            "brief": "do not invoke the model yet",
            "scopes": ["web_search"],
        }
        goal_token = _token(
            run_id=run_id, session_id=sid, epoch=epoch,
            action="goal.submit", jti="goal-jti",
        )
        goal = await client.post(
            f"/runs/{sid}/goal", json=goal_body, headers=_auth(goal_token)
        )
        assert goal.status_code == 501

        missing_goal = await client.post(f"/runs/{sid}/goal", json=goal_body)
        assert missing_goal.status_code == 401
        wrong_goal_path = await client.post(
            "/runs/other-session/goal", json=goal_body, headers=_auth(goal_token)
        )
        assert wrong_goal_path.status_code == 403

        payload = {"sentinel": "BROKER-SENTINEL"}
        result_body = {
            "schema_version": 2,
            "command_id": "result-scaffold",
            "run_id": run_id,
            "session_id": sid,
            "turn_id": "turn-1",
            "intent_id": "intent-1",
            "action_id": "action-1",
            "outcome": "succeeded",
            "payload": payload,
            "result_digest": canonical_request_digest(payload),
            "epoch": epoch,
        }
        result_token = _token(
            run_id=run_id, session_id=sid, epoch=epoch,
            action="tool_result.submit", jti="result-jti",
        )
        result = await client.post(
            f"/runs/{sid}/results", json=result_body,
            headers=_auth(result_token),
        )
        assert result.status_code == 501

        wrong_result_epoch = deepcopy(result_body)
        wrong_result_epoch["epoch"] = epoch + 1
        denied = await client.post(
            f"/runs/{sid}/results", json=wrong_result_epoch,
            headers=_auth(result_token),
        )
        assert denied.status_code == 403


@pytest.mark.anyio
async def test_control_surfaces_require_runtime_control_before_lookup(tmp_path):
    app = _app(tmp_path / "runtime.db")
    body = _create_body("control")
    async with _client(app) as client:
        created = await client.post(
            "/runs", json=body, headers=_auth(_create_token(body))
        )
        sid = created.json()["session_id"]
        token = _token(
            run_id=body["run_id"], session_id=sid, epoch=body["epoch"],
            action="runtime.control", jti="control-jti",
        )

        for action in ("stop", "cancel", "terminate", "kill"):
            missing = await client.post(f"/runs/{sid}/{action}")
            assert missing.status_code == 401
            scaffold = await client.post(
                f"/runs/{sid}/{action}", headers=_auth(token)
            )
            assert scaffold.status_code == 501

        health_missing = await client.get(f"/runs/{sid}/health")
        assert health_missing.status_code == 401
        health = await client.get(
            f"/runs/{sid}/health", headers=_auth(token)
        )
        assert health.status_code == 200
        assert health.json() == {"alive": True, "cancelled": False}


@pytest.mark.anyio
async def test_body_routes_authenticate_before_reading_or_validating_json(tmp_path):
    app = _app(tmp_path / "runtime.db")
    create_body = _create_body("body-order")
    create_token = _create_token(create_body)
    malformed = b'{"schema_version":2'
    oversized = b"x" * (256 * 1024 + 1)

    async with _client(app) as client:
        missing = await client.post("/runs", content=malformed)
        assert missing.status_code == 401
        wrong_action = await client.post(
            "/runs",
            content=malformed,
            headers=_auth(_create_token(create_body, action="events.read")),
        )
        assert wrong_action.status_code == 403
        invalid = await client.post(
            "/runs", content=malformed, headers=_auth(create_token)
        )
        assert invalid.status_code == 422
        too_large = await client.post(
            "/runs", content=oversized, headers=_auth(create_token)
        )
        assert too_large.status_code == 413
        unauthenticated_large = await client.post("/runs", content=oversized)
        assert unauthenticated_large.status_code == 401

        created = await client.post(
            "/runs", json=create_body, headers=_auth(create_token)
        )
        sid = created.json()["session_id"]
        common = {
            "run_id": create_body["run_id"],
            "session_id": sid,
            "epoch": create_body["epoch"],
        }
        route_tokens = (
            ("goal", "goal.submit"),
            ("results", "tool_result.submit"),
            ("approve", "runtime.control"),
        )
        for route, action in route_tokens:
            token = _token(
                **common, action=action, jti=f"malformed-{route}"
            )
            no_auth = await client.post(f"/runs/{sid}/{route}", content=malformed)
            assert no_auth.status_code == 401
            wrong_path = await client.post(
                f"/runs/other-session/{route}",
                content=malformed,
                headers=_auth(token),
            )
            assert wrong_path.status_code == 403
            valid_auth = await client.post(
                f"/runs/{sid}/{route}", content=malformed, headers=_auth(token)
            )
            assert valid_auth.status_code == 422

        string_epoch = deepcopy(create_body)
        string_epoch["command_id"] = "create-string-epoch"
        string_epoch["epoch"] = "7"
        strict = await client.post(
            "/runs", json=string_epoch, headers=_auth(create_token)
        )
        assert strict.status_code == 422
