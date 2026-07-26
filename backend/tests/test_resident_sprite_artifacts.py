from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.resident_sprite_artifacts import (
    MANIFEST_NAME,
    acknowledge_uncertain_request_cost,
    advance_stage,
    claim_stage,
    complete_stage,
    consume_request_budget,
    create_run,
    fail_stage_claim,
    load_run,
    read_artifact,
    release_expired_claim_if_safe,
    release_stage_claim,
    retry_run,
    review_quarantined_run,
    write_artifact,
    write_canonical_json_artifact,
)


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
from app.services.resident_sprite_generation import (
    ResidentSpriteContractError,
    ResidentSpriteRequest,
    SanitizedError,
    canonical_json_bytes,
    new_run_id,
)


def _request(**overrides: object) -> ResidentSpriteRequest:
    values = {
        "asset_key": "pilot-01",
        "display_name": "Pilot One",
        "appearance": "Short silver hair and a green coat.",
        "gender": "neutral",
        "age_group": "adult",
        "vibe": "calm",
        "tags": ["maker"],
        "model": "gpt-image-2",
    }
    values.update(overrides)
    return ResidentSpriteRequest.model_validate(values)


def _new_run(tmp_path: Path) -> tuple[str, ResidentSpriteRequest]:
    run_id = new_run_id()
    request = _request()
    create_run(tmp_path, run_id, request)
    return run_id, request


def _complete_provider_stage(
    tmp_path: Path,
    run_id: str,
    stage: str,
    *,
    owner: str = "worker-a",
    now: datetime = NOW,
) -> None:
    claim = claim_stage(
        tmp_path,
        run_id,
        stage,
        owner,
        now,
        timedelta(minutes=5),
    )
    write_artifact(
        tmp_path,
        run_id,
        claim.expected_artifact_path,
        f"{stage}-bytes".encode(),
    )
    complete_stage(
        tmp_path,
        run_id,
        stage=stage,
        owner=owner,
        attempt_id=claim.attempt_id,
        now=now + timedelta(seconds=1),
        provider_request_ids=(f"request-{stage}",),
    )


def _complete_required_provider_stages(tmp_path: Path, run_id: str) -> None:
    _complete_provider_stage(tmp_path, run_id, "anchor")
    for direction in ("down", "left", "up"):
        _complete_provider_stage(tmp_path, run_id, direction)


def test_create_write_advance_and_resume_verified_run(tmp_path: Path) -> None:
    run_id, request = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path,
        run_id,
        "anchor",
        "worker-a",
        NOW,
        timedelta(minutes=5),
    )
    artifact = write_artifact(tmp_path, run_id, claim.expected_artifact_path, b"anchor")
    checkpoint = complete_stage(
        tmp_path,
        run_id,
        stage="anchor",
        owner="worker-a",
        attempt_id=claim.attempt_id,
        now=NOW + timedelta(seconds=1),
        provider_request_ids=("provider-1",),
    )

    resumed = load_run(tmp_path, run_id)
    assert resumed == checkpoint
    assert resumed.request == request
    assert resumed.request_budget.direction_policy == "mirror_right"
    assert resumed.request_budget.submitted_image_request_count == 0
    assert resumed.completed_stages == ["anchor"]
    assert resumed.active_claim is None
    assert resumed.completed_claims == [claim]
    assert resumed.provider_request_ids == ["provider-1"]
    assert resumed.artifacts == [artifact]
    assert resumed.artifacts[0].relative_path == "anchor/identity.png"
    assert (tmp_path / run_id / MANIFEST_NAME).read_bytes() == canonical_json_bytes(resumed)


