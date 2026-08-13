"""Reset the town's built-in cast: remove the old preset/demo residents and
seed the new 10-person original town cast (with relations + goals).

Preview (read-only): python -m seed.reset_builtin_residents
Apply after backup:  python -m seed.reset_builtin_residents --apply --expect-targets N

What it deletes (player characters are NEVER touched):
- residents matching either
    a) one of the known legacy built-in slugs (old 14 presets + 5 demo NPCs), or
    b) creator_id == SYSTEM_USER_ID and resident_type == "npc" and slug not in
       the new roster (catches renamed/orphaned system NPCs)
- and, for each deleted resident, its dependent rows: messages/conversations,
  memories (own + about-them), personality history, goals, two-axis relations,
  follows/feed events, debates, treasury rows, llm_usage rows, bulletin posts
  and commissions they authored, issue stances (047), time capsules they carry
  and unfinished daily quests naming them; users.player_resident_id pointers
  are nulled and offices (046) they hold are vacated (the office row survives).

Deliberately NOT touched (historical records — the slug is display-only there,
and rewriting it would falsify the audit trail): lab_runs.researcher_slug,
lab_tasks.researcher_slug, world_change_proposals.author_slug, and *done*
daily quests. They keep pointing at a slug that no longer resolves to a
resident, by design.

Everything runs in one session against DATABASE_URL, so the same script works
on the sqlite dev database and the production Postgres.

"Player characters are NEVER touched" is now enforced, not just documented:
``purge_residents`` refuses a target list containing a player character unless
the caller passes ``allow_players=True``. Before that guard existed the promise
held only for the automatic path — 2026-07-25 16:53 a hand-written roster
migration called ``purge_residents`` directly with its own id list and destroyed
12 player characters.
"""
import argparse
import asyncio

from sqlalchemy import delete, or_, select, update

from app.database import async_session
from app.models.bulletin_post import BulletinPost
from app.models.commission import Commission
from app.models.conversation import Conversation, Message
from app.models.daily_quest import DailyQuest
from app.models.debate import Debate
from app.models.feed import FeedEvent, Follow
from app.models.issue_stance import IssueStance
from app.models.llm_usage import LLMUsage
from app.models.memory import Memory
from app.models.office import Office
from app.models.personality_history import PersonalityHistory
from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal
from app.models.resident_relation import ResidentRelation
from app.models.resident_treasury import ResidentTreasury
from app.models.time_capsule import TimeCapsule
from app.models.user import User
from seed.preset_characters import PRESET_CHARACTERS, SYSTEM_USER_ID, seed_presets
from seed.seed_residents import ensure_system_user
from app.services.system_users import ensure_admin_creator_user

# Old built-in roster: 14 legacy presets + 5 demo NPCs.
LEGACY_BUILTIN_SLUGS = {
    # legacy presets (nuwa-skill celebrities + 萧炎)
    "xiao-yan", "steve-jobs", "elon-musk", "charlie-munger", "feynman",
    "naval", "taleb", "paul-graham", "zhang-yiming", "karpathy",
    "ilya-sutskever", "mrbeast", "trump", "zhang-xuefeng",
    # legacy demo residents
    "isabella", "klaus", "adam", "mei", "tamara",
}

NEW_ROSTER_SLUGS = {c["slug"] for c in PRESET_CHARACTERS}


class PlayerPurgeRefused(RuntimeError):
    """``purge_residents`` was handed a player character without the explicit
    ``allow_players=True`` opt-in.

    Raised *before* the first DELETE, so a refusal leaves the database exactly
    as it was — including the legitimate NPC targets in the same call.
    """


async def find_targets(db) -> list[Resident]:
    """Old built-in NPCs to remove. Player residents are always excluded."""
    result = await db.execute(
        select(Resident).where(
            Resident.resident_type != "player",
            or_(
                Resident.slug.in_(LEGACY_BUILTIN_SLUGS),
                Resident.creator_id == SYSTEM_USER_ID,
            ),
            Resident.slug.notin_(NEW_ROSTER_SLUGS),
        )
    )
    return list(result.scalars().all())


