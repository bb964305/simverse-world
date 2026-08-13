"""S2-5 PolicyService — typed policy storage + the four-tier approval matrix.

Two orthogonal jobs, behind two independent default-False gates:

``POLIS_POLICY_ENABLED`` (storage)
    ``get``/``get_group``/``seed_defaults``/``apply_amend`` speak to the
    ``policies`` table. Off → every read falls back to ``ConfigService``
    (``system_config``) exactly as before S2-5 and no row is ever written.

``POLIS_POLICY_APPROVAL_ENABLED`` (routing)
    consumed by ``proposal_service`` (track A) and ``civic_service`` (track B);
    this module only supplies the matrix and the atomic write.

The matrix is pure rules — table lookup + threshold comparison + one conditional
UPDATE. **Zero new LLM calls**: the announcement rides the existing town-clerk
bulletin call inside ``civic_service.propose``.

Atomicity (KICKOFF §4): amends are optimistic-concurrency conditional UPDATEs
(``WHERE key = :k AND version = :expected``, ``rowcount == 1`` wins), never
read-modify-write; seeding is a dialect-aware idempotent upsert.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.policy import Policy
from app.services.config_service import ConfigService
from app.services.policy_labels import policy_label

logger = logging.getLogger(__name__)

# ── The four tiers (SOCIETY_EXPANSION_PLAN §3.3) ───────────────────────────
TIER_ADMINISTRATIVE = "administrative"
TIER_SIMPLE_MAJORITY = "simple_majority"
TIER_ABSOLUTE_MAJORITY = "absolute_majority"
TIER_CONSTITUTIONAL_CORE = "constitutional_core"

#: tier → {approval path, threshold, authority}. The threshold literals are the
#: spec defaults; the live numbers come from ``settings`` via
#: :func:`threshold_for` so ops can retune without a code change.
TIER_MATRIX: dict[str, dict[str, Any]] = {
    TIER_ADMINISTRATIVE:      {"path": "admin_direct",             "threshold": None,  "authority": "is_admin"},
    TIER_SIMPLE_MAJORITY:     {"path": "civic_poll",               "threshold": 0.50,  "authority": "vote"},
    TIER_ABSOLUTE_MAJORITY:   {"path": "civic_poll_supermajority", "threshold": 0.667, "authority": "vote", "quorum": True},
    TIER_CONSTITUTIONAL_CORE: {"path": "immutable",                "threshold": None,  "authority": "none"},
}

#: An unknown key routes to the conservative tier — never to ``admin_direct``
#: (silently reclassifying a referendum item as an administrative one is the
#: 最高级夺权手法 named in §3.3).
DEFAULT_TIER = TIER_SIMPLE_MAJORITY

#: Poll-metadata keys carried on ``options_json[0]`` — the same blob-on-opts[0]
#: convention ``civic_service`` already uses for ``_proposer_slug``.
META_KEY = "_policy_key"
META_THRESHOLD = "_policy_threshold"
META_QUORUM = "_policy_quorum"
META_OUTCOME = "_policy_outcome"

#: system_config probe counter (group/key) for constitutional-core touches.
PROBE_GROUP = "policy_probe"
CORE_TOUCH_KEY = "policy_core_touch_attempts"

#: Fiscal policies wired to S1-5 through fiscal_policy_service.
#: Keep the complete catalog separate from the compatibility-facing pending set:
#: older API clients still receive fiscal_pending, now false for every row.
FISCAL_POLICY_KEYS = frozenset({
    "tax_rate",
    "medical_subsidy_sc",
    "npc_default_wage_sc",
    "housing_development_scale",
})
FISCAL_PENDING_KEYS: frozenset[str] = frozenset()
BOOLEAN_POLICY_KEYS = frozenset({"caravan_enabled"})


def _routing_snapshot() -> dict[str, str]:
    """The value of the self-referential ``approval_routing`` policy: the
    tier→path map itself. Placing it at ``absolute_majority`` is the 自指保护
    of §3.3 — the routing rules can only be changed by supermajority, and any
    illegal downgrade is S3-7 (违宪控告) territory."""
    return {tier: spec["path"] for tier, spec in TIER_MATRIX.items()}


#: The seed catalog. ``default`` is either a literal or a ``("settings", name)``
#: pair resolved at call time (so a `.env` override is honored) — never frozen
#: at import.
POLICY_CATALOG: tuple[dict[str, Any], ...] = (
    # 行政级 — 对应管理者/镇长直批(活动核准、小额拨款、物理小改动)
    {"key": "civic_poll_days", "tier": TIER_ADMINISTRATIVE, "group": "civic",
     "default": ("settings", "civic_poll_days")},
    {"key": "market_day_weekday", "tier": TIER_ADMINISTRATIVE, "group": "civic",
     "default": ("settings", "market_day_weekday")},
    {"key": "market_day_discount", "tier": TIER_ADMINISTRATIVE, "group": "civic",
     "default": ("settings", "market_day_discount")},
    # 简单多数级 — 公民投票(税率、宵禁、营业规范、医疗补贴)
    {"key": "tax_rate", "tier": TIER_SIMPLE_MAJORITY, "group": "fiscal",
     "default": 0.0},
    {"key": "medical_subsidy_sc", "tier": TIER_SIMPLE_MAJORITY, "group": "fiscal",
     "default": 0},
    {"key": "npc_default_wage_sc", "tier": TIER_SIMPLE_MAJORITY, "group": "fiscal",
     "default": ("settings", "npc_default_wage_sc")},
    {"key": "caravan_enabled", "tier": TIER_SIMPLE_MAJORITY, "group": "civic",
     "default": ("settings", "caravan_enabled")},
    {"key": "curfew_hours", "tier": TIER_SIMPLE_MAJORITY, "group": "civic",
     "default": []},
    {"key": "business_hours", "tier": TIER_SIMPLE_MAJORITY, "group": "civic",
     "default": {"open": 8, "close": 20}},
    # 绝对多数级 — 公投高门槛(选举间隔、罢免门槛、审批路由规则本身、住房开发规模)
    {"key": "election_interval_days", "tier": TIER_ABSOLUTE_MAJORITY, "group": "routing",
     "default": ("settings", "election_interval_days")},
    {"key": "recall_threshold", "tier": TIER_ABSOLUTE_MAJORITY, "group": "routing",
     "default": 0.667},
    {"key": "approval_routing", "tier": TIER_ABSOLUTE_MAJORITY, "group": "routing",
     "default": ("routing_snapshot", None)},
    {"key": "housing_development_scale", "tier": TIER_ABSOLUTE_MAJORITY, "group": "fiscal",
     "default": 0},
    # 宪法核心 — 不可修改(选举存在、放逐权、实验楼审批门、信封定义、自指保护)
    {"key": "election_exists", "tier": TIER_CONSTITUTIONAL_CORE, "group": "constitution",
     "default": True},
    {"key": "exile_right", "tier": TIER_CONSTITUTIONAL_CORE, "group": "constitution",
     "default": True},
    {"key": "lab_approval_gate", "tier": TIER_CONSTITUTIONAL_CORE, "group": "constitution",
     "default": True},
    {"key": "lab_envelope_definition", "tier": TIER_CONSTITUTIONAL_CORE, "group": "constitution",
     "default": True},
    {"key": "lab_self_governance_immunity", "tier": TIER_CONSTITUTIONAL_CORE, "group": "constitution",
     "default": True},
)

CATALOG_BY_KEY: dict[str, dict[str, Any]] = {e["key"]: e for e in POLICY_CATALOG}


def procedure_for(tier: str) -> str:
    return TIER_MATRIX.get(tier, TIER_MATRIX[DEFAULT_TIER])["path"]


def threshold_for(tier: str) -> float | None:
    """Live threshold for a tier (settings-backed; None = not a vote tier)."""
    if tier == TIER_SIMPLE_MAJORITY:
        return settings.polis_policy_simple_majority_threshold
    if tier == TIER_ABSOLUTE_MAJORITY:
        return settings.polis_policy_absolute_majority_threshold
    return None


def requires_quorum(tier: str) -> bool:
    return bool(TIER_MATRIX.get(tier, {}).get("quorum"))


def catalog_default(key: str) -> Any:
    """Resolve a catalog entry's seed value at call time."""
    entry = CATALOG_BY_KEY.get(key)
    if entry is None:
        return None
    return _resolve_default(entry["default"])


