from typing import Any

from app.llm.json_extract import extract_json_object
from app.llm.metering import record_usage

# E-20: deep-forge research injects 12 full-text search results; cap the input
# so the tail doesn't blow up extraction cost (matches skill-import's [:8000]).
RESEARCH_INPUT_MAX_CHARS = 8000


class ExtractionStage:
    def __init__(self, llm_client, model: str, session_id: str | None = None):
        self._client = llm_client
        self._model = model
        self._session_id = session_id

    async def run(self, research_text: str, character_name: str) -> dict[str, Any]:
        from app.forge.prompts import EXTRACTION_SYSTEM_PROMPT, EXTRACTION_USER_TEMPLATE

        research_text = (research_text or "")[:RESEARCH_INPUT_MAX_CHARS]
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=3000,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": EXTRACTION_USER_TEMPLATE.format(
                    character_name=character_name,
                    research_text=research_text,
                ),
            }],
        )

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text = block.text
                break

        await record_usage(
            "forge_extract", model=self._model, owner="user", response=response,
            parse_ok=extract_json_object(text) is not None,
            conversation_id=self._session_id,
        )
        return self._parse(text)

    def _parse(self, text: str) -> dict[str, Any]:
        data = extract_json_object(text)
        if data is None:
            return {"core_models": [], "heuristics": [], "discarded": []}

        core_models = []
        heuristics = []
        discarded = []

        for model in data.get("mental_models", []):
            verdict = model.get("verdict", "discard")
            if verdict == "core_model":
                core_models.append(model)
            elif verdict == "heuristic":
                heuristics.append(model)
            else:
                discarded.append(model)

        # Also include explicit heuristics from LLM
        for h in data.get("decision_heuristics", []):
            heuristics.append(h)

        return {
            "core_models": core_models,
            "heuristics": heuristics,
            "discarded": discarded,
        }