async def _assert_no_players(db, ids: list[str]) -> None:
    """Refuse a target list containing a player character.

    2026-07-25 16:53: a hand-written roster migration skipped
    :func:`find_targets` (whose first condition is already
    ``resident_type != "player"``) and called :func:`purge_residents` with its
    own id list. That function trusted the list and cascaded across a dozen
    tables, destroying 12 player characters. This is the guard it lacked.

    Two deliberate choices:

    - **Raise, don't skip.** A silent skip returns success to a caller that
      asked for those ids to be gone, so the caller believes the purge
      completed. The 07-25 script would have "succeeded" either way; only a
      refusal would have stopped it.
    - **Read the database, not the passed objects.** The offending call site
      built its own target list, so ``target.resident_type`` is exactly the
      field that cannot be trusted. The authoritative check is a query by id.
    """
    rows = (await db.execute(
        select(Resident.slug).where(
            Resident.id.in_(ids), Resident.resident_type == "player",
        )
    )).scalars().all()
    if rows:
        raise PlayerPurgeRefused(
            f"refusing to purge {len(rows)} player character(s): "
            f"{', '.join(sorted(rows))}. Player characters are never part of a "
            "built-in roster reset (see find_targets); pass allow_players=True "
            "only if removing a player's own avatar is genuinely intended."
        )


async def purge_residents(
    db, targets: list[Resident], *, allow_players: bool = False,
) -> None:
    ids = [r.id for r in targets]
    slugs = [r.slug for r in targets]
    if not ids:
        return

    # Guard first: no DELETE has run yet, so a refusal is a true no-op.
    if not allow_players:
        await _assert_no_players(db, ids)

    # Messages hang off conversations — delete them first.
    conv_ids = [
        row[0]
        for row in (
            await db.execute(select(Conversation.id).where(Conversation.resident_id.in_(ids)))
        ).all()
    ]
    if conv_ids:
        await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))

    await db.execute(delete(Memory).where(
        or_(Memory.resident_id.in_(ids), Memory.related_resident_id.in_(ids))
    ))
    await db.execute(delete(PersonalityHistory).where(PersonalityHistory.resident_id.in_(ids)))
    await db.execute(delete(ResidentGoal).where(ResidentGoal.resident_id.in_(ids)))
    await db.execute(delete(ResidentRelation).where(
        or_(ResidentRelation.party_a.in_(ids), ResidentRelation.party_b.in_(ids))
    ))
    await db.execute(delete(LLMUsage).where(LLMUsage.resident_id.in_(ids)))
    await db.execute(delete(BulletinPost).where(BulletinPost.author_resident_id.in_(ids)))
    await db.execute(delete(Commission).where(Commission.issuer_resident_id.in_(ids)))
    await db.execute(delete(Follow).where(Follow.resident_slug.in_(slugs)))
    await db.execute(delete(FeedEvent).where(FeedEvent.resident_slug.in_(slugs)))
    await db.execute(delete(Debate).where(
        or_(Debate.resident_a_slug.in_(slugs), Debate.resident_b_slug.in_(slugs))
    ))
    await db.execute(delete(ResidentTreasury).where(ResidentTreasury.resident_slug.in_(slugs)))
    # 047: a stance row outlives its resident and the next NPC reusing the slug
    # would silently inherit its opinions.
    await db.execute(delete(IssueStance).where(IssueStance.resident_slug.in_(slugs)))
    # A capsule whose carrier is gone can never be delivered properly (the
    # notify payload would name a resident that no longer exists).
    await db.execute(delete(TimeCapsule).where(TimeCapsule.carrier_resident_slug.in_(slugs)))
    # A pending quest naming a deleted resident can never be completed, and the
    # generator only backfills when the user has NO row for today. Done quests
    # are paid-out history: deleting one lets today's quest regenerate and pay
    # a second time, so they stay. Filtering happens in Python because the slug
    # sits inside a JSON blob and json_extract/->> differ across sqlite and PG.
    stale_quests = [
        qid
        for qid, quest_json in (
            await db.execute(
                select(DailyQuest.id, DailyQuest.quest_json).where(DailyQuest.status != "done")
            )
        ).all()
        if (quest_json or {}).get("resident_slug") in slugs
    ]
    if stale_quests:
        await db.execute(delete(DailyQuest).where(DailyQuest.id.in_(stale_quests)))
    # 046: an office row is never deleted (vacancy must stay observable) — just
    # vacate it, or a ghost mayor/doctor blocks elections and duty lookups.
    await db.execute(
        update(Office)
        .where(Office.holder_slug.in_(slugs))
        .values(holder_slug=None)
        .execution_options(synchronize_session=False)
    )
    # A user "playing as" a deleted NPC would dangle — null the pointer.
    await db.execute(
        update(User)
        .where(User.player_resident_id.in_(ids))
        .values(player_resident_id=None)
        .execution_options(synchronize_session=False)
    )
    await db.execute(delete(Resident).where(Resident.id.in_(ids)))
    await db.commit()


