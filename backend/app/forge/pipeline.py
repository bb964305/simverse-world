import random
import re

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.models.forge_session import ForgeSession
from app.models.resident import Resident
from app.services.civic_membership import UGC_RESIDENT_TYPE
from app.forge.router_stage import InputRouter
from app.forge.research_stage import ResearchStage
from app.forge.extraction_stage import ExtractionStage
from app.forge.build_stage import BuildStage
from app.forge.validation_stage import ValidationStage
from app.forge.refinement_stage import RefinementStage
from app.forge.budget_guard import ForgeBudgetExceeded, enforce_forge_budget
from app.forge.progress import (
    notify_forge_progress, notify_forge_done, notify_forge_error,
)
from app.config import settings
from app.services.ugc_creation_quota import (
    claim_creation_slot,
    try_claim_forge_reward,
)
from app.services.slug_reservation import (
    SlugReservationConflict,
    consume_session_slug,
    create_reserved_forge_session,
    release_session_slug,
)


FORGE_NAME_MAX_CHARS = 100
FORGE_INPUT_MAX_CHARS = 8_000

# 流水线的两个终态。写在这里是因为本模块就是唯一给 session.status 赋终态的地方
# （"done" / "error"）；admin 监控曾经自己抄了一份写成 "completed"，全仓没有任何
# 地方产生该值，导致每个成功会话永久停留在「活跃会话」列表里。
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "error"})


class ForgeInputError(ValueError):
    pass


class ForgeSlugConflict(ForgeInputError):
    pass


def normalized_slug(name: str, *, fallback: str = "resident") -> str:
    slug = name.lower().strip()
    slug = re.sub(r'[^\w\u4e00-\u9fff-]', '-', slug)
    return re.sub(r'-+', '-', slug).strip('-') or fallback


def validate_inputs(character_name: str, raw_text: str = "", user_material: str = "") -> str:
    name = character_name.strip()
    if not name:
        raise ForgeInputError("character_name is required")
    if len(name) > FORGE_NAME_MAX_CHARS:
        raise ForgeInputError(
            f"character_name too long (max {FORGE_NAME_MAX_CHARS} chars)"
        )
    if len(raw_text) > FORGE_INPUT_MAX_CHARS or len(user_material) > FORGE_INPUT_MAX_CHARS:
        raise ForgeInputError(f"forge input too long (max {FORGE_INPUT_MAX_CHARS} chars)")
    slug = normalized_slug(name)
    if len(slug) > 100:
        raise ForgeInputError("generated slug too long (max 100 chars)")
    return slug