def test_canonical_json_artifact_uses_stable_encoding(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    write_canonical_json_artifact(tmp_path, run_id, "evidence/data.json", {"z": 1, "a": "雪"})
    assert (tmp_path / run_id / "evidence/data.json").read_bytes() == (
        b'{"a":"\xe9\x9b\xaa","z":1}'
    )


def test_create_and_artifact_writes_are_idempotent_but_conflicts_fail(tmp_path: Path) -> None:
    run_id, request = _new_run(tmp_path)
    assert create_run(tmp_path, run_id, request).request == request
    first = write_artifact(tmp_path, run_id, "strips/down.png", b"same")
    assert write_artifact(tmp_path, run_id, "strips/down.png", b"same") == first

    with pytest.raises(ResidentSpriteContractError) as exc:
        write_artifact(tmp_path, run_id, "strips/down.png", b"different")
    assert exc.value.code == "ARTIFACT_CONFLICT"
    assert (tmp_path / run_id / "strips/down.png").read_bytes() == b"same"

    with pytest.raises(ResidentSpriteContractError) as exc:
        create_run(tmp_path, run_id, _request(appearance="Different resident"))
    assert exc.value.code == "RUN_REQUEST_CONFLICT"


def test_request_budget_is_initialized_from_request_and_persisted_before_post(
    tmp_path: Path,
) -> None:
    run_id = new_run_id()
    request = _request(direction_policy="generate_right")
    created = create_run(tmp_path, run_id, request)
    assert created.request_budget.direction_policy == "generate_right"
    assert created.request_budget.global_ceiling == 14

    claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    )
    consumed = consume_request_budget(tmp_path, run_id, "anchor")
    assert consumed.submitted_image_request_count == 1
    assert consumed.stage_counts == {"anchor": 1}
    resumed = load_run(tmp_path, run_id)
    assert resumed.request_budget == consumed
    assert create_run(tmp_path, run_id, request).request_budget == consumed


def test_load_rejects_request_budget_policy_mismatch(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    manifest_path = tmp_path / run_id / MANIFEST_NAME
    payload = json.loads(manifest_path.read_bytes())
    payload["request_budget"]["direction_policy"] = "generate_right"
    manifest_path.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(ResidentSpriteContractError) as exc:
        load_run(tmp_path, run_id)
    assert exc.value.code == "MANIFEST_INVALID"


def test_request_budget_exhaustion_leaves_manifest_unchanged(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    )
    consume_request_budget(tmp_path, run_id, "anchor")
    consume_request_budget(tmp_path, run_id, "anchor")
    manifest_path = tmp_path / run_id / MANIFEST_NAME
    before = manifest_path.read_bytes()

    with pytest.raises(ResidentSpriteContractError) as exc:
        consume_request_budget(tmp_path, run_id, "anchor")
    assert exc.value.code == "REQUEST_BUDGET_EXHAUSTED"
    assert manifest_path.read_bytes() == before
    assert load_run(tmp_path, run_id).request_budget.stage_counts == {"anchor": 2}


def test_concurrent_budget_consumers_are_serialized(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    )

    def consume() -> str:
        try:
            consume_request_budget(tmp_path, run_id, "anchor")
        except ResidentSpriteContractError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: consume(), range(12)))

    assert results.count("ok") == 2
    assert results.count("REQUEST_BUDGET_EXHAUSTED") == 10
    budget = load_run(tmp_path, run_id).request_budget
    assert budget.submitted_image_request_count == 2
    assert budget.stage_counts == {"anchor": 2}


