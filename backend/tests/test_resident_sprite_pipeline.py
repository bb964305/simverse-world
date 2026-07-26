from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import app.services.resident_sprite_postprocess as postprocess
from app.services.resident_sprite_artifacts import load_run
from app.services.resident_sprite_generation import (
    CapabilityContract,
    CapabilityReceipt,
    CapabilityRevocation,
    ProviderImageResult,
    QualifiedSpriteCapability,
    ResidentSpriteContractError,
    ResidentSpriteRequest,
    SanitizedError,
    content_id,
    create_revocation_tombstone,
    generate_resident_sprite,
    new_run_id,
)
from app.services.resident_sprite_postprocess import ResidentSpritePostprocessError
from app.services.resident_sprite_provider import ProviderError


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _anchor_png() -> bytes:
    image = Image.new("RGB", (1024, 1024), (255, 0, 255))
    ImageDraw.Draw(image).rectangle((400, 160, 624, 920), fill=(20, 80, 120))
    return _png(image)


def _strip_png(direction: str, *, uneven: bool = False) -> bytes:
    colors = {
        "down": (220, 30, 30),
        "left": (30, 180, 60),
        "up": (30, 80, 220),
        "right": (190, 40, 210),
    }
    image = Image.new("RGB", (1536, 512), (255, 0, 255))
    draw = ImageDraw.Draw(image)
    for column in range(3):
        width = 40 if uneven and column == 0 else 160
        left = column * 512 + (512 - width) // 2
        right = left + width - 1
        draw.rectangle((left, 96, right, 463), fill=colors[direction])
        draw.point((left, 130), fill=(255, 255, 255))
    return _png(image)


def _request(**overrides: object) -> ResidentSpriteRequest:
    payload = {
        "asset_key": "pipeline-test",
        "display_name": "Pipeline Test",
        "appearance": "Silver hair, green coat, and a small satchel.",
        "gender": "neutral",
        "age_group": "adult",
        "vibe": "calm",
        "tags": ["maker"],
        "model": "gpt-image-2",
    }
    payload.update(overrides)
    return ResidentSpriteRequest.model_validate(payload)


def _capability(
    tmp_path: Path,
    *,
    multipart_field: str = "image[]",
    clock=lambda: NOW,
) -> QualifiedSpriteCapability:
    contract = CapabilityContract(
        normalized_origin="https://provider.example:443/v1",
        model_alias="gpt-image-2",
        multipart_field=multipart_field,
    )
    payload = {
        **contract.model_dump(mode="python"),
        "schema_version": 1,
        "wire_receipt_id": "a" * 64,
        "probe_id": "probe-1",
        "qualification_id": new_run_id(),
        "operator": "operator-a",
        "reviewer": "reviewer-b",
        "qualified_at": NOW,
        "expires_at": NOW + timedelta(days=30),
        "evidence_sha256": ["b" * 64],
        "provider_request_ids": ["qualification-request"],
        "blind_scores": [],
        "latency_ms": [1, 1, 1, 1, 1],
        "capability_request_count": 6,
        "capability_cost_upper_bound_usd": "0.70",
        "cost_source": "provider-unavailable",
    }
    draft = CapabilityReceipt.model_construct(**payload, receipt_id="")
    receipt = CapabilityReceipt(
        **payload,
        receipt_id=content_id(draft, "receipt_id"),
    )
    return QualifiedSpriteCapability(
        receipt=receipt,
        contract=contract,
        revocation_path=tmp_path / f"{receipt.receipt_id}.revoked.json",
        clock=clock,
    )


def _revoked_capability(tmp_path: Path) -> QualifiedSpriteCapability:
    capability = _capability(tmp_path)
    payload = {
        "schema_version": 1,
        "receipt_id": capability.receipt.receipt_id,
        "reason_code": "EDIT_UNSUPPORTED",
        "observed_at": NOW,
        "provider_request_id": "revocation-request",
        "actor": "operator-a",
    }
    draft = CapabilityRevocation.model_construct(**payload, revocation_id="")
    revocation = CapabilityRevocation(
        **payload,
        revocation_id=content_id(draft, "revocation_id"),
    )
    path = create_revocation_tombstone(tmp_path / "revocations", revocation)
    return capability.model_copy(update={"revocation_path": path})


