"""P2-F — executable adapter conformance/scoring gate (PRD P0/P2 adapter gate).

The gate scores a candidate runtime adapter on five weighted dimensions (three
mandatory) and only SELECTS one that scores ≥80/100 AND passes every mandatory
dimension. These tests prove the framework SCORES and ELIMINATES correctly using
deterministic fake candidates — they do NOT select a real adapter (that needs
real runtime endpoints, which are unconfigured; see the ADR). The honest
"undecided → Mock only" state is pinned by the ADR-existence test.

Design note: ``run_conformance`` probes a candidate that satisfies a small
duck-typed contract (handshake_manifest / emit_tool_intent / provider_events /
subagent_child_caps / bypass_broker / accepts_infra_handles / license path +
optional cancel/health hooks). A real Hermes/Grok adapter would ship a thin
conformance shim exposing the same hooks.
"""
import os
from pathlib import Path

import pytest

from app.config import settings
from app.lab import adapter_gate as gate
from app.lab import supervision
from app.lab.protocol import HandshakeManifest, RunEventEnvelope
from app.lab.sandbox.base import HttpAgentAdapter, HttpHandle, RunSpec
from app.models.lab_event import LabRunEvent
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from sqlalchemy import func, select


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _grant_secret(monkeypatch):
    monkeypatch.setattr(settings, "lab_grant_secret", "test-secret", raising=False)
    monkeypatch.setattr(settings, "lab_policy_version", "lab-policy-v1", raising=False)


# ── deterministic fake candidates (the runtimes under test) ───────────

class _Candidate:
    """A fully-compliant fake candidate."""
    name = "compliant"
    bypass_broker = False
    accepts_infra_handles = False
    license_manifest_path = None

    def handshake_manifest(self) -> HandshakeManifest:
        return HandshakeManifest(protocol_version=1, runtime=self.name, runtime_version="1",
                                 capabilities=["broker_mediation", "streaming"])

    def emit_tool_intent(self):
        return ("web.search", {"query": "conformance"})

    def provider_events(self):
        return [(1, {"summary": "a"}), (2, {"summary": "b"}), (3, {"summary": "c"})]

    def subagent_child_caps(self, parent_caps):
        return list(parent_caps)[:1]  # a proper subset — attenuated


class _BrokerBypass(_Candidate):
    name = "broker_bypass"
    bypass_broker = True             # can cause effects outside the Broker → mandatory fail


class _IsolationLeak(_Candidate):
    name = "isolation_leak"
    accepts_infra_handles = True     # constructor takes DB/Redis/world creds → mandatory fail


class _SubagentEscalation(_Candidate):
    name = "subagent_escalation"

    def subagent_child_caps(self, parent_caps):
        return list(parent_caps) + ["admin"]  # child exceeds parent → escalation


class _NoSubagent(_Candidate):
    name = "no_subagent"

    def subagent_child_caps(self, parent_caps):
        return None                  # feature not supported → 0 + "not supported"


class _CancelThrows(_Candidate):
    """Cancel/terminate hooks raise; only KILL stops it. Proves the supervision
    fence lands (fail-closed) even when an untrusted runtime's cancel faults —
    the P2-D review hand-off item folded into disconnect_replay_cancel."""
    name = "cancel_throws"

    def __init__(self):
        self._alive = True

    async def cancel(self, handle):
        raise RuntimeError("cancel boom")

    async def terminate(self, handle):
        raise RuntimeError("terminate boom")

    async def kill(self, handle):
        self._alive = False

    async def health(self, handle):
        return {"alive": self._alive}


def _results(**over) -> list[gate.DimensionResult]:
    """Full-marks DimensionResults, overridable per key (score or (score,ev))."""
    out = []
    for dim in gate.GATE_DIMENSIONS:
        v = over.get(dim.key, 1.0)
        score, ev = (v if isinstance(v, tuple) else (v, "ok"))
        out.append(gate.DimensionResult(key=dim.key, score=score, evidence=ev))
    return out


# ── 1 & 2. score_candidate: threshold + mandatory elimination ─────────

def test_score_full_marks_selected():
    v = gate.score_candidate("cand", _results())
    assert v.total == 100.0
    assert v.passed_mandatory is True and v.eliminated is False
    assert v.selected is True


def test_score_mandatory_zero_eliminates_even_with_others_full():
    v = gate.score_candidate("cand", _results(broker_mediation=0.0))
    assert v.passed_mandatory is False and v.eliminated is True
    assert v.selected is False                      # eliminated dominates the total


def test_score_below_threshold_not_selected():
    v = gate.score_candidate("cand", _results(**{d.key: 0.79 for d in gate.GATE_DIMENSIONS}))
    assert v.passed_mandatory is True               # 0.79 ≥ mandatory threshold
    assert round(v.total, 2) == 79.0
    assert v.selected is False                      # 79 < 80


def test_score_exactly_at_threshold_selected():
    v = gate.score_candidate("cand", _results(**{d.key: 0.80 for d in gate.GATE_DIMENSIONS}))
    assert round(v.total, 2) == 80.0
    assert v.selected is True


def test_score_mandatory_below_threshold_eliminates():
    # isolated_deployment 0.5 (< 0.6 mandatory threshold) → eliminated.
    v = gate.score_candidate("cand", _results(isolated_deployment=0.5))
    assert v.passed_mandatory is False and v.eliminated is True and v.selected is False


# ── 3. run_conformance: compliant vs broker-bypass ────────────────────

