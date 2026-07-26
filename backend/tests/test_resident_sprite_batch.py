from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from app.services.resident_sprite_batch import (
    ResidentSpriteBatchError,
    consolidate_batch,
    install_batch,
    load_batch,
    prepare_batch,
    recover_install,
    reserve_run,
    tree_sha256,
)
from app.services.resident_sprite_artifacts import create_run, load_run
from app.services.resident_sprite_generation import (
    ResidentSpriteRequest,
    canonical_json_bytes,
    new_run_id,
)
from app.services.resident_sprite_postprocess import derive_resident_portrait


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "frontend/config/resident-sprite-generation.json"
DENYLIST = REPO_ROOT / "frontend/config/resident-sprite-legacy-denylist.json"
SOURCE_AGENTS = REPO_ROOT / "frontend/public/assets/village/agents"
CLI_PATH = REPO_ROOT / "backend/scripts/manage_resident_sprite_batch.py"
CLI_SPEC = importlib.util.spec_from_file_location("resident_sprite_batch_cli", CLI_PATH)
assert CLI_SPEC and CLI_SPEC.loader
batch_cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(batch_cli)


def _copy_agents(tmp_path: Path) -> Path:
    target = tmp_path / "village/agents"
    target.parent.mkdir(parents=True)
    shutil.copytree(SOURCE_AGENTS, target)
    return target


def _prepare(tmp_path: Path, *, max_cost: str = "27.50"):
    agents = _copy_agents(tmp_path)
    batch_root = tmp_path / "batches"
    batch = prepare_batch(
        catalog_path=CATALOG,
        agents_root=agents,
        batch_root=batch_root,
        model="qualified-image-model",
        price_per_request_usd="0.10",
        max_cost_usd=max_cost,
        cost_source="provider-price-page-2026-07-26",
    )
    return batch, batch_root, agents


def _set_run_state(
    artifact_root: Path,
    run_id: str,
    *,
    state: str,
    submitted_request_count: int,
) -> None:
    manifest_path = artifact_root / run_id / "manifest.json"
    payload = json.loads(manifest_path.read_bytes())
    payload["state"] = state
    payload["request_budget"]["submitted_image_request_count"] = submitted_request_count
    manifest_path.write_bytes(canonical_json_bytes(payload))


def _consolidation_fixture(tmp_path: Path, *, target_count: int = 2, source_count: int = 4):
    target, batch_root, agents = _prepare(tmp_path)
    source = prepare_batch(
        catalog_path=CATALOG,
        agents_root=agents,
        batch_root=batch_root,
        model=target.model,
        price_per_request_usd=target.price_snapshot.price_per_request_usd,
        max_cost_usd=target.price_snapshot.max_cost_usd,
        cost_source=target.price_snapshot.cost_source,
    )
    target_root = tmp_path / "target-runs"
    source_root = tmp_path / "source-runs"
    request = ResidentSpriteRequest.model_validate_json(
        (batch_root / target.batch_id / target.items[0].request_file).read_bytes()
    )
    target_run_id = new_run_id()
    source_run_id = new_run_id()
    create_run(target_root, target_run_id, request)
    create_run(source_root, source_run_id, request)
    _set_run_state(
        target_root,
        target_run_id,
        state="failed",
        submitted_request_count=target_count,
    )
    _set_run_state(
        source_root,
        source_run_id,
        state="auto_qc_passed",
        submitted_request_count=source_count,
    )
    target_items = [
        target.items[0].model_copy(
            update={
                "run_id": target_run_id,
                "run_state": "failed",
                "submitted_request_count": target_count,
            }
        ),
        *[
            item.model_copy(update={"run_state": "auto_qc_passed"})
            for item in target.items[1:]
        ],
    ]
    source_items = [
        source.items[0].model_copy(
            update={
                "run_id": source_run_id,
                "run_state": "auto_qc_passed",
                "submitted_request_count": source_count,
            }
        ),
        *source.items[1:],
    ]
    (batch_root / target.batch_id / "batch.json").write_bytes(
        canonical_json_bytes(target.model_copy(update={"items": target_items}))
    )
    (batch_root / source.batch_id / "batch.json").write_bytes(
        canonical_json_bytes(source.model_copy(update={"items": source_items}))
    )
    return target, source, batch_root, target_root, source_root, target_run_id, source_run_id


