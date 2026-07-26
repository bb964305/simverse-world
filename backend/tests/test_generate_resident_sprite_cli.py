from __future__ import annotations

import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from app.services.resident_sprite_artifacts import (
    advance_stage,
    claim_stage,
    complete_stage,
    consume_request_budget,
    create_run,
    load_run,
    write_artifact,
    write_canonical_json_artifact,
)
from app.services.resident_sprite_generation import (
    ProviderImageResult,
    ResidentSpriteRequest,
    SanitizedError,
    new_run_id,
)
from app.services.resident_sprite_provider import ProviderError


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_resident_sprite.py"
SPEC = importlib.util.spec_from_file_location("generate_resident_sprite_cli", SCRIPT)
assert SPEC and SPEC.loader
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


VALID_SPEC = {
    "asset_key": "pilot-01",
    "display_name": "Pilot One",
    "appearance": "Green coat and silver hair.",
    "gender": "neutral",
    "age_group": "adult",
    "vibe": "calm",
    "tags": ["maker"],
    "model": "gpt-image-2",
}

CAPABILITY_AUTH_ARGS = [
    "--price-per-request-usd", "0.10",
    "--confirm-max-requests", "7",
    "--confirm-max-cost-usd", "0.70",
    "--cost-source", "provider-price-test",
]

CAPABILITY_CONFIRM_ARGS = [
    "--confirm-max-requests", "7",
    "--confirm-max-cost-usd", "0.70",
]


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def parser_commands() -> set[str]:
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction"
    )
    return set(subparsers.choices)