def test_read_artifact_only_allows_declared_verified_bytes(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    write_artifact(tmp_path, run_id, "frames/down.png", b"declared")
    undeclared = tmp_path / run_id / "frames/other.png"
    undeclared.write_bytes(b"not in manifest")

    assert read_artifact(tmp_path, run_id, "frames/down.png") == b"declared"
    with pytest.raises(ResidentSpriteContractError) as exc:
        read_artifact(tmp_path, run_id, "frames/other.png")
    assert exc.value.code == "ARTIFACT_NOT_DECLARED"


def test_read_artifact_fails_closed_after_declared_file_is_corrupted(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    write_artifact(tmp_path, run_id, "frames/down.png", b"declared")
    (tmp_path / run_id / "frames/down.png").write_bytes(b"corrupt!")

    with pytest.raises(ResidentSpriteContractError) as exc:
        read_artifact(tmp_path, run_id, "frames/down.png")
    assert exc.value.code == "ARTIFACT_CORRUPT"


@pytest.mark.parametrize(
    "relative_path",
    ["../escape.png", "/absolute.png", "frames/../escape.png", "frames//one.png", "frames\\one.png"],
)
def test_artifact_path_traversal_is_rejected(tmp_path: Path, relative_path: str) -> None:
    run_id, _ = _new_run(tmp_path)
    with pytest.raises((ResidentSpriteContractError, ValueError)):
        write_artifact(tmp_path, run_id, relative_path, b"bad")
    assert not (tmp_path / "escape.png").exists()


def test_symlinked_run_and_artifact_parent_are_rejected(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / run_id / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ResidentSpriteContractError):
        write_artifact(tmp_path, run_id, "linked/escape.png", b"bad")
    assert not (outside / "escape.png").exists()

    other_run_id = new_run_id()
    (tmp_path / other_run_id).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ResidentSpriteContractError) as exc:
        load_run(tmp_path, other_run_id)
    assert exc.value.code == "PATH_SYMLINK"


def test_broken_artifact_symlink_is_rejected_without_replacement(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    link = tmp_path / run_id / "broken.png"
    link.symlink_to(tmp_path / "missing.png")
    with pytest.raises(ResidentSpriteContractError) as exc:
        write_artifact(tmp_path, run_id, "broken.png", b"bad")
    assert exc.value.code == "ARTIFACT_PATH_INVALID"
    assert link.is_symlink()


@pytest.mark.parametrize("replacement", [b"originaL", b"longer-corruption"])
def test_load_fails_closed_on_artifact_hash_or_size_corruption(
    tmp_path: Path, replacement: bytes
) -> None:
    run_id, _ = _new_run(tmp_path)
    write_artifact(tmp_path, run_id, "candidate/texture.png", b"original")
    (tmp_path / run_id / "candidate/texture.png").write_bytes(replacement)

    with pytest.raises(ResidentSpriteContractError) as exc:
        load_run(tmp_path, run_id)
    assert exc.value.code == "ARTIFACT_CORRUPT"


def test_load_rejects_manifest_traversal_and_noncanonical_bytes(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    manifest_path = tmp_path / run_id / MANIFEST_NAME
    payload = json.loads(manifest_path.read_bytes())
    payload["artifacts"] = [
        {"relative_path": "../outside.png", "sha256": "0" * 64, "size": 0}
    ]
    manifest_path.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ResidentSpriteContractError):
        load_run(tmp_path, run_id)

    payload["artifacts"] = []
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(ResidentSpriteContractError) as exc:
        load_run(tmp_path, run_id)
    assert exc.value.code == "MANIFEST_INVALID"


def test_atomic_failure_cleans_temporary_file_and_preserves_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = _new_run(tmp_path)
    run_dir = tmp_path / run_id
    original_manifest = (run_dir / MANIFEST_NAME).read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected rename failure")

    monkeypatch.setattr("app.services.resident_sprite_artifacts.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_artifact(tmp_path, run_id, "anchor.png", b"data")

    assert not (run_dir / "anchor.png").exists()
    assert (run_dir / MANIFEST_NAME).read_bytes() == original_manifest
    assert not list(run_dir.rglob("*.tmp"))


def test_provider_claim_is_idempotent_and_only_one_worker_wins(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    first = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    )
    with pytest.raises(ResidentSpriteContractError) as exc:
        claim_stage(
            tmp_path,
            run_id,
            "anchor",
            "worker-a",
            NOW + timedelta(seconds=1),
            timedelta(minutes=5),
        )
    assert exc.value.code == "STAGE_CLAIM_HELD"
    assert claim_stage(
        tmp_path,
        run_id,
        "anchor",
        "worker-a",
        NOW + timedelta(seconds=1),
        timedelta(minutes=5),
        attempt_id=first.attempt_id,
    ) == first

    with pytest.raises(ResidentSpriteContractError) as exc:
        claim_stage(
            tmp_path,
            run_id,
            "anchor",
            "worker-b",
            NOW + timedelta(seconds=1),
            timedelta(minutes=5),
        )
    assert exc.value.code == "STAGE_CLAIM_HELD"
    assert load_run(tmp_path, run_id).active_claim == first


def test_concurrent_workers_only_one_claims_stage(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)

    def acquire(worker_number: int) -> str:
        try:
            claim_stage(
                tmp_path,
                run_id,
                "anchor",
                f"worker-{worker_number}",
                NOW,
                timedelta(minutes=5),
            )
        except ResidentSpriteContractError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(acquire, range(8)))
    assert results.count("ok") == 1
    assert results.count("STAGE_CLAIM_HELD") == 7


def test_expired_claim_can_be_taken_over_with_new_attempt(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    old = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(seconds=30)
    )
    replacement = claim_stage(
        tmp_path,
        run_id,
        "anchor",
        "worker-b",
        NOW + timedelta(seconds=30),
        timedelta(minutes=5),
    )
    assert replacement.owner == "worker-b"
    assert replacement.attempt_id != old.attempt_id
    assert load_run(tmp_path, run_id).active_claim == replacement


def test_expired_claim_cannot_complete_but_registered_artifact_can_be_taken_over(
    tmp_path: Path,
) -> None:
    run_id, _ = _new_run(tmp_path)
    old = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(seconds=30)
    )
    write_artifact(tmp_path, run_id, old.expected_artifact_path, b"anchor")
    with pytest.raises(ResidentSpriteContractError) as exc:
        complete_stage(
            tmp_path,
            run_id,
            stage="anchor",
            owner="worker-a",
            attempt_id=old.attempt_id,
            now=NOW + timedelta(seconds=30),
        )
    assert exc.value.code == "STAGE_CLAIM_EXPIRED"

    replacement = claim_stage(
        tmp_path,
        run_id,
        "anchor",
        "worker-b",
        NOW + timedelta(seconds=30),
        timedelta(minutes=5),
    )
    completed = complete_stage(
        tmp_path,
        run_id,
        stage="anchor",
        owner="worker-b",
        attempt_id=replacement.attempt_id,
        now=NOW + timedelta(seconds=31),
    )
    assert completed.state == "anchor_ready"


def test_expired_claim_with_unregistered_output_requires_reconciliation(
    tmp_path: Path,
) -> None:
    run_id, _ = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(seconds=30)
    )
    target = tmp_path / run_id / claim.expected_artifact_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"provider-output-before-crash")

    for _ in range(2):
        with pytest.raises(ResidentSpriteContractError) as exc:
            claim_stage(
                tmp_path,
                run_id,
                "anchor",
                "worker-b",
                NOW + timedelta(seconds=30),
                timedelta(minutes=5),
            )
        assert exc.value.code == "ORPHAN_RECONCILIATION_REQUIRED"
    assert load_run(tmp_path, run_id).active_claim == claim


