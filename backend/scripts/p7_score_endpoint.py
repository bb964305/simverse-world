"""Score a LIVE, configured runtime endpoint through the adapter conformance gate
(recovery plan Phase 7). Usage:

    python scripts/p7_score_endpoint.py --adapter hermes

Reads the adapter from ``get_adapter(name)`` (which reads its base_url/api_key
from settings / .env), drives a real probe run against the live endpoint, and
runs ``adapter_gate.run_conformance`` to produce a real verdict. Refuses to run
if the adapter is unconfigured (fail-closed) — no fabricated scores. Writes the
verdict JSON to ``P7_EVIDENCE_OUT``.
"""
import argparse
import asyncio
import json
import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.database import Base
import app.models  # noqa: F401
from app.config import settings
from app.lab import adapter_gate as gate
from app.lab.sandbox import get_adapter
from app.lab.sandbox.base import RunSpec
from app.lab.runtime_ref.http_candidate import HttpEndpointCandidate


async def score(adapter_name: str) -> dict:
    settings.lab_grant_secret = settings.lab_grant_secret or "p7-score-secret"
    settings.lab_policy_version = settings.lab_policy_version or "lab-policy-v1"
    adapter = get_adapter(adapter_name)
    if getattr(adapter, "base_url", "") == "":
        raise SystemExit(
            f"adapter {adapter_name!r} is unconfigured (empty base_url) — configure "
            f"LAB_{adapter_name.upper()}_BASE_URL/_API_KEY first. Refusing to fabricate a score.")

    eng = create_async_engine(settings.database_url)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    spec = RunSpec(run_id="p7-probe", task_id="p7-probe-task", researcher_slug="gate",
                   brief="research cyberpunk aesthetics: name one visual motif",
                   scopes=["web_search", "browse", "code"], budget_usd=0.5)
    candidate = HttpEndpointCandidate(adapter, name=adapter_name)
    await candidate.prepare(spec)

    async with Session() as db:
        results = await gate.run_conformance(candidate, db=db)
    verdict = gate.score_candidate(adapter_name, results,
                                   tie_break={"source": "live endpoint probe"})

    out = {
        "candidate": verdict.candidate, "total": verdict.total,
        "passed_mandatory": verdict.passed_mandatory, "selected": verdict.selected,
        "per_dimension": [{"key": r.key, "score": r.score, "evidence": r.evidence} for r in results],
        "probe_events": len(candidate.provider_events()),
        "probe_intent": list(candidate.emit_tool_intent()),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    dst = os.environ.get("P7_EVIDENCE_OUT")
    if dst:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print("EVIDENCE_WRITTEN=" + dst)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, help="adapter name (hermes|openclaw|computer_use|simverse_ref)")
    args = ap.parse_args()
    asyncio.run(score(args.adapter))