def test_parser_exposes_exact_ten_commands_and_no_replace_mode(tmp_path) -> None:
    assert parser_commands() == {
        "probe-wire",
        "qualify-generate",
        "qualify-review",
        "generate",
        "resume",
        "review-phaser",
        "approve",
        "reject",
        "publish",
        "recover",
    }
    spec = write_json(tmp_path / "resident.json", VALID_SPEC)
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(
            ["generate", "--spec", str(spec), "--replace-existing"]
        )
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["probe-wire", "--spec", "x.json"],
        ["qualify-generate", "--spec", "x.json", "--wire-receipt", "id"],
        ["qualify-review", "--qualification-id", "id", "--reviewer", "r"],
        ["generate"],
        ["resume"],
        ["review-phaser", "--run-id", "id"],
        ["approve", "--run-id", "id", "--reviewer", "r"],
        ["reject", "--run-id", "id", "--reviewer", "r"],
        ["publish"],
    ],
)
def test_required_arguments_are_enforced(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_validation_happens_before_injected_handler(tmp_path, capfd) -> None:
    invalid = write_json(tmp_path / "resident.json", {**VALID_SPEC, "asset_key": "../bad"})
    calls = 0

    def handler(args, payload):
        nonlocal calls
        calls += 1
        return {"ok": True}

    result = cli.main(
        ["generate", "--spec", str(invalid)], handlers={"generate": handler}
    )
    captured = capfd.readouterr()
    assert result == 2
    assert calls == 0
    assert json.loads(captured.err)["error"]["code"] == "VALIDATION_FAILED"
    assert captured.out == ""


def test_injected_provider_handler_returns_canonical_sanitized_json(tmp_path, capfd) -> None:
    spec = write_json(tmp_path / "resident.json", VALID_SPEC)

    def handler(args, payload):
        assert payload.sprite_key == "generated/pilot-01"
        return {"z": 2, "a": "ok"}

    result = cli.main(
        ["probe-wire", "--spec", str(spec), "--operator", "operator-a", *CAPABILITY_AUTH_ARGS],
        handlers={"probe-wire": handler},
    )
    captured = capfd.readouterr()
    assert result == 0
    assert captured.out == '{"a":"ok","z":2}\n'
    assert captured.err == ""


def test_async_handler_is_supported_without_default_network_client(tmp_path, capfd) -> None:
    spec = write_json(tmp_path / "resident.json", VALID_SPEC)

    async def handler(args, payload):
        return {"state": "wire-probed"}

    assert cli.main(
        ["probe-wire", "--spec", str(spec), "--operator", "operator-a", *CAPABILITY_AUTH_ARGS],
        handlers={"probe-wire": handler},
    ) == 0
    assert json.loads(capfd.readouterr().out) == {"state": "wire-probed"}


def test_provider_error_maps_to_exit_three_without_secret(tmp_path, capfd) -> None:
    marker = "sk-do-not-print-this"
    spec = write_json(tmp_path / "resident.json", VALID_SPEC)

    def handler(args, payload):
        raise ProviderError(
            SanitizedError(code="PROVIDER_TIMEOUT", message="provider request timed out")
        )

    result = cli.main(
        ["generate", "--spec", str(spec)], handlers={"generate": handler}
    )
    captured = capfd.readouterr()
    assert result == 3
    assert json.loads(captured.err)["error"]["code"] == "PROVIDER_TIMEOUT"
    assert marker not in captured.err + captured.out


@pytest.mark.parametrize(
    ("argv", "expected", "expected_code"),
    [
        (["probe-wire", "--spec", "{spec}", "--operator", "operator-a", *CAPABILITY_AUTH_ARGS], 2, "CONFIG_REQUIRED"),
        (["qualify-generate", "--spec", "{spec}", "--wire-receipt", "id", "--operator", "operator-a", *CAPABILITY_CONFIRM_ARGS], 2, "CONFIG_REQUIRED"),
        (["generate", "--spec", "{spec}"], 4, "CONFIG_REQUIRED"),
        (["resume", "--run-id", "{run}"], 4, "CONFIG_REQUIRED"),
        (["review-phaser", "--run-id", "{run}", "--reviewer", "reviewer-a"], 5, "CONFIG_REQUIRED"),
        (["reject", "--run-id", "{run}", "--reviewer", "reviewer-a", "--reason", "not ready"], 5, "CONFIG_REQUIRED"),
        (["publish", "--run-id", "{run}"], 6, "CONFIG_REQUIRED"),
        (["recover"], 6, "CONFIG_REQUIRED"),
    ],
)
def test_default_commands_report_missing_configuration_with_stable_codes(
    argv: list[str], expected: int, expected_code: str, tmp_path, capfd, monkeypatch
) -> None:
    for name in (
        cli.ENV_PROVIDER_BASE_URL,
        cli.ENV_PROVIDER_API_KEY,
        cli.ENV_PROVIDER_MODEL,
        cli.ENV_ARTIFACT_DIR,
        cli.ENV_ARTIFACT_ROOT,
        cli.ENV_QUALIFICATION_ROOT,
        cli.ENV_CAPABILITY_RECEIPT,
        cli.ENV_STATIC_ROOT,
        cli.ENV_STATIC_DIR,
    ):
        monkeypatch.delenv(name, raising=False)
    spec = write_json(tmp_path / "resident.json", VALID_SPEC)
    run_id = new_run_id()
    expanded = [item.format(spec=spec, run=run_id) for item in argv]
    assert cli.main(expanded) == expected
    captured = capfd.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == expected_code


def test_approval_checklist_is_strict_complete_and_bound(tmp_path, capfd) -> None:
    run_id = new_run_id()
    checks = {
        "identity_consistent": True,
        "directions_correct": True,
        "gait_readable": True,
        "scale_and_baseline_stable": True,
        "anatomy_and_clipping_clean": True,
        "background_clean": True,
        "asymmetry_acceptable": True,
        "originality_acceptable": True,
        "gameplay_fit": True,
    }
    checklist = write_json(
        tmp_path / "approval.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "decision": "approve",
            "checks": checks,
            "notes": "reviewed",
        },
    )

    def handler(args, payload):
        return {"accepted": payload.run_id}

    assert cli.main(
        ["approve", "--run-id", run_id, "--reviewer", "reviewer-a", "--checklist", str(checklist)],
        handlers={"approve": handler},
    ) == 0
    capfd.readouterr()

    checks["gameplay_fit"] = False
    write_json(checklist, {
        "schema_version": 1,
        "run_id": run_id,
        "decision": "approve",
        "checks": checks,
        "notes": "reviewed",
    })
    assert cli.main(
        ["approve", "--run-id", run_id, "--reviewer", "reviewer-a", "--checklist", str(checklist)],
        handlers={"approve": handler},
    ) == 5
    assert json.loads(capfd.readouterr().err)["error"]["code"] == "APPROVAL_INVALID"


