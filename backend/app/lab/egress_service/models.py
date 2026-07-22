"""Strict wire models shared by the Runner and the egress service."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.lab.protocol import args_digest, content_digest

EGRESS_TOOLS = frozenset({"web.search", "web.fetch", "browser.navigate"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EgressUsage(_StrictModel):
    requests: StrictInt = Field(default=0, ge=0, le=100)
    bytes: StrictInt = Field(default=0, ge=0, le=100_000_000)


class EgressActionCommand(_StrictModel):
    schema_version: Literal[1] = 1
    action_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    tool_name: Literal["web.search", "web.fetch", "browser.navigate"]
    args: dict[str, Any]
    args_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    egress_allowlist: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _validate_binding(self) -> "EgressActionCommand":
        if self.args_digest != args_digest(self.args):
            raise ValueError("args_digest does not match canonical args")
        if any(
            not isinstance(entry, str) or not entry.strip() or len(entry) > 253
            for entry in self.egress_allowlist
        ):
            raise ValueError("egress_allowlist contains an invalid host pattern")
        if len(set(self.egress_allowlist)) != len(self.egress_allowlist):
            raise ValueError("egress_allowlist contains duplicates")
        return self

    @property
    def request_digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class EgressActionStatus(_StrictModel):
    schema_version: Literal[1] = 1
    action_id: str = Field(min_length=1, max_length=200)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["pending", "processing", "succeeded", "failed"]
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=120)
    usage: EgressUsage = Field(default_factory=EgressUsage)
    attempts: StrictInt = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def _validate_terminal_payload(self) -> "EgressActionStatus":
        if self.state == "succeeded" and self.result is None:
            raise ValueError("succeeded action requires a result")
        if self.state == "failed" and not self.error_code:
            raise ValueError("failed action requires an error_code")
        if self.state in {"pending", "processing"} and (
            self.result is not None or self.error_code is not None
        ):
            raise ValueError("non-terminal action cannot carry a terminal payload")
        return self