def _resolve_default(spec: Any) -> Any:
    if isinstance(spec, tuple) and len(spec) == 2:
        kind, name = spec
        if kind == "settings":
            return getattr(settings, name)
        if kind == "routing_snapshot":
            return _routing_snapshot()
    return spec


class PolicyError(Exception):
    """Amend routing conflicts (router maps to 409)."""


class PolicyImmutableError(PolicyError):
    """A ``constitutional_core`` entry was targeted for direct modification.

    §3.3 红线: 宪法核心不可修改。The attempt is counted (probe: 核心条款触碰
    计数) and always rejected — 成功数恒 = 0."""


class PolicyValueError(PolicyError):
    """A typed fiscal value is outside the runtime-safe domain."""


def validate_fiscal_policy_value(key: str, value: Any) -> float | int:
    """Validate and normalize the four S1-5-backed fiscal policy values.

    Policy rows are JSON blobs, while TreasuryService consumes concrete,
    non-negative SC amounts and tax ratios. Rejecting invalid amendments here
    keeps every downstream fiscal write deterministic.
    """
    if key not in FISCAL_POLICY_KEYS:
        raise PolicyValueError(f"'{key}' is not a fiscal policy")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyValueError(f"policy '{key}' requires a numeric value")
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise PolicyValueError(f"policy '{key}' requires a finite value")
    if key == "tax_rate":
        if not 0.0 <= numeric <= 1.0:
            raise PolicyValueError("policy 'tax_rate' must be between 0 and 1")
        return numeric
    if numeric < 0 or not numeric.is_integer():
        raise PolicyValueError(f"policy '{key}' requires a non-negative integer")
    return int(numeric)


