"""Score a second REAL candidate: the reference runtime backed by the user-supplied
commercial endpoint in .env (AGENT_BASE_URL / AGENT_API_KEY / AGENT_MODEL), driven
through a real LLM agent loop, then run through the adapter conformance gate.

Honest framing: AGENT_* is a commercial Anthropic-compatible LLM endpoint
(DashScope Coding, qwen3.7-plus). This scores a runtime candidate BACKED BY that
commercial model — proving the runtime is portable across commercial providers.
It never prints the API key. It refuses if the endpoint is absent.
"""
import asyncio
import json
import os

import anthropic
from dotenv import dotenv_values
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.database import Base
import app.models  # noqa: F401
from app.config import settings
from app.lab import adapter_gate as gate
from app.lab.runtime_ref.agent import RefAgent, anthropic_completer
from app.lab.runtime_ref.candidate import SimverseRefCandidate

_ENV = "/Volumes/data/dev/simverse-world/backend/.env"


async def main():
    v = dotenv_values(_ENV)
    base = (v.get("AGENT_BASE_URL") or "").strip()
    key = (v.get("AGENT_API_KEY") or "").strip()
    model = (v.get("AGENT_MODEL") or "").strip()
    if not (base and key and model):
        raise SystemExit("AGENT_BASE_URL/AGENT_API_KEY/AGENT_MODEL not all set in .env — "
                         "refusing to run (no fabricated score).")
    print("candidate model: %s  endpoint_host: %s  (key redacted)"
          % (model, base.split("/")[2] if "://" in base else "?"))

    settings.lab_grant_secret = settings.lab_grant_secret or "p7-agent-secret"
    settings.lab_policy_version = settings.lab_policy_version or "lab-policy-v1"

    client = anthropic.AsyncAnthropic(api_key=key, base_url=base)
    completer = anthropic_completer(client, model)
    result = await RefAgent(complete=completer, max_steps=3).run(
        brief="research cyberpunk aesthetics: name key visual motifs",
        scopes=["web_search", "browse", "code"])
    print("REAL agent loop (commercial qwen endpoint): steps=%d tool_intents=%d tokens=%d"
          % (len(result.steps), len(result.tool_intents), result.model_tokens))

    eng = create_async_engine(settings.database_url)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    candidate = SimverseRefCandidate(result)
    candidate.name = "agent_qwen"  # distinct candidate id
    async with Session() as db:
        results = await gate.run_conformance(candidate, db=db)
    verdict = gate.score_candidate("agent_qwen", results,
                                   tie_break={"model": model, "provider": "dashscope-coding (commercial)"})

    print("\n=== P7 conformance verdict (commercial qwen3.7 endpoint) ===")
    for r in results:
        print("  %-26s score=%.2f  %s" % (r.key, r.score, r.evidence[:80]))
    print("  TOTAL=%.1f passed_mandatory=%s SELECTED=%s"
          % (verdict.total, verdict.passed_mandatory, verdict.selected))

    out = {
        "candidate": "agent_qwen", "model": model,
        "endpoint_protocol": "anthropic-messages-compatible (commercial)",
        "endpoint_host": base.split("/")[2] if "://" in base else "?",
        "total": verdict.total, "passed_mandatory": verdict.passed_mandatory,
        "selected": verdict.selected,
        "per_dimension": [{"key": r.key, "score": r.score, "evidence": r.evidence} for r in results],
        "real_agent": {"steps": len(result.steps),
                       "tool_intents": [[t, a] for t, a in result.tool_intents],
                       "model_tokens": result.model_tokens},
    }
    dst = os.environ.get("P7_EVIDENCE_OUT", "/tmp/p7ev/verdict-agent.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("\nEVIDENCE_WRITTEN=" + dst)


if __name__ == "__main__":
    asyncio.run(main())