def test_expired_claim_recovery_keeps_unregistered_output_and_claim(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(seconds=30)
    )
    target = tmp_path / run_id / claim.expected_artifact_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"provider-output-before-manifest")

    outcome = release_expired_claim_if_safe(
        tmp_path,
        run_id,
        stage="anchor",
        owner=claim.owner,
        attempt_id=claim.attempt_id,
        now=NOW + timedelta(seconds=30),
    )
    assert outcome.action == "orphan_reconciliation_required"
    assert outcome.manifest.active_claim == claim
    assert target.read_bytes() == b"provider-output-before-manifest"


def test_expired_claim_recovery_keeps_uncertain_external_request_claim(
    tmp_path: Path,
) -> None:
    run_id, _ = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(seconds=30)
    )
    consume_request_budget(tmp_path, run_id, "anchor")

    outcome = release_expired_claim_if_safe(
        tmp_path,
        run_id,
        stage="anchor",
        owner=claim.owner,
        attempt_id=claim.attempt_id,
        now=NOW + timedelta(seconds=30),
    )
    assert outcome.action == "external_request_status_uncertain"
    assert outcome.stage_request_count == 1
    assert outcome.manifest.active_claim == claim

    with pytest.raises(ResidentSpriteContractError) as exc:
        claim_stage(
            tmp_path,
            run_id,
            "anchor",
            "worker-b",
            NOW + timedelta(seconds=30),
            timedelta(minutes=5),
        )
    assert exc.value.code == "EXTERNAL_REQUEST_STATUS_UNCERTAIN"