def _atlas(index: int) -> bytes:
    image = Image.new("RGBA", (96, 128), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = ((index * 37) % 255, (index * 73) % 255, (index * 109) % 255, 255)
    for row in range(4):
        for column in range(3):
            x = column * 32 + 9
            y = row * 32 + 4
            draw.rectangle((x, y, x + 13, y + 25), fill=color)
    from io import BytesIO

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _approved(batch):
    return batch.model_copy(
        update={
            "items": [
                item.model_copy(
                    update={
                        "run_id": new_run_id(),
                        "run_state": "human_approved",
                        "submitted_request_count": 4,
                    }
                )
                for item in batch.items
            ]
        }
    )


def _mock_evidence(batch):
    index = {item.asset_key: offset + 1 for offset, item in enumerate(batch.items)}

    def evidence(_artifact_root, item, request, _reviewers):
        texture = _atlas(index[item.asset_key])
        portrait = derive_resident_portrait(texture)
        return texture, portrait, {
            "run_id": item.run_id,
            "request": request.model_dump(mode="json"),
            "request_sha256": item.request_sha256,
            "provider_request_ids": [f"provider-{item.asset_key}-{part}" for part in range(4)],
            "submitted_request_count": 4,
            "capability_receipt_id": "a" * 64,
            "normalized_origin": "https://provider.example",
            "model_alias": request.model,
            "prompt_version": request.prompt_version,
            "algorithm_version": request.algorithm_version,
            "phaser_reviewer": "reviewer-a",
            "approved_by": "reviewer-a",
            "review_surface": "phaser-canvas-v1",
            "phaser_version": "3.90.0",
            "phaser_frames": [
                f"{direction}-walk.{frame:03d}"
                for direction in ("down", "left", "right", "up")
                for frame in range(3)
            ],
            "phaser_screenshot_sha256": "e" * 64,
            "phaser_evidence_sha256": "b" * 64,
            "approval_evidence_sha256": "c" * 64,
            "capability_evidence_sha256": "d" * 64,
        }

    return evidence


def test_prepare_freezes_exact_catalog_and_worst_case_budget(tmp_path: Path) -> None:
    batch, batch_root, _agents = _prepare(tmp_path)

    assert len(batch.items) == 25
    assert batch.max_requests_per_item == 11
    assert batch.max_requests_total == 275
    assert len({item.asset_key for item in batch.items}) == 25
    assert len({item.sprite_key for item in batch.items}) == 25
    for item in batch.items:
        spec = json.loads((batch_root / batch.batch_id / item.request_file).read_text())
        assert spec["model"] == "qualified-image-model"
        assert spec["direction_policy"] == "mirror_right"
        assert "visual_reference" not in spec


def test_prepare_rejects_cost_cap_below_request_ceiling(tmp_path: Path) -> None:
    agents = _copy_agents(tmp_path)
    with pytest.raises(ResidentSpriteBatchError, match="worst-case") as exc:
        prepare_batch(
            catalog_path=CATALOG,
            agents_root=agents,
            batch_root=tmp_path / "batches",
            model="qualified-image-model",
            price_per_request_usd="0.10",
            max_cost_usd="27.49",
            cost_source="test",
        )
    assert exc.value.code == "COST_CAP_TOO_LOW"


def test_reserve_run_is_durable_and_idempotent(tmp_path: Path) -> None:
    batch, batch_root, _agents = _prepare(tmp_path)
    _, first = reserve_run(batch_root, batch.batch_id, "george")
    _, second = reserve_run(batch_root, batch.batch_id, "george")

    assert first.run_id == second.run_id
    assert load_batch(batch_root, batch.batch_id).items[0].run_id == first.run_id


def test_paid_confirmation_must_match_all_three_frozen_values(tmp_path: Path) -> None:
    batch, _batch_root, _agents = _prepare(tmp_path)
    args = Namespace(
        confirm_batch_id=batch.batch_id,
        confirm_max_requests=275,
        confirm_max_cost_usd="27.50",
    )
    batch_cli._confirm_paid(args, batch)
    args.confirm_max_requests = 274
    with pytest.raises(ResidentSpriteBatchError) as exc:
        batch_cli._confirm_paid(args, batch)
    assert exc.value.code == "PAID_CONFIRMATION_MISMATCH"


def test_generate_idempotently_skips_approved_item_at_request_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, batch_root, _agents = _prepare(tmp_path)
    item = batch.items[0].model_copy(
        update={
            "run_id": new_run_id(),
            "run_state": "human_approved",
            "submitted_request_count": 11,
        }
    )
    batch = batch.model_copy(update={"items": [item, *batch.items[1:]]})
    monkeypatch.setattr(batch_cli, "sync_batch", lambda *_args: batch)
    monkeypatch.setattr(batch_cli, "reserve_run", lambda *_args: (batch, item))
    monkeypatch.setattr(
        batch_cli,
        "load_run",
        lambda *_args: SimpleNamespace(run_id=item.run_id, state="human_approved"),
    )
    invoked = []
    monkeypatch.setattr(batch_cli, "_invoke_generator", lambda *args: invoked.append(args))
    args = Namespace(
        batch_root=batch_root,
        batch_id=batch.batch_id,
        artifact_root=tmp_path / "artifacts",
        asset_key=item.asset_key,
        confirm_batch_id=batch.batch_id,
        confirm_max_requests=275,
        confirm_max_cost_usd="27.50",
    )

    result = batch_cli._generate(args)

    assert invoked == []
    assert result["results"] == [{
        "asset_key": item.asset_key,
        "run_id": item.run_id,
        "state": "human_approved",
        "action": "no_generation",
    }]


def test_item_request_ceiling_is_direction_specific() -> None:
    assert batch_cli._item_request_ceiling(SimpleNamespace(direction_policy="mirror_right")) == 11
    assert batch_cli._item_request_ceiling(SimpleNamespace(direction_policy="generate_right")) == 14


def test_consolidate_carries_forward_verified_run_and_preserves_failed_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        target,
        source,
        batch_root,
        target_root,
        source_root,
        target_run_id,
        source_run_id,
    ) = _consolidation_fixture(tmp_path)
    monkeypatch.setattr(
        "app.services.resident_sprite_batch.sync_batch",
        lambda root, batch_id, _artifacts: load_batch(root, batch_id),
    )
    monkeypatch.setattr(
        "app.services.resident_sprite_batch._capability_receipt_id",
        lambda *_args: "a" * 64,
    )

    consolidated = consolidate_batch(
        batch_root=batch_root,
        batch_id=target.batch_id,
        artifact_root=target_root,
        source_batch_id=source.batch_id,
        source_artifact_root=source_root,
    )

    item = consolidated.items[0]
    assert item.run_id == source_run_id
    assert item.run_state == "auto_qc_passed"
    assert item.source_batch_id == source.batch_id
    assert item.superseded_run_ids == [target_run_id]
    assert item.superseded_request_count == 2
    assert item.submitted_request_count == 6
    assert load_run(target_root, source_run_id).state == "auto_qc_passed"
    evidence = json.loads(
        (batch_root / target.batch_id / "consolidations" / f"{item.asset_key}.json").read_bytes()
    )
    assert evidence["selected_run_id"] == source_run_id
    assert evidence["superseded_run_ids"] == [target_run_id]