async def main(*, apply: bool = False, expect_targets: int | None = None) -> None:
    """Preview by default; mutate only after an exact target-count handshake.

    The count is supplied after an operator reviews the preview.  If the roster
    changes between preview and apply, the command fails before its first write.
    """
    async with async_session() as db:
        targets = await find_targets(db)
        if targets:
            print("Built-in residents targeted by this reset:")
            for r in targets:
                print(f"  - {r.slug} ({r.name})")
        else:
            print("No old built-in residents found.")

        if not apply:
            print(
                "DRY RUN: no rows changed. After a verified database backup, "
                f"re-run with --apply --expect-targets {len(targets)}."
            )
            return
        if expect_targets is None:
            raise RuntimeError("--apply requires --expect-targets N")
        if expect_targets < 0:
            raise RuntimeError("--expect-targets must be non-negative")
        if len(targets) != expect_targets:
            raise RuntimeError(
                "roster changed after review: "
                f"expected {expect_targets} purge target(s), found {len(targets)}; "
                "refusing before the first write"
            )

        if targets:
            await purge_residents(db, targets)

        await ensure_system_user(db)
        await ensure_admin_creator_user(db)
        created = await seed_presets(db)
        # purge_residents vacates offices held by the old roster. Reconcile the
        # only two intentional duty/office overlaps after the replacement rows
        # exist; non-empty conflicts are reported and never overwritten.
        from app.services.office_service import reconcile_seed_offices
        office_report = await reconcile_seed_offices(db, apply=True)
        print(f"Seed office reconciliation: {office_report}")
        if (office_report["missing"] or office_report["ambiguous"]
                or office_report["conflicts"]):
            raise RuntimeError(
                f"seed office reconciliation was unsafe: {office_report}"
            )

        kept = (await db.execute(
            select(Resident.slug, Resident.name, Resident.resident_type, Resident.district)
        )).all()
        print(f"\nSeeded {created} new residents. World now contains:")
        for slug, name, rtype, district in kept:
            print(f"  - [{rtype}] {name} ({slug}) @ {district}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Preview or explicitly apply the destructive built-in roster reset"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform writes (requires --expect-targets)",
    )
    parser.add_argument(
        "--expect-targets",
        type=int,
        help="exact target count observed during the preceding dry run",
    )
    args = parser.parse_args()
    if args.apply and args.expect_targets is None:
        parser.error("--apply requires --expect-targets N")
    if not args.apply and args.expect_targets is not None:
        parser.error("--expect-targets is only valid together with --apply")
    asyncio.run(main(apply=args.apply, expect_targets=args.expect_targets))