def test_publish_handler_cannot_implicitly_receive_or_create_provider(tmp_path, capfd) -> None:
    run_id = new_run_id()
    observed = {}

    def handler(args, payload):
        observed["payload"] = payload
        observed["namespace"] = vars(args)
        return {"status": "mocked"}

    assert cli.main(
        ["publish", "--run-id", run_id], handlers={"publish": handler}
    ) == 0
    capfd.readouterr()
    assert observed["payload"] is None
    assert set(observed["namespace"]) == {"command", "run_id"}
    assert not any(isinstance(value, httpx.AsyncClient) for value in observed["namespace"].values())


def test_unknown_spec_keys_and_noncanonical_operator_are_rejected(tmp_path, capfd) -> None:
    spec = write_json(tmp_path / "resident.json", {**VALID_SPEC, "endpoint": "https://evil.invalid"})
    assert cli.main(
        ["probe-wire", "--spec", str(spec), "--operator", " operator-a", *CAPABILITY_AUTH_ARGS],
        handlers={"probe-wire": lambda *_: {"ok": True}},
    ) == 2
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "https://evil.invalid" not in captured.err


def test_default_validation_never_constructs_network_client(tmp_path, capfd, monkeypatch) -> None:
    invalid = write_json(tmp_path / "invalid.json", {**VALID_SPEC, "asset_key": "../escape"})

    def forbidden_client(*args, **kwargs):
        del args, kwargs
        raise AssertionError("network client must not be constructed")

    monkeypatch.setattr(cli.httpx, "AsyncClient", forbidden_client)
    assert cli.main(["generate", "--spec", str(invalid)]) == cli.EXIT_VALIDATION
    assert json.loads(capfd.readouterr().err)["error"]["code"] == "VALIDATION_FAILED"


def test_capability_cost_authorization_fails_before_network(tmp_path, capfd, monkeypatch) -> None:
    spec = write_json(tmp_path / "resident.json", VALID_SPEC)

    def forbidden_client(*args, **kwargs):
        del args, kwargs
        raise AssertionError("network client must not be constructed")

    monkeypatch.setattr(cli.httpx, "AsyncClient", forbidden_client)
    result = cli.main([
        "probe-wire", "--spec", str(spec), "--operator", "operator-a",
        "--price-per-request-usd", "0.10",
        "--confirm-max-requests", "7",
        "--confirm-max-cost-usd", "0.69",
        "--cost-source", "provider-price-test",
    ])
    assert result == cli.EXIT_VALIDATION
    assert json.loads(capfd.readouterr().err)["error"]["code"] == "PAID_CONFIRMATION_INVALID"


def test_default_probe_persists_strict_wire_evidence(tmp_path, capfd, monkeypatch) -> None:
    spec = write_json(tmp_path / "resident.json", VALID_SPEC)
    qualification_root = tmp_path / "qualification"
    monkeypatch.setenv(cli.ENV_PROVIDER_BASE_URL, "https://provider.example/v1")
    monkeypatch.setenv(cli.ENV_PROVIDER_API_KEY, "secret-never-print")
    monkeypatch.setenv(cli.ENV_PROVIDER_MODEL, VALID_SPEC["model"])
    monkeypatch.setenv(cli.ENV_QUALIFICATION_ROOT, str(qualification_root))

    class FakeHTTPClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

    class FakeProvider:
        def __init__(self, config, client):
            assert config.api_key == "secret-never-print"
            assert isinstance(client, FakeHTTPClient)

        async def probe_wire(self, prompt):
            assert "walking down" in prompt
            return (
                ProviderImageResult(
                    image_bytes=b"provider-png",
                    provider_request_id="request-1",
                    latency_ms=4,
                    submitted_request_count=1,
                ),
                "image[]",
            )

    monkeypatch.setattr(cli.httpx, "AsyncClient", FakeHTTPClient)
    monkeypatch.setattr(cli, "ResidentSpriteProvider", FakeProvider)

    assert cli.main([
        "probe-wire", "--spec", str(spec), "--operator", "operator-a",
        *CAPABILITY_AUTH_ARGS,
    ]) == 0
    captured = capfd.readouterr()
    result = json.loads(captured.out)
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert result["state"] == "wire_probed"
    assert receipt["wire_receipt_id"] == result["wire_receipt_id"]
    assert receipt["multipart_field"] == "image[]"
    assert receipt["submitted_request_count"] == 1
    assert receipt["cost_authorization"]["max_requests"] == 7
    assert receipt["cost_authorization"]["max_cost_usd"] == "0.70"
    assert "secret-never-print" not in captured.out + captured.err


