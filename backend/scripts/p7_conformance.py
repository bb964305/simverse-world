"""Drive the adapter conformance gate against the Simverse reference runtime with
a REAL LLM-driven agent loop, and persist the verdict as V04-V06 evidence."""
import asyncio
import json
import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.database import Base
import app.models  # noqa: F401 — register all models
from app.config import settings
from app.llm.client import get_client
from app.lab import adapter_gate as gate
from app.lab.runtime_ref.agent import RefAgent, anthropic_completer
from app.lab.runtime_ref.candidate import SimverseRefCandidate


async def main():
    settings.lab_grant_secret = "p7-real-secret"
    settings.lab_policy_version = "lab-policy-v1"
    eng = create_async_engine(settings.database_url)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    completer = anthropic_completer(get_client("system"), settings.llm_model)
    result = await RefAgent(complete=completer, max_steps=3).run(
        brief="research cyberpunk aesthetics: name key visual motifs",
        scopes=["web_search", "browse", "code"])
    print("REAL agent loop: steps=%d tool_intents=%s tokens=%d"
          % (len(result.steps), result.tool_intents, result.model_tokens))

    candidate = SimverseRefCandidate(result)
    async with Session() as db:
        results = await gate.run_conformance(candidate, db=db)
    verdict = gate.score_candidate(
        candidate.name, results,
        tie_break={"credential_surface": "one model-endpoint secret", "ops_burden": "low"})

    print("\n=== P7 conformance verdict (REAL LLM-driven candidate) ===")
    for r in results:
        print("  %-26s score=%.2f  %s" % (r.key, r.score, r.evidence[:90]))
    print("  TOTAL=%.1f passed_mandatory=%s eliminated=%s SELECTED=%s"
          % (verdict.total, verdict.passed_mandatory, verdict.eliminated, verdict.selected))

    ev = {
        "candidate": verdict.candidate, "total": verdict.total,
        "passed_mandatory": verdict.passed_mandatory, "selected": verdict.selected,
        "per_dimension": [{"key": r.key, "score": r.score, "evidence": r.evidence} for r in results],
        "real_agent": {"steps": len(result.steps), "tool_intents": result.tool_intents,
                       "model_tokens": result.model_tokens},
        "llm_endpoint_protocol": "anthropic-messages-compatible",
        "model": settings.llm_model,
    }
    out = os.environ.get("P7_EVIDENCE_OUT", "/tmp/p7ev/verdict.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(ev, f, indent=2, ensure_ascii=False)
    print("\nEVIDENCE_WRITTEN=" + out)


if __name__ == "__main__":
    asyncio.run(main())