def test_consolidate_rejects_capability_receipt_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, batch_root, target_root, source_root, *_ = _consolidation_fixture(tmp_path)
    monkeypatch.setattr(
        "app.services.resident_sprite_batch.sync_batch",
        lambda root, batch_id, _artifacts: load_batch(root, batch_id),
    )
    monkeypatch.setattr(
        "app.services.resident_sprite_batch._capability_receipt_id",
        lambda root, _run_id: "a" * 64 if root == target_root else "b" * 64,
    )

    with pytest.raises(ResidentSpriteBatchError) as exc:
        consolidate_batch(
            batch_root=batch_root,
            batch_id=target.batch_id,
            artifact_root=target_root,
            source_batch_id=source.batch_id,
            source_artifact_root=source_root,
        )

    assert exc.value.code == "CONSOLIDATION_CAPABILITY_MISMATCH"


def test_consolidate_rejects_combined_item_cost_above_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, batch_root, target_root, source_root, *_ = _consolidation_fixture(
        tmp_path, target_count=8, source_count=4
    )
    monkeypatch.setattr(
        "app.services.resident_sprite_batch.sync_batch",
        lambda root, batch_id, _artifacts: load_batch(root, batch_id),
    )
    monkeypatch.setattr(
        "app.services.resident_sprite_batch._capability_receipt_id",
        lambda *_args: "a" * 64,
    )

    with pytest.raises(ResidentSpriteBatchError) as exc:
        consolidate_batch(
            batch_root=batch_root,
            batch_id=target.batch_id,
            artifact_root=target_root,
            source_batch_id=source.batch_id,
            source_artifact_root=source_root,
        )

    assert exc.value.code == "CONSOLIDATION_ITEM_BUDGET_EXCEEDED"