def test_uncertain_cost_acknowledgement_is_exact_audited_and_retriable(
    tmp_path: Path,
) -> None:
    run_id, _ = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(seconds=30)
    )
    consume_request_budget(tmp_path, run_id, "anchor")

    with pytest.raises(ResidentSpriteContractError) as exc:
        acknowledge_uncertain_request_cost(
            tmp_path,
            run_id,
            stage="anchor",
            owner=claim.owner,
            attempt_id=claim.attempt_id,
            expected_stage_request_count=2,
            reviewer="project-owner",
            now=NOW + timedelta(seconds=30),
        )
    assert exc.value.code == "RECOVERY_REQUEST_COUNT_MISMATCH"

    acknowledged = acknowledge_uncertain_request_cost(
        tmp_path,
        run_id,
        stage="anchor",
        owner=claim.owner,
        attempt_id=claim.attempt_id,
        expected_stage_request_count=1,
        reviewer="project-owner",
        now=NOW + timedelta(seconds=30),
    )
    assert acknowledged.active_claim is None
    relative_path = f"recovery/uncertain-{claim.attempt_id}.json"
    evidence = json.loads(read_artifact(tmp_path, run_id, relative_path))
    assert evidence == {
        "schema_version": 1,
        "action": "uncertain_cost_accepted_for_retry",
        "run_id": run_id,
        "stage": "anchor",
        "attempt_id": claim.attempt_id,
        "stage_request_count": 1,
        "reviewer": "project-owner",
        "acknowledged_at": "2026-07-26T12:00:30Z",
    }

    replacement = claim_stage(
        tmp_path,
        run_id,
        "anchor",
        "worker-b",
        NOW + timedelta(seconds=31),
        timedelta(minutes=5),
    )
    assert replacement.attempt_id != claim.attempt_id


def test_expired_claim_recovery_releases_registered_output_or_unspent_claim(
    tmp_path: Path,
) -> None:
    unspent_run, _ = _new_run(tmp_path)
    unspent = claim_stage(
        tmp_path, unspent_run, "anchor", "worker-a", NOW, timedelta(seconds=30)
    )
    unspent_outcome = release_expired_claim_if_safe(
        tmp_path,
        unspent_run,
        stage="anchor",
        owner=unspent.owner,
        attempt_id=unspent.attempt_id,
        now=NOW + timedelta(seconds=30),
    )
    assert unspent_outcome.action == "expired_claim_released"
    assert unspent_outcome.manifest.active_claim is None

    registered_run, _ = _new_run(tmp_path)
    registered = claim_stage(
        tmp_path, registered_run, "anchor", "worker-b", NOW, timedelta(seconds=30)
    )
    consume_request_budget(tmp_path, registered_run, "anchor")
    write_artifact(
        tmp_path, registered_run, registered.expected_artifact_path, b"anchor"
    )
    registered_outcome = release_expired_claim_if_safe(
        tmp_path,
        registered_run,
        stage="anchor",
        owner=registered.owner,
        attempt_id=registered.attempt_id,
        now=NOW + timedelta(seconds=30),
    )
    assert registered_outcome.action == "expired_claim_released"
    assert registered_outcome.stage_request_count == 1
    assert registered_outcome.manifest.active_claim is None


