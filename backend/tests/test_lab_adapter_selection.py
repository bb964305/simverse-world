"""T1 / recovery-plan Phase 1 — adapter-selection fail-closed guard.

No real runtime endpoint is configured in this session (the archived ADR
`archive/2026-07-25/docs/adr/ADR-lab-runtime-adapter.md` stays 未选型 / undecided;
the written block is `archive/2026-07-25/docs/adr/T1-P2-blocking-report.md`).
These tests machine-assert the
honest state the ADR describes, so a future change can't silently enable or
fabricate a real adapter while it is unconfigured:

- the default runtime is Mock and OCI is off;
- each real HTTP adapter is import-safe but *fail-closed* — ``start()`` raises
  ``LabAdapterUnconfigured`` while its ``base_url`` is empty, so a run can never
  proceed against a runtime we cannot actually exercise;
- adapter *resolution* is fail-closed: only an explicit ``mock`` selects Mock.
  An unknown name, an empty/implicit name, or a real adapter whose import fails
  raises ``LabAdapterUnavailable`` — the registry must NOT degrade to Mock, so a
  configured real runtime cannot silently execute Mock work.

If real endpoints are later provisioned, these assertions on the *default*
(empty) settings still hold under test config; the live evaluation is a
separate, opt-in step documented in the ADR.
"""
import pytest

from app.config import Settings, settings
from app.lab.sandbox import get_adapter
from app.lab.sandbox.base import (
    LabAdapterUnavailable,
    LabAdapterUnconfigured,
    RunSpec,
)
from app.lab.sandbox.computer_use import ComputerUseAdapter
from app.lab.sandbox.codex import CodexAdapter
from app.lab.sandbox.hermes import HermesAdapter
from app.lab.sandbox.mock import MockAdapter
from app.lab.sandbox.openclaw import OpenClawAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _lab_code_defaults(monkeypatch):
    """Exercise fail-closed code defaults, independent of dotenv or OS config."""
    defaults = Settings.model_construct()
    for name in (
        "lab_adapter",
        "lab_oci_enabled",
        "lab_hermes_base_url",
        "lab_openclaw_base_url",
        "lab_computer_use_base_url",
        "lab_codex_base_url",
        "lab_simverse_ref_base_url",
    ):
        monkeypatch.setattr(settings, name, getattr(defaults, name))
    return defaults


def _spec() -> RunSpec:
    return RunSpec(
        run_id="r-1", task_id="t-1", researcher_slug="sage",
        brief="probe", scopes=["fs.read"], budget_usd=0.5,
    )


def test_default_runtime_is_mock_and_oci_off():
    # The honest default the ADR fixes in writing: Mock is the only enabled
    # runtime, OCI real execution stays gated off.
    assert settings.lab_adapter == "mock"
    assert settings.lab_oci_enabled is False


def test_real_endpoints_are_unconfigured():
    # The exact blocker recorded in the ADR / blocking report.
    assert settings.lab_hermes_base_url == ""
    assert settings.lab_openclaw_base_url == ""
    assert settings.lab_computer_use_base_url == ""
    assert settings.lab_codex_base_url == ""


@pytest.mark.anyio
@pytest.mark.parametrize(
    "adapter_cls", [OpenClawAdapter, HermesAdapter, ComputerUseAdapter, CodexAdapter]
)
async def test_real_adapter_start_is_fail_closed_when_unconfigured(adapter_cls):
    adapter = adapter_cls()  # import + construct is always safe
    assert adapter.base_url == ""
    with pytest.raises(LabAdapterUnconfigured):
        await adapter.start(_spec())


@pytest.mark.anyio
async def test_codex_start_sends_model_grant_only_on_codex_protocol(monkeypatch):
    import app.http

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"session_id": "provider-session"}

    class Client:
        request_json = None

        async def post(self, _url, **kwargs):
            self.request_json = kwargs["json"]
            return Response()

    client = Client()
    monkeypatch.setattr(app.http, "get_client", lambda: client)
    adapter = CodexAdapter()
    adapter.base_url = "http://codex-runtime"
    spec = _spec()
    spec.tenant_id = "tenant"
    spec.model_tier = "high"
    spec.model_name = "deepseek-v4-pro"
    spec.model_policy_version = "test-policy"
    spec.resource_cpu_cores = 4
    spec.resource_memory_mb = 4096
    spec.model_gateway_base_url = "http://model-gateway/v1"
    spec.model_gateway_token = "run-token"

    handle = await adapter.start(spec)

    assert handle.session_id == "provider-session"
    assert client.request_json["tenant_id"] == "tenant"
    assert client.request_json["model_name"] == "deepseek-v4-pro"
    assert client.request_json["model_gateway_token"] == "run-token"
    assert client.request_json["resource_cpu_cores"] == 4
    assert client.request_json["resource_memory_mb"] == 4096


def test_registry_returns_mock_only_for_explicit_mock():
    # Explicit mock is the ONLY name that resolves to Mock.
    assert isinstance(get_adapter("mock"), MockAdapter)
    assert isinstance(get_adapter("MOCK"), MockAdapter)


def test_registry_fails_closed_on_unknown_name():
    # Fail-closed: an unknown runtime must raise, NOT degrade to Mock. Otherwise
    # a misconfigured deployment would silently run Mock while claiming a real
    # runtime.
    with pytest.raises(LabAdapterUnavailable):
        get_adapter("no_such_runtime")


def test_registry_fails_closed_on_empty_or_implicit_name():
    # Empty / whitespace / None is not an explicit mock selection.
    with pytest.raises(LabAdapterUnavailable):
        get_adapter("")
    with pytest.raises(LabAdapterUnavailable):
        get_adapter("   ")
    with pytest.raises(LabAdapterUnavailable):
        get_adapter(None)  # type: ignore[arg-type]


def test_registry_returns_real_adapters_by_name_still_unconfigured():
    # Named real adapters construct (import-safe) but carry no endpoint, so any
    # run through them will fail-closed at start() — that is fail-closed, not a
    # silent Mock substitution.
    assert isinstance(get_adapter("hermes"), HermesAdapter)
    assert get_adapter("hermes").base_url == ""
    assert isinstance(get_adapter("openclaw"), OpenClawAdapter)
    assert isinstance(get_adapter("computer_use"), ComputerUseAdapter)
    assert isinstance(get_adapter("codex"), CodexAdapter)
    assert get_adapter("codex").base_url == ""


def test_simverse_ref_is_a_real_adapter_and_fail_closed_when_unconfigured():
    # The Phase 7 selected candidate is a real adapter: it constructs by name but
    # carries no endpoint by default, so it is fail-closed until deployed + set.
    from app.lab.sandbox.simverse_ref import SimverseRefAdapter
    adapter = get_adapter("simverse_ref")
    assert isinstance(adapter, SimverseRefAdapter)
    assert adapter.base_url == ""  # unconfigured by default → start() fail-closes
    assert settings.lab_simverse_ref_base_url == ""


def test_registry_reraises_import_failure_as_unavailable(monkeypatch):
    # If a real adapter's import/construct fails, resolution must fail closed,
    # not fall back to Mock.
    import app.lab.sandbox.hermes as hermes_mod

    def _boom(*a, **k):
        raise ImportError("simulated missing optional dependency")

    monkeypatch.setattr(hermes_mod, "HermesAdapter", _boom)
    with pytest.raises(LabAdapterUnavailable):
        get_adapter("hermes")
