"""Private versioned filesystem driver for staging and single-host deployments."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from app.lab.artifact_services.schemas import DeleteProof, ObjectRef
from app.lab.artifact_services.storage.base import (
    StorageError,
    StorageNotFound,
    validate_key,
    verify_file,
)


class FileSystemStorage:
    backend = "filesystem"

    def __init__(
        self,
        *,
        root: str | os.PathLike[str],
        buckets: Mapping[str, str],
        read_only_zones: frozenset[str] = frozenset(),
    ) -> None:
        self.root = Path(root)
        self.buckets = dict(buckets)
        self.read_only_zones = frozenset(read_only_zones)
        if not self.buckets or not set(self.buckets).issubset({"quarantine", "released"}):
            raise ValueError("filesystem storage zones are invalid")
        if not self.read_only_zones.issubset(self.buckets):
            raise ValueError("read-only zones must be configured storage zones")
        if len(set(self.buckets.values())) != len(self.buckets) or any(
            not name for name in self.buckets.values()
        ):
            raise ValueError("filesystem bucket names must be distinct and non-empty")

    def bucket_for(self, zone: str) -> str:
        try:
            return self.buckets[zone]
        except KeyError as exc:
            raise StorageError("unknown storage zone") from exc

    def _root_for(self, *, zone: str, bucket: str) -> Path:
        if self.bucket_for(zone) != bucket:
            raise StorageError("bucket is not authorized for this storage zone")
        root = (self.root / bucket).resolve()
        base = self.root.resolve()
        if root.parent != base:
            raise StorageError("bucket path escaped storage root")
        return root

    def _path(self, ref: ObjectRef) -> Path:
        if ref.backend != self.backend:
            raise StorageError("object reference belongs to another storage backend")
        validate_key(ref.key)
        try:
            version_id = str(uuid.UUID(ref.version_id))
        except ValueError as exc:
            raise StorageError("filesystem object version is not canonical") from exc
        if version_id != ref.version_id:
            raise StorageError("filesystem object version is not canonical")
        root = self._root_for(zone=ref.zone, bucket=ref.bucket)
        target = (root / ref.key / f"{version_id}.blob").resolve()
        if root not in target.parents:
            raise StorageError("object path escaped bucket root")
        return target

    @staticmethod
    def _copy_atomic(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                pass
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)

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
    ) -> ObjectRef:
        if zone in self.read_only_zones:
            raise StorageError("storage zone is read-only")
        validate_key(key)
        await verify_file(source, sha256=sha256, byte_size=byte_size)
        version_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"simverse-artifact:{operation_id}:{zone}:{bucket}:{key}",
            )
        )
        ref = ObjectRef(
            backend=self.backend,
            zone=zone,
            bucket=bucket,
            key=key,
            version_id=version_id,
            etag=sha256,
            byte_size=byte_size,
            sha256=sha256,
            content_type=content_type,
        )
        target = self._path(ref)
        await asyncio.to_thread(self._copy_atomic, source, target)
        await verify_file(target, sha256=sha256, byte_size=byte_size)
        return ref

    async def download_exact(
        self, ref: ObjectRef, *, destination: Path, max_bytes: int
    ) -> ObjectRef:
        source = self._path(ref)
        if not source.is_file():
            raise StorageNotFound("exact object version was not found")
        if ref.byte_size > max_bytes:
            raise StorageError("object exceeds the permitted download limit")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(
                self._copy_bounded, source, destination, max_bytes
            )
            await verify_file(
                destination,
                sha256=ref.sha256,
                byte_size=ref.byte_size,
                max_bytes=max_bytes,
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ref

    @staticmethod
    def _copy_bounded(source: Path, destination: Path, max_bytes: int) -> None:
        copied = 0
        with source.open("rb") as input_file, destination.open("wb") as output:
            while True:
                chunk = input_file.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise StorageError("download exceeded the permitted byte limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

    async def delete_exact(self, ref: ObjectRef) -> DeleteProof:
        if ref.zone in self.read_only_zones:
            raise StorageError("storage zone is read-only")
        target = self._path(ref)
        await asyncio.to_thread(target.unlink, missing_ok=True)
        if target.exists():
            raise StorageError("exact object version remains readable after delete")
        return DeleteProof(
            object_ref=ref,
            absent=True,
            checked_at=datetime.now(UTC),
        )

    async def ready(self) -> bool:
        try:
            for zone, bucket in self.buckets.items():
                root = self._root_for(zone=zone, bucket=bucket)
                if zone in self.read_only_zones:
                    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
                        return False
                else:
                    root.mkdir(parents=True, exist_ok=True)
                    fd, path = tempfile.mkstemp(prefix=".ready-", dir=root)
                    os.close(fd)
                    Path(path).unlink()
            return True
        except Exception:
            return False
