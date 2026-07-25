"""S1-3 议题立场与舆论动力学 (KICKOFF_S1-3_opinion.md).

Bounded-confidence (Deffuant) stance dynamics over free-string issue keys:
atomic upsert (`_bump_stance`), chat-mood convergence, debate seeding /
settle reinforcement, nightly rule drift, digest opinion_line — all zero new
LLM calls, all behind the independent `polis_opinion_enabled` gate
(default False → byte-identical fallback to the status quo).
"""

import asyncio
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select, func

from app.models.issue_stance import IssueStance


@pytest.fixture
def opinion_on(monkeypatch):
    """Flip the S1-3 gate on for one test (default stays False)."""
    from app.config import settings
    monkeypatch.setattr(settings, "polis_opinion_enabled", True)
    return settings


def _svc(db):
    from app.services.opinion_service import OpinionService
    return OpinionService(db)


async def _seed_row(db, key, slug, stance, *, last=None):
    row = IssueStance(
        issue_key=key, resident_slug=slug, stance=stance,
        interact_count=1, last_update_at=last or datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    return row


async def _resident(db, slug, name, *, sbti_dims=None):
    from app.models.resident import Resident
    meta = {"sbti": {"dimensions": sbti_dims}} if sbti_dims is not None else None
    r = Resident(slug=slug, name=name, creator_id="system", district="cafe",
                 status="idle", tile_x=1, tile_y=1, meta_json=meta)
    db.add(r)
    await db.commit()
    return r


def _debate_obj(topic, a_slug, b_slug, *, status="announced", winner=None):
    from app.models.debate import Debate
    return Debate(topic=topic, resident_a_slug=a_slug, resident_b_slug=b_slug,
                  status=status, winner=winner)


async def _count_rows(db):
    return (await db.execute(select(func.count()).select_from(IssueStance))).scalar()


# --------------------------------------------------------------------------- #
# Task 1 — table + migration                                                  #
# --------------------------------------------------------------------------- #

def test_integration_migration_single_head():
    """`alembic heads` stays single-headed and the S1-3 migration is on the
    chain (down_revision anchored on the real current head, 045)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("046_add_issue_stances")
    assert rev is not None
    assert rev.down_revision == "045_residents_creator_nullable"


@pytest.mark.anyio
async def test_issue_stances_table_created(db_engine):
    """models/__init__.py registers the model so Base.metadata.create_all
    (main.py test path) sees the new table."""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "issue_stances" in names


# --------------------------------------------------------------------------- #
# Task 2 — _bump_stance atomic upsert + bounded confidence                     #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_bump_stance_upsert_creates_row(db_session):
    svc = _svc(db_session)
    await svc._bump_stance("议题A", "ann", target=0.5, rate=1.0, source="seed",
                           insert_stance=0.5)
    row = (await db_session.execute(
        select(IssueStance).execution_options(populate_existing=True)
    )).scalar_one()
    assert row.issue_key == "议题A" and row.resident_slug == "ann"
    assert row.stance == pytest.approx(0.5)
    assert row.interact_count == 1
    assert row.updated_from == "seed"
    assert row.last_update_at is not None


@pytest.mark.anyio
async def test_bump_stance_upsert_conflict_no_duplicate(db_session):
    svc = _svc(db_session)
    await svc._bump_stance("k", "ann", target=0.2, rate=0.5, source="seed",
                           insert_stance=0.2)
    await svc._bump_stance("k", "ann", target=0.2, rate=0.5, source="chat")
    assert await _count_rows(db_session) == 1  # uq_issue_stance held
    row = (await db_session.execute(
        select(IssueStance).execution_options(populate_existing=True)
    )).scalar_one()
    assert row.interact_count == 2
    assert row.updated_from == "chat"


@pytest.mark.anyio
async def test_bump_stance_atomic_concurrent_no_lost_update(db_engine):
    """N concurrent bumps (own session each) — interact_count == N because the
    new value is computed inside the UPDATE, never read-modify-write."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.services.opinion_service import OpinionService

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    N = 12

    async def one_bump():
        async with factory() as db:
            await OpinionService(db)._bump_stance(
                "k", "ann", target=0.0, rate=0.0, source="chat", insert_stance=0.0,
            )

    await asyncio.gather(*(one_bump() for _ in range(N)))

    async with factory() as db:
        rows = (await db.execute(select(IssueStance))).scalars().all()
        assert len(rows) == 1
        assert rows[0].interact_count == N


@pytest.mark.anyio
async def test_bump_stance_clamped_to_unit_interval(db_session):
    svc = _svc(db_session)
    await _seed_row(db_session, "k", "ann", 0.9)
    await svc._bump_stance("k", "ann", target=1.0, rate=2.0, source="chat")
    assert await svc.get_stance("k", "ann") == pytest.approx(1.0)  # 1.1 → capped
    await svc._bump_stance("k", "ann", target=1.0, rate=2.0, source="chat")
    assert await svc.get_stance("k", "ann") == pytest.approx(1.0)

    await _seed_row(db_session, "k", "bo", -0.9)
    await svc._bump_stance("k", "bo", target=-1.0, rate=2.0, source="chat")
    assert await svc.get_stance("k", "bo") == pytest.approx(-1.0)  # floored


@pytest.mark.anyio
async def test_bounded_confidence_no_move_outside_epsilon(db_session):
    svc = _svc(db_session)
    await _seed_row(db_session, "k", "ann", 0.0)
    await svc._bump_stance("k", "ann", target=0.8, rate=1.0, source="chat")
    assert await svc.get_stance("k", "ann") == pytest.approx(0.0)  # |0-0.8| > ε=0.4


@pytest.mark.anyio
async def test_bounded_confidence_moves_inside_epsilon(db_session):
    svc = _svc(db_session)
    await _seed_row(db_session, "k", "ann", 0.0)
    await svc._bump_stance("k", "ann", target=0.3, rate=0.5, source="chat")
    # 0 + 0.5×(0.3-0) = 0.15, toward the target
    assert await svc.get_stance("k", "ann") == pytest.approx(0.15)


def test_normalize_issue_key_dedup():
    from app.services.opinion_service import OpinionService
    norm = OpinionService._normalize_issue_key
    assert norm("  关于「猫」的争论  ") == norm("关于「猫」的争论")
    assert norm("cat  vs \n dog") == norm("cat vs dog")
    assert norm("Cat") != norm("cat")  # no case folding (中文为主)
    assert len(norm("长" * 500)) == 300  # truncated to the column width


# --------------------------------------------------------------------------- #
# Task 2 — update_from_chat                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_update_from_chat_positive_converges(opinion_on, db_session):
    await _seed_row(db_session, "k", "ann", 0.3)
    await _seed_row(db_session, "k", "bo", 0.0)
    svc = _svc(db_session)
    n = await svc.update_from_chat("ann", "bo", "positive")
    assert n == 2
    # Deffuant step toward each other's snapshot (rate 0.08)
    assert await svc.get_stance("k", "ann") == pytest.approx(0.3 + 0.08 * (0.0 - 0.3))
    assert await svc.get_stance("k", "bo") == pytest.approx(0.0 + 0.08 * (0.3 - 0.0))


@pytest.mark.anyio
async def test_update_from_chat_negative_no_converge(opinion_on, db_session, monkeypatch):
    await _seed_row(db_session, "k", "ann", 0.3)
    await _seed_row(db_session, "k", "bo", 0.0)
    svc = _svc(db_session)
    assert await svc.update_from_chat("ann", "bo", "negative") == 0  # default: 不靠拢
    assert await svc.get_stance("k", "ann") == pytest.approx(0.3)
    assert await svc.get_stance("k", "bo") == pytest.approx(0.0)

    # neg_repel=True + gap beyond ε → mild mutual repulsion
    monkeypatch.setattr(opinion_on, "polis_opinion_neg_repel", True)
    await _seed_row(db_session, "k2", "ann", 0.5)
    await _seed_row(db_session, "k2", "bo", -0.5)
    n = await svc.update_from_chat("ann", "bo", "negative")
    assert n >= 2
    assert await svc.get_stance("k2", "ann") > 0.5
    assert await svc.get_stance("k2", "bo") < -0.5


@pytest.mark.anyio
async def test_update_from_chat_only_shared_issues(opinion_on, db_session):
    await _seed_row(db_session, "仅ann", "ann", 0.4)
    await _seed_row(db_session, "仅bo", "bo", -0.4)
    await _seed_row(db_session, "共同", "ann", 0.1)
    await _seed_row(db_session, "共同", "bo", 0.2)
    svc = _svc(db_session)
    n = await svc.update_from_chat("ann", "bo", "positive")
    assert n == 2  # only the shared issue moved
    assert await svc.get_stance("仅ann", "ann") == pytest.approx(0.4)  # untouched
    assert await svc.get_stance("仅bo", "bo") == pytest.approx(-0.4)  # untouched
    assert await _count_rows(db_session) == 4  # no issue invented out of thin air


# --------------------------------------------------------------------------- #
# Task 2 — update_from_debate                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_update_from_debate_seed_only_creates_opposing_stances(opinion_on, db_session):
    await _resident(db_session, "ann", "安", sbti_dims={"A1": "H", "A2": "L"})
    await _resident(db_session, "bo", "波", sbti_dims={"A1": "L", "A2": "H"})
    svc = _svc(db_session)
    n = await svc.update_from_debate(_debate_obj("猫和狗谁更好", "ann", "bo"), seed_only=True)
    assert n == 2
    ann = await svc.get_stance("猫和狗谁更好", "ann")
    bo = await svc.get_stance("猫和狗谁更好", "bo")
    # sign from SBTI A1 (ann H → +) and structural opposition; magnitude from A2
    assert ann == pytest.approx(0.3 * 1.25)   # A2=L → bolder stance
    assert bo == pytest.approx(-(0.3 * 0.75))  # A2=H → milder, opposite pole
    assert ann * bo < 0

    # A1=L on side a flips a's pole; b still lands opposite
    n = await svc.update_from_debate(_debate_obj("第二议题", "bo", "ann"), seed_only=True)
    assert n == 2
    assert await svc.get_stance("第二议题", "bo") < 0   # bo A1=L → −
    assert await svc.get_stance("第二议题", "ann") > 0


@pytest.mark.anyio
async def test_update_from_debate_seed_missing_sbti_fallback(opinion_on, db_session):
    """Production main path: 26/26 residents currently lack the A2 dim — the
    fallback must be exercised, not decorative (环境事实/PROGRESS S0 遗留 a)."""
    await _resident(db_session, "ann", "安")               # meta_json None
    await _resident(db_session, "bo", "波", sbti_dims={})  # sbti present, dims empty
    svc = _svc(db_session)
    n = await svc.update_from_debate(_debate_obj("无画像议题", "ann", "bo"), seed_only=True)
    assert n == 2
    assert await svc.get_stance("无画像议题", "ann") == pytest.approx(0.3)   # +seed_mag
    assert await svc.get_stance("无画像议题", "bo") == pytest.approx(-0.3)  # −seed_mag


@pytest.mark.anyio
async def test_update_from_debate_settle_only_when_settled(opinion_on, db_session):
    await _resident(db_session, "ann", "安")
    await _resident(db_session, "bo", "波")
    svc = _svc(db_session)
    d = _debate_obj("议题", "ann", "bo", status="announced")
    assert await svc.update_from_debate(d, seed_only=False) == 0
    assert await _count_rows(db_session) == 0  # announced 不触发 settle 增强


@pytest.mark.anyio
async def test_update_from_debate_settle_winner_reinforced_loser_regresses(opinion_on, db_session):
    await _resident(db_session, "ann", "安")
    await _resident(db_session, "bo", "波")
    await _seed_row(db_session, "议题", "ann", 0.375)
    await _seed_row(db_session, "议题", "bo", -0.225)
    svc = _svc(db_session)
    d = _debate_obj("议题", "ann", "bo", status="settled", winner="a")
    assert await svc.update_from_debate(d, seed_only=False) == 2
    ann = await svc.get_stance("议题", "ann")
    bo = await svc.get_stance("议题", "bo")
    assert ann > 0.375          # winner reinforced toward own pole
    assert abs(bo) < 0.225      # loser regresses toward 0
    assert bo < 0               # ... without flipping


# --------------------------------------------------------------------------- #
# Task 2 — drift (nightly bounded-confidence step, zero LLM)                   #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_drift_converges_within_epsilon_cluster(opinion_on, db_session):
    for slug, st in (("ann", 0.0), ("bo", 0.2), ("cid", 0.4)):
        await _seed_row(db_session, "k", slug, st)
    svc = _svc(db_session)
    var_before, n_before = await svc.issue_variance("k")
    moved = await svc.drift()
    assert moved >= 2
    var_after, n_after = await svc.issue_variance("k")
    assert n_before == n_after == 3
    assert var_after < var_before  # ε-cluster converges


@pytest.mark.anyio
async def test_drift_polarizes_across_gap(opinion_on, db_session):
    neg = [("n1", -0.8), ("n2", -0.7), ("n3", -0.75)]
    pos = [("p1", 0.8), ("p2", 0.7), ("p3", 0.75)]
    for slug, st in neg + pos:
        await _seed_row(db_session, "k", slug, st)
    svc = _svc(db_session)
    await svc.drift()
    for slug, _ in neg:
        assert await svc.get_stance("k", slug) < -0.5  # cluster intact
    for slug, _ in pos:
        assert await svc.get_stance("k", slug) > 0.5   # no merge across the gap


@pytest.mark.anyio
async def test_drift_affinity_weight_when_relations_on(opinion_on, db_session, monkeypatch):
    from app.config import settings
    from app.services import relation_service
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    ann = await _resident(db_session, "ann", "安")
    bo = await _resident(db_session, "bo", "波")
    cid = await _resident(db_session, "cid", "茜")
    await _seed_row(db_session, "k", "ann", 0.0)
    await _seed_row(db_session, "k", "bo", 0.3)
    await _seed_row(db_session, "k", "cid", -0.3)
    # ann loves bo, dislikes cid → weighted neighbour mean pulls ann positive
    await relation_service.bump(db_session, ann.id, bo.id, d_affinity=0.9)
    await relation_service.bump(db_session, ann.id, cid.id, d_affinity=-0.9)
    svc = _svc(db_session)
    await svc.drift()
    assert await svc.get_stance("k", "ann") > 0.0


@pytest.mark.anyio
async def test_drift_uniform_weight_when_relations_off(opinion_on, db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    await _resident(db_session, "ann", "安")
    await _seed_row(db_session, "k", "ann", 0.0)
    await _seed_row(db_session, "k", "bo", 0.3)
    await _seed_row(db_session, "k", "cid", -0.3)
    svc = _svc(db_session)
    moved = await svc.drift()
    # uniform weights: ann's neighbour mean is exactly 0 → ann holds position,
    # but drift still works for the asymmetric residents (no hard dependency)
    assert await svc.get_stance("k", "ann") == pytest.approx(0.0)
    assert moved >= 2  # bo/cid each drift toward their own neighbour mean


@pytest.mark.anyio
async def test_issue_variance_and_active_issues(opinion_on, db_session):
    now = datetime.now(UTC)
    for slug, st in (("a", 1.0), ("b", 0.0), ("c", -1.0)):
        await _seed_row(db_session, "hot", slug, st, last=now)
    for slug in ("a", "b", "c"):
        await _seed_row(db_session, "cold", slug, 0.1, last=now - timedelta(days=10))
    await _seed_row(db_session, "small", "a", 0.2, last=now)
    await _seed_row(db_session, "small", "b", 0.3, last=now)
    svc = _svc(db_session)
    var, n = await svc.issue_variance("hot")
    assert n == 3
    assert var == pytest.approx(2.0 / 3.0)
    active = await svc.top_active_issues()
    assert active == ["hot"]  # cold: outside window; small: < min participants


# --------------------------------------------------------------------------- #
# Gate fallback — flag off ⇒ return 0 and zero writes                          #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_disabled_update_from_chat_noop(db_session):
    await _seed_row(db_session, "k", "ann", 0.3)
    await _seed_row(db_session, "k", "bo", 0.0)
    svc = _svc(db_session)
    assert await svc.update_from_chat("ann", "bo", "positive") == 0
    assert await svc.get_stance("k", "ann") == pytest.approx(0.3)
    assert await svc.get_stance("k", "bo") == pytest.approx(0.0)


@pytest.mark.anyio
async def test_disabled_update_from_debate_noop(db_session):
    await _resident(db_session, "ann", "安")
    await _resident(db_session, "bo", "波")
    svc = _svc(db_session)
    assert await svc.update_from_debate(_debate_obj("议题", "ann", "bo"), seed_only=True) == 0
    assert await _count_rows(db_session) == 0


@pytest.mark.anyio
async def test_disabled_drift_noop(db_session):
    for slug, st in (("ann", 0.0), ("bo", 0.2), ("cid", 0.4)):
        await _seed_row(db_session, "k", slug, st)
    svc = _svc(db_session)
    assert await svc.drift() == 0
    for slug, st in (("ann", 0.0), ("bo", 0.2), ("cid", 0.4)):
        assert await svc.get_stance("k", slug) == pytest.approx(st)


# --------------------------------------------------------------------------- #
# Task 3 — wiring: create_debate / settle / chat wrapup / nightly order        #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_integration_create_debate_seeds_stances(opinion_on, db_session):
    from app.services import debate_service as ds
    await _resident(db_session, "ann", "安", sbti_dims={"A1": "H"})
    await _resident(db_session, "bo", "波", sbti_dims={"A1": "L"})
    d = await ds.create_debate(db_session, "关于「夜市」的争论", "ann", "bo")
    assert d.status == "announced"
    svc = _svc(db_session)
    ann = await svc.get_stance("关于「夜市」的争论", "ann")
    bo = await svc.get_stance("关于「夜市」的争论", "bo")
    assert ann is not None and bo is not None
    assert ann > 0 > bo  # two opposing seeded rows


@pytest.mark.anyio
async def test_integration_settle_hook_reinforces_via_aftermath(opinion_on, db_session):
    """Opportunistic settle seam: when settle IS driven (tests only today),
    the winner is reinforced and the loser regresses through _resident_aftermath."""
    from app.services import debate_service as ds
    await _resident(db_session, "ann", "安")
    await _resident(db_session, "bo", "波")
    d = await ds.create_debate(db_session, "议题X", "ann", "bo")  # seeds ±0.3
    d.status = "voting"
    d.votes_a, d.votes_b = 2, 0
    await db_session.commit()
    out = await ds.settle(db_session, d.id)
    assert out["winner"] == "a"
    svc = _svc(db_session)
    assert await svc.get_stance("议题X", "ann") > 0.3        # reinforced
    assert abs(await svc.get_stance("议题X", "bo")) < 0.3    # regressed toward 0


@pytest.mark.anyio
async def test_integration_chat_wrapup_moves_stance(opinion_on, db_session):
    """process_chat_wrapup (mood already extracted by the ONE wrapup call)
    moves shared-issue stances — LLM call count stays exactly 1."""
    import json
    from unittest.mock import AsyncMock, patch
    from app.memory.service import MemoryService

    ann = await _resident(db_session, "ann", "安")
    bo = await _resident(db_session, "bo", "波")
    await _seed_row(db_session, "k", "ann", 0.3)
    await _seed_row(db_session, "k", "bo", 0.0)

    payload = json.dumps({
        "summary": "聊得很来", "mood": "positive",
        "initiator": {"memories": []}, "target": {"memories": []},
    })
    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=payload)) as llm:
        out = await MemoryService(db_session).process_chat_wrapup(ann, bo, "对话全文")

    assert llm.await_count == 1  # 零新增 LLM 调用：只有 wrapup 本身
    assert out["mood"] == "positive"
    svc = _svc(db_session)
    assert await svc.get_stance("k", "ann") == pytest.approx(0.3 + 0.08 * (0.0 - 0.3))
    assert await svc.get_stance("k", "bo") == pytest.approx(0.08 * 0.3)