def test_install_replaces_all_50_files_and_emits_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, batch_root, agents = _prepare(tmp_path)
    approved = _approved(batch)
    old_sha = tree_sha256(agents)
    monkeypatch.setattr(
        "app.services.resident_sprite_batch.sync_batch", lambda *_args, **_kwargs: approved
    )
    monkeypatch.setattr(
        "app.services.resident_sprite_batch._approved_run_evidence", _mock_evidence(approved)
    )

    result = install_batch(
        batch_root=batch_root,
        batch_id=batch.batch_id,
        artifact_root=tmp_path / "artifacts",
        agents_root=agents,
        approved_reviewers=frozenset({"reviewer-a"}),
        denylist_path=DENYLIST,
    )

    assert result["state"] == "installed"
    assert tree_sha256(agents) != old_sha
    assert (agents / "generation-batch.json").is_file()
    for item in approved.items:
        directory = agents / item.sprite_key
        receipt = json.loads((directory / "generation-provenance.json").read_text())
        assert receipt["batch_id"] == batch.batch_id
        assert len(receipt["files"]) == 2
        with Image.open(directory / "texture.png") as texture:
            assert texture.size == (96, 128)
        with Image.open(directory / "portrait.png") as portrait:
            assert portrait.size == (256, 256)
    gate = subprocess.run(
        ["node", str(REPO_ROOT / "frontend/scripts/verify-asset-provenance.mjs"), "--release"],
        cwd=REPO_ROOT / "frontend",
        env={**os.environ, "SIMVERSE_ASSET_VILLAGE_DIR": str(agents.parent)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert gate.returncode == 0, gate.stdout + gate.stderr
    next_batch = prepare_batch(
        catalog_path=CATALOG,
        agents_root=agents,
        batch_root=tmp_path / "next-batches",
        model="qualified-image-model",
        price_per_request_usd="0.10",
        max_cost_usd="27.50",
        cost_source="provider-price-page-2026-07-26",
    )
    assert next_batch.baseline_tree_sha256 == tree_sha256(agents)


def test_interrupted_directory_swap_can_roll_back_without_mixed_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, batch_root, agents = _prepare(tmp_path)
    approved = _approved(batch)
    old_sha = tree_sha256(agents)
    monkeypatch.setattr(
        "app.services.resident_sprite_batch.sync_batch", lambda *_args, **_kwargs: approved
    )
    monkeypatch.setattr(
        "app.services.resident_sprite_batch._approved_run_evidence", _mock_evidence(approved)
    )
    real_replace = os.replace
    staging = agents.parent / f".agents-stage-{batch.batch_id}"

    def fail_new_tree(source, target):
        if Path(source) == staging and Path(target) == agents:
            raise OSError("injected crash after old tree move")
        return real_replace(source, target)

    monkeypatch.setattr("app.services.resident_sprite_batch.os.replace", fail_new_tree)
    with pytest.raises(OSError, match="injected crash"):
        install_batch(
            batch_root=batch_root,
            batch_id=batch.batch_id,
            artifact_root=tmp_path / "artifacts",
            agents_root=agents,
            approved_reviewers=frozenset({"reviewer-a"}),
            denylist_path=DENYLIST,
        )
    assert not agents.exists()
    monkeypatch.setattr("app.services.resident_sprite_batch.os.replace", real_replace)

    result = recover_install(agents, action="rollback")
    assert result == {"batch_id": batch.batch_id, "state": "rolled_back"}
    assert tree_sha256(agents) == old_sha
    assert not (agents.parent / ".resident-sprite-install.json").exists()


def test_install_rejects_duplicate_slot_art_before_switching_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, batch_root, agents = _prepare(tmp_path)
    approved = _approved(batch)
    old_sha = tree_sha256(agents)
    texture = _atlas(1)
    portrait = derive_resident_portrait(texture)

    def duplicate_evidence(_artifact_root, item, request, _reviewers):
        evidence = _mock_evidence(approved)(_artifact_root, item, request, _reviewers)[2]
        return texture, portrait, evidence

    monkeypatch.setattr(
        "app.services.resident_sprite_batch.sync_batch", lambda *_args, **_kwargs: approved
    )
    monkeypatch.setattr(
        "app.services.resident_sprite_batch._approved_run_evidence", duplicate_evidence
    )
    with pytest.raises(ResidentSpriteBatchError) as exc:
        install_batch(
            batch_root=batch_root,
            batch_id=batch.batch_id,
            artifact_root=tmp_path / "artifacts",
            agents_root=agents,
            approved_reviewers=frozenset({"reviewer-a"}),
            denylist_path=DENYLIST,
        )
    assert exc.value.code == "GENERATED_ASSET_DUPLICATE"
    assert tree_sha256(agents) == old_sha
    assert not (agents.parent / ".resident-sprite-install.json").exists()


def test_recovery_rejects_noncanonical_journal_paths(tmp_path: Path) -> None:
    _batch, _batch_root, agents = _prepare(tmp_path)
    journal = agents.parent / ".resident-sprite-install.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": new_run_id(),
                "phase": "staged",
                "agents_root": "agents",
                "staging": "/private/tmp/not-owned-by-this-recovery",
                "backup": ".agents-backup-wrong",
                "old_tree_sha256": "a" * 64,
                "new_tree_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ResidentSpriteBatchError) as exc:
        recover_install(agents, action="rollback")
    assert exc.value.code == "INSTALL_JOURNAL_INVALID"
    assert agents.is_dir()
