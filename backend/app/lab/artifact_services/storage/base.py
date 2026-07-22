"""Storage contract: immutable writes and exact-version reads/deletes only."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Protocol

from app.lab.artifact_services.schemas import DeleteProof, ObjectRef


class StorageError(RuntimeError):
    pass


class StorageConflict(StorageError):
    pass


class StorageNotFound(StorageError):
    pass


class ObjectStorage(Protocol):
    backend: str

    async def put_file(
        self,
        *,
        zone: str,
        bucket: str,
        key: str,
        source: Path,
        content_type: str,
        sha256: str,
        byte_size: int,
        operation_id: str,
    ) -> ObjectRef: ...

    async def download_exact(
        self, ref: ObjectRef, *, destination: Path, max_bytes: int
    ) -> ObjectRef: ...

    async def delete_exact(self, ref: ObjectRef) -> DeleteProof: ...

    async def ready(self) -> bool: ...


def validate_key(key: str) -> None:
    if (
        not key
        or key.startswith("/")
        or "\\" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or any(ord(char) < 32 for char in key)
    ):
        raise StorageError("object key is not canonical")


def hash_file_sync(path: Path, *, max_bytes: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise StorageError("object exceeds the permitted byte limit")
            digest.update(chunk)
    return digest.hexdigest(), size


async def hash_file(path: Path, *, max_bytes: int | None = None) -> tuple[str, int]:
    return await asyncio.to_thread(hash_file_sync, path, max_bytes=max_bytes)


async def verify_file(
    path: Path, *, sha256: str, byte_size: int, max_bytes: int | None = None
) -> None:
    actual_sha, actual_size = await hash_file(path, max_bytes=max_bytes)
    if actual_sha != sha256 or actual_size != byte_size:
        raise StorageConflict("object bytes diverge from their exact reference")