def test_default_resume_loads_bound_request_and_enables_retry(tmp_path, capfd, monkeypatch) -> None:
    artifact_root = tmp_path / "runs"
    run_id = new_run_id()
    request = ResidentSpriteRequest.model_validate(VALID_SPEC)
    create_run(artifact_root, run_id, request)
    observed = {}

    async def fake_generate(bound_request, *, run_id, retry_failed):
        observed.update(request=bound_request, run_id=run_id, retry_failed=retry_failed)
        return {"run_id": run_id, "state": "auto_qc_passed"}

    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(artifact_root))
    monkeypatch.setattr(cli, "_generate", fake_generate)
    assert cli.main(["resume", "--run-id", run_id]) == 0
    assert json.loads(capfd.readouterr().out)["state"] == "auto_qc_passed"
    assert observed == {"request": request, "run_id": run_id, "retry_failed": True}


def _image_bytes(size: tuple[int, int], *, strip: bool = False) -> bytes:
    image = Image.new("RGB", size, (255, 0, 255))
    draw = ImageDraw.Draw(image)
    if strip:
        for column in range(3):
            left = column * 512 + 176
            draw.rectangle((left, 96, left + 159, 463), fill=(30, 120, 180))
            draw.point((left, 130), fill=(255, 255, 255))
    else:
        draw.rectangle((400, 160, 624, 920), fill=(30, 120, 180))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_default_qualification_generate_and_review_persist_capability(tmp_path, capfd, monkeypatch) -> None:
    qualification_root = tmp_path / "qualification"
    qualification_root.mkdir()
    now = datetime.now(timezone.utc)
    config = cli.ProviderConfig(
        base_url="https://provider.example/v1", api_key="unused", model=VALID_SPEC["model"]
    )
    contract = cli.CapabilityContract(
        normalized_origin=config.normalized_origin,
        model_alias=config.model,
        multipart_field="image[]",
    )
    wire_payload = {
        "schema_version": 1,
        "normalized_origin": contract.normalized_origin,
        "model_alias": contract.model_alias,
        "transport_security": contract.transport_security,
        "multipart_field": "image[]",
        "request_shape": cli.WireRequestShape(),
        "calibration_source_sha256": "a" * 64,
        "calibration_output_sha256": "b" * 64,
        "provider_request_ids": ["wire-request"],
        "submitted_request_count": 1,
        "cost_authorization": cli.CapabilityCostAuthorization(
            price_per_request_upper_bound_usd="0.10",
            max_cost_usd="0.70",
            cost_source="provider-price-test",
        ),
        "operator": "operator-a",
        "observed_at": now,
        "expires_at": now + timedelta(hours=24),
    }
    draft = cli.WireReceipt.model_construct(**wire_payload, wire_receipt_id="")
    wire = cli.WireReceipt(
        **wire_payload,
        wire_receipt_id=cli.content_id(draft, "wire_receipt_id"),
    )
    cli._write_json(cli._wire_path(qualification_root, wire.wire_receipt_id), wire)
    spec = write_json(tmp_path / "resident.json", VALID_SPEC)
    monkeypatch.setenv(cli.ENV_PROVIDER_BASE_URL, "https://provider.example/v1")
    monkeypatch.setenv(cli.ENV_PROVIDER_API_KEY, "secret-never-print")
    monkeypatch.setenv(cli.ENV_PROVIDER_MODEL, VALID_SPEC["model"])
    monkeypatch.setenv(cli.ENV_QUALIFICATION_ROOT, str(qualification_root))

    def forbidden_client(*args, **kwargs):
        del args, kwargs
        raise AssertionError("network client must not be constructed")

    monkeypatch.setattr(cli.httpx, "AsyncClient", forbidden_client)
    assert cli.main([
        "qualify-generate", "--spec", str(spec), "--wire-receipt", wire.wire_receipt_id,
        "--operator", "operator-a", "--confirm-max-requests", "7",
        "--confirm-max-cost-usd", "0.71",
    ]) == cli.EXIT_VALIDATION
    assert json.loads(capfd.readouterr().err)["error"]["code"] == "PAID_CONFIRMATION_MISMATCH"

    class FakeHTTPClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

    class FakeQualificationProvider:
        def __init__(self, config, client):
            del config, client
            self.ordinal = 0

        def result(self, image_bytes):
            self.ordinal += 1
            return ProviderImageResult(
                image_bytes=image_bytes,
                provider_request_id=f"qualification-{self.ordinal}",
                latency_ms=self.ordinal,
                submitted_request_count=self.ordinal,
            )

        async def generate_anchor(self, *args, **kwargs):
            kwargs["gate"]()
            kwargs["budget"].consume_before_post("anchor")
            return self.result(_image_bytes((1024, 1024)))

        async def edit_strip(self, *args, **kwargs):
            kwargs["gate"]()
            kwargs["budget"].consume_before_post(kwargs["stage"])
            return self.result(_image_bytes((1536, 512), strip=True))

        async def generate_oneshot_draft(self, *args, **kwargs):
            kwargs["gate"]()
            kwargs["budget"].consume_before_post("anchor")
            return self.result(b"oneshot-candidate")

    monkeypatch.setattr(cli.httpx, "AsyncClient", FakeHTTPClient)
    monkeypatch.setattr(cli, "ResidentSpriteProvider", FakeQualificationProvider)
    assert cli.main([
        "qualify-generate", "--spec", str(spec), "--wire-receipt", wire.wire_receipt_id,
        "--operator", "operator-a", *CAPABILITY_CONFIRM_ARGS,
    ]) == 0
    generated = json.loads(capfd.readouterr().out)
    scores = write_json(tmp_path / "scores.json", {
        "schema_version": 1,
        "qualification_id": generated["qualification_id"],
        "scores": [
            {
                "candidate_id": candidate_id,
                "identity_consistency": 4,
                "layout_correctness": 4,
                "movement_readability": 4,
                "visual_fit": 4,
            }
            for candidate_id in generated["candidate_ids"]
        ],
        "notes": "blind review passed",
    })
    qualification_path = (
        qualification_root / "qualifications" / generated["qualification_id"] / "qualification.json"
    )
    original_qualification = qualification_path.read_bytes()
    tampered = json.loads(original_qualification)
    tampered["cost_authorization"]["max_cost_usd"] = "0.80"
    qualification_path.write_text(json.dumps(tampered), encoding="utf-8")
    assert cli.main([
        "qualify-review", "--qualification-id", generated["qualification_id"],
        "--reviewer", "reviewer-b", "--scores", str(scores),
    ]) == cli.EXIT_VALIDATION
    assert json.loads(capfd.readouterr().err)["error"]["code"] == "QUALIFICATION_COST_EVIDENCE_INVALID"
    qualification_path.write_bytes(original_qualification)
    assert cli.main([
        "qualify-review", "--qualification-id", generated["qualification_id"],
        "--reviewer", "reviewer-b", "--scores", str(scores),
    ]) == 0
    reviewed = json.loads(capfd.readouterr().out)
    receipt = cli.CapabilityReceipt.model_validate_json(Path(reviewed["receipt_path"]).read_bytes())
    assert reviewed["state"] == "qualified"
    assert len(receipt.latency_ms) == 5
    assert len(receipt.evidence_sha256) == 5
    assert receipt.capability_request_count == 6
    assert receipt.capability_cost_upper_bound_usd == "0.70"
    assert receipt.cost_source == "provider-price-test"