def test_expired_claim_recovery_is_atomic_with_budget_consumption(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(seconds=30)
    )

    def recover() -> str:
        return release_expired_claim_if_safe(
            tmp_path,
            run_id,
            stage="anchor",
            owner=claim.owner,
            attempt_id=claim.attempt_id,
            now=NOW + timedelta(seconds=30),
        ).action

    def consume() -> str:
        try:
            consume_request_budget(tmp_path, run_id, "anchor")
        except ResidentSpriteContractError as exc:
            return exc.code
        return "consumed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        recover_future = executor.submit(recover)
        consume_future = executor.submit(consume)
        recovery_action = recover_future.result()
        consume_action = consume_future.result()

    manifest = load_run(tmp_path, run_id)
    assert (recovery_action, consume_action) in {
        ("expired_claim_released", "STAGE_CLAIM_REQUIRED"),
        ("external_request_status_uncertain", "consumed"),
    }
    assert not (
        manifest.request_budget.stage_counts.get("anchor", 0) > 0
        and manifest.active_claim is None
    )


def test_expired_claim_recovery_rejects_active_claim(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    )
    with pytest.raises(ResidentSpriteContractError) as exc:
        release_expired_claim_if_safe(
            tmp_path,
            run_id,
            stage="anchor",
            owner=claim.owner,
            attempt_id=claim.attempt_id,
            now=NOW + timedelta(seconds=30),
        )
    assert exc.value.code == "STAGE_CLAIM_ACTIVE"
    assert load_run(tmp_path, run_id).active_claim == claim


def test_complete_and_release_require_exact_claim_ownership(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    )
    write_artifact(tmp_path, run_id, claim.expected_artifact_path, b"anchor")

    with pytest.raises(ResidentSpriteContractError) as exc:
        complete_stage(
            tmp_path,
            run_id,
            stage="anchor",
            owner="worker-b",
            attempt_id=claim.attempt_id,
            now=NOW + timedelta(seconds=1),
        )
    assert exc.value.code == "STAGE_CLAIM_MISMATCH"
    with pytest.raises(ResidentSpriteContractError) as exc:
        release_stage_claim(
            tmp_path,
            run_id,
            stage="anchor",
            owner="worker-a",
            attempt_id=new_run_id(),
        )
    assert exc.value.code == "STAGE_CLAIM_MISMATCH"

    completed = complete_stage(
        tmp_path,
        run_id,
        stage="anchor",
        owner="worker-a",
        attempt_id=claim.attempt_id,
        now=NOW + timedelta(seconds=1),
    )
    assert completed.state == "anchor_ready"
    assert complete_stage(
        tmp_path,
        run_id,
        stage="anchor",
        owner="worker-a",
        attempt_id=claim.attempt_id,
        now=NOW + timedelta(seconds=2),
    ) == completed


def test_release_matching_claim_allows_new_attempt(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    old = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    )
    released = release_stage_claim(
        tmp_path,
        run_id,
        stage="anchor",
        owner="worker-a",
        attempt_id=old.attempt_id,
    )
    assert released.active_claim is None
    new = claim_stage(
        tmp_path,
        run_id,
        "anchor",
        "worker-b",
        NOW + timedelta(seconds=1),
        timedelta(minutes=5),
    )
    assert new.attempt_id != old.attempt_id


