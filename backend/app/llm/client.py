import time
from typing import AsyncGenerator
import anthropic
from app.config import settings
from app.llm.json_extract import extract_json_object
from app.llm.metering import Meter, estimate_tokens, record_from_meter

_system_client: anthropic.AsyncAnthropic | None = None
_default_user_client: anthropic.AsyncAnthropic | None = None


def _reset_factory():
    """Reset all cached clients. Used in tests."""
    global _system_client, _default_user_client
    _system_client = None
    _default_user_client = None


class LLMClientFactory:
    """Not instantiated — just a namespace for documentation."""
    pass


def _make_anthropic_client(api_key: str, base_url: str | None = None) -> anthropic.AsyncAnthropic:
    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.AsyncAnthropic(**kwargs)


def get_client(owner: str = "system", *, user_config: dict | None = None) -> anthropic.AsyncAnthropic:
    """
    Get an LLM client by owner type.

    owner: "system" or "user"
    user_config: optional dict with keys: api_key, base_url, api_format
                 If provided and owner="user", creates a client with user's credentials.
                 If not provided, falls back to system defaults.
    """
    global _system_client, _default_user_client

    if owner == "system":
        if _system_client is None:
            _system_client = _make_anthropic_client(
                api_key=settings.effective_api_key,
                base_url=settings.llm_base_url or None,
            )
        return _system_client

    if owner == "user":
        if user_config and user_config.get("api_key"):
            # User has custom config — create a fresh client (not cached)
            return _make_anthropic_client(
                api_key=user_config["api_key"],
                base_url=user_config.get("base_url") or settings.llm_base_url or None,
            )
        # No custom config — fall back to system defaults
        if _default_user_client is None:
            _default_user_client = _make_anthropic_client(
                api_key=settings.effective_api_key,
                base_url=settings.llm_base_url or None,
            )
        return _default_user_client

    raise ValueError("owner must be 'system' or 'user'")


def extract_text(response) -> str:
    """Extract text from an LLM response, skipping ThinkingBlocks.

    Use this instead of resp.content[0].text to safely handle
    responses that may contain ThinkingBlock objects.
    """
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


def _message_text(msg: dict) -> str:
    """Flatten a single messages-API message to plain text for estimation."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return " ".join(parts)
    return ""


def _estimate_input_tokens(system_prompt: str, messages: list[dict]) -> int:
    total = estimate_tokens(system_prompt)
    for m in messages:
        total += estimate_tokens(_message_text(m))
    return total


async def chat(
    system_prompt: str,
    messages: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    *,
    owner: str = "system",
    meter: Meter | None = None,
    expects_json: bool = False,
) -> str:
    """Non-streaming LLM call. Returns text string.

    Handles thinking mode and ThinkingBlock extraction automatically.
    Use this instead of client.messages.create() directly.

    When ``meter`` is supplied, one ``llm_usage`` row is recorded per attempt
    (P1-1): usage/latency/cost from the real response, or a char estimate when
    the endpoint omits ``usage``. ``expects_json`` makes the wrapper set
    ``parse_ok`` by trying the shared balanced-brace extractor on the output —
    the E-05 signal for "paid for a call that fell back to defaults".
    """
    client = get_client(owner)
    resolved_model = model or settings.effective_model
    kwargs: dict = {
        "model": resolved_model,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if not settings.llm_thinking:
        kwargs["thinking"] = {"type": "disabled"}
    t0 = time.perf_counter()
    resp = await client.messages.create(**kwargs)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    text = extract_text(resp)
    if meter is not None:
        parse_ok = (extract_json_object(text) is not None) if expects_json else None
        await record_from_meter(
            meter,
            model=resolved_model,
            owner=owner,
            response=resp,
            est_input_tokens=_estimate_input_tokens(system_prompt, messages),
            est_output_tokens=estimate_tokens(text),
            parse_ok=parse_ok,
            latency_ms=latency_ms,
        )
    return text


async def stream_chat(
    system_prompt: str,
    messages: list[dict],
    model: str | None = None,
    *,
    owner: str = "user",
    user_config: dict | None = None,
    meter: Meter | None = None,
) -> AsyncGenerator[str, None]:
    """Yield text chunks from LLM streaming response.

    When ``meter`` is supplied, a single ``llm_usage`` row is recorded after
    the stream drains, reading token counts from the accumulated final message
    (or a char estimate if the endpoint omits them).
    """
    client = get_client(owner, user_config=user_config)
    resolved_model = model or settings.effective_model
    kwargs: dict = {
        "model": resolved_model,
        "max_tokens": settings.llm_max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if not settings.llm_thinking:
        kwargs["thinking"] = {"type": "disabled"}
    collected: list[str] = []
    t0 = time.perf_counter()
    async with client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            if meter is not None:
                collected.append(text)
            yield text
        if meter is not None:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            final = None
            try:
                final = await stream.get_final_message()
            except Exception:
                final = None
            await record_from_meter(
                meter,
                model=resolved_model,
                owner=owner,
                response=final,
                est_input_tokens=_estimate_input_tokens(system_prompt, messages),
                est_output_tokens=estimate_tokens("".join(collected)),
                latency_ms=latency_ms,
            )
