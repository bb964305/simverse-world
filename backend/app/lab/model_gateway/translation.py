from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


class TranslationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolTarget:
    kind: str
    name: str
    namespace: str | None = None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise TranslationError("message content must be text or a text block list")
    parts: list[str] = []
    for part in value:
        if not isinstance(part, dict) or part.get("type") not in {
            "input_text", "output_text", "text"
        } or not isinstance(part.get("text"), str):
            raise TranslationError("only text content blocks are supported")
        parts.append(part["text"])
    return "".join(parts)


def _tool_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _text(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _flattened_name(namespace: str, name: str) -> str:
    separator = "" if namespace.endswith("__") else "__"
    return f"{namespace}{separator}{name}"


def _chat_tools(raw_tools: Any) -> tuple[list[dict], dict[str, ToolTarget]]:
    if raw_tools is None:
        return [], {}
    if not isinstance(raw_tools, list):
        raise TranslationError("tools must be a list")
    tools: list[dict] = []
    registry: dict[str, ToolTarget] = {}
    for raw in raw_tools:
        if not isinstance(raw, dict):
            raise TranslationError("tool definitions must be objects")
        kind = raw.get("type")
        # Lab runs have no browser or outbound-network scope. New Codex clients may
        # still advertise the hosted Responses web tool with external access off.
        if kind == "web_search":
            continue
        if kind == "namespace":
            namespace = raw.get("name")
            nested_tools = raw.get("tools")
            if not isinstance(namespace, str) or not namespace:
                raise TranslationError("tool namespaces require a name")
            if not isinstance(nested_tools, list):
                raise TranslationError("tool namespaces require a tools list")
            for nested in nested_tools:
                if not isinstance(nested, dict) or nested.get("type") != "function":
                    raise TranslationError("tool namespaces may only contain functions")
                name = nested.get("name")
                if not isinstance(name, str) or not name:
                    raise TranslationError("namespaced functions require a name")
                flat_name = _flattened_name(namespace, name)
                if flat_name in registry:
                    raise TranslationError("flattened tool names must be unique")
                registry[flat_name] = ToolTarget(
                    kind="function", name=name, namespace=namespace
                )
                parameters = nested.get("parameters") or {
                    "type": "object", "properties": {}, "additionalProperties": False
                }
                function = {"name": flat_name, "parameters": parameters}
                description = nested.get("description") or raw.get("description")
                if isinstance(description, str):
                    function["description"] = description
                tools.append({"type": "function", "function": function})
            continue
        name = raw.get("name")
        if kind not in {"function", "custom"} or not isinstance(name, str) or not name:
            fields = sorted(str(key) for key in raw)
            raise TranslationError(
                f"unsupported tool definition type={kind!r} fields={fields!r}"
            )
        if name in registry:
            raise TranslationError("tool names must be unique")
        registry[name] = ToolTarget(kind=kind, name=name)
        if kind == "function":
            parameters = raw.get("parameters") or {
                "type": "object", "properties": {}, "additionalProperties": False
            }
        else:
            parameters = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            }
        function = {"name": name, "parameters": parameters}
        if isinstance(raw.get("description"), str):
            function["description"] = raw["description"]
        tools.append({"type": "function", "function": function})
    return tools, registry


def _chat_call_name(
    name: str, namespace: Any, registry: dict[str, ToolTarget]
) -> str:
    if isinstance(namespace, str) and namespace:
        candidate = _flattened_name(namespace, name)
        target = registry.get(candidate)
        if target and target.name == name and target.namespace == namespace:
            return candidate
        raise TranslationError("tool call references an unknown namespace")
    target = registry.get(name)
    if target and target.namespace is None:
        return name
    raise TranslationError("tool call references an unknown tool")


def responses_to_chat(
    body: dict,
    *,
    model: str,
    max_output_tokens: int,
    reasoning_for_call: Callable[[str], str | None],
    max_reasoning_bytes: int,
) -> tuple[dict, dict[str, ToolTarget]]:
    raw_input = body.get("input", "")
    items = [{"role": "user", "content": raw_input}] if isinstance(raw_input, str) else raw_input
    if not isinstance(items, list):
        raise TranslationError("input must be text or an item list")
    messages: list[dict] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    elif instructions is not None:
        raise TranslationError("instructions must be text")

    tools, registry = _chat_tools(body.get("tools"))
    reasoning_call_ids: set[str] = set()
    reasoning_bytes = 0
    for item in items:
        if not isinstance(item, dict):
            raise TranslationError("input items must be objects")
        kind = item.get("type")
        role = item.get("role")
        if kind in {None, "message"} and role in {"user", "assistant", "system", "developer"}:
            messages.append({
                "role": "system" if role == "developer" else role,
                "content": _text(item.get("content", "")),
            })
            continue
        if kind in {"reasoning", "item_reference"}:
            continue
        if kind in {"function_call", "custom_tool_call"}:
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                raise TranslationError("tool calls require call_id and name")
            if kind == "custom_tool_call":
                arguments = json.dumps(
                    {"input": item.get("input", "")},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                arguments = item.get("arguments", "{}")
                if not isinstance(arguments, str):
                    raise TranslationError("function arguments must be JSON text")
            chat_name = _chat_call_name(name, item.get("namespace"), registry)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": chat_name, "arguments": arguments},
                }],
            }
            if call_id not in reasoning_call_ids:
                reasoning_call_ids.add(call_id)
                reasoning = reasoning_for_call(call_id)
                if reasoning and reasoning_bytes < max_reasoning_bytes:
                    remaining = max_reasoning_bytes - reasoning_bytes
                    encoded = reasoning.encode("utf-8")[:remaining]
                    bounded = encoded.decode("utf-8", errors="ignore")
                    if bounded:
                        message["reasoning_content"] = bounded
                        reasoning_bytes += len(bounded.encode("utf-8"))
            messages.append(message)
            continue
        if kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise TranslationError("tool outputs require call_id")
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": _tool_output(item.get("output", "")),
            })
            continue
        raise TranslationError(f"unsupported Responses input item: {kind!r}")

    chat: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_output_tokens,
        "enable_thinking": True,
    }
    if tools:
        chat["tools"] = tools
        # DeepSeek hybrid-thinking models reject required/forced tool choice.
        chat["tool_choice"] = "auto"
    for key in ("temperature", "top_p"):
        if isinstance(body.get(key), (int, float)) and not isinstance(body.get(key), bool):
            chat[key] = body[key]
    return chat, registry


