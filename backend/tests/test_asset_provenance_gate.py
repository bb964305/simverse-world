from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_VILLAGE = REPO_ROOT / "frontend/public/assets/village"
VERIFIER = REPO_ROOT / "frontend/scripts/verify-asset-provenance.mjs"


def _copy_village(tmp_path: Path) -> Path:
    target = tmp_path / "village"
    shutil.copytree(SOURCE_VILLAGE, target)
    return target


def _run(
    village: Path, *, release: bool = False, manifest: Path | None = None
) -> subprocess.CompletedProcess[str]:
    arguments = ["node", str(VERIFIER)]
    if release:
        arguments.append("--release")
    return subprocess.run(
        arguments,
        cwd=REPO_ROOT / "frontend",
        env={
            **os.environ,
            "SIMVERSE_ASSET_VILLAGE_DIR": str(village),
            **({"SIMVERSE_ASSET_MANIFEST": str(manifest)} if manifest else {}),
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_generated_resident_baseline_passes_release_gate(tmp_path: Path) -> None:
    village = _copy_village(tmp_path)
    assert _run(village).returncode == 0
    release = _run(village, release=True)
    assert release.returncode == 0
    assert "all 50 generated resident files are cleared" in release.stdout


def test_deleting_all_resident_images_cannot_bypass_release_gate(tmp_path: Path) -> None:
    village = _copy_village(tmp_path)
    for directory in (village / "agents").iterdir():
        if directory.is_dir():
            (directory / "texture.png").unlink()
            (directory / "portrait.png").unlink()
    result = _run(village, release=True)
    assert result.returncode == 1
    assert "missing or unexpected files" in result.stderr


def test_unreceipted_hash_drift_fails_even_outside_release_mode(tmp_path: Path) -> None:
    village = _copy_village(tmp_path)
    path = village / "agents/乔治/texture.png"
    path.write_bytes(path.read_bytes() + b"drift")
    result = _run(village)
    assert result.returncode == 1
    assert "generated provenance or image derivation is invalid" in result.stderr


def test_policy_status_edit_cannot_override_invalid_generated_receipt(tmp_path: Path) -> None:
    village = _copy_village(tmp_path)
    receipt_path = village / "agents/乔治/generation-provenance.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["generation"]["request_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    manifest_path = REPO_ROOT / "frontend/config/asset-provenance.json"
    manifest = json.loads(manifest_path.read_text())
    policy = next(asset for asset in manifest["assets"] if asset.get("category"))
    assert policy["audit_status"] == "pending"
    assert policy["distribution_status"] == "blocked"
    policy["audit_status"] = "cleared"
    policy["distribution_status"] = "allowed"
    changed_manifest = tmp_path / "asset-provenance.json"
    changed_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    # The verifier computes status from per-slot receipts and does not trust
    # these two policy display fields.
    result = _run(village, release=True, manifest=changed_manifest)
    assert result.returncode == 1
    assert "generated provenance or image derivation is invalid" in result.stderr
