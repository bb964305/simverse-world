"""Model router for multimodal chat: routes image/video to appropriate vision models.

- Images  → vision prepass (SV_VISION_MODEL) turns the picture into a text
            description that is injected into the conversation chain, then the
            main model streams. The Anthropic image block is still attached so a
            vision-capable relay benefits too. When SV_VISION_MODEL is unset or
            the prepass fails, it degrades to the legacy image-block-only path
            (main model with settings.effective_model) — no crash.
- Videos  → kimi-k2.5 first (for summary), then main model with summary injected into text.
- No media → falls through to regular streaming, same as stream_chat().

Root cause it fixes (P1-1, TEST_REPORT_2026-07-24): the production model
(qwen3.7-plus via a 百炼 relay) has no vision capability and cannot fetch the
relative ``/static/...`` image URL, so the injected image block was never
consumed and the NPC answered "没有视觉能力". Describing the image with a
dedicated vision model first, and injecting that text, lets any text model
"see" it. SV_VISION_MODEL is read from the environment (os.environ) so ops can
turn the feature on without a config.py change.
"""
import base64
import copy
import logging
import os
from typing import AsyncGenerator

from app.config import settings
from app.llm.budget import BudgetTier, background_tier
from app.database import async_session
from app.llm.client import get_client, extract_text
from app.llm.metering import (
    Meter, estimate_tokens, record_from_meter, record_usage,
)

logger = logging.getLogger(__name__)

# Prompt/instruction copy for the vision prepass, kept together for tuning.
_VISION_SYSTEM = (
    "你是一个图像理解助手。请用中文客观、具体地描述这张图片的内容，"
    "包括主要物体、颜色、场景和文字（如果有）。控制在150字以内，只描述看到的内容。"
)
_VISION_PROMPT = "请描述这张图片的内容。"
# Coarse image token estimate for the usage-missing fallback path only (when the
# relay omits response.usage). Without it the estimated branch would count a
# vision call as ~0 tokens (only the short prompt), letting the budget breaker
# systematically undercount vision spend. ~1.6k ≈ a typical Anthropic image; the
# authoritative response.usage path (below) is unaffected by this constant.
_VISION_IMAGE_TOKEN_EST = 1600


