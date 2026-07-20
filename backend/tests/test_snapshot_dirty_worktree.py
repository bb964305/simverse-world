import hashlib
import json
import os
import subprocess
from pathlib import Path

from scripts.snapshot_dirty_worktree import _path_metadata, verify


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def _fixture_manifest(repo: Path, evidence: Path) -> Path:
    raw = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    raw_path = evidence / "dirty-status.z"
    raw_path.write_bytes(raw)
    path = repo / "dirty.txt"
    manifest = {
        "repo_root": str(repo.resolve()),
        "head": _git(repo, "rev-parse", "HEAD").decode().strip(),
        "raw_status_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_status_file": str(raw_path),
        "paths": [{"path": "dirty.txt", **_path_metadata(path)}],
    }
    manifest_path = evidence / "dirty-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    os.chmod(raw_path, 0o444)
    os.chmod(manifest_path, 0o444)
    return manifest_path


def test_verify_accepts_exact_read_only_baseline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    (repo / "dirty.txt").write_text("do not touch\n", encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    result = verify(repo, _fixture_manifest(repo, evidence))

    assert result["ok"] is True
    assert result["errors"] == []


def test_verify_fails_after_dirty_file_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    (repo / "dirty.txt").write_text("before\n", encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = _fixture_manifest(repo, evidence)
    (repo / "dirty.txt").write_text("after\n", encoding="utf-8")

    result = verify(repo, manifest)

    assert result["ok"] is False
    assert "path_metadata_mismatch" in result["errors"]