def _reviewable_run(root: Path, *, reviewed: bool = True) -> str:
    run_id = new_run_id()
    request = ResidentSpriteRequest.model_validate(VALID_SPEC)
    create_run(root, run_id, request)
    now = datetime.now(timezone.utc)
    for stage, relative_path in (
        ("anchor", "anchor.png"),
        ("down", "strips/down.png"),
        ("left", "strips/left.png"),
        ("up", "strips/up.png"),
    ):
        claim = claim_stage(
            root, run_id, stage, "test-owner", now, timedelta(minutes=5),
            expected_artifact_path=relative_path,
        )
        write_artifact(root, run_id, relative_path, stage.encode())
        complete_stage(
            root, run_id, stage=stage, owner=claim.owner,
            attempt_id=claim.attempt_id, now=now,
        )
    advance_stage(root, run_id, stage="strips", state="strips_ready")
    write_artifact(root, run_id, "candidate/texture.png", b"texture")
    write_artifact(root, run_id, "candidate/portrait.png", b"portrait")
    advance_stage(root, run_id, stage="postprocess", state="processed")
    write_canonical_json_artifact(root, run_id, "candidate/qc.json", {
        "direction_policy": "mirror_right", "findings": [], "passed": True,
    })
    advance_stage(root, run_id, stage="auto_qc", state="auto_qc_passed")
    advance_stage(root, run_id, stage="candidate", state="candidate_ready")
    if reviewed:
        advance_stage(root, run_id, stage="phaser_review", state="phaser_reviewed")
    return run_id