class ModelRouter:
    """Routes chat messages to the appropriate model based on media type."""

    async def chat_with_media(
        self,
        system_prompt: str,
        messages: list[dict],
        media_url: str | None,
        media_type: str | None,
        *,
        owner: str = "user",
        user_config: dict | None = None,
        meter: Meter | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream a response, injecting media context as appropriate.

        For images: the last user message is augmented with an image content block.
        For videos: kimi-k2.5 summarizes the video first; the summary is injected
                    as additional text in the last user message, then the main model streams.
        For no media: plain streaming, identical to stream_chat().

        Yields text chunks. When ``meter`` is supplied, the main streamed reply is
        metered (P1-1); a video pre-pass is metered separately as scenario="video".
        """
        augmented_messages = copy.deepcopy(messages)

        if media_type == "image" and media_url:
            # Vision prepass: describe the image with a vision-capable model and
            # inject that text so a text-only main model can still "see" it. The
            # image block is kept for vision-capable relays. Failure / unconfigured
            # → description is empty and we fall back to the legacy block-only path.
            description = await self._understand_image(media_url, meter=meter)
            if description:
                augmented_messages = self._inject_image_description(augmented_messages, description)
            augmented_messages = self._inject_image(augmented_messages, media_url)
            async for chunk in self._stream(system_prompt, augmented_messages, owner=owner, user_config=user_config, meter=meter):
                yield chunk

        elif media_type == "video" and media_url:
            video_summary = await self._understand_video(media_url, meter=meter)
            augmented_messages = self._inject_video_summary(augmented_messages, media_url, video_summary)
            async for chunk in self._stream(system_prompt, augmented_messages, owner=owner, user_config=user_config, meter=meter):
                yield chunk

        else:
            # No media — plain text stream (same path as stream_chat)
            async for chunk in self._stream(system_prompt, messages, owner=owner, user_config=user_config, meter=meter):
                yield chunk

    def _inject_image(self, messages: list[dict], image_url: str) -> list[dict]:
        """Augment the last user message with an image content block.

        Converts the last user message content from a plain string to a list of
        content blocks: [text block, image block]. This matches the Anthropic
        messages API multimodal format.
        """
        if not messages:
            return messages

        last_msg = messages[-1]
        if last_msg.get("role") != "user":
            return messages

        original_text = last_msg.get("content", "")
        if isinstance(original_text, str):
            text_block = {"type": "text", "text": original_text}
        else:
            # Already a list — wrap as-is; append image after
            messages[-1]["content"] = list(original_text) + [
                {
                    "type": "image",
                    "source": {"type": "url", "url": image_url},
                }
            ]
            return messages

        messages[-1]["content"] = [
            text_block,
            {
                "type": "image",
                "source": {"type": "url", "url": image_url},
            },
        ]
        return messages

    def _inject_image_description(self, messages: list[dict], description: str) -> list[dict]:
        """Prepend the vision model's description to the last user message text.

        Framed as ground truth about the image so a text-only main model treats
        it as what it "sees" instead of answering "没有视觉能力". Runs BEFORE
        ``_inject_image`` while content is still a plain string, so the note ends
        up inside the text block.
        """
        if not messages:
            return messages
        last_msg = messages[-1]
        if last_msg.get("role") != "user":
            return messages
        original = last_msg.get("content", "")
        note = f"[用户发送了一张图片。图片内容（AI 视觉识别）：{description}]"
        if isinstance(original, str):
            messages[-1]["content"] = f"{note}\n\n{original}" if original else note
        elif isinstance(original, list):
            messages[-1]["content"] = [{"type": "text", "text": note}, *original]
        return messages

    def _inject_video_summary(
        self,
        messages: list[dict],
        video_url: str,
        summary: str,
    ) -> list[dict]:
        """Append video summary as text to the last user message.

        Since videos cannot be sent directly as content blocks to the main model,
        we inject the summary from kimi-k2.5 as additional context text.
        """
        if not messages:
            return messages

        last_msg = messages[-1]
        if last_msg.get("role") != "user":
            return messages

        original_text = last_msg.get("content", "")
        if isinstance(original_text, str):
            injected = (
                f"{original_text}\n\n"
                f"[视频内容摘要 by AI: {summary}]"
            )
            messages[-1]["content"] = injected
        return messages

    async def _understand_video(self, video_url: str, *, meter: Meter | None = None) -> str:
        """Call kimi-k2.5 to understand the video and return a text summary.

        Uses the same DashScope Anthropic-compatible endpoint as the main model,
        but switches to kimi-k2.5 for video understanding capability.
        """
        client = get_client("system")
        prompt_text = f"请描述这个视频的内容：{video_url}"
        try:
            resp = await client.messages.create(
                model=settings.video_llm_model,
                max_tokens=512,
                system="你是一个视频理解助手。请用中文简洁描述视频的主要内容，不超过200字。",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt_text,
                            }
                        ],
                    }
                ],
            )
            text = extract_text(resp)
            await record_usage(
                "video", model=settings.video_llm_model, owner="system", response=resp,
                est_input_tokens=estimate_tokens(prompt_text), est_output_tokens=estimate_tokens(text),
                resident_id=(meter.resident_id if meter else None),
                user_id=(meter.user_id if meter else None),
                conversation_id=(meter.conversation_id if meter else None),
            )
            return text
        except Exception as exc:
            logger.warning("Video understanding failed for %s: %s", video_url, exc)
            return f"（视频理解失败，原始链接：{video_url}）"

    @staticmethod
    def _vision_model() -> str | None:
        """Vision model id from the environment (SV_VISION_MODEL).

        Read from os.environ — not config.py — so ops can enable the feature
        with a plain env var and no code/settings change (task constraint).
        Empty/unset → vision prepass is disabled (legacy behavior).
        """
        return (os.environ.get("SV_VISION_MODEL") or "").strip() or None

    def _image_source(self, image_url: str) -> dict | None:
        """Build an Anthropic image ``source`` block for the vision prepass.

        - http(s) URL → pass through as a url source (the vision endpoint fetches it).
        - local ``/static/uploads/...`` → the relay can't reach our internal URL,
          so read the file and inline it as base64. Returns None if unreadable
          (caller then skips the prepass and falls back to text).
        """
        if image_url.startswith(("http://", "https://")):
            return {"type": "url", "url": image_url}
        # Local file: only ever read from the uploads root. get_file_path just
        # strips a known prefix, so an unexpected absolute path (e.g. "/etc/...")
        # would otherwise resolve outside uploads — refuse anything not under
        # /static/uploads/ before touching the filesystem.
        if not image_url.startswith("/static/uploads/"):
            logger.warning("Refusing non-upload image path for vision prepass: %s", image_url)
            return None
        try:
            from app.media.service import MediaService, sniff_image_type
            path = MediaService().get_file_path(image_url)
            data = path.read_bytes()
            media_type = sniff_image_type(data) or "image/jpeg"
            b64 = base64.standard_b64encode(data).decode("ascii")
            return {"type": "base64", "media_type": media_type, "data": b64}
        except Exception as exc:
            logger.warning("Cannot load local image for vision prepass %s: %s", image_url, exc)
            return None

    async def _vision_budget_blocked(self) -> bool:
        """Whether the vision prepass should be skipped to save cost.

        The prepass is an *optional* enhancement, so when the global budget is
        fully spent (PLAYER_ONLY tier) we skip it and let only the player-visible
        main chat run. Fails open (never blocks) on any breaker error so a hiccup
        can't silently disable vision. Uses its own short-lived session — no DB
        connection is held across the LLM call.
        """
        try:
            async with async_session() as db:
                return await background_tier(db) == BudgetTier.PLAYER_ONLY
        except Exception as exc:
            logger.debug("vision budget check failed, allowing: %s", exc)
            return False

    async def _understand_image(self, image_url: str, *, meter: Meter | None = None) -> str:
        """Describe an image with the configured vision model; return "" to signal
        "no description available" (caller keeps the legacy image-block path).

        Metered as scenario="image" through the same ``record_usage`` path as the
        video prepass, so it can never bypass the Meter / budget accounting.
        Any failure returns "" instead of raising — the chat must not crash.
        """
        model = self._vision_model()
        if not model:
            return ""
        if await self._vision_budget_blocked():
            logger.info("vision prepass skipped (budget PLAYER_ONLY) for %s", image_url)
            return ""
        source = self._image_source(image_url)
        if source is None:
            return ""
        client = get_client("system")
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=512,
                system=_VISION_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _VISION_PROMPT},
                            {"type": "image", "source": source},
                        ],
                    }
                ],
            )
        except Exception as exc:
            logger.warning("Image understanding failed for %s: %s", image_url, exc)
            return ""
        # The create() call above is billable. Extract text defensively so a
        # malformed relay response (content=None / block.text=None) can't throw
        # BEFORE record_usage and leave a paid call unmetered (禁止绕过 Meter).
        try:
            text = (extract_text(resp) or "").strip()
        except Exception:
            text = ""
        await record_usage(
            "image", model=model, owner="system", response=resp,
            # Fallback path only (relay omits usage): include a coarse image cost
            # so vision spend isn't recorded as ~0 and the budget breaker undercounts.
            est_input_tokens=estimate_tokens(_VISION_SYSTEM + _VISION_PROMPT) + _VISION_IMAGE_TOKEN_EST,
            est_output_tokens=estimate_tokens(text),
            resident_id=(meter.resident_id if meter else None),
            user_id=(meter.user_id if meter else None),
            conversation_id=(meter.conversation_id if meter else None),
        )
        return text

    async def _stream(
        self,
        system_prompt: str,
        messages: list[dict],
        *,
        owner: str = "user",
        user_config: dict | None = None,
        meter: Meter | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream text from the main model. Internal helper."""
        client = get_client(owner, user_config=user_config)
        resolved_model = settings.effective_model
        kwargs: dict = {
            "model": resolved_model,
            "max_tokens": settings.llm_max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        if not settings.llm_thinking:
            kwargs["thinking"] = {"type": "disabled"}
        collected: list[str] = []
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if meter is not None:
                    collected.append(text)
                yield text
            if meter is not None:
                final = None
                try:
                    final = await stream.get_final_message()
                except Exception:
                    final = None
                est_in = estimate_tokens(system_prompt) + sum(
                    estimate_tokens(m.get("content") if isinstance(m.get("content"), str) else "")
                    for m in messages
                )
                await record_from_meter(
                    meter, model=resolved_model, owner=owner, response=final,
                    est_input_tokens=est_in, est_output_tokens=estimate_tokens("".join(collected)),
                )
