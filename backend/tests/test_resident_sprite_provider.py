from __future__ import annotations

import base64
import hashlib
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from PIL import Image

import app.services.resident_sprite_provider as provider_module
from app.services.resident_sprite_generation import (
    CapabilityContract,
    CapabilityCostAuthorization,
    CapabilityReceipt,
    CapabilityRevocation,
    RequestBudget,
    ResidentSpriteContractError,
    WireReceipt,
    WireRequestShape,
    content_id,
    create_revocation_tombstone,
    new_run_id,
    validate_capability_receipt,
    validate_wire_receipt,
)
from app.services.resident_sprite_provider import (
    ProviderConfig,
    ProviderError,
    QualificationBudget,
    ResidentSpriteProvider,
    calibration_anchor_png,
    decode_image_response,
    download_public_image_url,
    normalize_origin,
    parse_retry_after_ms,
    retry_delay_ms,
    retry_jitter_ms,
)


NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
FAKE_SECRET = "sk-test-super-secret-marker"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def png_bytes(size: tuple[int, int], color: str = "#FF00FF") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def image_response(size: tuple[int, int], request_id: str = "req-1") -> httpx.Response:
    encoded = base64.b64encode(png_bytes(size)).decode()
    return httpx.Response(
        200,
        headers={"x-request-id": request_id},
        json={"data": [{"b64_json": encoded}]},
    )