def _approval_file(path: Path, run_id: str) -> Path:
    checks = {name: True for name in cli.ApprovalChecks.model_fields}
    return write_json(path, {
        "schema_version": 1,
        "run_id": run_id,
        "decision": "approve",
        "checks": checks,
        "notes": "reviewed in Phaser",
    })


def test_default_phaser_review_validates_qc_and_advances_manifest(tmp_path, capfd, monkeypatch) -> None:
    artifact_root = tmp_path / "runs"
    run_id = _reviewable_run(artifact_root, reviewed=False)
    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(artifact_root))

    assert cli.main([
        "review-phaser", "--run-id", run_id, "--reviewer", "reviewer-a",
    ]) == 0
    assert json.loads(capfd.readouterr().out)["state"] == "phaser_reviewed"
    assert load_run(artifact_root, run_id).state == "phaser_reviewed"


def test_default_approve_and_publish_complete_offline_lifecycle(tmp_path, capfd, monkeypatch) -> None:
    artifact_root = tmp_path / "runs"
    static_root = tmp_path / "static"
    run_id = _reviewable_run(artifact_root)
    checklist = _approval_file(tmp_path / "approval.json", run_id)
    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(artifact_root))
    monkeypatch.setenv(cli.ENV_STATIC_ROOT, str(static_root))

    assert cli.main([
        "approve", "--run-id", run_id, "--reviewer", "reviewer-a",
        "--checklist", str(checklist),
    ]) == 0
    assert json.loads(capfd.readouterr().out)["state"] == "human_approved"
    assert cli.main(["publish", "--run-id", run_id]) == 0
    published = json.loads(capfd.readouterr().out)
    assert published["state"] == "published"
    target = Path(published["publish_path"])
    assert (target / "texture.png").read_bytes() == b"texture"
    assert (target / "portrait.png").read_bytes() == b"portrait"
    assert load_run(artifact_root, run_id).state == "published"


def test_default_reject_quarantines_with_sanitized_reason(tmp_path, capfd, monkeypatch) -> None:
    artifact_root = tmp_path / "runs"
    run_id = _reviewable_run(artifact_root)
    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(artifact_root))
    reason = "silhouette is unclear"

    assert cli.main([
        "reject", "--run-id", run_id, "--reviewer", "reviewer-a", "--reason", reason,
    ]) == 0
    assert json.loads(capfd.readouterr().out)["state"] == "quarantined"
    manifest = load_run(artifact_root, run_id)
    assert manifest.error is not None
    assert manifest.error.code == "HUMAN_REJECTED"
    assert reason not in manifest.error.message


def test_default_recover_releases_only_expired_claim(tmp_path, capfd, monkeypatch) -> None:
    artifact_root = tmp_path / "runs"
    run_id = new_run_id()
    create_run(artifact_root, run_id, ResidentSpriteRequest.model_validate(VALID_SPEC))
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    claim_stage(
        artifact_root, run_id, "anchor", "abandoned-worker", old,
        timedelta(hours=1), expected_artifact_path="anchor.png",
    )
    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(artifact_root))

    assert cli.main(["recover", "--run-id", run_id]) == 0
    result = json.loads(capfd.readouterr().out)
    assert result["action"] == "expired_claim_released"
    assert load_run(artifact_root, run_id).active_claim is None


def test_default_recover_preserves_unregistered_orphan_for_manual_reconciliation(
    tmp_path, capfd, monkeypatch
) -> None:
    artifact_root = tmp_path / "runs"
    run_id = new_run_id()
    create_run(artifact_root, run_id, ResidentSpriteRequest.model_validate(VALID_SPEC))
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    claim = claim_stage(
        artifact_root, run_id, "anchor", "abandoned-worker", old,
        timedelta(hours=1), expected_artifact_path="anchor.png",
    )
    orphan = artifact_root / run_id / claim.expected_artifact_path
    orphan.write_bytes(b"provider-output-without-manifest-registration")
    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(artifact_root))

    assert cli.main(["recover", "--run-id", run_id]) == 0
    result = json.loads(capfd.readouterr().out)
    assert result == {
        "action": "orphan_reconciliation_required",
        "expected_artifact_path": "anchor.png",
        "run_id": run_id,
        "stage": "anchor",
        "state": "requested",
    }
    recovered = load_run(artifact_root, run_id)
    assert recovered.active_claim == claim
    assert orphan.read_bytes() == b"provider-output-without-manifest-registration"


