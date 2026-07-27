"""P2 — sandbox guardrails (spec §5.3, §11): scope whitelist, sensitive-action
classification (financial hard-deny), budget breaker, redaction, egress/SSRF
allowlist, adapter import-safety, and the runner enforcing them.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import guard
from app.lab.sandbox import isolation
from app.lab.sandbox.base import StepEvent, ArtifactSpec, RunSpec, LabAdapterUnconfigured
from app.lab.sandbox.openclaw import OpenClawAdapter
from app.models.user import User
from app.models.resident import Resident
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_task import LabTask
from app.services import coin_service
from app.services import lab_task_service as svc
from app.lab.runner import run_one
from sqlalchemy import select


# ── guard: scope whitelist ────────────────────────────────────────────

def test_tool_scope_and_allow():
    assert guard.tool_scope("web.search") == "web_search"
    assert guard.tool_scope("browser.navigate") == "browse"
    assert guard.tool_scope("shell.exec") == "code"
    assert guard.tool_scope("unknown.tool") is None
    assert guard.is_tool_allowed("web.search", ["web_search"]) is True
    assert guard.is_tool_allowed("browser.navigate", ["web_search"]) is False
    assert guard.is_tool_allowed(None, []) is True         # think/message steps
    assert guard.is_tool_allowed("mystery.call", ["code"]) is False  # unknown → deny


# ── guard: sensitivity classification ─────────────────────────────────

def test_classify_action():
    assert guard.classify_action("web.search") == "allow"
    assert guard.classify_action("browser.navigate") == "allow"
    assert guard.classify_action("browser.login") == "approval"
    assert guard.classify_action("form.submit") == "approval"
    assert guard.classify_action("payment.charge") == "deny"
    assert guard.classify_action("wallet.transfer") == "deny"
    assert guard.classify_action(None) == "allow"


def test_check_budget():
    assert guard.check_budget(50, 50) is True
    assert guard.check_budget(51, 50) is False
    assert guard.check_budget(9999, 0) is True  # 0 = no cap


# ── guard: redaction ──────────────────────────────────────────────────

def test_redaction_scrubs_secrets():
    assert "[REDACTED]" in guard.redact_text("token: sk-ABCDEF0123456789ghijkl")
    assert "[REDACTED]" in guard.redact_text("email me at bob@example.com")
    out = guard.redact_payload({"api_key": "supersecret", "q": "hello", "nested": {"password": "x"}})
    assert out["api_key"] == "[REDACTED]"
    assert out["nested"]["password"] == "[REDACTED]"
    assert out["q"] == "hello"
    for value in (
        "0123456789abcdef0123456789abcdef",
        "QWxhZGRpbjpPcGVuU2VzYW1lMTIzNDU2Nzg5MDEyMzQ1Njc4OTA=",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJydW4tMSJ9.signature123",
    ):
        assert guard.redact_text(value) == "[REDACTED]"


# ── isolation: egress allowlist + SSRF ────────────────────────────────

def test_egress_allowlist_and_ssrf():
    allow = ["*.wikipedia.org", "api.example.com"]
    assert isolation.is_egress_allowed("https://en.wikipedia.org/wiki/X", allow) is True
    assert isolation.is_egress_allowed("https://api.example.com/v1", allow) is True
    assert isolation.is_egress_allowed("https://evil.com/x", allow) is False
    # SSRF: internal / metadata addresses always blocked, even if allowlisted.
    assert isolation.is_host_blocked("169.254.169.254") is True
    assert isolation.is_host_blocked("127.0.0.1") is True
    assert isolation.is_host_blocked("10.0.0.5") is True
    assert isolation.is_host_blocked("8.8.8.8") is False
    assert isolation.is_egress_allowed("http://169.254.169.254/latest/meta-data", ["*"]) is False


# ── real adapter import-safety ────────────────────────────────────────

@pytest.mark.anyio
async def test_unconfigured_adapter_raises_at_start_not_import():
    # Importing + constructing must work with no base_url; start() then errors.
    adapter = OpenClawAdapter()
    assert adapter.name == "openclaw"
    spec = RunSpec(run_id="r", task_id="t", researcher_slug="s", brief="b", scopes=[], budget_usd=0.1)
    with pytest.raises(LabAdapterUnconfigured):
        await adapter.start(spec)


# ── runner-level enforcement ──────────────────────────────────────────

class _FakeAdapter:
    name = "fake"

    def __init__(self, events, artifacts=None):
        self._events = events
        self._artifacts = artifacts or []
        self.approvals: list[tuple[str, bool]] = []

    async def start(self, spec):
        return self

    async def submit_goal(self, handle, brief, scopes):
        return None

    async def step_stream(self, handle):
        for ev in self._events:
            yield ev

    async def approve(self, handle, approval_id, decision):
        self.approvals.append((approval_id, decision))

    async def collect_artifacts(self, handle):
        return self._artifacts

    async def stop(self, handle):
        return None


@pytest.fixture
def guard_env(db_engine, monkeypatch):
    from app.config import settings
    for k, v in {
        "lab_enabled": True, "lab_adapter": "mock", "lab_creator_share": 0.2,
        "lab_platform_fee_rate": 0.1, "lab_default_budget_usd": 0.5,
        "lab_daily_tasks_per_user": 20, "lab_approval_timeout_s": 0,  # 0 → immediate default-deny
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.lab.runner.async_session", factory), \
         patch("app.services.lab_task_service.async_session", factory), \
         patch("app.services.lab_task_service.emit", new_callable=AsyncMock):
        yield factory


async def _seed_and_publish(factory, scopes):
    async with factory() as s:
        s.add(User(id="issuer", name="I", email="i@t.com", soul_coin_balance=1000))
        s.add(User(id="creator_user", name="C", email="c@t.com", soul_coin_balance=0))
        s.add(Resident(slug="sage", name="Sage", creator_id="creator_user", resident_type="npc",
                       meta_json={"lab": {"access": True}}))
        await s.commit()
    async with factory() as s:
        task = await svc.create_task(s, issuer_id="issuer", title="t", brief="b",
                                     scopes=scopes, reward_sc=100, researcher_slug="sage")
        return task.id, task.accepted_run_id


@pytest.mark.anyio
async def test_financial_action_hard_denied(guard_env):
    factory = guard_env
    fake = _FakeAdapter([
        StepEvent(phase="tool_call", tool="payment.charge", summary="pay $5",
                  approval={"id": "a1"}),
    ])
    tid, rid = await _seed_and_publish(factory, ["web_search"])
    with patch("app.lab.runner.get_adapter", return_value=fake):
        await run_one(rid)
    assert ("a1", False) in fake.approvals  # auto-denied, never on the user's behalf
    async with factory() as s:
        run = await s.get(LabRun, rid)
        assert run.status == "succeeded"  # denied action doesn't crash the run


@pytest.mark.anyio
async def test_scope_violation_fails_and_refunds(guard_env):
    factory = guard_env
    fake = _FakeAdapter([
        StepEvent(phase="tool_call", tool="shell.exec", summary="rm -rf"),  # needs 'code'
    ])
    tid, rid = await _seed_and_publish(factory, ["web_search"])  # only web_search granted
    with patch("app.lab.runner.get_adapter", return_value=fake):
        await run_one(rid)
    async with factory() as s:
        run = await s.get(LabRun, rid)
        task = await s.get(LabTask, tid)
        assert run.status == "failed"
        assert task.status == "failed"
        assert await coin_service.get_balance(s, "issuer") == 1000  # refunded


@pytest.mark.anyio
async def test_step_redacted_before_persist(guard_env):
    factory = guard_env
    fake = _FakeAdapter([
        StepEvent(phase="message", tool=None, summary="key is sk-ABCDEF0123456789ghijkl done"),
    ])
    tid, rid = await _seed_and_publish(factory, ["web_search"])
    with patch("app.lab.runner.get_adapter", return_value=fake):
        await run_one(rid)
    async with factory() as s:
        steps = (await s.execute(select(LabRunStep).where(LabRunStep.run_id == rid))).scalars().all()
        assert any("[REDACTED]" in st.summary and "sk-ABCDEF" not in st.summary for st in steps)


@pytest.mark.anyio
async def test_sensitive_action_times_out_to_deny(guard_env):
    factory = guard_env  # lab_approval_timeout_s == 0 → immediate default-deny
    fake = _FakeAdapter([
        StepEvent(phase="tool_call", tool="browser.login", summary="log in",
                  approval={"id": "b1"}),
    ])
    tid, rid = await _seed_and_publish(factory, ["browse"])
    with patch("app.lab.runner.get_adapter", return_value=fake):
        await run_one(rid)
    assert ("b1", False) in fake.approvals  # no human attached → default-deny