def provider(handler) -> tuple[ResidentSpriteProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ResidentSpriteProvider(
        ProviderConfig(
            base_url="HTTPS://Example.COM/v1/",
            api_key=FAKE_SECRET,
            model="gpt-image-2",
        ),
        client,
        clock=lambda: NOW,
    )
    return adapter, client


def wire_receipt(contract: CapabilityContract, expires_at: datetime) -> WireReceipt:
    payload = {
        "schema_version": 1,
        "normalized_origin": contract.normalized_origin,
        "model_alias": contract.model_alias,
        "transport_security": contract.transport_security,
        "adapter_version": contract.adapter_version,
        "endpoint": contract.edit_endpoint,
        "multipart_field": contract.multipart_field,
        "request_shape": WireRequestShape(),
        "output_dimensions": contract.strip_dimensions,
        "calibration_source_sha256": "a" * 64,
        "calibration_output_sha256": "b" * 64,
        "provider_request_ids": ["req-wire"],
        "submitted_request_count": 1,
        "cost_authorization": CapabilityCostAuthorization(
            price_per_request_upper_bound_usd="0.10",
            max_cost_usd="0.70",
            cost_source="provider-price-test",
        ),
        "operator": "operator-a",
        "observed_at": NOW,
        "expires_at": expires_at,
    }
    draft = WireReceipt.model_construct(**payload, wire_receipt_id="")
    return WireReceipt(**payload, wire_receipt_id=content_id(draft, "wire_receipt_id"))


def capability_receipt(contract: CapabilityContract, expires_at: datetime) -> CapabilityReceipt:
    payload = {
        **contract.model_dump(),
        "schema_version": 1,
        "wire_receipt_id": "a" * 64,
        "probe_id": "probe-1",
        "qualification_id": new_run_id(),
        "operator": "operator-a",
        "reviewer": "reviewer-b",
        "qualified_at": NOW,
        "expires_at": expires_at,
        "evidence_sha256": ["b" * 64],
        "provider_request_ids": ["req-1", "req-2", "req-3", "req-4", "req-5"],
        "blind_scores": [],
        "latency_ms": [1, 2, 3, 4, 5],
        "capability_request_count": 6,
        "capability_cost_upper_bound_usd": "0.70",
        "cost_source": "provider-unavailable",
    }
    draft = CapabilityReceipt.model_construct(**payload, receipt_id="")
    return CapabilityReceipt(**payload, receipt_id=content_id(draft, "receipt_id"))


def revocation(receipt_id: str) -> CapabilityRevocation:
    payload = {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "reason_code": "EDIT_UNSUPPORTED",
        "observed_at": NOW,
        "provider_request_id": "req-revoke",
        "actor": "operator-a",
    }
    draft = CapabilityRevocation.model_construct(**payload, revocation_id="")
    return CapabilityRevocation(**payload, revocation_id=content_id(draft, "revocation_id"))


def test_origin_normalization_and_rejection() -> None:
    assert normalize_origin("HTTPS://EXAMPLE.COM/v1/") == "https://example.com:443/v1"
    assert normalize_origin("http://localhost/v1") == "http://localhost:80/v1"
    assert normalize_origin("http://127.0.0.1") == "http://127.0.0.1:80/"
    assert normalize_origin("https://b\u00fccher.example/api") == "https://xn--bcher-kva.example:443/api"
    assert normalize_origin(
        "http://example.com/v1", allow_insecure_http_test=True
    ) == "http://example.com:80/v1"
    insecure = ProviderConfig(
        "http://example.com/v1",
        FAKE_SECRET,
        "gpt-image-2",
        allow_insecure_http_test=True,
    )
    assert insecure.transport_security == "insecure_http_test"
    assert ProviderConfig(
        "https://example.com/v1",
        FAKE_SECRET,
        "gpt-image-2",
        allow_insecure_http_test=True,
    ).transport_security == "insecure_http_test"
    for bad in (
        "ftp://example.com/v1",
        "http://example.com/v1",
        "https://user@example.com/v1",
        "https://example.com/v1?token=x",
        "https://example.com/v1#fragment",
    ):
        with pytest.raises(ResidentSpriteContractError):
            normalize_origin(bad)


@pytest.mark.anyio
async def test_provider_exposes_read_only_model_alias_without_secret() -> None:
    adapter, client = provider(lambda request: image_response((1024, 1024)))
    try:
        assert adapter.model_alias == "gpt-image-2"
        assert FAKE_SECRET not in repr(adapter)
        assert FAKE_SECRET not in repr(adapter._config)
        with pytest.raises(AttributeError) as exc:
            adapter.model_alias = FAKE_SECRET  # type: ignore[misc]
        assert FAKE_SECRET not in str(exc.value)
        assert adapter.model_alias == "gpt-image-2"
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_anchor_request_has_exact_json_and_fixed_endpoint() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = __import__("json").loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return image_response((1024, 1024))

    adapter, client = provider(handler)
    try:
        result = await adapter.generate_anchor(
            "anchor prompt", run_id=new_run_id(), budget=RequestBudget()
        )
    finally:
        await client.aclose()
    assert seen["url"] == "https://example.com/v1/images/generations"
    assert seen["json"] == {
        "model": "gpt-image-2",
        "prompt": "anchor prompt",
        "n": 1,
        "size": "1024x1024",
        "quality": "medium",
    }
    assert seen["auth"] == f"Bearer {FAKE_SECRET}"
    assert result.provider_request_id == "req-1"


@pytest.mark.anyio
async def test_edit_request_has_exact_multipart_shape() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["content_type"] = request.headers["content-type"]
        return image_response((1536, 1024))

    adapter, client = provider(handler)
    try:
        await adapter.edit_strip(
            b"PNG-ANCHOR",
            "strip prompt",
            multipart_field="image[]",
            run_id=new_run_id(),
            stage="down",
            logical_job="down-1",
            budget=RequestBudget(),
            gate=lambda: None,
        )
    finally:
        await client.aclose()
    body = seen["body"]
    assert seen["content_type"].startswith("multipart/form-data; boundary=")
    for name, value in {
        b'image[]': b"PNG-ANCHOR",
        b'model': b"gpt-image-2",
        b'prompt': b"strip prompt",
        b'n': b"1",
        b'size': b"1536x1024",
        b'quality': b"high",
    }.items():
        assert b'name="' + name + b'"' in body
        assert value in body
    assert b'filename="anchor.png"' in body
    assert b"Content-Type: image/png" in body
    assert b"response_format" not in body


@pytest.mark.anyio
async def test_probe_resume_accounts_prior_rejection_and_submits_once() -> None:
    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return image_response((1536, 1024), "req-ok")

    adapter, client = provider(handler)
    try:
        result, field_name = await adapter.probe_wire(
            "down prompt", prior_submitted_request_count=1
        )
    finally:
        await client.aclose()
    assert field_name == "image[]"
    assert result.submitted_request_count == 2
    assert len(requests) == 1


@pytest.mark.anyio
async def test_probe_negotiates_only_on_explicit_pre_job_rejection() -> None:
    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        if len(requests) == 1:
            return httpx.Response(422, json={"error": "field"})
        return image_response((1536, 1024), "req-ok")

    adapter, client = provider(handler)
    try:
        result, field_name = await adapter.probe_wire("down prompt")
    finally:
        await client.aclose()
    assert field_name == "image"
    assert result.submitted_request_count == 2
    assert b'name="image[]"' in requests[0]
    assert b'name="image"' in requests[1]


@pytest.mark.anyio
async def test_probe_does_not_fallback_after_job_id() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(422, headers={"x-request-id": "job-issued"}, json={})

    adapter, client = provider(handler)
    try:
        with pytest.raises(ProviderError):
            await adapter.probe_wire("down prompt")
    finally:
        await client.aclose()
    assert calls == 1


def test_calibration_anchor_exact_geometry_and_is_programmatic() -> None:
    raw = calibration_anchor_png()
    with Image.open(io.BytesIO(raw)) as image:
        assert image.mode == "RGB"
        assert image.size == (1024, 1024)
        assert image.getpixel((0, 0)) == (255, 0, 255)
        assert image.getpixel((512, 288)) == (0, 255, 255)
        assert image.getpixel((512, 500)) == (255, 215, 0)
        assert image.getpixel((384, 288)) == (0, 0, 0)
        assert image.getpixel((450, 800)) == (255, 215, 0)
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(calibration_anchor_png()).hexdigest()


def test_decode_accepts_only_b64_png_and_exact_dimensions(monkeypatch) -> None:
    response = image_response((1024, 1024))
    assert decode_image_response(response, (1024, 1024)).startswith(b"\x89PNG")
    cases = [
        (httpx.Response(200, json={"data": [{"url": "https://attacker.invalid/x"}]}), "PROVIDER_URL_RESPONSE_UNSUPPORTED"),
        (httpx.Response(200, json={"data": []}), "PROVIDER_RESPONSE_INVALID"),
        (httpx.Response(200, json={"data": [{"b64_json": "%%%"}]}), "PROVIDER_BASE64_INVALID"),
        (httpx.Response(200, content=b"not-json"), "PROVIDER_RESPONSE_INVALID"),
        (image_response((10, 10)), "PROVIDER_DIMENSIONS"),
    ]
    for bad_response, code in cases:
        with pytest.raises(ProviderError) as exc:
            decode_image_response(bad_response, (1024, 1024))
        assert exc.value.error.code == code
    monkeypatch.setattr("app.services.resident_sprite_provider.MAX_ENCODED_BYTES", 3)
    with pytest.raises(ProviderError) as exc:
        decode_image_response(response, (1024, 1024))
    assert exc.value.error.code == "PROVIDER_ENCODED_TOO_LARGE"


def test_decode_normalizes_only_bounded_provider_dimensions() -> None:
    square = image_response((1024, 1024))
    landscape = decode_image_response(
        square,
        (1536, 1024),
        allow_dimension_normalization=True,
    )
    portrait = decode_image_response(
        square,
        (1024, 1536),
        allow_dimension_normalization=True,
    )
    with Image.open(io.BytesIO(landscape)) as image:
        assert image.size == (1536, 1024)
    with Image.open(io.BytesIO(portrait)) as image:
        assert image.size == (1024, 1536)
        assert image.getpixel((0, 0))[:3] == (255, 0, 255)
    with pytest.raises(ProviderError) as exc:
        decode_image_response(
            image_response((640, 640)),
            (1536, 1024),
            allow_dimension_normalization=True,
        )
    assert exc.value.error.code == "PROVIDER_DIMENSIONS"
    with pytest.raises(ProviderError) as exc:
        decode_image_response(
            image_response((2048, 512)),
            (1536, 1024),
            allow_dimension_normalization=True,
        )
    assert exc.value.error.code == "PROVIDER_DIMENSIONS"


@pytest.mark.anyio
async def test_provider_accepts_url_response_only_through_injected_fetcher() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "req-url"},
            json={"data": [{"url": "https://images.example/output.png?signature=opaque"}]},
        )

    async def fetcher(url, expected_size, provider_response, timeout):
        observed.update(
            url=url,
            expected_size=expected_size,
            request_id=provider_response.headers["x-request-id"],
            timeout=timeout,
        )
        return png_bytes(expected_size)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ResidentSpriteProvider(
        ProviderConfig(
            base_url="https://provider.example/v1",
            api_key=FAKE_SECRET,
            model="gpt-image-2",
        ),
        client,
        image_url_fetcher=fetcher,
    )
    try:
        result = await adapter.generate_anchor(
            "anchor prompt", run_id=new_run_id(), budget=RequestBudget()
        )
    finally:
        await client.aclose()
    assert observed == {
        "url": "https://images.example/output.png?signature=opaque",
        "expected_size": (1024, 1024),
        "request_id": "req-url",
        "timeout": 180.0,
    }
    assert result.provider_request_id == "req-url"