def test_provider_failure_atomically_records_error_clears_claim_and_retries(
    tmp_path: Path,
) -> None:
    run_id, _ = _new_run(tmp_path)
    claim = claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    )
    error = SanitizedError(
        code="PROVIDER_TIMEOUT",
        message="provider timed out",
        provider_request_id="request-timeout",
        http_status=504,
    )
    failed = fail_stage_claim(
        tmp_path,
        run_id,
        stage="anchor",
        owner="worker-a",
        attempt_id=claim.attempt_id,
        error=error,
    )
    assert failed.state == "failed"
    assert failed.active_claim is None
    assert failed.error == error
    assert failed.provider_request_ids == ["request-timeout"]

    retrying = retry_run(tmp_path, run_id)
    assert retrying.state == "retrying"
    assert retrying.error is None
    replacement = claim_stage(
        tmp_path,
        run_id,
        "anchor",
        "worker-b",
        NOW + timedelta(seconds=1),
        timedelta(minutes=5),
    )
    assert replacement.attempt_id != claim.attempt_id


def test_illegal_jump_and_incomplete_strips_are_rejected(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    with pytest.raises(ResidentSpriteContractError) as exc:
        advance_stage(tmp_path, run_id, stage="postprocess", state="processed")
    assert exc.value.code == "STATE_TRANSITION_INVALID"

    _complete_provider_stage(tmp_path, run_id, "anchor")
    _complete_provider_stage(tmp_path, run_id, "down")
    with pytest.raises(ResidentSpriteContractError) as exc:
        advance_stage(tmp_path, run_id, stage="strips", state="strips_ready")
    assert exc.value.code == "STAGE_PRECONDITION_FAILED"


def test_full_state_graph_and_terminal_state_are_immutable(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    _complete_required_provider_stages(tmp_path, run_id)
    transitions = [
        ("strips", "strips_ready"),
        ("postprocess", "processed"),
        ("auto_qc", "auto_qc_passed"),
        ("candidate", "candidate_ready"),
        ("phaser_review", "phaser_reviewed"),
        ("human_approval", "human_approved"),
        ("publish_start", "publishing"),
        ("publish", "published"),
    ]
    for stage, state in transitions:
        manifest = advance_stage(tmp_path, run_id, stage=stage, state=state)
        assert manifest.state == state

    assert advance_stage(
        tmp_path, run_id, stage="publish", state="published"
    ).state == "published"
    with pytest.raises(ResidentSpriteContractError) as exc:
        advance_stage(tmp_path, run_id, stage="rollback", state="rolled_back")
    assert exc.value.code in {"RUN_TERMINAL", "STATE_TRANSITION_INVALID"}
    with pytest.raises(ResidentSpriteContractError) as exc:
        write_artifact(tmp_path, run_id, "late.txt", b"late")
    assert exc.value.code == "RUN_TERMINAL"


def test_failed_and_quarantined_states_require_explicit_recovery(tmp_path: Path) -> None:
    run_id, _ = _new_run(tmp_path)
    error = SanitizedError(code="PROVIDER_TIMEOUT", message="provider timed out")
    failed = advance_stage(
        tmp_path,
        run_id,
        stage="failure",
        state="failed",
        error=error,
    )
    assert failed.state == "failed"

    with pytest.raises(ResidentSpriteContractError) as exc:
        claim_stage(
            tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
        )
    assert exc.value.code == "EXPLICIT_RETRY_REQUIRED"
    assert retry_run(tmp_path, run_id).state == "retrying"
    assert claim_stage(
        tmp_path, run_id, "anchor", "worker-a", NOW, timedelta(minutes=5)
    ).stage == "anchor"

    quarantined_run, _ = _new_run(tmp_path)
    quarantined = advance_stage(
        tmp_path,
        quarantined_run,
        stage="quarantine",
        state="quarantined",
    )
    assert quarantined.state == "quarantined"
    with pytest.raises(ResidentSpriteContractError) as exc:
        advance_stage(
            tmp_path,
            quarantined_run,
            stage="postprocess",
            state="processed",
        )
    assert exc.value.code == "EXPLICIT_REVIEW_REQUIRED"
    assert review_quarantined_run(tmp_path, quarantined_run).state == "processed"
