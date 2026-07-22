"""Durable, path-confined artifact byte spool for one Runtime shard."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator


_LOCATOR_RE = re.compile(r"^[0-9a-f]{64}/[0-9a-f]{64}\.blob$")
_CHUNK_BYTES = 256 * 1024


class ArtifactSpoolError(RuntimeError):
    pass


class ArtifactSpoolCapacityError(ArtifactSpoolError):
    pass


@dataclass(frozen=True)
class SpooledArtifact:
    locator: str
    byte_size: int
    sha256: str


class ArtifactSpool:
    """Stores artifact bytes under deterministic, non-user-controlled paths."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int,
        max_artifact_bytes: int,
    ) -> None:
        raw_root = str(root)
        if not raw_root:
            raise ValueError("runtime artifact spool path is required")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("runtime artifact spool byte cap must be positive")
        if (
            type(max_artifact_bytes) is not int
            or max_artifact_bytes <= 0
            or max_artifact_bytes > max_bytes
        ):
            raise ValueError(
                "runtime artifact byte cap must be positive and within spool cap"
            )
        self.root = Path(raw_root).expanduser().resolve()
        self.max_bytes = max_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir() or self.root.is_symlink():
            raise ArtifactSpoolError("runtime artifact spool must be a real directory")
        if os.name == "posix":
            os.chmod(self.root, 0o700)

    @staticmethod
    def locator_for(session_id: str, artifact_id: str) -> str:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("runtime artifact session_id is required")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("runtime artifact artifact_id is required")
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        artifact_hash = hashlib.sha256(
            f"{session_id}\0{artifact_id}".encode("utf-8")
        ).hexdigest()
        return f"{session_hash}/{artifact_hash}.blob"

    def _path(self, locator: str) -> Path:
        if not isinstance(locator, str) or not _LOCATOR_RE.fullmatch(locator):
            raise ArtifactSpoolError("invalid runtime artifact spool locator")
        candidate = (self.root / locator).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactSpoolError("runtime artifact spool locator escaped root") from exc
        return candidate

    def _size_sync(self) -> int:
        total = 0
        if not self.root.exists():
            return 0
        for shard_dir in self.root.iterdir():
            if shard_dir.is_symlink() or not shard_dir.is_dir():
                continue
            for candidate in shard_dir.iterdir():
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    total += candidate.stat().st_size
                except FileNotFoundError:
                    continue
        return total

    async def size(self) -> int:
        return await asyncio.to_thread(self._size_sync)

    async def probe_writable(self) -> None:
        await self.initialize()

        def _probe() -> None:
            descriptor, name = tempfile.mkstemp(prefix=".ready-", dir=self.root)
            try:
                os.write(descriptor, b"ready")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass

        await asyncio.to_thread(_probe)

    async def put(
        self, session_id: str, artifact_id: str, content: bytes
    ) -> SpooledArtifact:
        if not isinstance(content, bytes):
            raise TypeError("runtime artifact spool content must be bytes")
        byte_size = len(content)
        if byte_size > self.max_artifact_bytes:
            raise ArtifactSpoolCapacityError("runtime artifact exceeds byte cap")
        digest = hashlib.sha256(content).hexdigest()
        locator = self.locator_for(session_id, artifact_id)
        destination = self._path(locator)

        async with self._write_lock:
            await self.initialize()
            existing_size = 0
            if destination.exists():
                existing = await self.digest(locator)
                if existing.sha256 != digest or existing.byte_size != byte_size:
                    raise ArtifactSpoolError("runtime artifact spool payload conflict")
                return existing
            current_size = await self.size()
            if current_size - existing_size + byte_size > self.max_bytes:
                raise ArtifactSpoolCapacityError("runtime artifact spool is full")
            await asyncio.to_thread(self._write_atomic, destination, content)
        return SpooledArtifact(locator=locator, byte_size=byte_size, sha256=digest)

    def _write_atomic(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.parent.is_symlink():
            raise ArtifactSpoolError("runtime artifact spool shard is a symlink")
        if os.name == "posix":
            os.chmod(destination.parent, 0o700)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".artifact-", dir=destination.parent
        )
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    async def digest(self, locator: str) -> SpooledArtifact:
        path = self._path(locator)

        def _digest() -> SpooledArtifact:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            try:
                digest = hashlib.sha256()
                byte_size = 0
                while True:
                    chunk = os.read(descriptor, _CHUNK_BYTES)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    digest.update(chunk)
            finally:
                os.close(descriptor)
            return SpooledArtifact(
                locator=locator,
                byte_size=byte_size,
                sha256=digest.hexdigest(),
            )

        return await asyncio.to_thread(_digest)

    async def iter_bytes(self, locator: str) -> AsyncIterator[bytes]:
        path = self._path(locator)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = await asyncio.to_thread(os.open, path, flags)
        try:
            while True:
                chunk = await asyncio.to_thread(os.read, descriptor, _CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(os.close, descriptor)

    async def delete(self, locator: str) -> bool:
        path = self._path(locator)

        def _delete() -> bool:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            try:
                path.parent.rmdir()
            except OSError:
                pass
            return True

        return await asyncio.to_thread(_delete)
