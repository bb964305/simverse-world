#!/usr/bin/env python3
"""Verify the immutable pre-implementation dirty-worktree baseline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path


def _git(repo_root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo_root), *args])


def _path_metadata(path: Path) -> dict[str, object | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"type": "missing", "size": None, "sha256": None}

    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path).encode()
        return {"type": "symlink", "size": info.st_size, "sha256": hashlib.sha256(target).hexdigest()}
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"type": "file", "size": info.st_size, "sha256": digest.hexdigest()}
    if stat.S_ISDIR(info.st_mode):
        return {"type": "directory", "size": info.st_size, "sha256": None}
    return {"type": "other", "size": info.st_size, "sha256": None}


def verify(repo_root: Path, manifest_path: Path) -> dict:
    repo_root = repo_root.resolve()
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []

    if Path(manifest.get("repo_root", "")).resolve() != repo_root:
        errors.append("repo_root_mismatch")
    head = _git(repo_root, "rev-parse", "HEAD").decode().strip()
    if manifest.get("head") != head:
        errors.append("head_mismatch")

    raw = _git(repo_root, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    raw_digest = hashlib.sha256(raw).hexdigest()
    if manifest.get("raw_status_sha256") != raw_digest:
        errors.append("raw_status_mismatch")

    changed_paths: list[dict[str, object]] = []
    for expected in manifest.get("paths", []):
        relative = expected["path"]
        actual = _path_metadata(repo_root / relative)
        comparable = {key: expected.get(key) for key in ("type", "size", "sha256")}
        if actual != comparable:
            changed_paths.append({"path": relative, "expected": comparable, "actual": actual})
    if changed_paths:
        errors.append("path_metadata_mismatch")

    mode = manifest_path.stat().st_mode
    if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        errors.append("manifest_is_writable")

    raw_path_value = manifest.get("raw_status_file")
    raw_file_digest = None
    if raw_path_value:
        raw_path = Path(raw_path_value).resolve()
        if not raw_path.is_file():
            errors.append("raw_status_file_missing")
        else:
            raw_file_digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            if raw_file_digest != raw_digest:
                errors.append("raw_status_file_mismatch")
            if raw_path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                errors.append("raw_status_file_is_writable")

    return {
        "ok": not errors,
        "repo_root": str(repo_root),
        "head": head,
        "raw_status_sha256": raw_digest,
        "raw_status_file_sha256": raw_file_digest,
        "dirty_path_count": len(manifest.get("paths", [])),
        "changed_paths": changed_paths,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--repo-root", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    result = verify(args.repo_root, args.manifest)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
