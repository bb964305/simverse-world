"""S1-1 public reputation regression tests."""
import pytest

from app.config import settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import election_service
from app.services import relation_service
from app.services.reputation_service import (
    credit_allowed,
    evidence_weight,
    get_many,
    gossip_tone,
    project,
    recompute,
    score_from_meta,
)


def _resident(slug: str, *, reputation: float | None = None, mood: float = 0.0):
    meta = {"sbti": {"dimensions": {"Ac1": "H"}}}
    if reputation is not None:
        meta["reputation"] = {"score": reputation, "samples": 1}
    return Resident(
        slug=slug,
        name=slug,
        district="central_plaza",
        status="idle",
        resident_type="npc",
        creator_id=None,
        tile_x=70,
        tile_y=56,
        mood_json={"valence": mood, "arousal": 0.2, "label": "calm"},
        meta_json=meta,
    )


@pytest.mark.anyio
async def test_recompute_disabled_is_noop(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", False)
    resident = _resident("disabled")
    db_session.add(resident)
    await db_session.commit()

    assert await recompute(db_session) == 0
    await db_session.refresh(resident)
    assert "reputation" not in (resident.meta_json or {})


@pytest.mark.anyio
async def test_recompute_uses_gossip_distortion_hops_and_mood(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", True)
    plain = _resident("plain", mood=-0.5)
    distorted = _resident("distorted", mood=-0.5)
    far = _resident("far", mood=-0.5)
    db_session.add_all([plain, distorted, far])
    await db_session.flush()
    db_session.add_all([
        Memory(
            resident_id=plain.id, type="event", content="plain",
            importance=0.7, source="gossip", related_resident_id=plain.id,
            metadata_json={"hops": 0, "distorted": False},
        ),
        Memory(
            resident_id=plain.id, type="event", content="distorted",
            importance=0.7, source="gossip", related_resident_id=distorted.id,
            metadata_json={"hops": 0, "distorted": True},
        ),
        Memory(
            resident_id=plain.id, type="event", content="far",
            importance=0.7, source="gossip", related_resident_id=far.id,
            metadata_json={"hops": 3, "distorted": False},
        ),
    ])
    await db_session.commit()

    assert await recompute(db_session) == 3
    await db_session.refresh(plain)
    await db_session.refresh(distorted)
    await db_session.refresh(far)
    plain_score = score_from_meta(plain.meta_json)
    distorted_score = score_from_meta(distorted.meta_json)
    far_score = score_from_meta(far.meta_json)
    assert distorted_score < plain_score < 0
    assert far_score > plain_score
    assert (plain.meta_json or {})["reputation"]["samples"] == 1


@pytest.mark.anyio
async def test_get_many_and_credit_threshold(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", True)
    low = _resident("low", reputation=-0.8)
    high = _resident("high", reputation=0.8)
    db_session.add_all([low, high])
    await db_session.commit()

    scores = await get_many(db_session, [low.id, high.id, "missing"])
    assert scores[low.id] == -0.8
    assert scores[high.id] == 0.8
    assert scores["missing"] == settings.rep_neutral
    assert credit_allowed(-0.8) is False
    assert credit_allowed(0.8) is True


@pytest.mark.anyio
async def test_open_election_ranks_reputation_when_enabled(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", True)
    low = _resident("low", reputation=-0.5)
    high = _resident("high", reputation=0.9)
    db_session.add_all([low, high])
    await db_session.commit()

    poll = await election_service.open_election(
        db_session,
        candidate_slugs=["low", "high"],
    )
    assert poll.options_json[0]["effect"]["slug"] == "high"


# ── F1 第 1 项：tone 由关系 affinity 决定 ──────────────────────────────


def test_gossip_tone_follows_affinity_sign():
    assert gossip_tone(0.2) > 0
    assert gossip_tone(-0.2) < 0
    assert gossip_tone(0.5) > gossip_tone(0.1) > gossip_tone(-0.1)


def test_gossip_tone_without_relation_keeps_the_legacy_constant():
    # 无关系行 / affinity=0 → 与修复前逐字节相同，base_tone 退化为偏置项
    assert gossip_tone(None) == settings.rep_gossip_base_tone
    assert gossip_tone(0.0) == settings.rep_gossip_base_tone
    assert gossip_tone("nonsense") == settings.rep_gossip_base_tone


def test_gossip_tone_applies_distortion_penalty_and_clamps():
    assert gossip_tone(0.0, distorted=True) == pytest.approx(
        settings.rep_gossip_base_tone + settings.rep_distortion_penalty
    )
    assert gossip_tone(1.0) == settings.rep_max
    assert gossip_tone(-1.0, distorted=True) == settings.rep_min


def test_evidence_weight_damps_by_hops_and_floors_importance():
    assert evidence_weight(0.6, 0, -0.5) == pytest.approx(-0.3)
    assert evidence_weight(0.6, 3, -0.5) == pytest.approx(-0.075)
    assert evidence_weight(0.6, 0, 0.5) == pytest.approx(0.3)
    assert evidence_weight(-1.0, 0, 0.5) == 0.0
    assert evidence_weight(None, 0, 0.5) == 0.0


# ── F1 第 1 项：接进 recompute ─────────────────────────────────────────


@pytest.mark.anyio
async def test_recompute_tone_follows_relation_affinity(db_session, monkeypatch):
    """同一个传话人、同样的 importance/hops,只有 affinity 不同 → 分数异号。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    teller = _resident("teller")
    liked = _resident("liked")
    disliked = _resident("disliked")
    db_session.add_all([teller, liked, disliked])
    await db_session.flush()
    db_session.add_all([
        Memory(
            resident_id=teller.id, type="event", content="about liked",
            importance=0.7, source="gossip", related_resident_id=liked.id,
            metadata_json={"hops": 1, "distorted": False},
        ),
        Memory(
            resident_id=teller.id, type="event", content="about disliked",
            importance=0.7, source="gossip", related_resident_id=disliked.id,
            metadata_json={"hops": 1, "distorted": False},
        ),
    ])
    await db_session.commit()
    await relation_service.bump(db_session, teller.id, liked.id, d_affinity=0.4)
    await relation_service.bump(db_session, teller.id, disliked.id, d_affinity=-0.4)

    assert await recompute(db_session) == 3
    await db_session.refresh(liked)
    await db_session.refresh(disliked)
    assert score_from_meta(liked.meta_json) > 0      # 正面互动 → 正分
    assert score_from_meta(disliked.meta_json) < 0   # 负面互动 → 负分


@pytest.mark.anyio
async def test_recompute_reads_relations_in_one_batch(db_session, monkeypatch):
    """性能红线:关系读取必须是批量的,不能每条记忆一次查询。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    teller = _resident("batch_teller")
    subjects = [_resident(f"batch_sub{i}") for i in range(5)]
    db_session.add_all([teller, *subjects])
    await db_session.flush()
    for subject in subjects:
        db_session.add(Memory(
            resident_id=teller.id, type="event", content="x",
            importance=0.7, source="gossip", related_resident_id=subject.id,
            metadata_json={"hops": 1, "distorted": False},
        ))
    await db_session.commit()
    for subject in subjects:
        await relation_service.bump(db_session, teller.id, subject.id, d_affinity=0.4)

    calls = {"n": 0}
    original = db_session.execute

    async def counting_execute(statement, *args, **kwargs):
        # 用编译后的 SQL 文本判定,不碰 Select.froms(1.4.23 起 deprecated)
        if "resident_relations" in str(statement):
            calls["n"] += 1
        return await original(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", counting_execute)
    assert await recompute(db_session) == 6
    assert calls["n"] == 1, f"关系查询 {calls['n']} 次,应为 1 次批量读"


# ── F1 第 3 项:人口口径 is_autonomous(spec §4.4 第 11 处读点) ──────────


@pytest.mark.anyio
async def test_recompute_covers_ugc_residents_but_never_the_player_avatar(
    db_session, monkeypatch
):
    """spec §4.4 第 11 处读点:声誉是社会属性,人口口径不是政治口径。

    不改的后果是被降级者退出夜间重算、分数永久冻结在降级前那一刻。
    """
    from app.services.civic_membership import UGC_RESIDENT_TYPE

    monkeypatch.setattr(settings, "rep_enabled", True)
    builtin = _resident("builtin")
    ugc = _resident("ugc")
    ugc.resident_type = UGC_RESIDENT_TYPE
    avatar = _resident("avatar")
    avatar.resident_type = "player"
    db_session.add_all([builtin, ugc, avatar])
    await db_session.commit()

    assert await recompute(db_session) == 2
    await db_session.refresh(ugc)
    await db_session.refresh(avatar)
    assert "reputation" in (ugc.meta_json or {})
    assert "reputation" not in (avatar.meta_json or {})


# ── F1 第 4 项:project() 只读投影 ───────────────────────────────────────


@pytest.mark.anyio
async def test_project_is_read_only_and_matches_recompute(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", False)
    subject = _resident("proj_subject")
    teller = _resident("proj_teller")
    db_session.add_all([subject, teller])
    await db_session.flush()
    db_session.add(Memory(
        resident_id=teller.id, type="event", content="x",
        importance=0.7, source="gossip", related_resident_id=subject.id,
        metadata_json={"hops": 1, "distorted": False},
    ))
    await db_session.commit()

    assert await project(db_session) == []            # 闸门关且未 force → 空
    rows = await project(db_session, force=True)      # 标定路径:开闸前也能读
    assert {row.slug for row in rows} == {"proj_subject", "proj_teller"}
    await db_session.refresh(subject)
    assert "reputation" not in (subject.meta_json or {})   # 只读,零写入

    monkeypatch.setattr(settings, "rep_enabled", True)
    projected = {row.resident_id: row.score for row in await project(db_session)}
    assert await recompute(db_session) == 2
    await db_session.refresh(subject)
    assert score_from_meta(subject.meta_json) == pytest.approx(projected[subject.id])