@pytest.mark.anyio
async def test_url_response_without_request_id_uses_only_url_digest() -> None:
    result_url = "https://images.example/output.png?signature=secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": result_url}]})

    async def fetcher(url, expected_size, provider_response, timeout):
        assert url == result_url
        return png_bytes(expected_size)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ResidentSpriteProvider(
        ProviderConfig(
            base_url="https://provider.example/v1",
            api_key=FAKE_SECRET,
            model="gpt-image-2",
        ),
        client,
        image_url_fetcher=fetcher,
    )
    try:
        result = await adapter.generate_anchor(
            "anchor prompt", run_id=new_run_id(), budget=RequestBudget()
        )
    finally:
        await client.aclose()
    expected = hashlib.sha256(result_url.encode("utf-8")).hexdigest()
    assert result.provider_request_id == f"result-url-sha256:{expected}"
    assert "secret-value" not in result.provider_request_id


@pytest.mark.anyio
async def test_public_image_download_rejects_private_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (provider_module.socket.AF_INET, provider_module.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    provider_response = httpx.Response(200, json={})
    with pytest.raises(ProviderError) as exc:
        await download_public_image_url(
            "https://images.example/output.png",
            (1024, 1024),
            provider_response,
            10,
        )
    assert exc.value.error.code == "PROVIDER_IMAGE_HOST_FORBIDDEN"


@pytest.mark.anyio
async def test_public_image_download_omits_auth_and_validates_png(monkeypatch) -> None:
    async def public_host(host, port):
        assert (host, port) == ("images.example", 443)

    observed = {}

    class StreamResponse:
        status_code = 200
        headers = {"Content-Type": "image/png"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def aiter_bytes(self):
            yield png_bytes((1536, 512))

    class DownloadClient:
        def __init__(self, **kwargs):
            observed["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        def stream(self, method, url, *, headers):
            observed.update(method=method, url=url, headers=headers)
            return StreamResponse()

    monkeypatch.setattr(provider_module, "_require_public_host", public_host)
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", DownloadClient)
    provider_response = httpx.Response(
        200,
        headers={"x-request-id": "req-provider"},
        json={},
    )
    result = await download_public_image_url(
        "https://images.example/output.png?signature=opaque",
        (1536, 512),
        provider_response,
        20,
    )
    assert result.startswith(b"\x89PNG")
    assert observed["method"] == "GET"
    assert observed["headers"] == {"Accept": "image/png"}
    assert "Authorization" not in observed["headers"]
    assert observed["client_kwargs"] == {
        "follow_redirects": False,
        "trust_env": False,
        "timeout": 20,
    }


def test_wire_and_capability_expiry_boundaries_and_compatibility(tmp_path) -> None:
    contract = CapabilityContract(
        normalized_origin="https://example.com:443/v1",
        model_alias="gpt-image-2",
        multipart_field="image[]",
    )
    wire = wire_receipt(contract, NOW + timedelta(hours=24))
    validate_wire_receipt(wire, wire.expires_at, contract)
    with pytest.raises(ResidentSpriteContractError) as exc:
        validate_wire_receipt(wire, wire.expires_at + timedelta(microseconds=1), contract)
    assert exc.value.code == "WIRE_RECEIPT_EXPIRED"

    receipt = capability_receipt(contract, NOW + timedelta(days=30))
    tombstone = tmp_path / "missing.json"
    validate_capability_receipt(receipt, receipt.expires_at, contract, tombstone)
    with pytest.raises(ResidentSpriteContractError) as exc:
        validate_capability_receipt(
            receipt, receipt.expires_at + timedelta(microseconds=1), contract, tombstone
        )
    assert exc.value.code == "CAPABILITY_EXPIRED"
    changed = contract.model_copy(update={"strip_dimensions": (1535, 1024)})
    with pytest.raises(ResidentSpriteContractError) as exc:
        validate_capability_receipt(receipt, NOW, changed, tombstone)
    assert exc.value.code == "CAPABILITY_INCOMPATIBLE"


def test_revocation_is_create_exclusive_and_existing_must_validate(tmp_path) -> None:
    item = revocation("a" * 64)
    path = create_revocation_tombstone(tmp_path, item)
    original = path.read_bytes()
    assert create_revocation_tombstone(tmp_path, item) == path
    assert path.read_bytes() == original
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ResidentSpriteContractError) as exc:
        create_revocation_tombstone(tmp_path, item)
    assert exc.value.code == "REVOCATION_INVALID"


def test_request_budgets_increment_before_post_and_lower_ceiling_wins() -> None:
    mirrored = RequestBudget()
    mirrored.consume_before_post("anchor")
    mirrored.consume_before_post("anchor")
    with pytest.raises(ResidentSpriteContractError) as exc:
        mirrored.consume_before_post("anchor")
    assert exc.value.code == "REQUEST_BUDGET_EXHAUSTED"
    assert mirrored.submitted_image_request_count == 2

    generated = RequestBudget(direction_policy="generate_right")
    for stage in ("anchor", "anchor", "down", "down", "down", "left", "left", "left", "up", "up", "up", "right", "right", "right"):
        generated.consume_before_post(stage)
    assert generated.submitted_image_request_count == 14
    with pytest.raises(ResidentSpriteContractError):
        generated.consume_before_post("right")

    qualification = QualificationBudget()
    for _ in range(5):
        qualification.consume_before_post("qualification")
    with pytest.raises(ResidentSpriteContractError):
        qualification.consume_before_post("qualification")


def test_retry_jitter_and_retry_after_are_exact() -> None:
    run_id = "0123456789abcdef0123456789abcdef"
    expected = int.from_bytes(
        hashlib.sha256(f"{run_id}:down:job-1:1".encode()).digest()[:2], "big"
    ) % 251
    assert retry_jitter_ms(run_id, "down", "job-1", 1) == expected
    assert parse_retry_after_ms("7", NOW) == 7000
    assert parse_retry_after_ms("invalid", NOW) == 0
    assert parse_retry_after_ms("Sat, 25 Jul 2026 11:59:59 GMT", NOW) == 0
    assert parse_retry_after_ms("Sat, 25 Jul 2026 12:01:00 GMT", NOW) == 30000
    assert retry_delay_ms(run_id, "down", "job-1", 1, "7", NOW) == 7000


@pytest.mark.anyio
async def test_retry_matrix_and_exact_sleeps() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, headers={"Retry-After": "0"}, json={})
        return image_response((1536, 1024))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    adapter = ResidentSpriteProvider(
        ProviderConfig("https://example.com/v1", FAKE_SECRET, "gpt-image-2"),
        client,
        clock=lambda: NOW,
        sleeper=sleeper,
    )
    run_id = new_run_id()
    try:
        await adapter.edit_strip(
            b"anchor",
            "prompt",
            multipart_field="image[]",
            run_id=run_id,
            stage="down",
            logical_job="down-job",
            budget=RequestBudget(),
            gate=lambda: None,
        )
    finally:
        await client.aclose()
    assert calls == 3
    assert sleeps == [
        (1000 + retry_jitter_ms(run_id, "down", "down-job", 1)) / 1000,
        (2000 + retry_jitter_ms(run_id, "down", "down-job", 2)) / 1000,
    ]


@pytest.mark.anyio
async def test_anchor_retry_stops_at_two_posts_without_extra_sleep() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "0"}, json={})

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ResidentSpriteProvider(
        ProviderConfig("https://example.com/v1", FAKE_SECRET, "gpt-image-2"),
        client,
        clock=lambda: NOW,
        sleeper=sleeper,
    )
    budget = RequestBudget()
    run_id = new_run_id()
    try:
        with pytest.raises(ProviderError) as exc:
            await adapter.generate_anchor(
                "anchor prompt", run_id=run_id, budget=budget, gate=lambda: None
            )
    finally:
        await client.aclose()

    assert exc.value.error.code == "REQUEST_BUDGET_EXHAUSTED"
    assert calls == budget.submitted_image_request_count == 2
    assert sleeps == [
        (1000 + retry_jitter_ms(run_id, "anchor", "anchor", 1)) / 1000
    ]


