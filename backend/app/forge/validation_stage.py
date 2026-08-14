from collections.abc import Awaitable, Callable
from typing import Any

from app.llm.json_extract import extract_json_object
from app.llm.metering import record_usage


class ValidationStage:
    def __init__(self, llm_client, model: str, session_id: str | None = None,
                 user_id: str | None = None,
                 budget_check: Callable[[], Awaitable[None]] | None = None):
        self._client = llm_client
        self._model = model
        self._session_id = session_id
        self._user_id = user_id
        self._budget_check = budget_check

    async def run(
        self,
        character_name: str,
        ability_md: str,
        persona_md: str,
        soul_md: str,
    ) -> dict[str, Any]:
        from app.forge.prompts import VALIDATION_SYSTEM_PROMPT, VALIDATION_USER_TEMPLATE

        if self._budget_check is not None:
            await self._budget_check()
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=2000,
            system=VALIDATION_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": VALIDATION_USER_TEMPLATE.format(
                    character_name=character_name,
                    ability_md=ability_md,
                    persona_md=persona_md,
                    soul_md=soul_md,
                ),
            }],
        )

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text = block.text
                break

        data = extract_json_object(text)
        await record_usage(
            "forge_validate", model=self._model, owner="user", response=response,
            parse_ok=data is not None,
            conversation_id=self._session_id,
            user_id=self._user_id,
        )
        if self._budget_check is not None:
            await self._budget_check()
        if data is not None:
            return data

        return {
            "known_answers": [],
            "edge_case": {},
            "style_check": {},
            "overall_score": 0.0,
            "suggestions": ["Validation parsing failed"],
        }
