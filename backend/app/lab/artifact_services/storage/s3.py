"""Minimal S3-compatible, exact-version driver using AWS Signature Version 4.

This driver deliberately implements only the operations required by the Artifact
trust planes.  It never lists a bucket and rejects storage without version IDs.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from app.lab.artifact_services.schemas import DeleteProof, ObjectRef
from app.lab.artifact_services.storage.base import (
    StorageConflict,
    StorageError,
    StorageNotFound,
    validate_key,
    verify_file,
)


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str
    region: str
    access_key: str
    secret_key: str
    buckets: Mapping[str, str]
    session_token: str | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        endpoint = urlsplit(self.endpoint_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise ValueError("S3 endpoint must be an absolute HTTP(S) URL")
        if not self.region or not self.access_key or not self.secret_key:
            raise ValueError("S3 region and credentials are required")
        if not self.buckets or not set(self.buckets).issubset({"quarantine", "released"}):
            raise ValueError("S3 storage zones are invalid")
        if len(set(self.buckets.values())) != len(self.buckets):
            raise ValueError("S3 bucket names must be distinct")


class S3SigV4Storage:
    backend = "s3"

    def __init__(self, config: S3Config, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._endpoint = urlsplit(config.endpoint_url.rstrip("/"))
        self._client = client or httpx.AsyncClient(
            trust_env=False, timeout=config.timeout_seconds, follow_redirects=False
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def bucket_for(self, zone: str) -> str:
        try:
            return self.config.buckets[zone]
        except KeyError as exc:
            raise StorageError("unknown storage zone") from exc

    @staticmethod
    def _sign(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    def _signing_key(self, date_stamp: str) -> bytes:
        date_key = self._sign(("AWS4" + self.config.secret_key).encode(), date_stamp)
        region_key = self._sign(date_key, self.config.region)
        service_key = self._sign(region_key, "s3")
        return self._sign(service_key, "aws4_request")

    def _url(self, bucket: str, key: str | None, query: list[tuple[str, str]]) -> str:
        path = self._endpoint.path.rstrip("/") + "/" + quote(bucket, safe="-_.~")
        if key is not None:
            validate_key(key)
            path += "/" + quote(key, safe="/-_.~")
        canonical_query = urlencode(sorted(query), quote_via=quote, safe="-_.~")
        return urlunsplit(
            (self._endpoint.scheme, self._endpoint.netloc, path, canonical_query, "")
        )

    def _signed_headers(
        self,
        *,
        method: str,
        url: str,
        payload_sha256: str,
        headers: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, str]:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = moment.strftime("%Y%m%d")
        parsed = urlsplit(url)
        values = {
            "host": parsed.netloc,
            "x-amz-content-sha256": payload_sha256,
            "x-amz-date": amz_date,
            **{
                key.lower(): " ".join(value.strip().split())
                for key, value in (headers or {}).items()
            },
        }
        if self.config.session_token:
            values["x-amz-security-token"] = self.config.session_token
        signed_names = ";".join(sorted(values))
        canonical_headers = "".join(f"{name}:{values[name]}\n" for name in sorted(values))
        canonical_query = urlencode(
            sorted(parse_qsl(parsed.query, keep_blank_values=True)),
            quote_via=quote,
            safe="-_.~",
        )
        canonical_request = "\n".join(
            [
                method,
                quote(parsed.path, safe="/-_.~"),
                canonical_query,
                canonical_headers,
                signed_names,
                payload_sha256,
            ]
        )
        scope = f"{date_stamp}/{self.config.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        values["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.config.access_key}/{scope},"
            f"SignedHeaders={signed_names},Signature={signature}"
        )
        return values

    async def _send(
        self,
        method: str,
        *,
        bucket: str,
        key: str | None = None,
        query: list[tuple[str, str]] | None = None,
        payload_sha256: str = _EMPTY_SHA256,
        headers: Mapping[str, str] | None = None,
        content=None,
        stream: bool = False,
    ) -> httpx.Response:
        url = self._url(bucket, key, query or [])
        signed = self._signed_headers(
            method=method,
            url=url,
            payload_sha256=payload_sha256,
            headers=headers,
        )
        request = self._client.build_request(method, url, headers=signed, content=content)
        return await self._client.send(request, stream=stream)

    @staticmethod
    async def _file_stream(path: Path) -> AsyncIterator[bytes]:
        handle = path.open("rb")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, 1024 * 1024)
                if not chunk:
                    return
                yield chunk
        finally:
            handle.close()

    async def _head(
        self, *, zone: str, bucket: str, key: str, version_id: str | None
    ) -> httpx.Response:
        if self.bucket_for(zone) != bucket:
            raise StorageError("bucket is not authorized for this storage zone")
        query = [] if version_id is None else [("versionId", version_id)]
        return await self._send("HEAD", bucket=bucket, key=key, query=query)

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
        if self.bucket_for(zone) != bucket:
            raise StorageError("bucket is not authorized for this storage zone")
        validate_key(key)
        await verify_file(source, sha256=sha256, byte_size=byte_size)
        metadata = {
            "content-type": content_type,
            "content-length": str(byte_size),
            "if-none-match": "*",
            "x-amz-meta-operation-id": operation_id,
            "x-amz-meta-sha256": sha256,
            "x-amz-meta-byte-size": str(byte_size),
            "x-amz-meta-zone": zone,
        }
        response = await self._send(
            "PUT",
            bucket=bucket,
            key=key,
            payload_sha256=sha256,
            headers=metadata,
            content=self._file_stream(source),
        )
        if response.status_code in {409, 412}:
            response = await self._head(zone=zone, bucket=bucket, key=key, version_id=None)
            if response.status_code != 200:
                raise StorageConflict("immutable object key already exists with unknown state")
        elif response.status_code not in {200, 201}:
            raise StorageError(f"S3 PUT failed with status {response.status_code}")
        version_id = response.headers.get("x-amz-version-id")
        etag = response.headers.get("etag", "").strip('"')
        if not version_id or not etag:
            raise StorageError("S3 response omitted version ID or ETag")
        if (
            response.headers.get("x-amz-meta-operation-id") not in {None, operation_id}
            or response.headers.get("x-amz-meta-sha256") not in {None, sha256}
            or response.headers.get("x-amz-meta-byte-size") not in {None, str(byte_size)}
        ):
            raise StorageConflict("existing immutable object metadata diverges")
        return ObjectRef(
            backend=self.backend,
            zone=zone,
            bucket=bucket,
            key=key,
            version_id=version_id,
            etag=etag,
            byte_size=byte_size,
            sha256=sha256,
            content_type=content_type,
        )

    async def download_exact(
        self, ref: ObjectRef, *, destination: Path, max_bytes: int
    ) -> ObjectRef:
        if ref.backend != self.backend or self.bucket_for(ref.zone) != ref.bucket:
            raise StorageError("object reference is not authorized for this driver")
        if ref.byte_size > max_bytes:
            raise StorageError("object exceeds the permitted download limit")
        try:
            response = await self._send(
                "GET",
                bucket=ref.bucket,
                key=ref.key,
                query=[("versionId", ref.version_id)],
                stream=True,
            )
        except httpx.HTTPError as exc:
            raise StorageError("S3 GET request failed") from exc
        if response.status_code == 404:
            await response.aclose()
            raise StorageNotFound("exact object version was not found")
        if response.status_code != 200:
            await response.aclose()
            raise StorageError(f"S3 GET failed with status {response.status_code}")
        version_id = response.headers.get("x-amz-version-id")
        etag = response.headers.get("etag", "").strip('"')
        if version_id != ref.version_id or etag != ref.etag:
            await response.aclose()
            raise StorageConflict(
                "S3 GET response diverges from the requested exact object version"
            )
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                response_size = int(content_length)
            except ValueError as exc:
                await response.aclose()
                raise StorageConflict("S3 GET returned an invalid content length") from exc
            if response_size != ref.byte_size:
                await response.aclose()
                raise StorageConflict(
                    "S3 GET content length diverges from the exact reference"
                )
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        digest = hashlib.sha256()
        try:
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise StorageError("download exceeded the permitted byte limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except (httpx.HTTPError, OSError, StorageError) as exc:
            destination.unlink(missing_ok=True)
            raise StorageError("S3 GET stream failed") from exc
        finally:
            await response.aclose()
        if size != ref.byte_size or digest.hexdigest() != ref.sha256:
            destination.unlink(missing_ok=True)
            raise StorageConflict("downloaded object bytes diverge from exact reference")
        return ref

    async def delete_exact(self, ref: ObjectRef) -> DeleteProof:
        if ref.backend != self.backend or self.bucket_for(ref.zone) != ref.bucket:
            raise StorageError("object reference is not authorized for this driver")
        response = await self._send(
            "DELETE",
            bucket=ref.bucket,
            key=ref.key,
            query=[("versionId", ref.version_id)],
        )
        if response.status_code not in {200, 204, 404}:
            raise StorageError(f"S3 DELETE failed with status {response.status_code}")
        probe = await self._head(
            zone=ref.zone,
            bucket=ref.bucket,
            key=ref.key,
            version_id=ref.version_id,
        )
        if probe.status_code != 404:
            raise StorageError("exact object version remains readable after delete")
        return DeleteProof(
            object_ref=ref,
            absent=True,
            checked_at=datetime.now(UTC),
        )

    async def ready(self) -> bool:
        try:
            for zone, bucket in self.config.buckets.items():
                response = await self._send(
                    "GET", bucket=bucket, query=[("versioning", "")]
                )
                if response.status_code != 200 or "<Status>Enabled</Status>" not in response.text:
                    return False
                if self.bucket_for(zone) != bucket:
                    return False
            return True
        except Exception:
            return False