def test_default_recover_preserves_claim_when_external_request_result_is_uncertain(
    tmp_path, capfd, monkeypatch
) -> None:
    artifact_root = tmp_path / "runs"
    run_id = new_run_id()
    create_run(artifact_root, run_id, ResidentSpriteRequest.model_validate(VALID_SPEC))
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    claim = claim_stage(
        artifact_root, run_id, "anchor", "abandoned-worker", old,
        timedelta(hours=1), expected_artifact_path="anchor.png",
    )
    consume_request_budget(artifact_root, run_id, "anchor")
    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(artifact_root))

    assert cli.main(["recover", "--run-id", run_id]) == 0
    result = json.loads(capfd.readouterr().out)
    assert result == {
        "action": "external_request_status_uncertain",
        "request_count": 1,
        "run_id": run_id,
        "stage": "anchor",
        "state": "requested",
    }
    recovered = load_run(artifact_root, run_id)
    assert recovered.active_claim == claim
    assert not (artifact_root / run_id / "anchor.png").exists()


def test_uncertain_recovery_requires_all_exact_confirmations(
    tmp_path, capfd, monkeypatch
) -> None:
    artifact_root = tmp_path / "runs"
    run_id = new_run_id()
    create_run(artifact_root, run_id, ResidentSpriteRequest.model_validate(VALID_SPEC))
    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(artifact_root))

    assert cli.main(["recover", "--run-id", run_id, "--accept-uncertain-cost"]) == 2
    error = json.loads(capfd.readouterr().err)["error"]
    assert error["code"] == "UNCERTAIN_RECOVERY_CONFIRMATION_REQUIRED"


def test_backend_storage_env_names_take_priority_with_legacy_fallback(tmp_path, monkeypatch) -> None:
    artifact_dir = tmp_path / "artifact-dir"
    legacy_artifact_root = tmp_path / "legacy-artifact-root"
    static_dir = tmp_path / "static-dir"
    legacy_static_root = tmp_path / "legacy-static-root"
    for path in (artifact_dir, legacy_artifact_root, static_dir, legacy_static_root):
        path.mkdir()
    monkeypatch.setenv(cli.ENV_ARTIFACT_DIR, str(artifact_dir))
    monkeypatch.setenv(cli.ENV_ARTIFACT_ROOT, str(legacy_artifact_root))
    monkeypatch.setenv(cli.ENV_STATIC_DIR, str(static_dir))
    monkeypatch.setenv(cli.ENV_STATIC_ROOT, str(legacy_static_root))

    assert cli._artifact_root(create=False, exit_code=4) == artifact_dir
    assert cli._static_root(create=False, exit_code=6) == static_dir

    monkeypatch.delenv(cli.ENV_ARTIFACT_DIR)
    monkeypatch.delenv(cli.ENV_STATIC_DIR)
    assert cli._artifact_root(create=False, exit_code=4) == legacy_artifact_root
    assert cli._static_root(create=False, exit_code=6) == legacy_static_root


def test_static_dir_defaults_to_backend_static_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(cli.ENV_STATIC_DIR, raising=False)
    monkeypatch.delenv(cli.ENV_STATIC_ROOT, raising=False)
    monkeypatch.chdir(tmp_path)

    assert cli._static_root(create=True, exit_code=6) == tmp_path / "static"


def test_atomic_create_handles_partial_writes_and_fsyncs_parent(tmp_path, monkeypatch) -> None:
    target = tmp_path / "evidence.bin"
    payload = b"partial writes must not truncate evidence"
    real_write = cli.os.write
    real_fsync = cli.os.fsync
    writes = []
    fsyncs = []

    def partial_write(fd, data):
        writes.append(len(data))
        return real_write(fd, data[:3])

    def observed_fsync(fd):
        fsyncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(cli.os, "write", partial_write)
    monkeypatch.setattr(cli.os, "fsync", observed_fsync)
    cli._atomic_create(target, payload)

    assert target.read_bytes() == payload
    assert len(writes) > 1
    assert len(fsyncs) >= 2