@pytest.mark.anyio
async def test_integration_nightly_drift_before_digest(opinion_on, db_session):
    """Ordering hard requirement (§7): the drift block sits BEFORE the digest
    block in run_nightly_jobs, so the same night's opinion_line reflects the
    post-drift variance. Source-order guard (test_m5_space precedent) plus the
    functional half: drift lowers the variance the digest will read."""
    import inspect
    from app.tasks import nightly_cron
    src = inspect.getsource(nightly_cron.run_nightly_jobs)
    assert "OpinionService" in src and "drift" in src
    assert "MUST run before digest" in src
    assert src.index("drift") < src.index("generate_village_digest")

    # functional half: tonight's digest material is post-drift
    for slug, st in (("ann", 0.0), ("bo", 0.2), ("cid", 0.4)):
        await _seed_row(db_session, "k", slug, st)
    svc = _svc(db_session)
    var_before, _ = await svc.issue_variance("k")
    await svc.drift()
    var_after, _ = await svc.issue_variance("k")
    assert var_after < var_before


# --------------------------------------------------------------------------- #
# Task 4 — digest opinion_line (zero new LLM)                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_disabled_digest_has_no_opinion_line(db_session):
    """Gate off → gather_material has no opinion_line key and the digest
    prompt is byte-identical to the status quo."""
    from datetime import date
    from app.services import digest_service as ds
    for slug, st in (("ann", 0.5), ("bo", 0.0), ("cid", -0.5)):
        await _seed_row(db_session, "k", slug, st)
    material = await ds.gather_material(db_session, date(2026, 7, 25))
    assert "opinion_line" not in material
    prompt = ds._build_prompt(date(2026, 7, 25), material)
    assert "小镇舆论" not in prompt