def chat_to_response(
    upstream: dict,
    *,
    public_model: str,
    tool_registry: dict[str, ToolTarget],
) -> tuple[dict, dict[str, str]]:
    choices = upstream.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise TranslationError("upstream response has no choice")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise TranslationError("upstream response has no message")
    response_id = "resp_" + uuid.uuid4().hex
    output: list[dict] = []
    reasoning_by_call: dict[str, str] = {}
    content = message.get("content")
    if isinstance(content, str) and content:
        output.append({
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": content,
                "annotations": [],
                "logprobs": [],
            }],
        })
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise TranslationError("upstream tool_calls must be a list")
    reasoning = message.get("reasoning_content")
    for raw in tool_calls:
        function = raw.get("function") if isinstance(raw, dict) else None
        call_id = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(function, dict) or not isinstance(call_id, str) or not call_id:
            raise TranslationError("upstream tool call is malformed")
        name = function.get("name")
        arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or not name or not isinstance(arguments, str):
            raise TranslationError("upstream tool call is malformed")
        target = tool_registry.get(name)
        if target is None:
            raise TranslationError("upstream called a tool that was not offered")
        if isinstance(reasoning, str) and reasoning:
            reasoning_by_call[call_id] = reasoning
        if target.kind == "custom":
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise TranslationError("custom tool arguments are not JSON") from exc
            tool_input = decoded.get("input") if isinstance(decoded, dict) else None
            if not isinstance(tool_input, str):
                raise TranslationError("custom tool call has no string input")
            output.append({
                "id": "ctc_" + uuid.uuid4().hex,
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": target.name,
                "input": tool_input,
            })
        else:
            item = {
                "id": "fc_" + uuid.uuid4().hex,
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": target.name,
                "arguments": arguments,
            }
            if target.namespace is not None:
                item["namespace"] = target.namespace
            output.append(item)

    raw_usage = upstream.get("usage") or {}
    input_tokens = int(raw_usage.get("prompt_tokens") or 0)
    output_tokens = int(raw_usage.get("completion_tokens") or 0)
    details = raw_usage.get("completion_tokens_details") or {}
    reasoning_tokens = int(details.get("reasoning_tokens") or 0)
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "model": public_model,
        "output": output,
        "parallel_tool_calls": False,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": input_tokens + output_tokens,
        },
        "metadata": {},
    }
    return response, reasoning_by_call


def response_events(response: dict) -> Iterator[dict]:
    in_progress = deepcopy(response)
    in_progress["status"] = "in_progress"
    in_progress["output"] = []
    in_progress["usage"] = None
    yield {"type": "response.created", "sequence_number": 0, "response": in_progress}
    sequence = 1
    for index, item in enumerate(response["output"]):
        empty = deepcopy(item)
        if item["type"] == "message":
            empty["content"] = []
        elif item["type"] == "function_call":
            empty["arguments"] = ""
        else:
            empty["input"] = ""
        yield {
            "type": "response.output_item.added", "sequence_number": sequence,
            "output_index": index, "item": empty,
        }
        sequence += 1
        if item["type"] == "message":
            part = item["content"][0]
            empty_part = deepcopy(part)
            empty_part["text"] = ""
            yield {
                "type": "response.content_part.added", "sequence_number": sequence,
                "item_id": item["id"], "output_index": index,
                "content_index": 0, "part": empty_part,
            }
            sequence += 1
            yield {
                "type": "response.output_text.delta", "sequence_number": sequence,
                "item_id": item["id"], "output_index": index,
                "content_index": 0, "delta": part["text"], "logprobs": [],
            }
            sequence += 1
            yield {
                "type": "response.output_text.done", "sequence_number": sequence,
                "item_id": item["id"], "output_index": index,
                "content_index": 0, "text": part["text"], "logprobs": [],
            }
            sequence += 1
            yield {
                "type": "response.content_part.done", "sequence_number": sequence,
                "item_id": item["id"], "output_index": index,
                "content_index": 0, "part": part,
            }
            sequence += 1
        else:
            field = "arguments" if item["type"] == "function_call" else "input"
            prefix = "response.function_call_arguments" if field == "arguments" else "response.custom_tool_call_input"
            yield {
                "type": prefix + ".delta", "sequence_number": sequence,
                "item_id": item["id"], "output_index": index,
                "delta": item[field],
            }
            sequence += 1
            yield {
                "type": prefix + ".done", "sequence_number": sequence,
                "item_id": item["id"], "output_index": index,
                field: item[field],
            }
            sequence += 1
        yield {
            "type": "response.output_item.done", "sequence_number": sequence,
            "output_index": index, "item": item,
        }
        sequence += 1
    yield {
        "type": "response.completed", "sequence_number": sequence,
        "response": response,
    }