def _approved_run(root: Path) -> str:
    run_id = _reviewable_run(root)
    advance_stage(root, run_id, stage="human_approval", state="human_approved")
    return run_id


def _publication_fixture(root: Path, static_root: Path, run_id: str) -> tuple[Path, dict, dict]:
    manifest = load_run(root, run_id)
    texture = (root / run_id / "candidate" / "texture.png").read_bytes()
    portrait = (root / run_id / "candidate" / "portrait.png").read_bytes()
    metadata = {
        "schema_version": 1,
        "run_id": run_id,
        "asset_key": manifest.request.asset_key,
        "texture_sha256": cli._sha256(texture),
        "portrait_sha256": cli._sha256(portrait),
    }
    files = cli._publication_files(metadata, texture, portrait)
    target = static_root / "resident-sprites" / manifest.request.asset_key / run_id
    return target, metadata, files


def test_publish_reuses_identical_existing_directory_without_replacing_it(
    tmp_path, capfd, monkeypatch
) -> None:
    artifact_root = tmp_path / "runs"
    static_root = tmp_path / "static"
    static_root.mkdir()
    run_id = _approved_run(artifact_root)
    target, _, files = _publication_fixture(artifact_root, static_root, run_id)
    target.mkdir(parents=True)
    for name, data in files.items():
        (target / name).write_bytes(data)
    original_inode = target.stat().st_ino
    monkeypatch.setenv(cli.ENV_ARTIFACT_DIR, str(artifact_root))
    monkeypatch.setenv(cli.ENV_STATIC_DIR, str(static_root))

    assert cli.main(["publish", "--run-id", run_id]) == 0
    result = json.loads(capfd.readouterr().out)
    assert result["state"] == "published"
    assert target.stat().st_ino == original_inode
    assert load_run(artifact_root, run_id).state == "published"


def test_publish_conflicting_existing_directory_is_untouched_and_fails_closed(
    tmp_path, capfd, monkeypatch
) -> None:
    artifact_root = tmp_path / "runs"
    static_root = tmp_path / "static"
    static_root.mkdir()
    run_id = _approved_run(artifact_root)
    target, _, _ = _publication_fixture(artifact_root, static_root, run_id)
    target.mkdir(parents=True)
    conflict = target / "unrelated.txt"
    conflict.write_bytes(b"must survive")
    monkeypatch.setenv(cli.ENV_ARTIFACT_DIR, str(artifact_root))
    monkeypatch.setenv(cli.ENV_STATIC_DIR, str(static_root))

    assert cli.main(["publish", "--run-id", run_id]) == cli.EXIT_PUBLISH
    error = json.loads(capfd.readouterr().err)
    assert error["error"]["code"] == "PUBLISH_TARGET_CONFLICT"
    assert conflict.read_bytes() == b"must survive"
    assert load_run(artifact_root, run_id).state == "human_approved"


def test_recover_refuses_symlink_target_without_deleting_or_rolling_back(
    tmp_path, capfd, monkeypatch
) -> None:
    artifact_root = tmp_path / "runs"
    static_root = tmp_path / "static"
    static_root.mkdir()
    run_id = _approved_run(artifact_root)
    target, metadata, _ = _publication_fixture(artifact_root, static_root, run_id)
    write_canonical_json_artifact(
        artifact_root, run_id, "publish/publication.json", metadata
    )
    advance_stage(artifact_root, run_id, stage="publish_start", state="publishing")
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "sentinel.txt"
    sentinel.write_bytes(b"keep")
    target.parent.mkdir(parents=True)
    target.symlink_to(victim, target_is_directory=True)
    monkeypatch.setenv(cli.ENV_ARTIFACT_DIR, str(artifact_root))
    monkeypatch.setenv(cli.ENV_STATIC_DIR, str(static_root))

    assert cli.main(["recover", "--run-id", run_id]) == cli.EXIT_PUBLISH
    error = json.loads(capfd.readouterr().err)
    assert error["error"]["code"] == "RECOVERY_TARGET_CONFLICT"
    assert sentinel.read_bytes() == b"keep"
    assert target.is_symlink()
    assert load_run(artifact_root, run_id).state == "publishing"