@pytest.mark.anyio
async def test_direction_retry_stops_at_three_posts_without_extra_sleep() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "0"}, json={})

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ResidentSpriteProvider(
        ProviderConfig("https://example.com/v1", FAKE_SECRET, "gpt-image-2"),
        client,
        clock=lambda: NOW,
        sleeper=sleeper,
    )
    budget = RequestBudget()
    run_id = new_run_id()
    try:
        with pytest.raises(ProviderError) as exc:
            await adapter.edit_strip(
                b"anchor",
                "prompt",
                multipart_field="image[]",
                run_id=run_id,
                stage="down",
                logical_job="down-job",
                budget=budget,
                gate=lambda: None,
            )
    finally:
        await client.aclose()

    assert exc.value.error.code == "REQUEST_BUDGET_EXHAUSTED"
    assert calls == budget.submitted_image_request_count == 3
    assert sleeps == [
        (1000 + retry_jitter_ms(run_id, "down", "down-job", 1)) / 1000,
        (2000 + retry_jitter_ms(run_id, "down", "down-job", 2)) / 1000,
    ]


@pytest.mark.anyio
async def test_preexhausted_budget_returns_provider_error_without_post_or_sleep() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return image_response((1024, 1024))

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ResidentSpriteProvider(
        ProviderConfig("https://example.com/v1", FAKE_SECRET, "gpt-image-2"),
        client,
        sleeper=sleeper,
    )
    budget = RequestBudget()
    budget.consume_before_post("anchor")
    budget.consume_before_post("anchor")
    try:
        with pytest.raises(ProviderError) as exc:
            await adapter.generate_anchor(
                "anchor prompt", run_id=new_run_id(), budget=budget, gate=lambda: None
            )
    finally:
        await client.aclose()

    assert exc.value.error.code == "REQUEST_BUDGET_EXHAUSTED"
    assert calls == 0
    assert sleeps == []


