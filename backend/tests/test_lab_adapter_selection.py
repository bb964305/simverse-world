"""T1 — adapter-selection fail-closed guard (hard-constraint #1).

No real runtime endpoint is configured in this session (ADR
`docs/adr/ADR-lab-runtime-adapter.md` stays 未选型 / undecided; the written
block is `docs/adr/T1-P2-blocking-report.md`). These tests machine-assert the
honest state the ADR describes, so a future change can't silently enable or
fabricate a real adapter while it is unconfigured:

- the default runtime is Mock and OCI is off;
- each real HTTP adapter is import-safe but *fail-closed* — ``start()`` raises
  ``LabAdapterUnconfigured`` while its ``base_url`` is empty, so a run can never
  proceed against a runtime we cannot actually exercise;
- the adapter registry degrades unknown/real-unconfigured names to Mock rather
  than hard-crashing the runner.

If real endpoints are later provisioned, these assertions on the *default*
(empty) settings still hold under test config; the live evaluation is a
separate, opt-in step documented in the ADR.
"""
import pytest

from app.config import settings
from app.lab.sandbox import get_adapter
from app.lab.sandbox.base import LabAdapterUnconfigured, RunSpec
from app.lab.sandbox.computer_use import ComputerUseAdapter
from app.lab.sandbox.hermes import HermesAdapter
from app.lab.sandbox.mock import MockAdapter
from app.lab.sandbox.openclaw import OpenClawAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


@pytest.mark.anyio
@pytest.mark.parametrize("adapter_cls", [OpenClawAdapter, HermesAdapter, ComputerUseAdapter])
async def test_real_adapter_start_is_fail_closed_when_unconfigured(adapter_cls):
    adapter = adapter_cls()  # import + construct is always safe
    assert adapter.base_url == ""
    with pytest.raises(LabAdapterUnconfigured):
        await adapter.start(_spec())


def test_registry_returns_mock_and_degrades_unknown_to_mock():
    assert isinstance(get_adapter("mock"), MockAdapter)
    assert isinstance(get_adapter(""), MockAdapter)
    assert isinstance(get_adapter("no_such_runtime"), MockAdapter)


def test_registry_returns_real_adapters_by_name_still_unconfigured():
    # Named real adapters construct (import-safe) but carry no endpoint, so any
    # run through them will fail-closed at start().
    assert isinstance(get_adapter("hermes"), HermesAdapter)
    assert get_adapter("hermes").base_url == ""
    assert isinstance(get_adapter("openclaw"), OpenClawAdapter)
    assert isinstance(get_adapter("computer_use"), ComputerUseAdapter)
