"""Simverse reference runtime — a REAL LLM-driven agent loop (recovery plan
Phase 7).

This is a genuine, self-hosted agent runtime candidate for the Lab adapter gate:
given a brief + scopes it drives a REAL LLM to produce a bounded, protocol-shaped
sequence of steps (think → tool_call → observation → message) and a terminal
artifact. It is the "inner loop" a real provider owns in the Thin-D architecture.

Trust contract (why this is gate-admissible):
* It ONLY INTENDS tool calls — it emits ``(tool, args)`` and never executes them.
  Every effect is mediated by the Gateway's Broker (the mandatory
  ``broker_mediation`` capability). The loop's "observation" for a tool step is a
  neutral placeholder; the real effect + real observation come from the Broker.
* It holds no infra handle (DB/Redis/world) — it is a plain HTTP/LLM process.
* Tool intents are constrained to the run's granted scopes.

The LLM call is injected as ``complete(messages) -> (text, tokens)`` so the loop
is deterministic under test (a fake completer) and real in production (the
project's Anthropic-compatible endpoint via ``app.llm.client``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.lab.sandbox.base import ArtifactSpec, StepEvent

# scope -> the tool the runtime may INTEND for it (mirrors the Broker's registry
# so an intent under a granted scope maps to a real, brokerable tool).
_SCOPE_TOOL = {
    "web_search": "web.search",
    "browse": "browser.navigate",
    "http": "http.get",
    "code": "code.run",
}

Completer = Callable[[list[dict]], Awaitable[tuple[str, int]]]

_SYSTEM = (
    "You are a research agent inside a sandboxed runtime. You may ONLY intend tool "
    "calls; a Broker executes them. Given a research brief and the tools you are "
    "granted, respond with a STRICT JSON object and nothing else:\n"
    '{"plan": "<one short line>", "tool": "<one granted tool or null>", '
    '"query": "<the tool argument, short>", "conclusion": "<one short line, or empty>"}\n'
    "Choose a tool ONLY from the granted list. If the research is done, set tool to "
    "null and give a conclusion."
)


@dataclass
class AgentResult:
    steps: list[StepEvent]
    artifacts: list[ArtifactSpec]
    tool_intents: list[tuple[str, dict]]  # (tool, args) the loop INTENDED
    model_tokens: int = 0


def _granted_tools(scopes: list[str]) -> list[str]:
    return [_SCOPE_TOOL[s] for s in scopes if s in _SCOPE_TOOL]


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply (tolerant of prose/fences)."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


@dataclass
class RefAgent:
    """A bounded reference agent loop. ``max_steps`` caps the tool rounds so the
    loop always terminates (belt-and-braces with the Gateway budget)."""
    complete: Completer
    max_steps: int = 3
    tokens: int = field(default=0, init=False)

    async def run(self, *, brief: str, scopes: list[str], on_step=None,
                  should_cancel=None) -> AgentResult:
        """Drive the loop. ``on_step(StepEvent)`` (optional) is invoked as each
        step is produced so a server can stream it incrementally; ``should_cancel()``
        (optional) is polled each round so a cooperative cancel stops the loop."""
        granted = _granted_tools(scopes)
        steps: list[StepEvent] = []
        intents: list[tuple[str, dict]] = []

        def _emit(step: StepEvent) -> None:
            steps.append(step)
            if on_step is not None:
                on_step(step)

        transcript: list[dict] = [
            {"role": "user", "content": (
                f"Brief: {brief}\nGranted tools: {granted or ['(none)']}\n"
                "Produce your next action as the strict JSON object.")}
        ]

        conclusion = ""
        for _round in range(self.max_steps):
            if should_cancel is not None and should_cancel():
                break
            text, toks = await self.complete([{"role": "system", "content": _SYSTEM}, *transcript])
            self.tokens += int(toks or 0)
            action = _extract_json(text)
            plan = str(action.get("plan") or "").strip()
            tool = action.get("tool")
            query = str(action.get("query") or "").strip()
            conclusion = str(action.get("conclusion") or "").strip() or conclusion

            _emit(StepEvent(phase="think", summary=plan or "planning", model_tokens=int(toks or 0)))

            if tool and tool in granted:
                args = {"query": query} if query else {}
                intents.append((tool, args))
                # INTENT only — the Broker executes; the runtime records a neutral
                # observation and asks the LLM to continue from it.
                _emit(StepEvent(phase="tool_call", tool=tool, summary=f"intend {tool}", payload=args))
                _emit(StepEvent(phase="observation", summary="(broker will execute; continuing)"))
                transcript.append({"role": "assistant", "content": text})
                transcript.append({"role": "user", "content":
                                   f"The Broker executed {tool}. Continue or conclude (strict JSON)."})
                continue
            # No further tool → wrap up.
            break

        summary = conclusion or "research complete"
        _emit(StepEvent(phase="message", summary=summary))
        artifacts = [ArtifactSpec(kind="text", title="research summary", text_md=summary)]
        return AgentResult(steps=steps, artifacts=artifacts, tool_intents=intents,
                           model_tokens=self.tokens)


def anthropic_completer(client, model: str) -> Completer:
    """A real completer over the project's Anthropic-compatible LLM client.
    Extracts the ``text`` block (a reasoning-model reply carries a separate
    ``thinking`` block first) and reports total tokens."""
    async def _complete(messages: list[dict]) -> tuple[str, int]:
        system = ""
        conv = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                conv.append(m)
        resp = await client.messages.create(
            model=model, max_tokens=600, system=system or None, messages=conv,
        )
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        toks = int(resp.usage.input_tokens + resp.usage.output_tokens)
        return text, toks
    return _complete
