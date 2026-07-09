"""P1-5: forge pipelines push progress over WS instead of the client polling.

Covers the shared notify helpers (envelope shape + failure-swallowing) and the
deep/quick/error paths of the canonical pipeline (app/forge/pipeline.py), which
drives the DeepForge component. The guided/quick legacy paths in
services/forge_service.py reuse the exact same helpers at the same points.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

# Register models with Base.metadata before db_engine create_all.
from app.models.user import User  # noqa: F401
from app.models.forge_session import ForgeSession  # noqa: F401
from app.models.resident import Resident  # noqa: F401


# ── notify helpers (app/forge/progress.py) ───────────────────────────

@pytest.mark.anyio
async def test_notify_helpers_send_expected_envelopes():
    from app.forge import progress

    with patch.object(progress.manager, "send", new_callable=AsyncMock) as mock_send:
        await progress.notify_forge_progress("u1", "f1", "build", "building")
        await progress.notify_forge_done("u1", "f1")
        await progress.notify_forge_error("u1", "f1", "boom")

    sent = [c.args[1] for c in mock_send.call_args_list]
    assert sent[0] == {"type": "forge_progress", "forge_id": "f1", "stage": "build", "status": "building"}
    assert sent[1] == {"type": "forge_done", "forge_id": "f1", "status": "done"}
    assert sent[2] == {"type": "forge_error", "forge_id": "f1", "status": "error", "error": "boom"}
    # All addressed to the creating user.
    assert all(c.args[0] == "u1" for c in mock_send.call_args_list)


@pytest.mark.anyio
async def test_notify_swallows_send_failure():
    """A WS hiccup must never propagate into the forge run."""
    from app.forge import progress

    with patch.object(progress.manager, "send", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = RuntimeError("socket gone")
        # Should not raise.
        await progress.notify_forge_progress("u1", "f1", "build", "building")
        await progress.notify_forge_done("u1", "f1")
        await progress.notify_forge_error("u1", "f1", "boom")


# ── pipeline integration (app/forge/pipeline.py) ─────────────────────

async def _make_user(db_session):
    user = User(name="creator", email=f"forge-{id(db_session)}@test.com")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.mark.anyio
async def test_deep_pipeline_pushes_each_stage_then_done(db_session):
    from app.forge.pipeline import ForgePipeline
    from app.forge.router_stage import InputRouter
    from app.forge.research_stage import ResearchStage
    from app.forge.extraction_stage import ExtractionStage
    from app.forge.build_stage import BuildStage
    from app.forge.validation_stage import ValidationStage
    from app.forge.refinement_stage import RefinementStage
    from app.forge import progress

    user = await _make_user(db_session)
    mock_client = AsyncMock()
    pipeline = ForgePipeline(db=db_session, system_client=mock_client, user_client=mock_client, model="test")

    layers = {"ability_md": "# A", "persona_md": "# P", "soul_md": "# S"}

    with patch.object(InputRouter, "run", AsyncMock(return_value={"mode": "deep", "reason": "x", "has_user_material": False})), \
         patch.object(ResearchStage, "run", AsyncMock(return_value={"web": []})), \
         patch.object(ResearchStage, "format_for_llm", Mock(return_value="research text")), \
         patch.object(ExtractionStage, "run", AsyncMock(return_value={"core_models": [], "heuristics": []})), \
         patch.object(BuildStage, "run", AsyncMock(return_value=dict(layers))), \
         patch.object(ValidationStage, "run", AsyncMock(return_value={"overall_score": 0.9})), \
         patch.object(RefinementStage, "run", AsyncMock(return_value={**layers, "refinement_log": []})), \
         patch.object(progress.manager, "send", new_callable=AsyncMock) as mock_send:
        session = await pipeline.start(user_id=user.id, character_name="乔布斯", user_material="bio")
        assert session.mode == "deep"
        await pipeline.run_to_completion(session.id)

    sent = [c.args[1] for c in mock_send.call_args_list]
    progress_stages = [m["stage"] for m in sent if m["type"] == "forge_progress"]
    assert progress_stages == ["research", "extraction", "build", "validation", "refinement"]
    assert sent[-1]["type"] == "forge_done"
    assert all(c.args[0] == user.id for c in mock_send.call_args_list)


@pytest.mark.anyio
async def test_quick_pipeline_pushes_build_then_done(db_session):
    from app.forge.pipeline import ForgePipeline
    from app.forge.router_stage import InputRouter
    from app.forge.build_stage import BuildStage
    from app.forge import progress

    user = await _make_user(db_session)
    mock_client = AsyncMock()
    pipeline = ForgePipeline(db=db_session, system_client=mock_client, user_client=mock_client, model="test")

    with patch.object(InputRouter, "run", AsyncMock(return_value={"mode": "quick", "reason": "fictional", "has_user_material": False})), \
         patch.object(BuildStage, "run", AsyncMock(return_value={"ability_md": "# A", "persona_md": "# P", "soul_md": "# S"})), \
         patch.object(progress.manager, "send", new_callable=AsyncMock) as mock_send:
        session = await pipeline.start(user_id=user.id, character_name="赛博黑客", raw_text="虚构")
        assert session.mode == "quick"
        await pipeline.run_to_completion(session.id)

    sent = [c.args[1] for c in mock_send.call_args_list]
    assert [m["type"] for m in sent] == ["forge_progress", "forge_done"]
    assert sent[0]["stage"] == "build"


@pytest.mark.anyio
async def test_pipeline_pushes_forge_error_on_stage_failure(db_session):
    from app.forge.pipeline import ForgePipeline
    from app.forge.router_stage import InputRouter
    from app.forge.build_stage import BuildStage
    from app.forge import progress

    user = await _make_user(db_session)
    mock_client = AsyncMock()
    pipeline = ForgePipeline(db=db_session, system_client=mock_client, user_client=mock_client, model="test")

    with patch.object(InputRouter, "run", AsyncMock(return_value={"mode": "quick", "reason": "fictional", "has_user_material": False})), \
         patch.object(BuildStage, "run", AsyncMock(side_effect=RuntimeError("build blew up"))), \
         patch.object(progress.manager, "send", new_callable=AsyncMock) as mock_send:
        session = await pipeline.start(user_id=user.id, character_name="X", raw_text="虚构")
        await pipeline.run_to_completion(session.id)
        await db_session.refresh(session)

    assert session.status == "error"
    sent = [c.args[1] for c in mock_send.call_args_list]
    assert sent[-1]["type"] == "forge_error"
    assert "build blew up" in sent[-1]["error"]
    # No forge_done should have been emitted on the failure path.
    assert not any(m["type"] == "forge_done" for m in sent)
