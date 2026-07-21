"""Import every model module so Base.metadata / mapper configuration is
complete in any process that touches the ORM (API, agent-worker, scripts).

Mapper configuration resolves cross-table FKs lazily at first query; a process
that only imports some models (e.g. the agent-worker) blows up with
NoReferencedTableError on relationships into un-imported tables.
"""
import app.models.user  # noqa: F401
import app.models.resident  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.transaction  # noqa: F401
import app.models.system_config  # noqa: F401
import app.models.forge_session  # noqa: F401
import app.models.pending_message  # noqa: F401
import app.models.memory  # noqa: F401
import app.models.personality_history  # noqa: F401
import app.models.llm_usage  # noqa: F401
import app.models.world_event  # noqa: F401
import app.models.notification  # noqa: F401
import app.models.achievement  # noqa: F401
import app.models.shop  # noqa: F401
import app.models.location_visit  # noqa: F401
import app.models.digest  # noqa: F401
import app.models.daily_quest  # noqa: F401
import app.models.commission  # noqa: F401
import app.models.resident_goal  # noqa: F401
import app.models.bulletin_post  # noqa: F401
import app.models.time_capsule  # noqa: F401
import app.models.feed  # noqa: F401
import app.models.season  # noqa: F401
import app.models.goal_investment  # noqa: F401
import app.models.debate  # noqa: F401
# Lab (experiment building) core — P1
import app.models.coin_hold  # noqa: F401
import app.models.coin_hold_entry  # noqa: F401
import app.models.lab_terminalization  # noqa: F401
import app.models.resident_treasury  # noqa: F401
import app.models.lab_task  # noqa: F401
import app.models.lab_run  # noqa: F401
import app.models.lab_artifact  # noqa: F401
# World governance overlay — P3
import app.models.world_change_proposal  # noqa: F401
import app.models.dynamic_location  # noqa: F401
import app.models.dynamic_mechanic  # noqa: F401
# Lab Agent v1 — grant/policy/broker/ledger protocol contracts (P0, T1)
import app.models.lab_event  # noqa: F401
import app.models.lab_grant  # noqa: F401
import app.models.lab_action  # noqa: F401
import app.models.lab_lease  # noqa: F401
import app.models.lab_budget  # noqa: F401
import app.models.lab_worker_attempt  # noqa: F401
import app.models.world_revision  # noqa: F401
# Lab Agent protocol v2 durable Gateway state (migration 039)
import app.models.lab_runtime  # noqa: F401
import app.models.lab_control  # noqa: F401
