"""Simverse reference runtime — standalone HTTP server (recovery plan Phase 7).

A real, self-hosted agent runtime speaking the Lab HTTP wire protocol
(``HttpAgentAdapter`` in ``app/lab/sandbox/base.py``). It drives the real
``RefAgent`` loop against the project's Anthropic-compatible LLM endpoint and
streams protocol steps back to the Gateway. It holds NO DB/Redis/world handle —
its only outbound credential is the model endpoint — and it only INTENDS tool
calls; the Gateway's Broker mediates every effect.

Run standalone:  ``python -m app.lab.runtime_ref.server``  (default 127.0.0.1:8900)
The Gateway's ``SimverseRefAdapter`` points at this via
``settings.lab_simverse_ref_base_url``.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.lab.runtime_ref.agent import RefAgent, anthropic_completer


@dataclass
class _Session:
    session_id: str
    scopes: list[str]
    steps: list[dict] = field(default_factory=list)   # protocol step dicts with seq
    artifacts: list[dict] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    task: asyncio.Task | None = None


_SESSIONS: dict[str, _Session] = {}


class StartBody(BaseModel):
    run_id: str
    scopes: list[str] = []
    budget_usd: float = 0.5
    egress_allowlist: list[str] = []


class GoalBody(BaseModel):
    brief: str
    scopes: list[str] = []


class ApproveBody(BaseModel):
    approval_id: str
    decision: bool


def _completer():
    from app.llm.client import get_client
    return anthropic_completer(get_client("system"), settings.llm_model)


def create_app(completer_factory=_completer, max_steps: int = 3) -> FastAPI:
    app = FastAPI(title="Simverse Lab reference runtime", version="1.0")

    def _sess(sid: str) -> _Session:
        s = _SESSIONS.get(sid)
        if s is None:
            raise HTTPException(status_code=404, detail="session not found")
        return s

    @app.post("/runs")
    async def start_run(body: StartBody):
        sid = f"ref-{uuid.uuid4().hex[:12]}"
        _SESSIONS[sid] = _Session(session_id=sid, scopes=list(body.scopes))
        return {"session_id": sid}

    @app.post("/runs/{sid}/goal")
    async def submit_goal(sid: str, body: GoalBody):
        s = _sess(sid)
        scopes = body.scopes or s.scopes

        def on_step(step) -> None:
            seq = len(s.steps) + 1
            s.steps.append({
                "seq": seq, "phase": step.phase, "tool": step.tool,
                "summary": step.summary, "payload": step.payload,
                "model_tokens": step.model_tokens, "approval": step.approval,
            })

        # Run the loop to completion here (buffering steps via on_step), so the
        # Gateway's subsequent /steps poll gets every step + done in one shot. This
        # is robust across transports (a fire-and-forget background task is not
        # reliably driven by every ASGI server / test transport). Incremental live
        # streaming during a long run is a noted follow-up; the poll-with-cursor
        # protocol contract holds either way.
        agent = RefAgent(complete=completer_factory(), max_steps=max_steps)
        result = await agent.run(brief=body.brief, scopes=scopes,
                                 on_step=on_step, should_cancel=lambda: s.cancelled)
        s.artifacts = [
            {"kind": a.kind, "title": a.title, "uri": a.uri, "text_md": a.text_md, "meta": a.meta}
            for a in result.artifacts]
        s.done = True
        return {"ok": True}

    @app.get("/runs/{sid}/steps")
    async def get_steps(sid: str, after: int = 0):
        s = _sess(sid)
        fresh = [st for st in s.steps if st["seq"] > after]
        return {"steps": fresh, "done": s.done or s.cancelled}

    @app.post("/runs/{sid}/approve")
    async def approve(sid: str, body: ApproveBody):
        _sess(sid)
        return {"ok": True}

    @app.get("/runs/{sid}/artifacts")
    async def artifacts(sid: str):
        s = _sess(sid)
        return {"artifacts": s.artifacts}

    async def _teardown(s: _Session) -> None:
        s.cancelled = True
        if s.task is not None and not s.task.done():
            s.task.cancel()

    @app.post("/runs/{sid}/stop")
    async def stop(sid: str):
        await _teardown(_sess(sid))
        return {"ok": True}

    @app.post("/runs/{sid}/cancel")
    async def cancel(sid: str):
        await _teardown(_sess(sid))
        return {"ok": True}

    @app.post("/runs/{sid}/terminate")
    async def terminate(sid: str):
        await _teardown(_sess(sid))
        return {"ok": True}

    @app.post("/runs/{sid}/kill")
    async def kill(sid: str):
        await _teardown(_sess(sid))
        return {"ok": True}

    @app.get("/runs/{sid}/health")
    async def health(sid: str):
        s = _sess(sid)
        alive = not (s.done or s.cancelled)
        return {"alive": alive, "cancelled": s.cancelled}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8900, log_level="warning")
