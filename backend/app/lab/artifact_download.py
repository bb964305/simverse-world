"""Released-only, exact-version Artifact download boundary for the API."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.lab.artifact_services.mime import normalize_content_type
from app.lab.artifact_services.schemas import ObjectRef
from app.lab.artifact_services.storage.base import StorageError
from app.lab.artifact_services.storage.filesystem import FileSystemStorage
from app.lab.artifact_services.storage.s3 import S3Config, S3SigV4Storage
from app.models.lab_artifact import LabArtifact


class ArtifactDownloadError(RuntimeError):
    """Base error for the released-object read boundary."""


class ArtifactDownloadConfigurationError(ArtifactDownloadError):
    pass


class ArtifactDownloadIntegrityError(ArtifactDownloadError):
    pass


@dataclass(frozen=True)
class PreparedArtifactDownload:
    path: Path
    filename: str
    content_type: str
    sha256: str
    byte_size: int


def _safe_filename(artifact: LabArtifact) -> str:
    value = (artifact.original_filename or f"artifact-{artifact.id}").replace(
        "\\", "/"
    )
    value = value.rsplit("/", 1)[-1]
    value = "".join(
        char for char in value if ord(char) >= 32 and char not in {'"', "\x7f"}
    ).strip().strip(".")
    if not value:
        value = f"artifact-{artifact.id}"
    return value[:180]


def _released_ref(artifact: LabArtifact) -> ObjectRef:
    fields = (
        artifact.storage_backend,
        artifact.released_bucket,
        artifact.released_key,
        artifact.released_version_id,
        artifact.released_etag,
        artifact.sha256,
        artifact.content_type,
    )
    if artifact.storage_status != "released" or any(not value for value in fields):
        raise ArtifactDownloadIntegrityError(
            "artifact does not have a complete released object reference"
        )
    try:
        return ObjectRef(
            backend=artifact.storage_backend,
            zone="released",
            bucket=artifact.released_bucket,
            key=artifact.released_key,
            version_id=artifact.released_version_id,
            etag=artifact.released_etag,
            byte_size=artifact.byte_size,
            sha256=artifact.sha256,
            content_type=artifact.content_type,
        )
    except Exception as exc:  # noqa: BLE001 - persisted locators are untrusted input
        raise ArtifactDownloadIntegrityError(
            "artifact released object reference is invalid"
        ) from exc


class ReleasedArtifactReader:
    """Reader with one capability: GET one exact released object version."""

    def __init__(self, *, storage, bucket: str, max_bytes: int) -> None:
        if not bucket or max_bytes <= 0:
            raise ArtifactDownloadConfigurationError(
                "released Artifact reader requires a bucket and positive byte limit"
            )
        self.storage = storage
        self.bucket = bucket
        self.max_bytes = max_bytes

    @classmethod
    def from_settings(cls) -> "ReleasedArtifactReader":
        backend = settings.lab_artifact_download_backend.strip().lower()
        bucket = settings.lab_artifact_download_released_bucket.strip()
        max_bytes = settings.lab_artifact_download_max_bytes
        if not bucket or type(max_bytes) is not int or max_bytes <= 0:
            raise ArtifactDownloadConfigurationError(
                "released Artifact reader requires a bucket and positive byte limit"
            )
        if backend == "filesystem":
            root = settings.lab_artifact_download_storage_root.strip()
            if not root:
                raise ArtifactDownloadConfigurationError(
                    "filesystem released reader requires a storage root"
                )
            try:
                storage = FileSystemStorage(
                    root=root,
                    buckets={"released": bucket},
                    read_only_zones=frozenset({"released"}),
                )
            except ValueError as exc:
                raise ArtifactDownloadConfigurationError(str(exc)) from exc
        elif backend == "s3":
            required = (
                settings.lab_artifact_download_s3_endpoint_url,
                settings.lab_artifact_download_s3_region,
                settings.lab_artifact_download_s3_access_key,
                settings.lab_artifact_download_s3_secret_key,
            )
            if any(not value.strip() for value in required):
                raise ArtifactDownloadConfigurationError(
                    "S3 released reader credentials are incomplete"
                )
            if settings.lab_artifact_download_timeout_s <= 0:
                raise ArtifactDownloadConfigurationError(
                    "S3 released reader timeout must be positive"
                )
            try:
                storage = S3SigV4Storage(S3Config(
                    endpoint_url=settings.lab_artifact_download_s3_endpoint_url,
                    region=settings.lab_artifact_download_s3_region,
                    access_key=settings.lab_artifact_download_s3_access_key,
                    secret_key=settings.lab_artifact_download_s3_secret_key,
                    session_token=(
                        settings.lab_artifact_download_s3_session_token or None
                    ),
                    buckets={"released": bucket},
                    timeout_seconds=settings.lab_artifact_download_timeout_s,
                ))
            except ValueError as exc:
                raise ArtifactDownloadConfigurationError(str(exc)) from exc
        else:
            raise ArtifactDownloadConfigurationError(
                "released Artifact reader backend must be filesystem or s3"
            )
        return cls(storage=storage, bucket=bucket, max_bytes=max_bytes)

    async def aclose(self) -> None:
        close = getattr(self.storage, "aclose", None)
        if close is not None:
            await close()

    async def prepare(self, artifact: LabArtifact) -> PreparedArtifactDownload:
        ref = _released_ref(artifact)
        if ref.backend != self.storage.backend or ref.bucket != self.bucket:
            raise ArtifactDownloadIntegrityError(
                "artifact reference is outside the API released-reader capability"
            )
        content_type = normalize_content_type(ref.content_type)
        allowed = {
            normalize_content_type(value)
            for value in settings.lab_artifact_allowed_mime_types
        }
        if content_type not in allowed:
            raise ArtifactDownloadIntegrityError(
                "artifact content type is not permitted for release"
            )
        if ref.byte_size > self.max_bytes:
            raise ArtifactDownloadIntegrityError(
                "artifact exceeds the API download byte limit"
            )

        fd, temp_name = tempfile.mkstemp(prefix="simverse-artifact-", suffix=".download")
        os.close(fd)
        destination = Path(temp_name)
        try:
            await self.storage.download_exact(
                ref,
                destination=destination,
                max_bytes=self.max_bytes,
            )
        except StorageError as exc:
            destination.unlink(missing_ok=True)
            raise ArtifactDownloadIntegrityError(
                "released Artifact bytes failed exact-version verification"
            ) from exc
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise ArtifactDownloadError(
                "released Artifact could not be staged for download"
            ) from exc
        return PreparedArtifactDownload(
            path=destination,
            filename=_safe_filename(artifact),
            content_type=content_type,
            sha256=ref.sha256,
            byte_size=ref.byte_size,
        )


async def prepare_released_artifact(
    artifact: LabArtifact,
) -> PreparedArtifactDownload:
    reader = ReleasedArtifactReader.from_settings()
    try:
        return await reader.prepare(artifact)
    finally:
        await reader.aclose()