class ForgePipeline:
    def __init__(
        self,
        db: AsyncSession,
        system_client,
        user_client,
        model: str | None = None,
        searxng_url: str | None = None,
    ):
        self._db = db
        self._system_client = system_client
        self._user_client = user_client
        self._model = model or settings.effective_model
        self._searxng_url = searxng_url or f"{settings.searxng_url}/search"

    async def start(
        self,
        user_id: str,
        character_name: str,
        raw_text: str = "",
        user_material: str = "",
    ) -> ForgeSession:
        """Atomically reserve a slug/quota slot, then route the session."""
        slug = validate_inputs(character_name, raw_text, user_material)
        try:
            session = await create_reserved_forge_session(
                self._db,
                user_id=user_id,
                character_name=character_name.strip(),
                requested_slug=slug,
                mode="pending",
                status="routing",
                current_stage="router",
                research_data={
                    "raw_text": raw_text,
                    "user_material": user_material,
                    "target_slug": slug,
                },
            )
            # Keep the cross-service lock order ForgeSession -> User, matching
            # terminal completion (session fence -> optional reward claim).
            await claim_creation_slot(self._db, user_id)
        except SlugReservationConflict as exc:
            await self._db.rollback()
            raise ForgeSlugConflict(str(exc)) from exc
        except Exception:
            await self._db.rollback()
            raise
        # The UGC quota claim and durable session reservation are inseparable.
        await self._db.commit()
        await self._db.refresh(session)

        try:
            router = InputRouter(
                llm_client=self._system_client,
                model=self._model,
                user_id=user_id,
                session_id=session.id,
                budget_check=lambda: self._check_budget(session.id, user_id),
            )
            route_result = await router.run(session.character_name, raw_text, user_material)
            session.mode = route_result["mode"]
            session.research_data = {
                **session.research_data,
                "route_result": route_result,
            }
            session.status = "routed"
            await self._db.commit()
            await self._db.refresh(session)
        except Exception as exc:
            await self._db.rollback()
            session = await self._db.get(ForgeSession, session.id)
            session.status = "error"
            release_session_slug(session)
            session.refinement_log = {"error": str(exc)}
            await self._db.commit()
            raise
        return session

    async def run_to_completion(self, session_id: str) -> ForgeSession:
        """Run the pipeline to completion based on session mode."""
        claimed = await self._db.execute(
            update(ForgeSession)
            .where(
                ForgeSession.id == session_id,
                ForgeSession.status == "routed",
            )
            .values(status="running")
            .execution_options(synchronize_session=False)
        )
        await self._db.commit()
        session = (await self._db.execute(
            select(ForgeSession)
            .where(ForgeSession.id == session_id)
            .execution_options(populate_existing=True)
        )).scalar_one()

        # The routed -> running CAS gives one worker sole execution authority.
        # Replays return the durable state without creating or rewarding again.
        if (claimed.rowcount or 0) != 1:
            return session

        try:
            if session.mode == "deep":
                await self._run_deep(session)
            else:
                await self._run_quick(session)

            # Lock and re-read terminal state immediately before the atomic
            # resident/reward/session commit. This is a second fence against a
            # stale runner surviving a concurrent terminalization.
            session = (await self._db.execute(
                select(ForgeSession)
                .where(ForgeSession.id == session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one()
            if session.status in TERMINAL_STATUSES:
                await self._db.rollback()
                return session

            # Create Resident from completed session
            await self._create_resident(session)
            session.status = "done"
            await self._db.commit()
            await notify_forge_done(session.user_id, session.id)
        except Exception as e:
            # The failure may have poisoned the session (e.g. a mid-stage commit
            # blew up) — roll back first so the terminal-state write below can't
            # itself fail and leave the session stuck non-terminal (P1 fix).
            try:
                await self._db.rollback()
            except Exception:
                pass
            result = await self._db.execute(
                select(ForgeSession).where(ForgeSession.id == session_id)
            )
            session = result.scalar_one()
            session.status = "error"
            # Failed attempts do not hold a name forever. ``research_data``
            # retains the diagnostic slug, while the unique reservation opens.
            release_session_slug(session)
            session.refinement_log = {
                **(session.refinement_log or {}),
                "error": str(e),
            }
            await self._db.commit()
            await notify_forge_error(session.user_id, session.id, str(e))
            return session

        return session

    async def _check_budget(self, session_id: str, user_id: str) -> None:
        """Enforce global, per-user and per-forge budgets between paid calls."""
        await enforce_forge_budget(self._db, session_id, user_id)

    async def _create_resident(self, session: ForgeSession):
        """Create a Resident from a completed forge session."""
        from app.services.resident_placement import SPRITE_KEYS, allocate_resident_location
        from app.services.coin_service import reward_pending

        build = session.build_output or {}
        ability_md = build.get("ability_md", "")
        persona_md = build.get("persona_md", "")
        soul_md = build.get("soul_md", "")
        name = session.character_name

        # The slug was durably reserved before the router's first paid call.
        # Never fall back to a diagnostic copy: a stale/error sweep may have
        # deliberately released that reservation for another creator.
        if not session.target_slug:
            raise ForgeSlugConflict("Forge session lost its slug reservation")
        slug = session.target_slug
        if (await self._db.execute(
            select(Resident.id).where(Resident.slug == slug)
        )).scalar_one_or_none() is not None:
            raise ForgeSlugConflict("Resident slug already exists")

        district, tile_x, tile_y, home_loc_id = await allocate_resident_location(
            self._db,
            ability_text=ability_md,
            persona_text=persona_md,
            soul_text=soul_md,
        )

        # UGC type: the forge builds a player-authored character, not the
        # player's avatar — it inhabits the town but holds no political rights.
        resident = Resident(
            slug=slug, name=name, district=district, status="idle", heat=10,
            model_tier="standard", token_cost_per_turn=1, creator_id=session.user_id,
            resident_type=UGC_RESIDENT_TYPE,
            ability_md=ability_md, persona_md=persona_md, soul_md=soul_md,
            meta_json={"origin": "forge"},
            sprite_key=random.choice(SPRITE_KEYS),
            tile_x=tile_x, tile_y=tile_y, star_rating=2,
            home_location_id=home_loc_id,
        )
        self._db.add(resident)
        await self._db.flush()
        session.validation_report = {
            **(session.validation_report or {}),
            "star_rating": resident.star_rating,
            "district": resident.district,
            "resident_id": resident.id,
        }
        consume_session_slug(session)

        # Reward claim, resident insert, ledger credit and session terminal state
        # are committed together by run_to_completion.
        reward_granted = await try_claim_forge_reward(self._db, session.user_id)
        if reward_granted:
            await reward_pending(
                self._db, session.user_id, 50, f"forge_creation:{session.id}"
            )
        session.validation_report = {
            **(session.validation_report or {}),
            "reward_granted": reward_granted,
        }

    async def _run_quick(self, session: ForgeSession):
        """Quick mode: BuildStage only."""
        session.status = "building"
        session.current_stage = "build"
        await self._db.commit()
        await notify_forge_progress(session.user_id, session.id, "build", "building")

        raw_text = session.research_data.get("raw_text", "")
        user_material = session.research_data.get("user_material", "")
        input_text = user_material or raw_text or session.character_name

        await self._check_budget(session.id, session.user_id)
        build = BuildStage(llm_client=self._user_client, model=self._model,
                           session_id=session.id, user_id=session.user_id,
                           budget_check=lambda: self._check_budget(
                               session.id, session.user_id
                           ))
        build_result = await build.run(
            character_name=session.character_name,
            research_text=input_text,
        )
        session.build_output = build_result
        # Clear research_data to signal research was skipped
        session.research_data = {}

    async def _run_deep(self, session: ForgeSession):
        """Deep mode: Research -> Extract -> Build -> Validate -> Refine."""
        user_material = session.research_data.get("user_material", "")

        # Stage 1: Research
        session.status = "researching"
        session.current_stage = "research"
        await self._db.commit()
        await notify_forge_progress(session.user_id, session.id, "research", "researching")

        research = ResearchStage(searxng_url=self._searxng_url)
        research_results = await research.run(session.character_name, user_material)
        research_text = research.format_for_llm(research_results, user_material)
        session.research_data = {
            **session.research_data,
            "search_results": {k: len(v) for k, v in research_results.items()},
            "research_text_length": len(research_text),
        }
        await self._db.commit()

        # Stage 2: Extraction
        session.status = "extracting"
        session.current_stage = "extraction"
        await self._db.commit()
        await notify_forge_progress(session.user_id, session.id, "extraction", "extracting")

        await self._check_budget(session.id, session.user_id)
        extraction = ExtractionStage(llm_client=self._user_client, model=self._model,
                                     session_id=session.id, user_id=session.user_id,
                                     budget_check=lambda: self._check_budget(
                                         session.id, session.user_id
                                     ))
        extraction_result = await extraction.run(research_text, session.character_name)
        session.extraction_data = extraction_result
        await self._db.commit()

        # Stage 3: Build
        session.status = "building"
        session.current_stage = "build"
        await self._db.commit()
        await notify_forge_progress(session.user_id, session.id, "build", "building")

        await self._check_budget(session.id, session.user_id)
        build = BuildStage(llm_client=self._user_client, model=self._model,
                           session_id=session.id, user_id=session.user_id,
                           budget_check=lambda: self._check_budget(
                               session.id, session.user_id
                           ))
        build_result = await build.run(
            character_name=session.character_name,
            research_text=research_text,
            extraction_data=extraction_result,
        )
        session.build_output = build_result
        await self._db.commit()

        # Stage 4: Validation
        session.status = "validating"
        session.current_stage = "validation"
        await self._db.commit()
        await notify_forge_progress(session.user_id, session.id, "validation", "validating")

        await self._check_budget(session.id, session.user_id)
        validation = ValidationStage(llm_client=self._user_client, model=self._model,
                                     session_id=session.id, user_id=session.user_id,
                                     budget_check=lambda: self._check_budget(
                                         session.id, session.user_id
                                     ))
        validation_report = await validation.run(
            character_name=session.character_name,
            ability_md=build_result["ability_md"],
            persona_md=build_result["persona_md"],
            soul_md=build_result["soul_md"],
        )
        session.validation_report = validation_report
        await self._db.commit()

        # Stage 5: Refinement
        session.status = "refining"
        session.current_stage = "refinement"
        await self._db.commit()
        await notify_forge_progress(session.user_id, session.id, "refinement", "refining")

        await self._check_budget(session.id, session.user_id)
        refinement = RefinementStage(llm_client=self._user_client, model=self._model,
                                     session_id=session.id, user_id=session.user_id,
                                     budget_check=lambda: self._check_budget(
                                         session.id, session.user_id
                                     ))
        refined = await refinement.run(
            character_name=session.character_name,
            ability_md=build_result["ability_md"],
            persona_md=build_result["persona_md"],
            soul_md=build_result["soul_md"],
            validation_report=validation_report,
        )
        session.build_output = {
            "ability_md": refined["ability_md"],
            "persona_md": refined["persona_md"],
            "soul_md": refined["soul_md"],
        }
        session.refinement_log = {"stages": refined.get("refinement_log", [])}
