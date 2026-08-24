"""Restart-safe scheduler and one-turn executor for hosted Agent players."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import SecretStr
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.llm.metering import record_usage
from app.models.agent_player import AgentPlayer
from app.models.hosted_agent import (
    HostedAgentController,
    HostedAgentDailyUsage,
    HostedAgentTurn,
)
from app.services.content_guard import assert_resident_content_clean
from app.services.hosted_agent_api_client import (
    HostedAgentApiClient,
    HostedAgentApiError,
)
from app.services.hosted_agent_provider import (
    HostedGeneratedIdentity,
    HostedModelDecision,
    HostedOpenAIClient,
    HostedProviderError,
    decision_to_agent_request,
    deterministic_public_action_summary,
    derive_hosted_identity,
    hosted_decision_token_reservation,
    hosted_identity_token_reservation,
    hosted_preflight_token_reservation,
    validate_decision_against_observation,
    validate_hosted_provider_base_url,
)
from app.services.hosted_agent_service import (
    HostedAgentError,
    abandon_turn_and_release_controller,
    adopt_recoverable_turn,
    block_unknown_provider_outcome,
    begin_provider_stage_call,
    claim_due_controller,
    complete_provisioning_without_identity_change,
    complete_provider_stage_call,
    completed_provider_stage_result,
    create_hosted_identity,
    create_turn_journal,
    decrypt_secret_bundle,
    decrypt_turn_value,
    fail_controller,
    fail_unbilled_turn_and_release_controller,
    mark_turn_committing,
    mark_turn_failed,
    persist_turn_decision,
    reconcile_private_journal,
    release_controller,
    renew_controller_lease,
    replace_secret_bundle_under_lease,
    reserve_turn_provider_budget,
    settle_daily_budget,
    HOSTED_DECISION_FIELD,
)
from app.tasks.loop_heartbeat import beat


logger = logging.getLogger(__name__)
T = TypeVar("T")


class HostedLeaseLost(RuntimeError):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _retry_seconds(controller: HostedAgentController) -> int:
    return min(3600, max(30, 30 * (2 ** min(controller.retry_count, 7))))


def _seconds_until_utc_reset() -> int:
    now = datetime.now(UTC)
    reset = datetime.combine(
        now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )
    return max(1, int((reset - now).total_seconds()))


class HostedAgentWorker:
    def __init__(self) -> None:
        self.worker_id = (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )
        self.api = HostedAgentApiClient()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    async def aclose(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.api.aclose()

    async def run(self) -> None:
        while not self._stopping.is_set():
            await beat("hosted_agent", check=True)
            self._tasks = {task for task in self._tasks if not task.done()}
            if settings.hosted_agent_runner_enabled:
                while len(self._tasks) < settings.hosted_agent_runner_max_concurrent:
                    async with async_session() as db:
                        controller = await claim_due_controller(
                            db, worker_id=self.worker_id
                        )
                    if controller is None:
                        break
                    task = asyncio.create_task(
                        self._process(controller),
                        name=f"hosted-agent:{controller.id}",
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._task_done)
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=settings.hosted_agent_runner_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # Never interpolate exception text: provider implementations may
            # reflect request headers into arbitrary exception messages.
            logger.error("hosted Agent task failed (%s)", type(exc).__name__)

    async def _lease_owned(self, controller: HostedAgentController) -> bool:
        async with async_session() as db:
            current = await db.get(HostedAgentController, controller.id)
            now = datetime.now(UTC)
            return bool(
                current is not None
                and current.desired_status == "running"
                and current.runtime_status == "claimed"
                and current.lease_token == controller.lease_token
                and current.lease_epoch == controller.lease_epoch
                and current.control_version == controller.control_version
                and (_aware(current.lease_expires_at) or now) > now
            )

    async def _watched(
        self,
        controller: HostedAgentController,
        operation: Awaitable[T],
    ) -> T:
        task = asyncio.create_task(operation)
        last_renew = asyncio.get_running_loop().time()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=1.0)
                if task in done:
                    return task.result()
                if self._stopping.is_set() or not await self._lease_owned(controller):
                    raise HostedLeaseLost
                now_mono = asyncio.get_running_loop().time()
                if now_mono - last_renew >= 30:
                    async with async_session() as db:
                        if not await renew_controller_lease(db, controller=controller):
                            raise HostedLeaseLost
                    last_renew = now_mono
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _meter(
        self,
        controller: HostedAgentController,
        usage: Any,
        *,
        scenario: str,
        parse_ok: bool,
    ) -> None:
        user_id = controller.owner_user_id
        resident_id = None
        if controller.agent_player_id:
            async with async_session() as db:
                profile = await db.get(AgentPlayer, controller.agent_player_id)
                if profile is not None:
                    user_id = profile.user_id
                    resident_id = profile.resident_id
        try:
            await record_usage(
                scenario,
                model=controller.model[:80],
                owner="user",
                est_input_tokens=usage.input_tokens,
                est_output_tokens=usage.output_tokens,
                user_id=user_id,
                resident_id=resident_id,
                parse_ok=parse_ok,
                latency_ms=usage.latency_ms,
            )
        except Exception:
            # The durable per-controller budget is authoritative. Optional
            # aggregate telemetry must never replay a paid provider call, and
            # exception text may contain provider-controlled data.
            logger.warning("hosted Agent aggregate usage metering failed")

    async def _provider_call(
        self,
        controller: HostedAgentController,
        *,
        reserve_tokens: int,
        operation: Callable[[], Awaitable[T]],
        scenario: str,
        stage: str = "preflight",
        result_payload: Callable[[T], dict[str, Any]] | None = None,
    ) -> T:
        async with async_session() as db:
            marker = await begin_provider_stage_call(
                db,
                controller=controller,
                stage=stage,
                reserve_tokens=reserve_tokens,
            )
        try:
            result = await self._watched(controller, operation())
        except HostedProviderError as exc:
            if exc.usage is not None:
                async with async_session() as db:
                    within_reservation = await complete_provider_stage_call(
                        db,
                        controller=controller,
                        turn_id=marker.id,
                        result_value={"stage": stage, "ok": False},
                        usage={
                            "provider_request_id": exc.usage.provider_request_id,
                            "input_tokens": exc.usage.input_tokens,
                            "output_tokens": exc.usage.output_tokens,
                        },
                        success=False,
                        error_code=exc.code,
                    )
                await self._meter(
                    controller, exc.usage, scenario=scenario, parse_ok=False
                )
                if within_reservation is None:
                    raise HostedLeaseLost from exc
                if not within_reservation:
                    raise HostedAgentError(
                        "provider_usage_exceeded_reservation",
                        "Provider usage exceeded the admitted token reservation",
                        502,
                    ) from exc
            elif exc.definitively_unbilled:
                async with async_session() as db:
                    released = await fail_unbilled_turn_and_release_controller(
                        db,
                        controller=controller,
                        turn_id=marker.id,
                        error_code=exc.code,
                        runtime_status=(
                            "auth_blocked"
                            if exc.code == "provider_auth_failed"
                            else "backoff"
                        ),
                        retry_seconds=_retry_seconds(controller),
                    )
                if not released:
                    raise HostedLeaseLost from exc
            else:
                # A read/write/protocol/overall timeout may occur after the
                # provider has executed and billed the completion. Preserve
                # the reservation and require an explicit operator resume.
                async with async_session() as db:
                    blocked = await block_unknown_provider_outcome(
                        db, controller=controller, turn_id=marker.id
                    )
                if not blocked:
                    raise HostedLeaseLost from exc
            raise
        except Exception:
            # The reservation intentionally remains charged until UTC reset
            # when the outcome of an interrupted remote call is unknown.
            raise
        usage = result if hasattr(result, "input_tokens") else result[1]
        async with async_session() as db:
            within_reservation = await complete_provider_stage_call(
                db,
                controller=controller,
                turn_id=marker.id,
                result_value=(
                    result_payload(result)
                    if result_payload is not None
                    else {"stage": stage, "ok": True}
                ),
                usage={
                    "provider_request_id": usage.provider_request_id,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
            )
        if within_reservation is None:
            raise HostedLeaseLost
        await self._meter(controller, usage, scenario=scenario, parse_ok=True)
        if not within_reservation:
            raise HostedAgentError(
                "provider_usage_exceeded_reservation",
                "Provider usage exceeded the admitted token reservation",
                502,
            )
        return result

    async def _provision(
        self, controller: HostedAgentController, bundle: dict[str, Any]
    ) -> None:
        base_url, _host = await validate_hosted_provider_base_url(str(bundle["base_url"]))
        async with HostedOpenAIClient(
            base_url=base_url,
            api_key=SecretStr(str(bundle["api_key"])),
            model=controller.model,
        ) as provider:
            async with async_session() as db:
                preflight_result = await completed_provider_stage_result(
                    db, controller=controller, stage="preflight"
                )
            if preflight_result is None:
                await self._provider_call(
                    controller,
                    reserve_tokens=hosted_preflight_token_reservation(),
                    operation=provider.preflight,
                    scenario="hosted_agent_preflight",
                    stage="preflight",
                    result_payload=lambda _result: {"stage": "preflight", "ok": True},
                )
            if controller.agent_player_id:
                async with async_session() as db:
                    if not await complete_provisioning_without_identity_change(
                        db, controller=controller
                    ):
                        raise HostedLeaseLost
                return
            async with async_session() as db:
                identity_result = await completed_provider_stage_result(
                    db, controller=controller, stage="identity"
                )
            if identity_result is not None:
                generated = HostedGeneratedIdentity.model_validate(
                    identity_result.get("identity")
                )
            else:
                generated, _usage = await self._provider_call(
                    controller,
                    reserve_tokens=hosted_identity_token_reservation(
                        display_name=str(bundle["display_name"]),
                        public_goal=str(bundle["public_goal"]),
                    ),
                    operation=lambda: provider.initialize_identity(
                        display_name=str(bundle["display_name"]),
                        public_goal=str(bundle["public_goal"]),
                    ),
                    scenario="hosted_agent_identity",
                    stage="identity",
                    result_payload=lambda result: {
                        "stage": "identity",
                        "identity": result[0].model_dump(),
                    },
                )
        public_identity, role_card, private_identity = derive_hosted_identity(
            generated=generated,
            display_name=str(bundle["display_name"]),
            model_label=controller.model,
            sprite_key=str(bundle["sprite_key"]),
            public_goal=str(bundle["public_goal"]),
        )
        assert_resident_content_clean(
            name=public_identity["display_name"],
            ability_md=role_card["ability_md"],
            persona_md=role_card["persona_md"],
            soul_md=role_card["soul_md"],
        )
        completed_bundle = {
            **bundle,
            "private_identity": private_identity,
            "journal": [],
            "disclosed_player_slugs": [],
        }
        async with async_session() as db:
            await create_hosted_identity(
                db,
                controller=controller,
                display_name=public_identity["display_name"],
                sprite_key=str(bundle["sprite_key"]),
                public_role=role_card,
                public_identity=public_identity,
                play_secret_bundle=completed_bundle,
            )

    async def _session_call(
        self,
        *,
        controller: HostedAgentController,
        play_token: SecretStr,
        call: Callable[[SecretStr], Awaitable[T]],
    ) -> T:
        assert controller.agent_player_id is not None
        session = await self.api.session(
            profile_id=controller.agent_player_id, play_token=play_token
        )
        try:
            return await self._watched(controller, call(session))
        except HostedAgentApiError as exc:
            if exc.status_code != 401:
                raise
        self.api.invalidate_session(controller.agent_player_id)
        session = await self.api.session(
            profile_id=controller.agent_player_id, play_token=play_token
        )
        return await self._watched(controller, call(session))

    async def _claim_daily_reward(
        self,
        *,
        controller: HostedAgentController,
        bundle: dict[str, Any],
        play_token: SecretStr,
    ) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
        if bundle.get("last_daily_reward_date") == today:
            return bundle
        await self._session_call(
            controller=controller,
            play_token=play_token,
            call=lambda session: self.api.daily_reward(session_token=session),
        )
        bundle = {**bundle, "last_daily_reward_date": today}
        async with async_session() as db:
            await replace_secret_bundle_under_lease(
                db, controller=controller, bundle=bundle
            )
        return bundle

    async def _submit_turn(
        self,
        *,
        controller: HostedAgentController,
        turn: HostedAgentTurn,
        play_token: SecretStr,
    ) -> None:
        decision_data = decrypt_turn_value(turn, HOSTED_DECISION_FIELD)
        decision = HostedModelDecision.model_validate(decision_data)
        action_type, params = decision_to_agent_request(decision)
        async with async_session() as db:
            turn = await mark_turn_committing(
                db, controller=controller, turn_id=turn.id
            )
        headers = {
            "X-Simverse-Hosted-Controller-ID": controller.id,
            "X-Simverse-Hosted-Lease-Token": str(controller.lease_token),
            "X-Simverse-Hosted-Lease-Epoch": str(controller.lease_epoch),
            "X-Simverse-Hosted-Control-Version": str(controller.control_version),
            "X-Simverse-Hosted-Turn-ID": turn.id,
            "X-Simverse-Hosted-Event-Cursor": str(turn.event_cursor or 0),
        }
        try:
            if action_type == "npc_chat_turn":
                await self._session_call(
                    controller=controller,
                    play_token=play_token,
                    call=lambda session: self.api.npc_chat(
                        session_token=session,
                        turn_id=str(turn.action_id),
                        observation_seq=int(turn.observation_seq or 0),
                        resident_slug=str(params["resident_slug"]),
                        text=str(params["text"]),
                        fence_headers=headers,
                    ),
                )
            else:
                await self._session_call(
                    controller=controller,
                    play_token=play_token,
                    call=lambda session: self.api.action(
                        session_token=session,
                        action_id=str(turn.action_id),
                        observation_seq=int(turn.observation_seq or 0),
                        action_type=action_type,
                        params=params,
                        fence_headers=headers,
                    ),
                )
        except (HostedAgentApiError, HostedLeaseLost) as exc:
            async with async_session() as db:
                current = await db.get(HostedAgentTurn, turn.id)
                if current is not None and current.state in {
                    "completed", "failed", "abandoned"
                }:
                    return
                retry_same = isinstance(exc, HostedLeaseLost) or (
                    isinstance(exc, HostedAgentApiError)
                    and (
                        exc.status_code >= 500
                        or exc.status_code == 429
                        or exc.code
                        in {
                            "agent_api_unavailable",
                            "npc_chat_temporarily_unavailable",
                            "turn_in_progress",
                        }
                    )
                )
                if not retry_same and isinstance(exc, HostedAgentApiError):
                    await abandon_turn_and_release_controller(
                        db,
                        controller=controller,
                        turn_id=turn.id,
                        error_code=exc.code,
                    )
                    return
            raise

    async def _play(
        self, controller: HostedAgentController, bundle: dict[str, Any]
    ) -> None:
        play_token_raw = bundle.get("play_token")
        if not isinstance(play_token_raw, str) or not play_token_raw:
            raise HostedAgentError(
                "play_credential_unavailable", "Hosted Agent play credential is unavailable", 503
            )
        play_token = SecretStr(play_token_raw)
        try:
            bundle = await self._claim_daily_reward(
                controller=controller, bundle=bundle, play_token=play_token
            )
            observation = await self._session_call(
                controller=controller,
                play_token=play_token,
                call=lambda session: self.api.observe(session_token=session),
            )
            async with async_session() as db:
                bundle = await reconcile_private_journal(db, controller=controller)

            retry_at = _aware(controller.provider_retry_at)
            now = datetime.now(UTC)
            if retry_at is not None and retry_at > now:
                status = (
                    "auth_blocked"
                    if controller.last_error_code == "provider_auth_failed"
                    else "budget_paused"
                    if controller.last_error_code in {
                        "token_budget_exhausted",
                        "provider_call_budget_exhausted",
                        "hosted_action_budget_exhausted",
                    }
                    else "backoff"
                )
                async with async_session() as db:
                    await release_controller(
                        db,
                        controller=controller,
                        runtime_status=status,
                        last_presence_at=now,
                    )
                return

            async with async_session() as db:
                recovered = await adopt_recoverable_turn(db, controller=controller)
            if recovered is not None:
                await self._submit_turn(
                    controller=controller, turn=recovered, play_token=play_token
                )
                return
            if (_aware(controller.next_action_at) or now) > now:
                async with async_session() as db:
                    await release_controller(
                        db, controller=controller, last_presence_at=now
                    )
                return
            async with async_session() as db:
                usage = await db.get(
                    HostedAgentDailyUsage, (controller.id, now.date())
                )
                if usage is not None and usage.actions >= controller.max_actions_per_day:
                    await fail_controller(
                        db,
                        controller=controller,
                        error_code="hosted_action_budget_exhausted",
                        runtime_status="budget_paused",
                        retry_seconds=_seconds_until_utc_reset(),
                        presence_ok=True,
                    )
                    return
                turn = await create_turn_journal(
                    db, controller=controller, observation=observation
                )

            private_context = {
                "identity": bundle.get("private_identity", {}),
                "journal": bundle.get("journal", [])[-20:],
                "disclosed_player_slugs": bundle.get("disclosed_player_slugs", []),
            }
            reserve_tokens = hosted_decision_token_reservation(
                observation=observation,
                public_identity=controller.identity_json,
                private_identity=private_context,
                max_output_tokens=controller.max_output_tokens,
            )
            async with async_session() as db:
                turn = await reserve_turn_provider_budget(
                    db,
                    controller=controller,
                    turn_id=turn.id,
                    reserve_tokens=reserve_tokens,
                )
            base_url, _host = await validate_hosted_provider_base_url(str(bundle["base_url"]))
            async with HostedOpenAIClient(
                base_url=base_url,
                api_key=SecretStr(str(bundle["api_key"])),
                model=controller.model,
            ) as provider:
                try:
                    decision, provider_usage = await self._watched(
                        controller,
                        provider.decide(
                            observation=observation,
                            public_identity=controller.identity_json,
                            private_identity=private_context,
                            max_tokens=controller.max_output_tokens,
                        ),
                    )
                except HostedProviderError as exc:
                    if exc.usage is not None:
                        async with async_session() as db:
                            within_reservation = await settle_daily_budget(
                                db,
                                controller_id=controller.id,
                                usage_date=turn.budget_date,
                                reserve_tokens=reserve_tokens,
                                input_tokens=exc.usage.input_tokens,
                                output_tokens=exc.usage.output_tokens,
                            )
                        await self._meter(
                            controller,
                            exc.usage,
                            scenario="hosted_agent_decision",
                            parse_ok=False,
                        )
                        async with async_session() as db:
                            await mark_turn_failed(
                                db,
                                turn_id=turn.id,
                                error_code=exc.code,
                            )
                        if not within_reservation:
                            raise HostedAgentError(
                                "provider_usage_exceeded_reservation",
                                "Provider usage exceeded the admitted token reservation",
                                502,
                            ) from exc
                        raise
                    if exc.definitively_unbilled:
                        runtime_status = (
                            "auth_blocked"
                            if exc.code == "provider_auth_failed"
                            else "backoff"
                        )
                        async with async_session() as db:
                            released = await fail_unbilled_turn_and_release_controller(
                                db,
                                controller=controller,
                                turn_id=turn.id,
                                error_code=exc.code,
                                runtime_status=runtime_status,
                                retry_seconds=_retry_seconds(controller),
                            )
                        if released:
                            return
                    async with async_session() as db:
                        await block_unknown_provider_outcome(
                            db,
                            controller=controller,
                            turn_id=turn.id,
                        )
                    return
            async with async_session() as db:
                within_reservation = await settle_daily_budget(
                    db,
                    controller_id=controller.id,
                    usage_date=turn.budget_date,
                    reserve_tokens=reserve_tokens,
                    input_tokens=provider_usage.input_tokens,
                    output_tokens=provider_usage.output_tokens,
                )
            if not within_reservation:
                raise HostedAgentError(
                    "provider_usage_exceeded_reservation",
                    "Provider usage exceeded the admitted token reservation",
                    502,
                )
            await self._meter(
                controller,
                provider_usage,
                scenario="hosted_agent_decision",
                parse_ok=True,
            )
            validate_decision_against_observation(decision, observation)
            if decision.action == "message_player":
                disclosed = {
                    str(item)
                    for item in private_context["disclosed_player_slugs"]
                    if isinstance(item, str)
                }
                if decision.player_slug not in disclosed:
                    prefix = "我是一个由 AI 控制的 Simverse 居民。"
                    message_affordance = next(
                        (
                            item
                            for item in observation.get("affordances", [])
                            if isinstance(item, dict)
                            and item.get("action") == "message_player"
                        ),
                        {},
                    )
                    max_chars = int(message_affordance.get("max_chars", 280))
                    text = (prefix + str(decision.text or ""))[:max_chars]
                    decision = decision.model_copy(update={"text": text})
                    validate_decision_against_observation(decision, observation)
            action_type, _params = decision_to_agent_request(decision)
            async with async_session() as db:
                turn = await persist_turn_decision(
                    db,
                    controller=controller,
                    turn_id=turn.id,
                    decision=decision.model_dump(exclude_none=True),
                    action_type=action_type,
                    public_summary=deterministic_public_action_summary(decision),
                    usage={
                        "provider_request_id": provider_usage.provider_request_id,
                        "input_tokens": provider_usage.input_tokens,
                        "output_tokens": provider_usage.output_tokens,
                        "total_tokens": provider_usage.total_tokens,
                    },
                )
            await self._submit_turn(
                controller=controller, turn=turn, play_token=play_token
            )
        finally:
            play_token = SecretStr("")

    async def _process(self, controller: HostedAgentController) -> None:
        turn_id: str | None = None
        bundle: dict[str, Any] = {}
        try:
            bundle = decrypt_secret_bundle(controller)
            retry_at = _aware(controller.provider_retry_at)
            if controller.provider_validation_required and (
                retry_at is None or retry_at <= datetime.now(UTC)
            ):
                await self._provision(controller, bundle)
            elif controller.agent_player_id:
                await self._play(controller, bundle)
            else:
                async with async_session() as db:
                    await release_controller(
                        db,
                        controller=controller,
                        runtime_status="backoff",
                    )
        except HostedLeaseLost:
            return
        except HostedProviderError as exc:
            status = "auth_blocked" if exc.code == "provider_auth_failed" else "backoff"
            async with async_session() as db:
                await fail_controller(
                    db,
                    controller=controller,
                    error_code=exc.code,
                    runtime_status=status,
                    retry_seconds=_retry_seconds(controller),
                    presence_ok=controller.agent_player_id is not None,
                )
        except HostedAgentApiError as exc:
            action_budget_exhausted = exc.code == "hosted_action_budget_exhausted"
            status = (
                "auth_blocked"
                if exc.status_code in {401, 403}
                else "budget_paused"
                if action_budget_exhausted
                else "backoff"
            )
            async with async_session() as db:
                await fail_controller(
                    db,
                    controller=controller,
                    error_code=exc.code,
                    runtime_status=status,
                    retry_seconds=(
                        _seconds_until_utc_reset()
                        if action_budget_exhausted
                        else _retry_seconds(controller)
                    ),
                    presence_ok=controller.agent_player_id is not None,
                )
        except HostedAgentError as exc:
            status = (
                "budget_paused"
                if exc.code in {"token_budget_exhausted", "provider_call_budget_exhausted"}
                else "error"
            )
            async with async_session() as db:
                if turn_id:
                    await mark_turn_failed(db, turn_id=turn_id, error_code=exc.code)
                await fail_controller(
                    db,
                    controller=controller,
                    error_code=exc.code,
                    runtime_status=status,
                    retry_seconds=(
                        _seconds_until_utc_reset()
                        if status == "budget_paused"
                        else _retry_seconds(controller)
                    ),
                    presence_ok=controller.agent_player_id is not None,
                )
        except Exception:
            async with async_session() as db:
                await fail_controller(
                    db,
                    controller=controller,
                    error_code="hosted_worker_error",
                    runtime_status="error",
                    retry_seconds=_retry_seconds(controller),
                    presence_ok=controller.agent_player_id is not None,
                )
        finally:
            for key in ("api_key", "play_token"):
                if key in bundle:
                    bundle[key] = ""