def validate_boolean_policy_value(key: str, value: Any) -> bool:
    if key not in BOOLEAN_POLICY_KEYS:
        raise PolicyValueError(f"'{key}' is not a boolean policy")
    if not isinstance(value, bool):
        raise PolicyValueError(f"policy '{key}' requires a boolean value")
    return value


@dataclass(frozen=True)
class AmendResult:
    """Where a proposed amend was routed (pure routing, no side effect beyond
    opening the poll for the vote tiers)."""
    key: str
    tier: str
    path: str
    threshold: float | None = None
    quorum: bool = False
    poll_id: int | None = None
    applied: bool = False
    fiscal_pending: bool = False


class PolicyService:
    """Mirrors ``ConfigService(db)`` construction (``config_service.py:11``)."""

    def __init__(self, db: AsyncSession):
        self._db = db

    # ── reads ────────────────────────────────────────────────────────────
    async def _row(self, key: str) -> Policy | None:
        return (await self._db.execute(
            select(Policy).where(Policy.key == key)
        )).scalar_one_or_none()

    async def get(self, key: str, *, default: Any = None) -> Any:
        """Typed policy value. Gate off (or a miss during the migration
        period) → ``ConfigService`` on ``system_config``, i.e. today's read."""
        if settings.polis_policy_enabled:
            row = await self._row(key)
            if row is not None:
                return json.loads(row.value)
        return await ConfigService(self._db).get(key, default=default)

    async def get_group(self, group: str) -> dict[str, Any]:
        if not settings.polis_policy_enabled:
            return await ConfigService(self._db).get_group(group)
        rows = (await self._db.execute(
            select(Policy).where(Policy.group == group)
        )).scalars().all()
        return {r.key: json.loads(r.value) for r in rows}

    async def list_all(self) -> list[dict[str, Any]]:
        """Admin/player projection: every row with its tier/procedure/version.

        Batch read on purpose — the tick-cost 红线 (§7) forbids per-resident
        policy queries; callers load the whole (hundred-row) table once.
        """
        if not settings.polis_policy_enabled:
            return []
        rows = (await self._db.execute(
            select(Policy).order_by(Policy.group, Policy.key)
        )).scalars().all()
        return [{
            "key": r.key,
            "value": json.loads(r.value),
            "tier": r.tier,
            "procedure": r.procedure,
            "group": r.group,
            "version": r.version,
            "updated_by": r.updated_by,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "fiscal_pending": r.key in FISCAL_PENDING_KEYS,
        } for r in rows]

    async def classify(self, key: str) -> str:
        """The tier governing ``key``. Row wins (a seeded world may retune a
        key's tier through the supermajority route), then the seed catalog,
        then the conservative default."""
        if settings.polis_policy_enabled:
            row = await self._row(key)
            if row is not None:
                return row.tier
        entry = CATALOG_BY_KEY.get(key)
        return entry["tier"] if entry else DEFAULT_TIER

    # ── seeding ──────────────────────────────────────────────────────────
    async def seed_defaults(self) -> int:
        """Idempotent upsert of the catalog into ``policies``. Returns the
        number of rows actually inserted (0 on a second run).

        Dialect-aware: ``INSERT ... ON CONFLICT (key) DO NOTHING`` on both
        sqlite (dev) and postgresql (prod); any other dialect falls back to a
        select-then-insert of the missing keys only.
        """
        if not settings.polis_policy_enabled:
            return 0
        now = datetime.now(UTC)
        rows = [{
            "key": e["key"],
            "value": json.dumps(_resolve_default(e["default"])),
            "tier": e["tier"],
            "procedure": procedure_for(e["tier"]),
            "group": e["group"],
            "version": 1,
            "updated_by": "seed",
            "created_at": now,
            "updated_at": now,
        } for e in POLICY_CATALOG]
        if not rows:
            return 0

        dialect = ""
        try:
            dialect = self._db.get_bind().dialect.name
        except Exception:  # pragma: no cover - defensive
            dialect = ""

        inserted = 0
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as _insert
            else:
                from sqlalchemy.dialects.postgresql import insert as _insert
            stmt = _insert(Policy.__table__).values(rows).on_conflict_do_nothing(
                index_elements=[Policy.__table__.c.key]
            )
            result = await self._db.execute(stmt)
            inserted = result.rowcount or 0
        else:  # pragma: no cover - no third dialect in this project
            existing = set((await self._db.execute(select(Policy.key))).scalars().all())
            missing = [r for r in rows if r["key"] not in existing]
            if missing:
                await self._db.execute(Policy.__table__.insert(), missing)
                inserted = len(missing)
        await self._db.commit()
        return inserted

    # ── writes ───────────────────────────────────────────────────────────
    async def apply_amend(
        self,
        key: str,
        new_value: Any,
        *,
        expected_version: int | None = None,
        updated_by: str,
    ) -> bool:
        """Optimistic-concurrency conditional UPDATE (KICKOFF §4).

        ``expected_version=None`` means "read the current version, then CAS on
        it" — still a conditional UPDATE, never a read-modify-write of the
        value; a racing amend between the read and the UPDATE makes this call
        lose (returns False) rather than clobber.
        """
        if not settings.polis_policy_enabled:
            return False
        if key in FISCAL_POLICY_KEYS:
            new_value = validate_fiscal_policy_value(key, new_value)
        elif key in BOOLEAN_POLICY_KEYS:
            new_value = validate_boolean_policy_value(key, new_value)
        tier = await self.classify(key)
        if tier == TIER_CONSTITUTIONAL_CORE:
            await self._record_core_touch(key, updated_by)
            raise PolicyImmutableError(
                f"policy '{key}' is constitutional_core and cannot be modified"
            )
        if expected_version is None:
            row = await self._row(key)
            if row is None:
                return False
            expected_version = row.version
        result = await self._db.execute(
            update(Policy)
            .where(Policy.key == key, Policy.version == expected_version)
            .values(
                value=json.dumps(new_value),
                version=Policy.version + 1,
                updated_by=updated_by,
                updated_at=datetime.now(UTC),
            )
            # ``fetch`` (not ``False``) on purpose: this UPDATE runs inside the
            # caller's session and the caller (admin endpoint / _close_one)
            # reads the new version right after. With no synchronization the
            # identity map would keep serving the pre-amend row.
            .execution_options(synchronize_session="fetch")
        )
        won = (result.rowcount or 0) == 1
        await self._db.commit()
        # C1: 政策值变了 —— 作废本进程的「小镇现况」快照(``policies`` 白名单里
        # 的 6 条直接进 prompt)。**无条件清**,不看 ``won``:CAS 输了说明有别人
        # 刚改成功,本进程手上那份快照照样是旧的。跨进程仍受 TTL 约束(见
        # ``invalidate_town_facts_cache`` 的 docstring)。局部 import:
        # town_facts_service 模块级 import 本模块,反向模块级会成环。
        try:
            from app.services.town_facts_service import invalidate_town_facts_cache
            invalidate_town_facts_cache()
        except Exception:  # pragma: no cover - 缓存清理不该反过来打断写入
            logger.warning("town facts cache invalidation failed", exc_info=True)
        return won

    async def propose_amend(
        self,
        key: str,
        new_value: Any,
        *,
        origin: str,
        author: str,
        rng=None,
    ) -> AmendResult:
        """Pure routing: tier → path.

        ``administrative`` → returns the admin_direct route (the admin endpoint
        applies); ``simple_majority`` / ``absolute_majority`` → opens a civic
        poll carrying the threshold (and quorum flag) metadata;
        ``constitutional_core`` → :class:`PolicyImmutableError`.

        ``rng`` is accepted for signature conformance with the seeded-RNG
        discipline; this path has no random branch (routing is a table lookup).
        """
        if key in FISCAL_POLICY_KEYS:
            new_value = validate_fiscal_policy_value(key, new_value)
        elif key in BOOLEAN_POLICY_KEYS:
            new_value = validate_boolean_policy_value(key, new_value)
        tier = await self.classify(key)
        if tier == TIER_CONSTITUTIONAL_CORE:
            await self._record_core_touch(key, author)
            raise PolicyImmutableError(
                f"policy '{key}' is constitutional_core and cannot be amended"
            )
        path = procedure_for(tier)
        fiscal = key in FISCAL_PENDING_KEYS
        if tier == TIER_ADMINISTRATIVE:
            return AmendResult(key=key, tier=tier, path=path, fiscal_pending=fiscal)

        threshold = threshold_for(tier)
        quorum = requires_quorum(tier)
        poll_id = await self._open_amend_poll(
            key, new_value, tier=tier, threshold=threshold, quorum=quorum,
            author=author, origin=origin,
        )
        return AmendResult(key=key, tier=tier, path=path, threshold=threshold,
                           quorum=quorum, poll_id=poll_id, fiscal_pending=fiscal)

    async def _open_amend_poll(
        self, key: str, new_value: Any, *, tier: str, threshold: float | None,
        quorum: bool, author: str, origin: str,
    ) -> int | None:
        """Open a civic poll for a vote-tier amend and stamp the tier metadata
        onto ``options_json[0]`` (the same blob-on-opts[0] convention
        ``civic_service`` uses for ``_proposer_slug``).

        Reuses ``civic_service.propose`` verbatim — the clerk announcement is
        an existing call, so this adds **zero new LLM calls**.
        """
        from sqlalchemy.orm.attributes import flag_modified
        from app.services import civic_service

        # 标题走中文标签,绝不用原始键:它经 town_facts_service 进每位 NPC 的
        # decide prompt(K4 禁 ``tax``),又经 _clerk_announce 广播成全镇的持久记忆。
        label = (f"将「{policy_label(key)}」调整为 "
                 f"{json.dumps(new_value, ensure_ascii=False)}")
        poll = await civic_service.propose(
            self._db,
            label,
            [
                {"label": "赞成", "effect": {
                    "type": "policy", "key": key, "value": new_value, "tier": tier,
                }},
                {"label": "反对", "effect": None},
            ],
            proposer_slug=author if origin == "resident" else None,
        )
        if poll is None:  # civic_polls_enabled off — nothing to stamp
            return None
        opts = list(poll.options_json or [])
        if opts:
            opts[0][META_KEY] = key
            opts[0][META_THRESHOLD] = threshold
            opts[0][META_QUORUM] = quorum
            poll.options_json = opts
            flag_modified(poll, "options_json")
            await self._db.commit()
        return poll.id

    # ── probe bookkeeping ────────────────────────────────────────────────
    async def _record_core_touch(self, key: str, actor: str) -> None:
        """Count a constitutional-core modification attempt (§6 probe:
        尝试数可 >0, 成功数恒 = 0).

        A best-effort **telemetry** counter in ``system_config`` — not policy
        state, so the §4 conditional-UPDATE 红线 (which governs policy writes)
        does not apply; an under-count under heavy concurrency is acceptable
        and a failure here must never mask the rejection.
        """
        if not settings.polis_policy_enabled:
            return
        try:
            svc = ConfigService(self._db)
            cur = await svc.get(CORE_TOUCH_KEY, default=None)
            if not isinstance(cur, dict):
                cur = {"attempts": 0, "by_key": {}}
            cur["attempts"] = int(cur.get("attempts", 0)) + 1
            by_key = dict(cur.get("by_key") or {})
            by_key[key] = int(by_key.get(key, 0)) + 1
            cur["by_key"] = by_key
            cur["last_actor"] = actor
            await svc.set(CORE_TOUCH_KEY, cur, group=PROBE_GROUP,
                          updated_by=actor or "unknown")
        except Exception:
            logger.warning("core-touch probe counter update failed", exc_info=True)
