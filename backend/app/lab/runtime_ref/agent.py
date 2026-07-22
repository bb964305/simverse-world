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

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from pydantic import ValidationError

from app.lab.protocol import executor_output_declarations
from app.lab.sandbox.base import ArtifactSpec, StepEvent

# Scope -> production-v2 tool intents. Network tools are filtered again through
# deployment availability so importing this Runtime cannot advertise a handler
# that the operator did not explicitly enable.
_SCOPE_TOOLS = {
    "code": ("code.run",),
    "web_search": ("web.search",),
    "http": ("web.fetch",),
    "browse": ("browser.navigate",),
}

Completer = Callable[[list[dict]], Awaitable[tuple[str, int]]]

_SYSTEM = (
    "You are a research agent inside a sandboxed runtime. You may ONLY intend tool "
    "calls; a Broker executes them. Given a research brief and the tools you are "
    "granted, respond with a STRICT JSON object and nothing else:\n"
    '{"plan": "<one short line>", "tool": "<one granted tool or null>", '
    '"args": {"<argument>": "<value>"}, "conclusion": "<one short line, or empty>"}\n'
    "Use args={\"query\": ...} for web.search, args={\"url\": ...} for "
    "web.fetch/browser.navigate. For code.run use args={\"code\": ..., "
    "\"outputs\": [...]}; omit outputs when no file should be exported. Each "
    "output is written under /scratch and has exactly relative_path, kind "
    "(file/image/dataset), expected_use (deliverable/evidence), title, "
    "content_type, optional original_filename, required, max_bytes, and optional "
    "expected_sha256. The code must write every required declared path. "
    "Choose a tool ONLY from the granted list. If the research is done, set tool to "
    "null and give a conclusion."
)


@dataclass
class AgentResult:
    steps: list[StepEvent]
    artifacts: list[ArtifactSpec]
    tool_intents: list[tuple[str, dict]]  # (tool, args) the loop INTENDED
    model_tokens: int = 0


@dataclass(frozen=True)
class AgentTurn:
    """One protocol-v2 model turn.

    An ``intent`` turn is a hard pause point. The caller must persist the
    checkpoint and wait for a real Broker result before invoking
    :meth:`RefAgent.resume_turn`. Only a ``final`` turn carries an artifact.
    """

    state: Literal["intent", "final", "failed"]
    steps: tuple[StepEvent, ...]
    checkpoint: dict[str, Any]
    tool_intent: tuple[str, dict] | None = None
    artifact: ArtifactSpec | None = None


CHECKPOINT_VERSION = 1


def _granted_tools(scopes: list[str]) -> list[str]:
    try:
        from app.lab.egress_service.config import configured_runtime_tools

        available = {"code.run", *configured_runtime_tools()}
    except ValueError:
        available = {"code.run"}
    granted: list[str] = []
    for scope in scopes:
        for tool in _SCOPE_TOOLS.get(scope, ()):
            if tool in available and tool not in granted:
                granted.append(tool)
    return granted