class FakeProvider:
    def __init__(
        self,
        artifact_root: Path,
        *,
        fail_stage: str | None = None,
        uneven_stage: str | None = None,
    ) -> None:
        self.artifact_root = artifact_root
        self.model_alias = "gpt-image-2"
        self.contract_origin = "https://provider.example:443/v1"
        self.fail_stage = fail_stage
        self.uneven_stage = uneven_stage
        self.calls: list[str] = []
        self.multipart_fields: list[str] = []
        self.persisted_counts: list[int] = []
        self.gate_calls = 0

    def _before_post(self, run_id, stage, budget, gate) -> None:
        gate()
        self.gate_calls += 1
        budget.consume_before_post(stage)
        self.persisted_counts.append(
            load_run(self.artifact_root, run_id).request_budget.submitted_image_request_count
        )
        self.calls.append(stage)
        if self.fail_stage == stage:
            raise ProviderError(
                SanitizedError(
                    code="PROVIDER_TEST_FAILURE",
                    message="injected provider failure",
                    provider_request_id=f"request-{stage}",
                    http_status=503,
                )
            )

    async def generate_anchor(self, prompt, *, run_id, budget, logical_job, gate):
        assert "immutable identity reference" in prompt
        assert logical_job == "anchor"
        self._before_post(run_id, "anchor", budget, gate)
        return ProviderImageResult(
            image_bytes=_anchor_png(),
            provider_request_id="request-anchor",
            latency_ms=1,
            submitted_request_count=budget.submitted_image_request_count,
        )

    async def edit_strip(
        self,
        anchor_png,
        prompt,
        *,
        multipart_field,
        run_id,
        stage,
        logical_job,
        budget,
        gate,
    ):
        assert anchor_png == _anchor_png()
        assert f"walking {stage}" in prompt
        assert multipart_field in {"image[]", "image"}
        self.multipart_fields.append(multipart_field)
        assert logical_job == f"{stage}-strip"
        self._before_post(run_id, stage, budget, gate)
        return ProviderImageResult(
            image_bytes=_strip_png(stage, uneven=self.uneven_stage == stage),
            provider_request_id=f"request-{stage}",
            latency_ms=1,
            submitted_request_count=budget.submitted_image_request_count,
        )


class BlockingAnchorProvider(FakeProvider):
    def __init__(self, artifact_root: Path) -> None:
        super().__init__(artifact_root)
        self.anchor_started = asyncio.Event()
        self.release_anchor = asyncio.Event()

    async def generate_anchor(self, *args, **kwargs):
        result = await super().generate_anchor(*args, **kwargs)
        self.anchor_started.set()
        await self.release_anchor.wait()
        return result


class PreRequestBlockingAnchorProvider(FakeProvider):
    def __init__(self, artifact_root: Path) -> None:
        super().__init__(artifact_root)
        self.anchor_started = asyncio.Event()
        self.release_anchor = asyncio.Event()

    async def generate_anchor(self, *args, **kwargs):
        self.anchor_started.set()
        await self.release_anchor.wait()
        return await super().generate_anchor(*args, **kwargs)


class CrashingAnchorProvider(FakeProvider):
    def __init__(self, artifact_root: Path, *, consume_budget: bool) -> None:
        super().__init__(artifact_root)
        self.consume_budget = consume_budget

    async def generate_anchor(self, prompt, *, run_id, budget, logical_job, gate):
        del prompt, logical_job
        if self.consume_budget:
            self._before_post(run_id, "anchor", budget, gate)
        raise RuntimeError("injected adapter crash")