@pytest.mark.anyio
async def test_run_conformance_compliant_passes_mandatory(db_session, tmp_path):
    cand = _Candidate()
    cand.license_manifest_path = str(tmp_path / "license.json")
    Path(cand.license_manifest_path).write_text("{}")

    results = await gate.run_conformance(cand, db=db_session)
    by = {r.key: r for r in results}
    for key in ("broker_mediation", "disconnect_replay_cancel", "isolated_deployment"):
        assert by[key].score >= 0.6, f"{key}={by[key].score} evidence={by[key].evidence}"

    verdict = gate.score_candidate(cand.name, results)
    assert verdict.passed_mandatory is True


@pytest.mark.anyio
async def test_run_conformance_broker_bypass_eliminated(db_session):
    results = await gate.run_conformance(_BrokerBypass(), db=db_session)
    by = {r.key: r for r in results}
    assert by["broker_mediation"].score < 0.6      # bypass path detected
    verdict = gate.score_candidate("broker_bypass", results)
    assert verdict.eliminated is True and verdict.selected is False


@pytest.mark.anyio
async def test_run_conformance_isolation_leak_eliminated(db_session):
    results = await gate.run_conformance(_IsolationLeak(), db=db_session)
    verdict = gate.score_candidate("isolation_leak", results)
    assert verdict.eliminated is True


@pytest.mark.anyio
async def test_cancel_that_throws_still_fences_scores_partial(db_session):
    # disconnect_replay_cancel stays ≥ mandatory (system fences fail-closed) but
    # loses the cooperation credit (escalated to KILL because hooks threw).
    res = await gate.probe_disconnect_replay_cancel(_CancelThrows(), db=db_session)
    assert res.score >= 0.6                         # fenced → mandatory still satisfied
    assert "kill" in res.evidence                   # cooperation was NOT achieved


# ── 4. subagent attenuation ───────────────────────────────────────────

@pytest.mark.anyio
async def test_subagent_attenuation_subset_high_escalation_zero(db_session):
    good = await gate.probe_subagent_attenuation(_Candidate(), db=db_session)
    assert good.score >= 0.9

    bad = await gate.probe_subagent_attenuation(_SubagentEscalation(), db=db_session)
    assert bad.score == 0.0
    assert "exceed" in bad.evidence.lower() or "escalat" in bad.evidence.lower()

    none = await gate.probe_subagent_attenuation(_NoSubagent(), db=db_session)
    assert none.score == 0.0 and "not supported" in none.evidence.lower()


# ── 5. ADR honestly records the undecided / Mock-only state ───────────

def test_adr_records_unselected_mock_only():
    adr = Path(__file__).resolve().parents[2] / "docs" / "adr" / "ADR-lab-runtime-adapter.md"
    assert adr.exists(), f"ADR missing at {adr}"
    text = adr.read_text(encoding="utf-8")
    assert "Proposed" in text
    # honest undecided state, verbatim-ish anchors the test pins.
    assert "未选型" in text
    assert "Mock" in text
    assert "LAB_HERMES_BASE_URL" in text            # the hard blocker: no real endpoints


# ── 6. HttpAgentAdapter is supervisable via a fake HTTP client ────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Serves two /steps pages then done — no real network."""
    def __init__(self):
        self._pages = [
            {"steps": [{"seq": 1, "phase": "think", "summary": "s1"},
                       {"seq": 2, "phase": "message", "summary": "s2"}], "done": False},
            {"steps": [{"seq": 3, "phase": "message", "summary": "s3"}], "done": True},
        ]
        self._i = 0

    async def get(self, url, headers=None, timeout=None, params=None):
        page = self._pages[min(self._i, len(self._pages) - 1)]
        self._i += 1
        return _FakeResp(page)


@pytest.mark.anyio
async def test_http_adapter_provider_events_feed_supervision_dedup(db_session, monkeypatch):
    db = db_session
    db.add(LabTask(id="tk", issuer_user_id="issuer", title="t"))
    db.add(LabRun(id="rn", task_id="tk", researcher_slug="sage", status="running", adapter="hermes"))
    await db.commit()

    fake_client = _FakeHttpClient()  # singleton, like the real get_client()
    monkeypatch.setattr("app.http.get_client", lambda: fake_client, raising=False)

    adapter = HttpAgentAdapter(base_url="http://fake.local", api_key="k")
    handle = HttpHandle(adapter, "sess1", RunSpec(run_id="rn", task_id="tk", researcher_slug="sage",
                                                  brief="b", scopes=[], budget_usd=0.0))

    session = await supervision.open_session(
        db, run_id="rn",
        manifest=HandshakeManifest(protocol_version=1, runtime="hermes", runtime_version="1",
                                   capabilities=["broker_mediation"]),
    )

    def _builder_for(cursor, ev):
        def build(seq):
            return RunEventEnvelope(
                event_id=f"e{cursor}", tenant_id="issuer", run_id="rn", task_id="tk",
                seq=seq, type="plan.updated", actor="hermes", fencing_epoch=0,
                policy_version="lab-policy-v1", occurred_at=__import__("datetime").datetime.now(
                    __import__("datetime").UTC),
                payload={"summary": ev.summary},
            )
        return build

    seen = []
    async for cursor, ev in adapter.read_provider_events(handle):
        seen.append(cursor)
        await supervision.ingest_provider_event(db, session, provider_cursor=cursor,
                                                envelope_builder=_builder_for(cursor, ev))

    assert seen == [1, 2, 3]                         # provider cursors surfaced from polling
    # A replay of cursor 2 dedups to no new row (supervision + ledger).
    replay = await supervision.ingest_provider_event(
        db, session, provider_cursor=2, envelope_builder=_builder_for(2, type("E", (), {"summary": "s2"})()))
    assert replay is None
    count = (await db.execute(
        select(func.count()).select_from(LabRunEvent).where(LabRunEvent.run_id == "rn")
    )).scalar_one()
    assert count == 3                               # exactly one canonical row per distinct cursor