def _tool_args(action: dict, tool: str) -> dict | None:
    raw_args = action.get("args")
    if isinstance(raw_args, dict):
        args = raw_args
    else:
        # Checkpoint/model compatibility with the previous one-string schema.
        query = action.get("query")
        if not isinstance(query, str) or not query.strip():
            return None
        key = {
            "web.search": "query",
            "web.fetch": "url",
            "browser.navigate": "url",
            "code.run": "code",
        }.get(tool)
        if key is None:
            return None
        args = {key: query.strip()}
    if any(not isinstance(key, str) or not key for key in args):
        return None
    if tool == "code.run":
        try:
            executor_output_declarations(args)
        except (TypeError, ValueError, ValidationError):
            return None
    try:
        encoded = json.dumps(
            args,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return copy.deepcopy(args) if len(encoded) <= 8_192 else None


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply (tolerant of prose/fences)."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        value = json.loads(m.group(0))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


@dataclass
class RefAgent:
    """A bounded reference agent loop. ``max_steps`` caps the tool rounds so the
    loop always terminates (belt-and-braces with the Gateway budget)."""
    complete: Completer
    max_steps: int = 3
    tokens: int = field(default=0, init=False)

    @staticmethod
    def initial_checkpoint(*, brief: str, scopes: list[str]) -> dict[str, Any]:
        if not isinstance(brief, str) or not brief.strip():
            raise ValueError("brief is required")
        if any(not isinstance(scope, str) or not scope for scope in scopes):
            raise ValueError("scopes must contain non-empty strings")
        granted = _granted_tools(scopes)
        return {
            "version": CHECKPOINT_VERSION,
            "scopes": list(scopes),
            "rounds_completed": 0,
            "model_tokens": 0,
            "transcript": [
                {
                    "role": "user",
                    "content": (
                        f"Brief: {brief}\nGranted tools: {granted or ['(none)']}\n"
                        "Produce your next action as the strict JSON object."
                    ),
                }
            ],
        }

    @staticmethod
    def _validated_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(checkpoint, dict) or checkpoint.get("version") != CHECKPOINT_VERSION:
            raise ValueError("unsupported agent checkpoint")
        scopes = checkpoint.get("scopes")
        if not isinstance(scopes, list) or any(
            not isinstance(scope, str) or not scope for scope in scopes
        ):
            raise ValueError("agent checkpoint has invalid scopes")
        rounds = checkpoint.get("rounds_completed")
        tokens = checkpoint.get("model_tokens")
        if type(rounds) is not int or rounds < 0:
            raise ValueError("agent checkpoint has invalid round count")
        if type(tokens) is not int or tokens < 0:
            raise ValueError("agent checkpoint has invalid token count")
        transcript = checkpoint.get("transcript")
        if not isinstance(transcript, list) or not transcript:
            raise ValueError("agent checkpoint has no transcript")
        for message in transcript:
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message["role"] not in {"user", "assistant"}
                or not isinstance(message["content"], str)
            ):
                raise ValueError("agent checkpoint has an invalid message")
        return copy.deepcopy(checkpoint)

    async def start_turn(self, *, brief: str, scopes: list[str]) -> AgentTurn:
        """Run only until the first real intent or terminal model answer."""

        return await self._advance(
            self.initial_checkpoint(brief=brief, scopes=scopes)
        )

    async def advance_turn(self, checkpoint: dict[str, Any]) -> AgentTurn:
        """Continue a checkpoint saved before a model call."""

        return await self._advance(checkpoint)

    async def resume_turn(
        self,
        *,
        checkpoint: dict[str, Any],
        tool: str,
        outcome: Literal["succeeded", "denied", "failed"],
        payload: dict[str, Any],
    ) -> AgentTurn:
        """Resume from one exact Broker result without fabricating observation."""

        restored = self._validated_checkpoint(checkpoint)
        if not isinstance(tool, str) or not tool:
            raise ValueError("tool is required")
        if outcome not in {"succeeded", "denied", "failed"}:
            raise ValueError("invalid Broker outcome")
        if not isinstance(payload, dict):
            raise ValueError("Broker result payload must be an object")
        result_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        restored["transcript"].append({
            "role": "user",
            "content": (
                f"The Broker result for {tool} has outcome={outcome}. "
                f"Result payload: {result_json}\n"
                "Continue or conclude using the strict JSON object."
            ),
        })
        return await self._advance(restored)

    async def _advance(self, checkpoint: dict[str, Any]) -> AgentTurn:
        restored = self._validated_checkpoint(checkpoint)
        if restored["rounds_completed"] >= self.max_steps:
            failed = StepEvent(
                phase="message",
                summary="model step limit reached before a final answer",
            )
            return AgentTurn(
                state="failed", steps=(failed,), checkpoint=restored
            )

        messages = [
            {"role": "system", "content": _SYSTEM},
            *copy.deepcopy(restored["transcript"]),
        ]
        text, raw_tokens = await self.complete(messages)
        if not isinstance(text, str):
            raise ValueError("model response must be text")
        if raw_tokens is None:
            tokens = 0
        elif type(raw_tokens) is int:
            tokens = raw_tokens
        else:
            raise ValueError("model token count must be an integer")
        if tokens < 0:
            raise ValueError("model token count must be non-negative")
        self.tokens = restored["model_tokens"] + tokens
        restored["model_tokens"] = self.tokens
        restored["rounds_completed"] += 1
        restored["transcript"].append({"role": "assistant", "content": text})

        action = _extract_json(text)
        plan = str(action.get("plan") or "").strip()
        tool = action.get("tool")
        conclusion = str(action.get("conclusion") or "").strip()
        think = StepEvent(
            phase="think",
            summary=plan or "planning",
            model_tokens=tokens,
        )

        if tool:
            granted = _granted_tools(restored["scopes"])
            if not isinstance(tool, str) or tool not in granted:
                failed = StepEvent(
                    phase="message",
                    summary="model requested a tool outside the granted scopes",
                )
                return AgentTurn(
                    state="failed",
                    steps=(think, failed),
                    checkpoint=restored,
                )
            args = _tool_args(action, tool)
            if args is None:
                failed = StepEvent(
                    phase="message", summary="model returned invalid tool arguments"
                )
                return AgentTurn(
                    state="failed",
                    steps=(think, failed),
                    checkpoint=restored,
                )
            intent = StepEvent(
                phase="tool_call",
                tool=tool,
                summary=f"intend {tool}",
                payload=args,
            )
            return AgentTurn(
                state="intent",
                steps=(think, intent),
                checkpoint=restored,
                tool_intent=(tool, args),
            )

        if not conclusion:
            failed = StepEvent(
                phase="message", summary="model returned no final conclusion"
            )
            return AgentTurn(
                state="failed", steps=(think, failed), checkpoint=restored
            )
        final = StepEvent(phase="message", summary=conclusion)
        return AgentTurn(
            state="final",
            steps=(think, final),
            checkpoint=restored,
            artifact=ArtifactSpec(
                kind="text", title="research summary", text_md=conclusion
            ),
        )

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
            conclusion = str(action.get("conclusion") or "").strip() or conclusion

            _emit(StepEvent(phase="think", summary=plan or "planning", model_tokens=int(toks or 0)))

            if tool and tool in granted:
                args = _tool_args(action, tool)
                if args is None:
                    _emit(
                        StepEvent(
                            phase="message",
                            summary="model returned invalid tool arguments",
                        )
                    )
                    break
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