@pytest.mark.anyio
async def test_enabled_digest_opinion_line_present(opinion_on, db_session):
    from datetime import date
    from app.services import digest_service as ds
    for slug, st in (("ann", 0.5), ("bo", 0.0), ("cid", -0.5)):
        await _seed_row(db_session, "夜市该不该扩建", slug, st)
    material = await ds.gather_material(db_session, date(2026, 7, 25))
    assert "夜市该不该扩建" in material["opinion_line"]
    prompt = ds._build_prompt(date(2026, 7, 25), material)
    assert "小镇舆论" in prompt and "夜市该不该扩建" in prompt


@pytest.mark.anyio
async def test_integration_digest_opinion_line_zero_new_llm(opinion_on, db_session):
    """Gate on: the opinion material rides the SAME single compose_digest
    call — LLM client create count stays exactly 1."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.models.memory import Memory
    from app.services import digest_service as ds

    day = datetime.now(UTC).date()
    db_session.add(Memory(
        resident_id="r1", type="event", content="今天大家聊得很开心",
        importance=0.9, source="chat_resident", created_at=datetime.now(UTC),
    ))
    await db_session.commit()
    for slug, st in (("ann", 0.5), ("bo", 0.0), ("cid", -0.5)):
        await _seed_row(db_session, "夜市该不该扩建", slug, st)

    block = MagicMock()
    block.text = "# 今日头条\n镇上为夜市吵起来了"
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    with patch.object(ds, "get_client", return_value=client), \
         patch.object(ds, "record_usage", new=AsyncMock()):
        digest = await ds.generate_village_digest(db_session, day)

    assert client.messages.create.await_count == 1  # 素材增强零新增调用
    prompt = client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "小镇舆论" in prompt and "夜市该不该扩建" in prompt
    assert "夜市" in digest.content_md
