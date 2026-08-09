"""S1-3 议题立场与舆论动力学 — bounded-confidence opinion dynamics (zero LLM).

Every resident holds a scalar ``stance ∈ [-1, 1]`` per *issue* (a denormalized
free-string key — no issues registry, see ``app/models/issue_stance.py``). The
stance moves in small Deffuant steps on three **zero-new-LLM** signals:

- ``update_from_chat``  — consumes the ``mood`` already returned by
  ``MemoryService.process_chat_wrapup`` (no extra call);
- ``update_from_debate`` — ``create_debate`` seeds opposing stances (reliable:
  announced always happens); ``settle`` reinforcement is opportunistic (the
  debate lifecycle is only half-wired today — KICKOFF §1 现状缺口);
- ``drift`` — nightly rule step shaped after ``civic_service._npc_choice``:
  each resident moves toward the (affinity-weighted) mean of "trusted
  neighbours" = other stance-holders within ε.

All writes go through ``_bump_stance``: a single ``INSERT .. ON CONFLICT DO
UPDATE`` whose new value is computed *inside* the SQL (bounded-confidence CASE
+ portable clamp) — never read-modify-write, per the ``coin_service`` /
``relation_service`` atomicity standard.

Gate: ``settings.polis_opinion_enabled`` (default False) → the three public
mutators return 0 without touching the table (byte-identical fallback).
"""

from __future__ import annotations

import logging
import random
import re
import uuid
from datetime import datetime, timedelta, UTC
from typing import TYPE_CHECKING

from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.issue_stance import IssueStance

if TYPE_CHECKING:  # pragma: no cover
    from app.models.debate import Debate

logger = logging.getLogger(__name__)

# Seed-magnitude modulation from the SBTI A2 (规则与灵活度) dimension, shaped
# after civic_service._npc_choice: 守序 (H) residents take milder stances,
# 求变 (L) residents bolder ones. Missing SBTI → exactly ±seed_mag (fallback).
_A2_MAG = {"H": 0.75, "L": 1.25}
# ε value that always passes the bounded-confidence CASE (unconditional move),
# used for debate seeding / settle reinforcement where the move is structural.
_EPS_BYPASS = 2.0


class OpinionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── keys ─────────────────────────────────────────────────────────────
    @staticmethod
    def _normalize_issue_key(topic: str) -> str:
        """Strip + collapse internal whitespace, truncate to the column width.
        No case folding (topics are 中文-first). Same topic string recurring
        across debates/polls reuses the same rows — desired behaviour."""
        return re.sub(r"\s+", " ", (topic or "").strip())[:300]

    # ── reads ────────────────────────────────────────────────────────────
    async def get_stance(self, issue_key: str, resident_slug: str) -> float | None:
        """Current stance, or None when the resident never took one."""
        return (await self.db.execute(
            select(IssueStance.stance).where(
                IssueStance.issue_key == issue_key,
                IssueStance.resident_slug == resident_slug,
            )
        )).scalar_one_or_none()

    async def list_stances(
        self, resident_slug: str, *, limit: int = 3,
    ) -> list[tuple[str, float]]:
        """这位居民最近表过态的几个议题 —— ``[(issue_key, stance), ...]``,最近
        更新的在前。

        「小镇现况」的自身事实层用它折出定性措辞(``town_facts_service._collect_self``)。
        一条 SQL 走 ``ix_issue_stance_resident``,不做 ``top_active_issues()`` +
        逐个 ``get_stance()`` 的 N+1。``nulls_last`` + issue_key 兜底排序是为了
        PG/SQLite 拿到同一个顺序(两家对 ``DESC`` 下 NULL 的默认位置相反),prompt
        快照不能因为换库就换个模样。

        与本节其余读法一致**不看闸门**:``polis_opinion_enabled`` 只管三个写入
        口;读侧的闸门语义由调用方定义(事实层在闸关时压根不调它)。
        """
        rows = (await self.db.execute(
            select(IssueStance.issue_key, IssueStance.stance)
            .where(IssueStance.resident_slug == resident_slug)
            .order_by(IssueStance.last_update_at.desc().nulls_last(),
                      IssueStance.issue_key)
            .limit(limit)
        )).all()
        return [(key, stance) for key, stance in rows]

    async def issue_variance(self, issue_key: str) -> tuple[float, int]:
        """(population variance of stances, participant count) — probe + digest."""
        rows = (await self.db.execute(
            select(IssueStance.stance).where(IssueStance.issue_key == issue_key)
        )).scalars().all()
        n = len(rows)
        if n == 0:
            return 0.0, 0
        mean = sum(rows) / n
        return sum((x - mean) ** 2 for x in rows) / n, n

    async def top_active_issues(self, n: int = 5) -> list[str]:
        """Active issue keys, most-participated / most-recent first."""
        return await self._active_issues(limit=n)

    async def _active_issues(self, limit: int | None) -> list[str]:
        """"活跃议题" (§2, pure SQL): participants >= min AND last touched
        within the window. The window is WORLD days converted through
        world_clock (never a raw utcnow comparison against world rhythm)."""
        cutoff = self._active_cutoff_utc()
        q = (
            select(IssueStance.issue_key)
            .group_by(IssueStance.issue_key)
            .having(
                func.count() >= settings.polis_opinion_min_participants,
                func.max(IssueStance.last_update_at) >= cutoff,
            )
            .order_by(func.count().desc(), func.max(IssueStance.last_update_at).desc())
        )
        if limit is not None:
            q = q.limit(limit)
        return list((await self.db.execute(q)).scalars().all())

    @staticmethod
    def _active_cutoff_utc() -> datetime:
        """Real-UTC instant that lies ``active_window_days`` WORLD days back."""
        from app import world_clock
        w_cut = world_clock.now_world() - timedelta(
            days=settings.polis_opinion_active_window_days
        )
        return world_clock.world_to_real(w_cut).astimezone(UTC)

    # ── atomic write path ────────────────────────────────────────────────
    async def _bump_stance(
        self, issue_key: str, resident_slug: str, *,
        target: float, rate: float, source: str,
        insert_stance: float | None = None,
        epsilon: float | None = None,
    ) -> None:
        """One atomic bounded-confidence step (upsert, value computed in SQL):

        ``stance ← clamp(stance + rate×(target−stance), -1, 1)`` — applied only
        when ``|stance − target| <= ε`` (bounded confidence), all inside a
        single ``INSERT .. ON CONFLICT (issue_key, resident_slug) DO UPDATE``
        so concurrent wrapup workers can never lose an update.

        ``insert_stance`` is the value a *fresh* row gets (debate seeding);
        default = one bounded step from 0.0. ``epsilon`` overrides the gate
        (``_EPS_BYPASS`` → unconditional move, for structural updates).
        """
        eps = settings.polis_opinion_epsilon if epsilon is None else epsilon
        now = datetime.now(UTC)
        if insert_stance is None:
            insert_stance = rate * target if abs(target) <= eps else 0.0
        insert_stance = max(-1.0, min(1.0, insert_stance))

        if self.db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _insert
        else:
            from sqlalchemy.dialects.sqlite import insert as _insert

        tbl = IssueStance.__table__
        col = tbl.c.stance
        stepped = case(
            (func.abs(col - target) <= eps, col + rate * (target - col)),
            else_=col,
        )
        clamped = case((stepped > 1.0, 1.0), (stepped < -1.0, -1.0), else_=stepped)
        stmt = _insert(tbl).values(
            id=str(uuid.uuid4()),
            issue_key=issue_key,
            resident_slug=resident_slug,
            stance=insert_stance,
            confidence=0.5,
            updated_from=source,
            interact_count=1,
            last_update_at=now,
            created_at=now,
        ).on_conflict_do_update(
            index_elements=["issue_key", "resident_slug"],
            set_={
                "stance": clamped,
                "interact_count": tbl.c.interact_count + 1,
                "updated_from": source,
                "last_update_at": now,
            },
        )
        await self.db.execute(stmt)
        await self.db.commit()

    # ── signal 1: resident-resident chat wrapup mood ─────────────────────
    async def update_from_chat(
        self, a_slug: str, b_slug: str, mood: str, *,
        rng: random.Random | None = None,
    ) -> int:
        """Deffuant convergence on the wrapup ``mood`` (already-paid LLM output).

        positive → both parties move toward each other's snapshot stance on
        every *shared* issue within ε; negative → no convergence (optionally a
        mild repulsion beyond ε when ``polis_opinion_neg_repel``); neutral →
        no-op. Issues are never invented here — only rows both parties already
        hold are touched. Returns the number of (issue, resident) updates.
        """
        if not settings.polis_opinion_enabled:
            return 0
        if mood not in ("positive", "negative"):
            return 0
        if mood == "negative" and not settings.polis_opinion_neg_repel:
            return 0

        eps = settings.polis_opinion_epsilon
        rate = settings.polis_opinion_chat_rate
        shared = (await self.db.execute(
            select(IssueStance.issue_key)
            .where(IssueStance.resident_slug.in_([a_slug, b_slug]))
            .group_by(IssueStance.issue_key)
            .having(func.count(func.distinct(IssueStance.resident_slug)) == 2)
        )).scalars().all()

        n = 0
        for key in shared:
            # Snapshot both sides first so the second bump never reads a value
            # already moved inside this call (§4 无向对/成对更新).
            a_st = await self.get_stance(key, a_slug)
            b_st = await self.get_stance(key, b_slug)
            if a_st is None or b_st is None:
                continue
            gap = abs(a_st - b_st)
            if mood == "positive":
                if gap > eps:
                    continue  # bounded confidence: too far apart to converge
                await self._bump_stance(key, a_slug, target=b_st, rate=rate, source="chat")
                await self._bump_stance(key, b_slug, target=a_st, rate=rate, source="chat")
                n += 2
            else:  # negative + neg_repel: mild repulsion, only beyond ε
                if gap <= eps or a_st == b_st:
                    continue
                away = 1.0 if a_st > b_st else -1.0
                await self._bump_stance(key, a_slug, target=away, rate=rate * 0.5,
                                        source="chat", epsilon=_EPS_BYPASS)
                await self._bump_stance(key, b_slug, target=-away, rate=rate * 0.5,
                                        source="chat", epsilon=_EPS_BYPASS)
                n += 2
        return n

    # ── signal 2: debates ────────────────────────────────────────────────
    async def update_from_debate(self, debate: "Debate", *, seed_only: bool = False) -> int:
        """Debate hooks.

        ``seed_only=True`` (create_debate, reliable): seed opposing stances for
        the two debaters. Side a's pole comes from SBTI A1 (H→+, L→−, missing→+),
        side b takes the opposite pole (debate roles are structurally opposed);
        magnitude is ``seed_mag`` modulated by A2 — missing SBTI falls back to
        exactly ±seed_mag (production main path today: 0/26 residents have A2).

        ``seed_only=False`` (settle, opportunistic — not driven by app code
        today): winner reinforced toward their pole, loser regresses toward 0.
        Only acts on ``status == 'settled'`` with a real winner.
        """
        if not settings.polis_opinion_enabled:
            return 0
        key = self._normalize_issue_key(debate.topic)
        rate = settings.polis_opinion_chat_rate

        if seed_only:
            a_pref, a_mag = await self._seed_params(debate.resident_a_slug)
            _, b_mag = await self._seed_params(debate.resident_b_slug)
            a_sign = a_pref if a_pref is not None else 1.0
            b_sign = -a_sign
            for slug, val in (
                (debate.resident_a_slug, a_sign * a_mag),
                (debate.resident_b_slug, b_sign * b_mag),
            ):
                await self._bump_stance(key, slug, target=val, rate=rate,
                                        source="seed", insert_stance=val,
                                        epsilon=_EPS_BYPASS)
            return 2

        if debate.status != "settled" or debate.winner not in ("a", "b"):
            return 0
        win_slug = debate.resident_a_slug if debate.winner == "a" else debate.resident_b_slug
        lose_slug = debate.resident_b_slug if debate.winner == "a" else debate.resident_a_slug
        w_st = await self.get_stance(key, win_slug)
        if w_st:
            direction = 1.0 if w_st > 0 else -1.0
        else:  # never seeded (hook was off) → pole from the debate role
            direction = 1.0 if debate.winner == "a" else -1.0
        mag = settings.polis_opinion_seed_mag
        await self._bump_stance(key, win_slug, target=direction, rate=rate,
                                source="debate", insert_stance=direction * mag,
                                epsilon=_EPS_BYPASS)
        await self._bump_stance(key, lose_slug, target=0.0, rate=rate,
                                source="debate", insert_stance=0.0,
                                epsilon=_EPS_BYPASS)
        return 2

    async def _seed_params(self, slug: str) -> tuple[float | None, float]:
        """(A1-preferred sign or None, A2-modulated magnitude) for one slug."""
        from app.models.resident import Resident
        mag = settings.polis_opinion_seed_mag
        res = (await self.db.execute(
            select(Resident).where(Resident.slug == slug)
        )).scalar_one_or_none()
        dims = {}
        if res is not None:
            dims = (((res.meta_json or {}).get("sbti") or {}).get("dimensions") or {})
        if not dims:
            return None, mag  # 缺 SBTI 回落 ±seed_mag — production main path
        sign = {"H": 1.0, "L": -1.0}.get(dims.get("A1"))
        mag *= _A2_MAG.get(dims.get("A2"), 1.0)
        return sign, min(1.0, mag)

    # ── signal 3: nightly rule drift (shaped after _npc_choice) ──────────
    async def drift(self, *, rng: random.Random | None = None) -> int:
        """One synchronous bounded-confidence step per active issue.

        For every stance-holder: trusted neighbours = other holders within ε;
        move toward their affinity-weighted mean (weights from
        relation_service only when ``realism_relations_enabled``; uniform
        otherwise — optional enhancement, no hard dependency). Pure rule, zero
        LLM. Runs once per night from nightly_cron *before* the digest.
        """
        if not settings.polis_opinion_enabled:
            return 0
        eps = settings.polis_opinion_epsilon
        rate = settings.polis_opinion_drift_rate
        moved = 0
        for key in await self._active_issues(limit=None):
            rows = (await self.db.execute(
                select(IssueStance.resident_slug, IssueStance.stance)
                .where(IssueStance.issue_key == key)
            )).all()
            snapshot = {slug: st for slug, st in rows}
            weights: dict[tuple[str, str], float] = {}
            if settings.realism_relations_enabled:
                weights = await self._affinity_weights(list(snapshot))
            for slug, st in snapshot.items():
                neigh = [(o, ost) for o, ost in snapshot.items()
                         if o != slug and abs(ost - st) <= eps]
                if not neigh:
                    continue
                wsum = tsum = 0.0
                for o, ost in neigh:
                    w = weights.get((slug, o), 1.0)
                    wsum += w
                    tsum += w * ost
                target = tsum / wsum
                if abs(target - st) < 1e-12:
                    continue
                await self._bump_stance(key, slug, target=target, rate=rate, source="drift")
                moved += 1
        return moved

    async def _affinity_weights(self, slugs: list[str]) -> dict[tuple[str, str], float]:
        """(slug, other_slug) → weight = max(0.1, 1 + affinity). Best-effort:
        any failure falls back to uniform weights (empty dict)."""
        try:
            from app.models.resident import Resident
            from app.services import relation_service
            id_by_slug = dict((await self.db.execute(
                select(Resident.slug, Resident.id).where(Resident.slug.in_(slugs))
            )).all())
            slug_by_id = {v: k for k, v in id_by_slug.items()}
            out: dict[tuple[str, str], float] = {}
            for slug in slugs:
                rid = id_by_slug.get(slug)
                if not rid:
                    continue
                rels = await relation_service.relations_for(self.db, rid)
                for oid, view in rels.items():
                    other = slug_by_id.get(oid)
                    if other and other != slug:
                        out[(slug, other)] = max(0.1, 1.0 + view.affinity)
            return out
        except Exception:
            logger.warning("opinion affinity weights failed; uniform fallback", exc_info=True)
            return {}

    # ── digest material (task 4 helper; zero new LLM) ────────────────────
    async def digest_opinion_line(self) -> str | None:
        """One line of town opinion for the village digest material — same
        pattern as circle_line: pure SQL, feeds the SAME compose_digest call."""
        parts: list[str] = []
        for key in await self.top_active_issues(n=settings.polis_opinion_digest_issues):
            var, n = await self.issue_variance(key)
            shape = "意见分歧明显" if var >= settings.polis_opinion_variance_split else "看法渐趋一致"
            parts.append(f"关于「{key}」，镇上 {n} 人表过态，{shape}")
        return "；".join(parts) if parts else None
