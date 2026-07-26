from __future__ import annotations

import base64
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.services.resident_sprite_artifacts import (
    advance_stage,
    claim_stage,
    complete_stage,
    create_run,
    load_run,
    read_artifact,
    write_artifact,
    write_canonical_json_artifact,
)
from app.services.resident_sprite_batch import (
    _approved_run_evidence,
    prepare_batch,
    reserve_run,
    sync_batch,
)
from app.services.resident_sprite_generation import (
    ResidentSpriteRequest,
    generate_resident_sprite,
)
from app.services.resident_sprite_postprocess import derive_resident_portrait
from tests.test_resident_sprite_pipeline import FakeProvider, _capability


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "backend/scripts/review_resident_sprite_batch.py"
SPEC = importlib.util.spec_from_file_location("resident_sprite_batch_review", SCRIPT)
assert SPEC and SPEC.loader
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def _png(size: tuple[int, int], color=(45, 110, 180, 255)) -> bytes:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, size[0] - 5, size[1] - 5), fill=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _atlas() -> bytes:
    image = Image.new("RGBA", (96, 128), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for row in range(4):
        for column in range(3):
            x, y = column * 32 + 10, row * 32 + 5
            draw.rectangle((x, y, x + 11, y + 24), fill=(40 + row * 40, 90 + column * 30, 170, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _prepared_candidate(tmp_path: Path):
    agents = tmp_path / "village/agents"
    import shutil

    shutil.copytree(REPO_ROOT / "frontend/public/assets/village/agents", agents)
    batch_root = tmp_path / "batches"
    artifact_root = tmp_path / "artifacts"
    batch = prepare_batch(
        catalog_path=REPO_ROOT / "frontend/config/resident-sprite-generation.json",
        agents_root=agents,
        batch_root=batch_root,
        model="qualified-image-model",
        price_per_request_usd="0.10",
        max_cost_usd="27.50",
        cost_source="test-price-evidence",
    )
    _, item = reserve_run(batch_root, batch.batch_id, "george")
    request = ResidentSpriteRequest.model_validate_json(
        (batch_root / batch.batch_id / item.request_file).read_bytes()
    )
    create_run(artifact_root, item.run_id, request)
    now = datetime.now(timezone.utc)
    for stage, relative_path in (
        ("anchor", "anchor.png"),
        ("down", "strips/down.png"),
        ("left", "strips/left.png"),
        ("up", "strips/up.png"),
    ):
        claim = claim_stage(
            artifact_root,
            item.run_id,
            stage,
            "test-provider",
            now,
            timedelta(minutes=5),
            expected_artifact_path=relative_path,
        )
        write_artifact(artifact_root, item.run_id, relative_path, _png((32, 32)))
        complete_stage(
            artifact_root,
            item.run_id,
            stage=stage,
            owner=claim.owner,
            attempt_id=claim.attempt_id,
            now=now + timedelta(seconds=1),
            provider_request_ids=(f"provider-{stage}",),
        )
    advance_stage(artifact_root, item.run_id, stage="strips", state="strips_ready")
    texture = _atlas()
    write_artifact(artifact_root, item.run_id, "candidate/texture.png", texture)
    write_artifact(
        artifact_root,
        item.run_id,
        "candidate/portrait.png",
        derive_resident_portrait(texture),
    )
    advance_stage(artifact_root, item.run_id, stage="postprocess", state="processed")
    write_canonical_json_artifact(
        artifact_root,
        item.run_id,
        "candidate/qc.json",
        {"direction_policy": "mirror_right", "findings": [], "passed": True},
    )
    advance_stage(artifact_root, item.run_id, stage="auto_qc", state="auto_qc_passed")
    return batch, item, batch_root, artifact_root, texture


def _payload(texture: bytes) -> dict:
    screenshot = _png((640, 360), color=(32, 36, 43, 255))
    return {
        "checklist": {
            "identity_consistent": True,
            "directions_correct": True,
            "gait_readable": True,
            "scale_and_baseline_stable": True,
            "anatomy_and_clipping_clean": True,
            "background_clean": True,
            "asymmetry_acceptable": True,
            "originality_acceptable": True,
            "gameplay_fit": True,
        },
        "notes": "Phaser canvas reviewed.",
        "phaser_version": review.PHASER_VERSION,
        "frames": list(review.REQUIRED_FRAMES),
        "texture_sha256": review._sha256(texture),
        "screenshot_data_url": "data:image/png;base64," + base64.b64encode(screenshot).decode(),
    }


def test_approve_item_persists_phaser_screenshot_and_advances_state(tmp_path: Path) -> None:
    batch, item, batch_root, artifact_root, texture = _prepared_candidate(tmp_path)

    result = review.approve_item(
        batch_root=batch_root,
        batch_id=batch.batch_id,
        artifact_root=artifact_root,
        asset_key=item.asset_key,
        reviewer="reviewer-a",
        payload=_payload(texture),
    )

    assert result["state"] == "human_approved"
    manifest = load_run(artifact_root, item.run_id)
    assert manifest.state == "human_approved"
    evidence = json.loads(read_artifact(artifact_root, item.run_id, "review/phaser.json"))
    assert evidence["schema_version"] == 2
    assert evidence["render"]["review_surface"] == "phaser-canvas-v1"
    assert evidence["render"]["frames"] == list(review.REQUIRED_FRAMES)
    screenshot = read_artifact(artifact_root, item.run_id, "review/phaser-screenshot.png")
    assert evidence["render"]["screenshot_sha256"] == review._sha256(screenshot)


def test_wrong_frame_evidence_cannot_advance_candidate(tmp_path: Path) -> None:
    batch, item, batch_root, artifact_root, texture = _prepared_candidate(tmp_path)
    payload = _payload(texture)
    payload["frames"] = payload["frames"][:-1]

    with pytest.raises(review.ReviewError) as exc:
        review.approve_item(
            batch_root=batch_root,
            batch_id=batch.batch_id,
            artifact_root=artifact_root,
            asset_key=item.asset_key,
            reviewer="reviewer-a",
            payload=payload,
        )

    assert exc.value.code == "PHASER_FRAMES_INVALID"
    assert load_run(artifact_root, item.run_id).state == "auto_qc_passed"
    with pytest.raises(Exception):
        read_artifact(artifact_root, item.run_id, "review/phaser.json")


def test_screenshot_must_be_exact_640_by_360_png(tmp_path: Path) -> None:
    _batch, _item, _batch_root, _artifact_root, texture = _prepared_candidate(tmp_path)
    payload = _payload(texture)
    payload["screenshot_data_url"] = "data:image/png;base64," + base64.b64encode(
        _png((639, 360))
    ).decode()
    with pytest.raises(review.ReviewError) as exc:
        review._validate_render_payload(
            payload,
            run_id="a" * 32,
            texture_sha256=review._sha256(texture),
        )
    assert exc.value.code == "SCREENSHOT_INVALID"


def test_approval_recovers_after_evidence_write_before_state_advance(tmp_path: Path) -> None:
    batch, item, batch_root, artifact_root, texture = _prepared_candidate(tmp_path)
    first_payload = _payload(texture)
    first_screenshot, render = review._validate_render_payload(
        first_payload,
        run_id=item.run_id,
        texture_sha256=review._sha256(texture),
    )
    write_artifact(
        artifact_root, item.run_id, "review/phaser-screenshot.png", first_screenshot
    )
    write_canonical_json_artifact(
        artifact_root,
        item.run_id,
        "review/phaser.json",
        {
            "schema_version": 2,
            "run_id": item.run_id,
            "reviewer": "reviewer-a",
            "reviewed_at": datetime.now(timezone.utc),
            "render": render,
        },
    )
    retry_payload = _payload(texture)
    retry_payload["screenshot_data_url"] = "data:image/png;base64," + base64.b64encode(
        _png((640, 360), color=(80, 30, 120, 255))
    ).decode()

    result = review.approve_item(
        batch_root=batch_root,
        batch_id=batch.batch_id,
        artifact_root=artifact_root,
        asset_key=item.asset_key,
        reviewer="reviewer-a",
        payload=retry_payload,
    )

    assert result["state"] == "human_approved"
    assert (
        read_artifact(artifact_root, item.run_id, "review/phaser-screenshot.png")
        == first_screenshot
    )


def test_review_http_surface_requires_token_and_same_origin(tmp_path: Path) -> None:
    batch, item, batch_root, artifact_root, _texture = _prepared_candidate(tmp_path)
    context = review.ReviewContext(
        batch_root=batch_root,
        batch_id=batch.batch_id,
        artifact_root=artifact_root,
        reviewer="reviewer-a",
        token="test-review-token",
        atlas_path=REPO_ROOT / "frontend/public/assets/village/agents/sprite.json",
        phaser_path=REPO_ROOT / "frontend/node_modules/phaser/dist/phaser.min.js",
        origin="",
    )
    server = review.ThreadingHTTPServer(("127.0.0.1", 0), review.handler_for(context))
    context.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with pytest.raises(urllib.error.HTTPError) as missing_token:
            opener.open(f"{context.origin}/api/batch", timeout=2)
        assert missing_token.value.code == 403

        with opener.open(
            f"{context.origin}/?token={context.token}", timeout=2
        ) as response:
            page = response.read().decode()
        assert response.status == 200
        assert "Phaser" in page
        assert "居民形象批次审核" in page

        request = urllib.request.Request(
            f"{context.origin}/api/items/{item.asset_key}/reject?token={context.token}",
            data=json.dumps({"reason": "not ready"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": "http://attacker.invalid"},
        )
        with pytest.raises(urllib.error.HTTPError) as wrong_origin:
            opener.open(request, timeout=2)
        assert wrong_origin.value.code == 403
        assert load_run(artifact_root, item.run_id).state == "auto_qc_passed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.anyio
async def test_pipeline_review_evidence_is_accepted_by_static_installer(tmp_path: Path) -> None:
    import shutil

    agents = tmp_path / "village/agents"
    shutil.copytree(REPO_ROOT / "frontend/public/assets/village/agents", agents)
    batch_root = tmp_path / "batches"
    artifact_root = tmp_path / "artifacts"
    batch = prepare_batch(
        catalog_path=REPO_ROOT / "frontend/config/resident-sprite-generation.json",
        agents_root=agents,
        batch_root=batch_root,
        model="gpt-image-2",
        price_per_request_usd="0.10",
        max_cost_usd="27.50",
        cost_source="test-price-evidence",
    )
    _, reserved = reserve_run(batch_root, batch.batch_id, "george")
    request = ResidentSpriteRequest.model_validate_json(
        (batch_root / batch.batch_id / reserved.request_file).read_bytes()
    )
    result = await generate_resident_sprite(
        request,
        client=FakeProvider(artifact_root),
        artifact_root=artifact_root,
        run_id=reserved.run_id,
        capability=_capability(tmp_path),
    )
    assert result.state == "auto_qc_passed"
    texture = read_artifact(artifact_root, reserved.run_id, "candidate/texture.png")
    review.approve_item(
        batch_root=batch_root,
        batch_id=batch.batch_id,
        artifact_root=artifact_root,
        asset_key=reserved.asset_key,
        reviewer="reviewer-a",
        payload=_payload(texture),
    )
    synced = sync_batch(batch_root, batch.batch_id, artifact_root)
    item = next(candidate for candidate in synced.items if candidate.asset_key == "george")

    accepted_texture, portrait, evidence = _approved_run_evidence(
        artifact_root,
        item,
        request,
        frozenset({"reviewer-a"}),
    )

    assert accepted_texture == texture
    assert portrait == derive_resident_portrait(texture)
    assert evidence["review_surface"] == "phaser-canvas-v1"
    assert evidence["phaser_frames"] == list(review.REQUIRED_FRAMES)