@pytest.mark.anyio
async def test_mirror_right_pipeline_orders_calls_and_persists_budget(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    result = await generate_resident_sprite(
        _request(),
        client=provider,
        artifact_root=tmp_path,
        capability=_capability(tmp_path),
    )

    assert result.state == "auto_qc_passed"
    assert result.qc_findings == []
    assert provider.calls == ["anchor", "down", "left", "up"]
    assert provider.persisted_counts == [1, 2, 3, 4]
    assert provider.gate_calls == 4
    manifest = load_run(tmp_path, result.run_id)
    assert manifest.request_budget.submitted_image_request_count == 4
    paths = {artifact.relative_path for artifact in manifest.artifacts}
    assert paths == {
        "evidence/capability.json",
        "anchor.png",
        "strips/down.png",
        "strips/left.png",
        "strips/up.png",
        "candidate/texture.png",
        "candidate/portrait.png",
        "candidate/qc.json",
    }
    assert result.manifest_path == str(tmp_path / result.run_id / "manifest.json")
    assert len(result.staged_artifact_paths) == len(paths)


@pytest.mark.anyio
async def test_generate_right_requests_and_stages_independent_strip(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    result = await generate_resident_sprite(
        _request(direction_policy="generate_right"),
        client=provider,
        artifact_root=tmp_path,
        capability=_capability(tmp_path),
    )

    assert result.state == "auto_qc_passed"
    assert provider.calls == ["anchor", "down", "left", "up", "right"]
    assert provider.persisted_counts == [1, 2, 3, 4, 5]
    assert (tmp_path / result.run_id / "strips/right.png").is_file()


@pytest.mark.anyio
async def test_completed_run_resumes_without_provider_calls(tmp_path: Path) -> None:
    run_id = new_run_id()
    request = _request()
    capability = _capability(tmp_path)
    first_provider = FakeProvider(tmp_path)
    first = await generate_resident_sprite(
        request,
        client=first_provider,
        artifact_root=tmp_path,
        run_id=run_id,
        capability=capability,
    )
    resumed_provider = FakeProvider(tmp_path, fail_stage="anchor")
    resumed = await generate_resident_sprite(
        request,
        client=resumed_provider,
        artifact_root=tmp_path,
        run_id=run_id,
        capability=capability,
    )

    assert resumed == first
    assert resumed_provider.calls == []
    assert load_run(tmp_path, run_id).request_budget.submitted_image_request_count == 4


@pytest.mark.anyio
async def test_concurrent_run_cannot_share_an_active_stage_claim(tmp_path: Path) -> None:
    run_id = new_run_id()
    request = _request()
    capability = _capability(tmp_path)
    first_provider = BlockingAnchorProvider(tmp_path)
    first_task = asyncio.create_task(
        generate_resident_sprite(
            request,
            client=first_provider,
            artifact_root=tmp_path,
            run_id=run_id,
            capability=capability,
        )
    )
    await first_provider.anchor_started.wait()

    second_provider = FakeProvider(tmp_path)
    try:
        with pytest.raises(ResidentSpriteContractError) as error:
            await generate_resident_sprite(
                request,
                client=second_provider,
                artifact_root=tmp_path,
                run_id=run_id,
                capability=capability,
            )
        assert error.value.code == "STAGE_CLAIM_HELD"
        assert second_provider.calls == []
        assert first_provider.calls == ["anchor"]
    finally:
        first_provider.release_anchor.set()
    first_result = await first_task
    assert first_result.state == "auto_qc_passed"


@pytest.mark.anyio
async def test_cancel_before_request_budget_releases_claim_for_immediate_resume(
    tmp_path: Path,
) -> None:
    run_id = new_run_id()
    request = _request()
    capability = _capability(tmp_path)
    blocked = PreRequestBlockingAnchorProvider(tmp_path)
    task = asyncio.create_task(
        generate_resident_sprite(
            request, client=blocked, artifact_root=tmp_path,
            run_id=run_id, capability=capability,
        )
    )
    await blocked.anchor_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert load_run(tmp_path, run_id).active_claim is None

    resumed = await generate_resident_sprite(
        request, client=FakeProvider(tmp_path), artifact_root=tmp_path,
        run_id=run_id, capability=capability,
    )
    assert resumed.state == "auto_qc_passed"


@pytest.mark.anyio
async def test_cancel_after_request_budget_retains_claim_and_blocks_duplicate_post(
    tmp_path: Path,
) -> None:
    run_id = new_run_id()
    request = _request()
    capability = _capability(tmp_path)
    blocked = BlockingAnchorProvider(tmp_path)
    task = asyncio.create_task(
        generate_resident_sprite(
            request, client=blocked, artifact_root=tmp_path,
            run_id=run_id, capability=capability,
        )
    )
    await blocked.anchor_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    interrupted = load_run(tmp_path, run_id)
    assert interrupted.request_budget.submitted_image_request_count == 1
    assert interrupted.active_claim is not None

    resumed_provider = FakeProvider(tmp_path)
    with pytest.raises(ResidentSpriteContractError) as error:
        await generate_resident_sprite(
            request, client=resumed_provider, artifact_root=tmp_path,
            run_id=run_id, capability=capability,
        )
    assert error.value.code == "STAGE_CLAIM_HELD"
    assert resumed_provider.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("consume_budget", [False, True])
async def test_unexpected_provider_crash_releases_claim_only_before_budget_consumption(
    tmp_path: Path,
    consume_budget: bool,
) -> None:
    run_id = new_run_id()
    with pytest.raises(RuntimeError, match="injected adapter crash"):
        await generate_resident_sprite(
            _request(),
            client=CrashingAnchorProvider(tmp_path, consume_budget=consume_budget),
            artifact_root=tmp_path,
            run_id=run_id,
            capability=_capability(tmp_path),
        )

    manifest = load_run(tmp_path, run_id)
    assert manifest.request_budget.submitted_image_request_count == int(consume_budget)
    assert (manifest.active_claim is not None) is consume_budget


@pytest.mark.anyio
async def test_qc_findings_quarantine_candidate(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path, uneven_stage="up")
    result = await generate_resident_sprite(
        _request(),
        client=provider,
        artifact_root=tmp_path,
        capability=_capability(tmp_path),
    )

    assert result.state == "quarantined"
    assert "FRAME_SIZE_DRIFT" in {finding.code for finding in result.qc_findings}
    assert (tmp_path / result.run_id / "candidate/qc.json").is_file()


@pytest.mark.anyio
async def test_provider_failure_is_sanitized_and_checkpointed(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path, fail_stage="left")
    result = await generate_resident_sprite(
        _request(),
        client=provider,
        artifact_root=tmp_path,
        capability=_capability(tmp_path),
    )

    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "PROVIDER_TEST_FAILURE"
    assert provider.calls == ["anchor", "down", "left"]
    manifest = load_run(tmp_path, result.run_id)
    assert manifest.error == result.error
    assert manifest.request_budget.submitted_image_request_count == 3
    assert manifest.active_claim is None


@pytest.mark.anyio
async def test_failed_provider_run_requires_explicit_retry_to_resume(tmp_path: Path) -> None:
    run_id = new_run_id()
    request = _request()
    capability = _capability(tmp_path)
    failed_provider = FakeProvider(tmp_path, fail_stage="left")
    failed = await generate_resident_sprite(
        request,
        client=failed_provider,
        artifact_root=tmp_path,
        run_id=run_id,
        capability=capability,
    )

    retry_provider = FakeProvider(tmp_path)
    unchanged = await generate_resident_sprite(
        request,
        client=retry_provider,
        artifact_root=tmp_path,
        run_id=run_id,
        capability=capability,
    )
    assert unchanged == failed
    assert retry_provider.calls == []

    resumed = await generate_resident_sprite(
        request,
        client=retry_provider,
        artifact_root=tmp_path,
        run_id=run_id,
        capability=capability,
        retry_failed=True,
    )
    assert resumed.state == "auto_qc_passed"
    assert resumed.error is None
    assert retry_provider.calls == ["left", "up"]
    assert load_run(tmp_path, run_id).request_budget.submitted_image_request_count == 5


@pytest.mark.anyio
async def test_postprocess_error_is_evidenced_and_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_postprocess(*args, **kwargs):
        del args, kwargs
        raise ResidentSpritePostprocessError(
            "FRAME_EMPTY", "generated strip contains an empty frame"
        )

    monkeypatch.setattr(postprocess, "build_resident_sprite_atlas", fail_postprocess)
    provider = FakeProvider(tmp_path)
    capability = _capability(tmp_path)
    result = await generate_resident_sprite(
        _request(),
        client=provider,
        artifact_root=tmp_path,
        capability=capability,
    )

    assert result.state == "quarantined"
    assert result.error == SanitizedError(
        code="FRAME_EMPTY", message="generated strip contains an empty frame"
    )
    assert [finding.code for finding in result.qc_findings] == ["FRAME_EMPTY"]
    run_dir = tmp_path / result.run_id
    assert (run_dir / "candidate/qc.json").is_file()
    assert not (run_dir / "candidate/texture.png").exists()
    assert not (run_dir / "candidate/portrait.png").exists()

    resumed_provider = FakeProvider(tmp_path, fail_stage="anchor")
    resumed = await generate_resident_sprite(
        _request(),
        client=resumed_provider,
        artifact_root=tmp_path,
        run_id=result.run_id,
        capability=capability,
    )
    assert resumed == result
    assert resumed_provider.calls == []


@pytest.mark.anyio
async def test_missing_capability_fails_before_provider_or_artifact_work(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    with pytest.raises(ResidentSpriteContractError) as error:
        await generate_resident_sprite(
            _request(), client=provider, artifact_root=tmp_path
        )
    assert error.value.code == "CAPABILITY_REQUIRED"
    assert provider.calls == []
    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_singular_multipart_field_is_forwarded(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    result = await generate_resident_sprite(
        _request(),
        client=provider,
        artifact_root=tmp_path,
        capability=_capability(tmp_path, multipart_field="image"),
    )

    assert result.state == "auto_qc_passed"
    assert provider.multipart_fields == ["image", "image", "image"]


@pytest.mark.anyio
async def test_expired_capability_fails_before_artifacts_or_calls(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    capability = _capability(
        tmp_path, clock=lambda: NOW + timedelta(days=31)
    )
    with pytest.raises(ResidentSpriteContractError) as error:
        await generate_resident_sprite(
            _request(),
            client=provider,
            artifact_root=tmp_path,
            capability=capability,
        )
    assert error.value.code == "CAPABILITY_EXPIRED"
    assert provider.calls == []
    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_revoked_capability_fails_before_artifacts_or_calls(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    provider = FakeProvider(artifact_root)
    capability = _revoked_capability(tmp_path)
    with pytest.raises(ResidentSpriteContractError) as error:
        await generate_resident_sprite(
            _request(),
            client=provider,
            artifact_root=artifact_root,
            capability=capability,
        )
    assert error.value.code == "CAPABILITY_REVOKED"
    assert provider.calls == []
    assert not artifact_root.exists()


@pytest.mark.anyio
async def test_contract_mismatch_fails_before_artifacts_or_calls(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    capability = _capability(tmp_path)
    incompatible = capability.model_copy(
        update={
            "contract": capability.contract.model_copy(
                update={"normalized_origin": "https://other.example:443/v1"}
            )
        }
    )
    with pytest.raises(ResidentSpriteContractError) as error:
        await generate_resident_sprite(
            _request(),
            client=provider,
            artifact_root=tmp_path,
            capability=incompatible,
        )
    assert error.value.code == "CAPABILITY_INCOMPATIBLE"
    assert provider.calls == []
    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_model_mismatch_fails_before_artifacts_or_calls(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    provider.model_alias = "other-model"
    with pytest.raises(ResidentSpriteContractError) as error:
        await generate_resident_sprite(
            _request(),
            client=provider,
            artifact_root=tmp_path,
            capability=_capability(tmp_path),
        )
    assert error.value.code == "MODEL_MISMATCH"
    assert provider.calls == []
    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_provider_origin_mismatch_fails_before_artifacts_or_calls(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    provider.contract_origin = "https://other.example:443/v1"
    with pytest.raises(ResidentSpriteContractError) as error:
        await generate_resident_sprite(
            _request(),
            client=provider,
            artifact_root=tmp_path,
            capability=_capability(tmp_path),
        )
    assert error.value.code == "PROVIDER_ORIGIN_MISMATCH"
    assert provider.calls == []
    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_corrupted_staged_artifact_fails_closed_on_resume(tmp_path: Path) -> None:
    run_id = new_run_id()
    request = _request()
    capability = _capability(tmp_path)
    first_provider = FakeProvider(tmp_path)
    await generate_resident_sprite(
        request,
        client=first_provider,
        artifact_root=tmp_path,
        run_id=run_id,
        capability=capability,
    )
    (tmp_path / run_id / "anchor.png").write_bytes(b"corrupt")

    resumed_provider = FakeProvider(tmp_path)
    with pytest.raises(ResidentSpriteContractError) as error:
        await generate_resident_sprite(
            request,
            client=resumed_provider,
            artifact_root=tmp_path,
            run_id=run_id,
            capability=capability,
        )
    assert error.value.code == "ARTIFACT_CORRUPT"
    assert resumed_provider.calls == []
