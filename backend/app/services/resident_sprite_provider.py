"""Injectable, fail-closed image provider adapter for resident sprites."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import ipaddress
import math
import posixpath
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from app.services.resident_sprite_generation import (
    CapabilityContract,
    CapabilityReceipt,
    CapabilityRevocation,
    ProviderImageResult,
    RequestBudget,
    ResidentSpriteContractError,
    SanitizedError,
    WireReceipt,
    content_id,
    create_revocation_tombstone,
    validate_capability_receipt,
    validate_wire_receipt,
)


MAX_ENCODED_BYTES = 35 * 1024 * 1024
MAX_DECODED_BYTES = 25 * 1024 * 1024
MAX_DECODED_PIXELS = 8_294_400
MAX_IMAGE_URL_LENGTH = 4096
MIN_NORMALIZABLE_PIXELS = 655_360
MIN_NORMALIZABLE_EDGE = 512
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
PROBE_FALLBACK_STATUSES = frozenset({400, 415, 422})


class ProviderError(RuntimeError):
    """Provider failure carrying only allowlisted, sanitized metadata."""

    def __init__(self, error: SanitizedError) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.error = error


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float = 180.0
    allow_insecure_http_test: bool = False

    @property
    def normalized_origin(self) -> str:
        return normalize_origin(
            self.base_url,
            allow_insecure_http_test=self.allow_insecure_http_test,
        )

    @property
    def transport_security(self) -> str:
        return (
            "insecure_http_test"
            if self.allow_insecure_http_test
            else "https_or_loopback"
        )


class QualificationBudget:
    """The paid A/B comparison has exactly five submissions and no retries."""

    def __init__(self) -> None:
        self.submitted_image_request_count = 0

    def consume_before_post(self, stage: str) -> None:
        del stage
        if self.submitted_image_request_count >= 5:
            raise ResidentSpriteContractError(
                "REQUEST_BUDGET_EXHAUSTED", "qualification request budget is exhausted"
            )
        self.submitted_image_request_count += 1


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")


def normalize_origin(
    base_url: str,
    *,
    allow_insecure_http_test: bool = False,
) -> str:
    parts = urlsplit(base_url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ResidentSpriteContractError("PROVIDER_ORIGIN_INVALID", "provider origin must use HTTP(S)")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ResidentSpriteContractError("PROVIDER_ORIGIN_INVALID", "provider origin authority is invalid")
    if parts.scheme.lower() == "http":
        raw_host = parts.hostname.lower()
        if not _is_loopback_host(raw_host) and not allow_insecure_http_test:
            raise ResidentSpriteContractError(
                "PROVIDER_HTTPS_REQUIRED", "non-loopback provider origins must use HTTPS"
            )
    if parts.query or parts.fragment:
        raise ResidentSpriteContractError("PROVIDER_ORIGIN_INVALID", "provider origin cannot have query or fragment")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise ResidentSpriteContractError("PROVIDER_ORIGIN_INVALID", "provider host or port is invalid") from exc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    raw_path = parts.path or "/"
    path = posixpath.normpath("/" + raw_path.lstrip("/"))
    if path == "/.":
        path = "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), f"{host}:{port}", path, "", ""))


def endpoint_url(normalized_origin: str, endpoint: str) -> str:
    if endpoint not in {"/images/generations", "/images/edits"}:
        raise ValueError("provider endpoint is not fixed by the contract")
    return f"{normalized_origin.rstrip('/')}{endpoint}"


def retry_jitter_ms(run_id: str, stage: str, logical_job: str, retry_ordinal: int) -> int:
    if retry_ordinal not in {1, 2}:
        raise ValueError("retry ordinal must be 1 or 2")
    seed = f"{run_id}:{stage}:{logical_job}:{retry_ordinal}".encode()
    return int.from_bytes(hashlib.sha256(seed).digest()[:2], "big") % 251


def parse_retry_after_ms(value: str | None, now: datetime) -> int:
    if value is None:
        return 0
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(int(value) * 1000, 30_000)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    delta_ms = math.ceil((retry_at - now).total_seconds() * 1000)
    return min(max(delta_ms, 0), 30_000)


def retry_delay_ms(
    run_id: str,
    stage: str,
    logical_job: str,
    retry_ordinal: int,
    retry_after: str | None,
    now: datetime,
) -> int:
    base_ms = 1000 if retry_ordinal == 1 else 2000
    deterministic = base_ms + retry_jitter_ms(run_id, stage, logical_job, retry_ordinal)
    return max(deterministic, parse_retry_after_ms(retry_after, now))


def _provider_request_id(response: httpx.Response) -> str | None:
    for name in ("x-request-id", "request-id", "x-provider-request-id"):
        value = response.headers.get(name)
        if value:
            return value[:200]
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        for name in ("request_id", "id"):
            value = payload.get(name)
            if isinstance(value, str) and value:
                return value[:200]
    return None


def _provider_evidence_id(response: httpx.Response) -> str | None:
    request_id = _provider_request_id(response)
    if request_id is not None:
        return request_id
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    result_url = data[0].get("url")
    if not isinstance(result_url, str) or not result_url:
        return None
    digest = hashlib.sha256(result_url.encode("utf-8")).hexdigest()
    return f"result-url-sha256:{digest}"


def _error(
    code: str,
    message: str,
    *,
    response: httpx.Response | None = None,
) -> ProviderError:
    return ProviderError(
        SanitizedError(
            code=code,
            message=message[:500],
            provider_request_id=None if response is None else _provider_request_id(response),
            http_status=None if response is None else response.status_code,
        )
    )


def _request_budget_has_capacity(budget: RequestBudget, stage: str) -> bool:
    """Check whether another stage request can be reserved without mutating it."""
    return (
        budget.stage_counts.get(stage, 0) < RequestBudget.stage_ceiling(stage)
        and budget.submitted_image_request_count < budget.global_ceiling
    )


def _budget_exhausted_error(
    response: httpx.Response | None = None,
) -> ProviderError:
    return _error(
        "REQUEST_BUDGET_EXHAUSTED",
        "submitted image request budget is exhausted",
        response=response,
    )


def decode_image_response(
    response: httpx.Response,
    expected_size: tuple[int, int],
    *,
    allow_dimension_normalization: bool = False,
) -> bytes:
    try:
        payload = response.json()
    except ValueError as exc:
        raise _error("PROVIDER_RESPONSE_INVALID", "provider returned malformed JSON", response=response) from exc
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise _error("PROVIDER_RESPONSE_INVALID", "provider response contains no image", response=response)
    item = items[0]
    if "url" in item:
        raise _error(
            "PROVIDER_URL_RESPONSE_UNSUPPORTED",
            "provider URL responses are unsupported",
            response=response,
        )
    encoded = item.get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise _error("PROVIDER_RESPONSE_INVALID", "provider response contains no b64_json", response=response)
    if len(encoded.encode("ascii", errors="ignore")) > MAX_ENCODED_BYTES:
        raise _error("PROVIDER_ENCODED_TOO_LARGE", "encoded provider image exceeds 35 MiB", response=response)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _error("PROVIDER_BASE64_INVALID", "provider image base64 is invalid", response=response) from exc
    return _validate_downloaded_png(
        decoded,
        expected_size,
        response,
        allow_dimension_normalization=allow_dimension_normalization,
    )


def _validated_public_image_url(
    value: object,
    *,
    allow_insecure_http_test: bool = False,
) -> tuple[str, str, int]:
    if not isinstance(value, str) or not value or len(value) > MAX_IMAGE_URL_LENGTH:
        raise _error("PROVIDER_IMAGE_URL_INVALID", "provider image URL is invalid")
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if (
        scheme not in ({"https", "http"} if allow_insecure_http_test else {"https"})
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise _error("PROVIDER_IMAGE_URL_INVALID", "provider image URL must be public HTTPS")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port or 443
    except (UnicodeError, ValueError) as exc:
        raise _error("PROVIDER_IMAGE_URL_INVALID", "provider image URL authority is invalid") from exc
    if not allow_insecure_http_test and port != 443:
        raise _error("PROVIDER_IMAGE_URL_INVALID", "provider image URL must use port 443")
    return value, host, port


async def _require_public_host(host: str, port: int) -> None:
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise _error("PROVIDER_IMAGE_HOST_INVALID", "provider image host did not resolve") from exc
    if not addresses:
        raise _error("PROVIDER_IMAGE_HOST_INVALID", "provider image host did not resolve")
    try:
        resolved = {ipaddress.ip_address(item[4][0]) for item in addresses}
    except ValueError as exc:
        raise _error("PROVIDER_IMAGE_HOST_INVALID", "provider image host resolution was invalid") from exc
    if not resolved or any(not address.is_global for address in resolved):
        raise _error("PROVIDER_IMAGE_HOST_FORBIDDEN", "provider image host is not public")


def _validate_downloaded_png(
    decoded: bytes,
    expected_size: tuple[int, int],
    provider_response: httpx.Response,
    *,
    allow_dimension_normalization: bool = False,
) -> bytes:
    if len(decoded) > MAX_DECODED_BYTES:
        raise _error(
            "PROVIDER_IMAGE_TOO_LARGE",
            "downloaded provider image exceeds 25 MiB",
            response=provider_response,
        )
    try:
        with Image.open(io.BytesIO(decoded)) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise _error(
                    "PROVIDER_IMAGE_FORMAT",
                    "provider image must be one PNG",
                    response=provider_response,
                )
            width, height = image.size
            if width * height > MAX_DECODED_PIXELS:
                raise _error(
                    "PROVIDER_PIXEL_LIMIT",
                    "provider image exceeds the pixel cap",
                    response=provider_response,
                )
            image.load()
            if (width, height) == expected_size:
                return decoded
            if (
                not allow_dimension_normalization
                or width < MIN_NORMALIZABLE_EDGE
                or height < MIN_NORMALIZABLE_EDGE
                or width * height < MIN_NORMALIZABLE_PIXELS
                or max(width, height) / min(width, height) > 3
            ):
                raise _error(
                    "PROVIDER_DIMENSIONS",
                    "provider image dimensions are incorrect",
                    response=provider_response,
                )
            source = image.convert("RGBA")
            if expected_size[0] >= expected_size[1]:
                normalized = ImageOps.fit(
                    source,
                    expected_size,
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
            else:
                normalized = ImageOps.pad(
                    source,
                    expected_size,
                    method=Image.Resampling.LANCZOS,
                    color=(255, 0, 255, 255),
                    centering=(0.5, 0.5),
                )
            output = io.BytesIO()
            normalized.save(output, format="PNG", optimize=False, compress_level=9)
            return output.getvalue()
    except ProviderError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise _error(
            "PROVIDER_IMAGE_FORMAT",
            "provider image is not a valid PNG",
            response=provider_response,
        ) from exc
    return decoded


async def download_public_image_url(
    url: str,
    expected_size: tuple[int, int],
    provider_response: httpx.Response,
    timeout: float,
    *,
    allow_insecure_http_test: bool = False,
    allow_dimension_normalization: bool = False,
) -> bytes:
    value, host, port = _validated_public_image_url(
        url,
        allow_insecure_http_test=allow_insecure_http_test,
    )
    await _require_public_host(host, port)
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=timeout,
        ) as client:
            async with client.stream(
                "GET",
                value,
                headers={"Accept": "image/png"},
            ) as response:
                if response.status_code != 200:
                    raise _error(
                        "PROVIDER_IMAGE_DOWNLOAD_FAILED",
                        "provider image download failed",
                        response=provider_response,
                    )
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"image/png", "application/octet-stream"}:
                    raise _error(
                        "PROVIDER_IMAGE_CONTENT_TYPE",
                        "provider image download content type is invalid",
                        response=provider_response,
                    )
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        parsed_length = int(length)
                    except ValueError as exc:
                        raise _error(
                            "PROVIDER_IMAGE_LENGTH_INVALID",
                            "provider image content length is invalid",
                            response=provider_response,
                        ) from exc
                    if parsed_length < 0 or parsed_length > MAX_DECODED_BYTES:
                        raise _error(
                            "PROVIDER_IMAGE_TOO_LARGE",
                            "downloaded provider image exceeds 25 MiB",
                            response=provider_response,
                        )
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_DECODED_BYTES:
                        raise _error(
                            "PROVIDER_IMAGE_TOO_LARGE",
                            "downloaded provider image exceeds 25 MiB",
                            response=provider_response,
                        )
    except ProviderError:
        raise
    except httpx.TimeoutException as exc:
        raise _error("PROVIDER_IMAGE_DOWNLOAD_TIMEOUT", "provider image download timed out") from exc
    except httpx.RequestError as exc:
        raise _error("PROVIDER_IMAGE_DOWNLOAD_FAILED", "provider image download failed") from exc
    return _validate_downloaded_png(
        bytes(chunks),
        expected_size,
        provider_response,
        allow_dimension_normalization=allow_dimension_normalization,
    )


def calibration_anchor_png() -> bytes:
    image = Image.new("RGB", (1024, 1024), "#FF00FF")
    draw = ImageDraw.Draw(image)
    draw.ellipse((384, 160, 640, 416), fill="cyan", outline="black", width=16)
    draw.rectangle((416, 416, 608, 736), fill="gold", outline="black", width=16)
    draw.rectangle((336, 448, 416, 672), fill="cyan", outline="black", width=16)
    draw.rectangle((608, 448, 688, 672), fill="cyan", outline="black", width=16)
    draw.rectangle((432, 736, 496, 928), fill="gold", outline="black", width=16)
    draw.rectangle((528, 736, 592, 928), fill="gold", outline="black", width=16)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


class ResidentSpriteProvider:
    def __init__(
        self,
        config: ProviderConfig,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        image_url_fetcher: Callable[
            [str, tuple[int, int], httpx.Response, float], Awaitable[bytes]
        ] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._clock = clock
        self._sleeper = sleeper
        if image_url_fetcher is None:
            async def configured_image_url_fetcher(
                url: str,
                expected_size: tuple[int, int],
                provider_response: httpx.Response,
                timeout: float,
            ) -> bytes:
                return await download_public_image_url(
                    url,
                    expected_size,
                    provider_response,
                    timeout,
                    allow_insecure_http_test=config.allow_insecure_http_test,
                    allow_dimension_normalization=True,
                )

            self._image_url_fetcher = configured_image_url_fetcher
        else:
            self._image_url_fetcher = image_url_fetcher

    async def _decode_response(
        self,
        response: httpx.Response,
        expected_size: tuple[int, int],
    ) -> bytes:
        try:
            return decode_image_response(
                response,
                expected_size,
                allow_dimension_normalization=True,
            )
        except ProviderError as exc:
            if exc.error.code != "PROVIDER_URL_RESPONSE_UNSUPPORTED":
                raise
        try:
            payload = response.json()
            url = payload["data"][0]["url"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _error(
                "PROVIDER_RESPONSE_INVALID",
                "provider response contains no usable image",
                response=response,
            ) from exc
        return await self._image_url_fetcher(
            url,
            expected_size,
            response,
            self._config.timeout,
        )

    @property
    def contract_origin(self) -> str:
        return self._config.normalized_origin

    @property
    def model_alias(self) -> str:
        return self._config.model

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.api_key}"}

    async def generate_anchor(
        self,
        prompt: str,
        *,
        run_id: str,
        budget: RequestBudget,
        logical_job: str = "anchor",
        gate: Callable[[], None] | None = None,
        allow_retry: bool = True,
    ) -> ProviderImageResult:
        payload = {
            "model": self._config.model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "quality": "medium",
        }
        return await self._post_with_retry(
            endpoint="/images/generations",
            expected_size=(1024, 1024),
            run_id=run_id,
            stage="anchor",
            logical_job=logical_job,
            budget=budget,
            gate=gate,
            allow_retry=allow_retry,
            request_kwargs={"json": payload},
        )

    async def generate_oneshot_draft(
        self,
        prompt: str,
        *,
        run_id: str,
        budget: RequestBudget,
        gate: Callable[[], None],
    ) -> ProviderImageResult:
        payload = {
            "model": self._config.model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1536",
            "quality": "high",
        }
        return await self._post_with_retry(
            endpoint="/images/generations",
            expected_size=(1024, 1536),
            run_id=run_id,
            stage="anchor",
            logical_job="qualification-oneshot",
            budget=budget,
            gate=gate,
            allow_retry=False,
            request_kwargs={"json": payload},
        )

    async def edit_strip(
        self,
        anchor_png: bytes,
        prompt: str,
        *,
        multipart_field: str,
        run_id: str,
        stage: str,
        logical_job: str,
        budget: RequestBudget,
        gate: Callable[[], None],
        allow_retry: bool = True,
    ) -> ProviderImageResult:
        if multipart_field not in {"image[]", "image"}:
            raise ResidentSpriteContractError("MULTIPART_FIELD_INVALID", "multipart field is invalid")
        return await self._post_with_retry(
            endpoint="/images/edits",
            expected_size=(1536, 1024),
            run_id=run_id,
            stage=stage,
            logical_job=logical_job,
            budget=budget,
            gate=gate,
            allow_retry=allow_retry,
            request_kwargs={
                "files": {multipart_field: ("anchor.png", anchor_png, "image/png")},
                "data": {
                    "model": self._config.model,
                    "prompt": prompt,
                    "n": "1",
                    "size": "1536x1024",
                    "quality": "high",
                },
            },
        )

    async def probe_wire(
        self,
        prompt: str,
        *,
        prior_submitted_request_count: int = 0,
    ) -> tuple[ProviderImageResult, str]:
        """Submit at most two no-retry calibration edits with frozen negotiation."""
        if prior_submitted_request_count not in {0, 1}:
            raise ResidentSpriteContractError(
                "PRIOR_REQUEST_COUNT_INVALID", "prior probe request count must be zero or one"
            )
        anchor = calibration_anchor_png()
        attempts = prior_submitted_request_count
        fields = ("image[]", "image") if attempts == 0 else ("image[]",)
        for field_name in fields:
            attempts += 1
            started = time.monotonic()
            try:
                response = await self._client.post(
                    endpoint_url(self.contract_origin, "/images/edits"),
                    headers=self._headers(),
                    files={field_name: ("calibration-anchor.png", anchor, "image/png")},
                    data={
                        "model": self._config.model,
                        "prompt": prompt,
                        "n": "1",
                        "size": "1536x1024",
                        "quality": "high",
                    },
                    timeout=self._config.timeout,
                )
            except httpx.TimeoutException as exc:
                raise _error("PROVIDER_TIMEOUT", "provider request timed out") from exc
            except httpx.RequestError as exc:
                raise _error("PROVIDER_NETWORK_ERROR", "provider request failed") from exc
            if response.status_code == 200:
                decoded = await self._decode_response(response, (1536, 1024))
                return (
                    ProviderImageResult(
                        image_bytes=decoded,
                        provider_request_id=_provider_evidence_id(response),
                        latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                        submitted_request_count=attempts,
                    ),
                    field_name,
                )
            if (
                field_name == "image[]"
                and response.status_code in PROBE_FALLBACK_STATUSES
                and _provider_request_id(response) is None
            ):
                continue
            raise _error("PROVIDER_HTTP_ERROR", "provider rejected the wire probe", response=response)
        raise _error("PROVIDER_HTTP_ERROR", "provider rejected both wire probe forms")

    async def _post_with_retry(
        self,
        *,
        endpoint: str,
        expected_size: tuple[int, int],
        run_id: str,
        stage: str,
        logical_job: str,
        budget: RequestBudget,
        gate: Callable[[], None] | None,
        allow_retry: bool,
        request_kwargs: dict,
    ) -> ProviderImageResult:
        retry_ordinal = 0
        while True:
            if gate is not None:
                gate()
            try:
                budget.consume_before_post(stage)
            except ResidentSpriteContractError as exc:
                if exc.code == "REQUEST_BUDGET_EXHAUSTED":
                    raise _budget_exhausted_error() from exc
                raise
            started = time.monotonic()
            try:
                response = await self._client.post(
                    endpoint_url(self.contract_origin, endpoint),
                    headers=self._headers(),
                    timeout=self._config.timeout,
                    **request_kwargs,
                )
            except httpx.TimeoutException as exc:
                if not allow_retry:
                    raise _error("PROVIDER_TIMEOUT", "provider request timed out") from exc
                if not _request_budget_has_capacity(budget, stage):
                    raise _budget_exhausted_error() from exc
                if retry_ordinal >= 2:
                    raise _error("PROVIDER_TIMEOUT", "provider request timed out") from exc
                retry_ordinal += 1
                delay = retry_delay_ms(
                    run_id, stage, logical_job, retry_ordinal, None, self._clock()
                )
                await self._sleeper(delay / 1000)
                continue
            except httpx.RequestError as exc:
                raise _error("PROVIDER_NETWORK_ERROR", "provider request failed") from exc

            if response.status_code == 200:
                decoded = await self._decode_response(response, expected_size)
                return ProviderImageResult(
                    image_bytes=decoded,
                    provider_request_id=_provider_evidence_id(response),
                    latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                    submitted_request_count=budget.submitted_image_request_count,
                )
            if allow_retry and response.status_code in RETRYABLE_STATUSES:
                if not _request_budget_has_capacity(budget, stage):
                    raise _budget_exhausted_error(response)
                if retry_ordinal < 2:
                    retry_ordinal += 1
                    delay = retry_delay_ms(
                        run_id,
                        stage,
                        logical_job,
                        retry_ordinal,
                        response.headers.get("Retry-After"),
                        self._clock(),
                    )
                    await self._sleeper(delay / 1000)
                    continue
            raise _error("PROVIDER_HTTP_ERROR", "provider returned an unsuccessful status", response=response)


def capability_gate(
    receipt: CapabilityReceipt,
    expected: CapabilityContract,
    revocation_path: Path,
    clock: Callable[[], datetime],
) -> Callable[[], None]:
    return lambda: validate_capability_receipt(receipt, clock(), expected, revocation_path)


def wire_gate(
    receipt: WireReceipt,
    expected: CapabilityContract,
    clock: Callable[[], datetime],
) -> Callable[[], None]:
    return lambda: validate_wire_receipt(receipt, clock(), expected)


def revoke_capability(
    directory: Path,
    *,
    receipt_id: str,
    reason_code: str,
    observed_at: datetime,
    provider_request_id: str | None,
    actor: str,
) -> CapabilityRevocation:
    payload = {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "reason_code": reason_code,
        "observed_at": observed_at,
        "provider_request_id": provider_request_id,
        "actor": actor,
    }
    payload["revocation_id"] = content_id(payload, "revocation_id")
    revocation = CapabilityRevocation.model_validate(payload)
    create_revocation_tombstone(directory, revocation)
    return revocation