@pytest.mark.anyio
async def test_anchor_timeout_honors_budget_boundary_without_extra_sleep() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ResidentSpriteProvider(
        ProviderConfig("https://example.com/v1", FAKE_SECRET, "gpt-image-2"),
        client,
        clock=lambda: NOW,
        sleeper=sleeper,
    )
    budget = RequestBudget()
    run_id = new_run_id()
    try:
        with pytest.raises(ProviderError) as exc:
            await adapter.generate_anchor(
                "anchor prompt", run_id=run_id, budget=budget, gate=lambda: None
            )
    finally:
        await client.aclose()

    assert exc.value.error.code == "REQUEST_BUDGET_EXHAUSTED"
    assert calls == budget.submitted_image_request_count == 2
    assert sleeps == [
        (1000 + retry_jitter_ms(run_id, "anchor", "anchor", 1)) / 1000
    ]


@pytest.mark.anyio
async def test_timeout_without_retry_is_one_post_and_no_sleep() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ResidentSpriteProvider(
        ProviderConfig("https://example.com/v1", FAKE_SECRET, "gpt-image-2"),
        client,
        sleeper=sleeper,
    )
    budget = RequestBudget()
    try:
        with pytest.raises(ProviderError) as exc:
            await adapter.generate_anchor(
                "anchor prompt",
                run_id=new_run_id(),
                budget=budget,
                gate=lambda: None,
                allow_retry=False,
            )
    finally:
        await client.aclose()

    assert exc.value.error.code == "PROVIDER_TIMEOUT"
    assert calls == budget.submitted_image_request_count == 1
    assert sleeps == []


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 415, 422])
async def test_other_4xx_are_never_retried(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={})

    adapter, client = provider(handler)
    try:
        with pytest.raises(ProviderError):
            await adapter.edit_strip(
                b"anchor",
                "prompt",
                multipart_field="image[]",
                run_id=new_run_id(),
                stage="down",
                logical_job="down-job",
                budget=RequestBudget(),
                gate=lambda: None,
            )
    finally:
        await client.aclose()
    assert calls == 1


def test_secret_is_absent_from_repr_and_sanitized_errors() -> None:
    config = ProviderConfig("https://example.com/v1", FAKE_SECRET, "gpt-image-2")
    assert FAKE_SECRET not in repr(config)
    response = httpx.Response(200, json={"data": [{"url": f"https://x/{FAKE_SECRET}"}]})
    with pytest.raises(ProviderError) as exc:
        decode_image_response(response, (1, 1))
    rendered = repr(exc.value.error.model_dump()) + str(exc.value)
    assert FAKE_SECRET not in rendered
    assert "https://x/" not in rendered
