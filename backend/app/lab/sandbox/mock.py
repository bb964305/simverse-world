"""MockAdapter — scripted, dependency-free sandbox (spec §5.2).

The first-version default: no external I/O, deterministic steps + fake artifacts,
so the whole economy/UI loop (publish → run → artifact → settle) is testable and
demoable before any real runtime is wired. Respects the granted ``scopes`` (only
emits tool calls for scopes it was given).
"""
from __future__ import annotations

from typing import AsyncIterator

from app.lab.sandbox.base import ArtifactSpec, RunSpec, SandboxHandle, StepEvent

# Illustrative-only per-step token cost (Mock never calls a real model). Small
# constants so the whole scripted run stays well under the default
# ``lab_budget_model_tokens`` limit while still exercising the live spend path.
_MOCK_TOKENS_STEP = 120   # a reasoning / observation / message step
_MOCK_TOKENS_TOOL = 80    # a tool-call step


class _MockHandle(SandboxHandle):
    def __init__(self, spec: RunSpec) -> None:
        self.spec = spec
        self.brief = ""
        self.scopes: list[str] = []


class MockAdapter:
    name = "mock"

    async def start(self, spec: RunSpec) -> _MockHandle:
        return _MockHandle(spec)

    async def submit_goal(self, handle: _MockHandle, brief: str, scopes: list[str]) -> None:
        handle.brief = brief
        handle.scopes = list(scopes or [])

    async def step_stream(self, handle: _MockHandle) -> AsyncIterator[StepEvent]:
        brief = (handle.brief or "").strip()
        yield StepEvent(phase="think", summary="拆解任务目标，规划检索与验证路径",
                        model_tokens=_MOCK_TOKENS_STEP)
        if "web_search" in handle.scopes:
            yield StepEvent(
                phase="tool_call", tool="web.search",
                summary=f"检索：{brief[:40]}", payload={"query": brief[:80]},
                cost_usd_cents=1, model_tokens=_MOCK_TOKENS_TOOL,
            )
            yield StepEvent(phase="observation", summary="获取到若干候选来源（已脱敏）",
                            model_tokens=_MOCK_TOKENS_STEP)
        if "browse" in handle.scopes:
            yield StepEvent(
                phase="tool_call", tool="browser.navigate",
                summary="打开首个来源并阅读", payload={"url": "https://example.org/(redacted)"},
                cost_usd_cents=1, model_tokens=_MOCK_TOKENS_TOOL,
            )
            yield StepEvent(phase="observation", summary="提取要点并交叉验证",
                            model_tokens=_MOCK_TOKENS_STEP)
        yield StepEvent(phase="message", summary="整理结论，产出交付物",
                        model_tokens=_MOCK_TOKENS_STEP)

    async def approve(self, handle: _MockHandle, approval_id: str, decision: bool) -> None:
        # Mock never pauses for approval.
        return None

    async def collect_artifacts(self, handle: _MockHandle) -> list[ArtifactSpec]:
        brief = (handle.brief or "").strip()
        return [
            ArtifactSpec(
                kind="text",
                title="研究简报（Mock）",
                text_md=(
                    f"# 研究简报\n\n针对「{brief[:60]}」的模拟结论：\n\n"
                    "- 要点一（占位）\n- 要点二（占位）\n- 要点三（占位）\n\n"
                    "> 由 MockAdapter 生成，仅用于经济/UI 闭环验证，非真实联网结果。"
                ),
                meta={"mock": True},
            )
        ]

    async def stop(self, handle: _MockHandle) -> None:
        return None

    # Cancel-escalation surface (P2-D supervision). The Mock has no live process,
    # so a cancel is instantaneous: ``health`` reports already-stopped, which the
    # supervisor reads as a cooperative ACK.
    async def cancel(self, handle: _MockHandle) -> None:
        return None

    async def terminate(self, handle: _MockHandle) -> None:
        return None

    async def kill(self, handle: _MockHandle) -> None:
        return None

    async def health(self, handle: _MockHandle) -> dict:
        return {"alive": False, "cancelled": True}
