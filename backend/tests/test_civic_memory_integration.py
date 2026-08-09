"""S7 —— 镇务广播收敛到 ``_clerk_announce`` 单一出口。

``_close_one`` → ``_execute_outcome`` → ``install_mayor`` 是一条**嵌套调用
链**:三处各广播一次,一次选举就给每人写 3 条,而 ``_fetch_event_candidates``
只有 30 个坑(``app/memory/service.py``)——镇务记忆会反过来把个人记忆挤出去。
``_clerk_announce`` 是 ``_close_one`` 全部终止分支与开票征询的唯一汇合点,收敛
到那一处既不漏也不重。

本文件的四条硬断言:

- **每位非赢家恰 1 条**(硬数字;写成 3 条也能满足「每人都收到了」这种软断言)。
- **赢家只有第一人称那条**——``install_mayor`` 已经给她写过,再收一条第三人称
  的就是同一件事在同一个人脑子里记两遍。
- **B2 真幂等**:手工把 ``poll.status`` 拨回 ``open`` 再结一次(``close_due_polls``
  只取 ``status=='open'`` 且 ``_close_one`` 首行就置 closed,不回拨的「再跑一次」
  是空转,断言恒绿)。
- **同一人连任**:``ref`` 用 ``poll.id`` 而不是 slug,两届各写满一轮。
"""
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.bulletin_post import BulletinPost
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.season import Poll, Vote
from app.services import civic_service, election_service


def _res(slug, name, sbti=None, duty=None, **kw):
    meta = {}
    if sbti:
        meta["sbti"] = {"dimensions": sbti}
    if duty:
        meta["duty"] = duty
    d = dict(slug=slug, name=name, district="central_plaza", status="idle",
             resident_type="npc", creator_id="sys", tile_x=70, tile_y=56,
             meta_json=meta or None)
    d.update(kw)
    return Resident(**d)


@pytest.fixture
def broadcast_on(monkeypatch):
    """开广播总闸(S1 的六个闸门默认全关)。"""
    monkeypatch.setattr(settings, "civic_memory_broadcast_enabled", True)


async def _town(db) -> dict[str, Resident]:
    """生产名册(npc / UGC resident / player)的最小复刻。

    收件人 = ``is_autonomous`` = 文书 + 两位候选 + UGC = 4 人;玩家分身不收。
    文书没有 SBTI,所以不会被 ``open_election`` 选进候选集。
    """
    people = {
        "clerk": _res("zhao", "赵启文", duty={"key": "town_clerk"}),
        "cand_a": _res("cand-a", "候选甲", sbti={"Ac1": "H"}),
        "cand_b": _res("cand-b", "候选乙", sbti={"So1": "H"}),
        "ugc": _res("bai-xing", "白杏", resident_type="resident"),
        "player": _res("p-chen", "陈铁生", resident_type="player"),
    }
    for r in people.values():
        db.add(r)
    await db.commit()
    return people


async def _elect(db, people, *, winner: str = "cand_a") -> Poll:
    """开一张镇长选举 → 投一张真人票 → 结票。返回那张 poll。"""
    poll = await election_service.open_election(db, days=0)
    assert poll is not None
    idx = next(i for i, o in enumerate(poll.options_json)
               if o["effect"]["slug"] == people[winner].slug)
    db.add(Vote(poll_id=poll.id, user_id=f"u-{poll.id}", option_idx=idx))
    await db.commit()
    assert await civic_service.close_due_polls(db) == 1
    return poll


async def _civic_events(db) -> dict[str, list[str]]:
    """``civic_event`` → 收到这条广播的 resident_id 列表。

    刻意返回 list 而不是 set:重复广播(rev1 的三出口)会让列表变长,set 会把它
    悄悄吞掉。
    """
    rows = (await db.execute(
        select(Memory).where(Memory.source == "civic")
    )).scalars().all()
    out: dict[str, list[str]] = {}
    for m in rows:
        out.setdefault(m.metadata_json["civic_event"], []).append(m.resident_id)
    return out


# ── 单一出口 ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_full_election_writes_exactly_one_result_memory_per_non_winner(
        db_session, broadcast_on):
    people = await _town(db_session)
    poll = await _elect(db_session, people)

    assert await election_service.current_mayor(db_session) == "cand-a"

    events = await _civic_events(db_session)
    # 一次完整选举 = 恰两轮广播:开票征询一轮、结果一轮。三出口版本会多出
    # install_mayor / _execute_outcome 那两把互不相认的键。
    assert sorted(events) == [f"civic:poll_open:{poll.id}",
                             f"civic:poll_result:{poll.id}"]
    assert sorted(events[f"civic:poll_result:{poll.id}"]) == sorted([
        people["clerk"].id, people["cand_b"].id, people["ugc"].id,
    ])
    assert sorted(events[f"civic:poll_open:{poll.id}"]) == sorted([
        people["clerk"].id, people["cand_a"].id, people["cand_b"].id,
        people["ugc"].id,
    ])


@pytest.mark.anyio
async def test_winner_keeps_only_the_first_person_memory(db_session, broadcast_on):
    people = await _town(db_session)
    poll = await _elect(db_session, people)

    mine = (await db_session.execute(
        select(Memory).where(Memory.resident_id == people["cand_a"].id)
    )).scalars().all()
    first_person = [m for m in mine if "我当选" in m.content]
    assert len(first_person) == 1
    assert first_person[0].source == "reflection"
    # 结果那轮把她排除掉了;开票征询那轮她照收(那时还不是赢家)。
    assert [m.metadata_json["civic_event"] for m in mine if m.source == "civic"] \
        == [f"civic:poll_open:{poll.id}"]


# ── B2 真幂等 ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_reclosing_the_same_poll_writes_nothing_new(db_session, broadcast_on):
    """幂等键是 ``poll.id`` 这个稳定值,不是刚建的那行公告的主键(每次补跑都是
    新 uuid,幂等会完全落空)。"""
    people = await _town(db_session)
    poll = await _elect(db_session, people)
    before = len((await db_session.execute(
        select(Memory).where(Memory.source == "civic"))).scalars().all())
    assert before == 7   # 征询 4 人 + 结果 3 人(赢家排除);零条时下面的比对是空转

    poll.status = "open"
    await db_session.commit()
    # 这一次是真的重新结了一遍票(不是空转):公告板会多出一条结果公告。
    assert await civic_service.close_due_polls(db_session) == 1
    posts = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.title.like("镇务结果%"))
    )).scalars().all()
    assert len(posts) == 2

    after = len((await db_session.execute(
        select(Memory).where(Memory.source == "civic"))).scalars().all())
    assert after == before


@pytest.mark.anyio
async def test_two_elections_for_the_same_slug_each_write_a_full_round(
        db_session, broadcast_on):
    """连任不该被幂等键静默吞掉 —— rev1 拿 slug 当 ref,第二届写 0 条。"""
    people = await _town(db_session)
    first = await _elect(db_session, people)
    second = await _elect(db_session, people)
    assert first.id != second.id

    events = await _civic_events(db_session)
    results = {k: v for k, v in events.items() if ":poll_result:" in k}
    assert sorted(results) == sorted([f"civic:poll_result:{first.id}",
                                      f"civic:poll_result:{second.id}"])
    assert [len(v) for v in results.values()] == [3, 3]


# ── 分档 importance ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_notice_and_result_land_in_two_ranks(db_session, broadcast_on):
    """开票征询是「镇上在议一件事」,结果是「这件事定了」——后者才配最高档。"""
    people = await _town(db_session)
    poll = await _elect(db_session, people)

    rows = (await db_session.execute(
        select(Memory).where(Memory.source == "civic",
                             Memory.resident_id == people["clerk"].id)
    )).scalars().all()
    by_event = {m.metadata_json["civic_event"]: m for m in rows}
    assert by_event[f"civic:poll_open:{poll.id}"].importance == pytest.approx(
        settings.civic_memory_notice_importance)
    assert by_event[f"civic:poll_result:{poll.id}"].importance == pytest.approx(
        settings.civic_memory_importance)
    assert "镇长选举" in by_event[f"civic:poll_result:{poll.id}"].content


# ── 其余终止分支 ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_lapsed_poll_also_broadcasts_its_outcome(db_session, broadcast_on):
    """流会也是镇务结果:无人投票的那条分支同样只走一次 ``_clerk_announce``,
    且没有赢家可排除 —— 4 位自治居民各收 1 条。"""
    people = await _town(db_session)
    poll = await civic_service.propose(
        db_session, "广场是否加装长椅",
        [{"label": "加装", "effect": None}, {"label": "维持原样", "effect": None}],
        days=0)
    assert poll is not None
    assert await civic_service.close_due_polls(db_session) == 1

    events = await _civic_events(db_session)
    assert sorted(events[f"civic:poll_result:{poll.id}"]) == sorted([
        people["clerk"].id, people["cand_a"].id, people["cand_b"].id,
        people["ugc"].id,
    ])
    body = (await db_session.execute(
        select(Memory).where(
            Memory.metadata_json["civic_event"].as_string()
            == f"civic:poll_result:{poll.id}")
    )).scalars().first().content
    assert "流会" in body


# ── 闸关 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_leaves_the_whole_chain_untouched(db_session):
    """闸关时结票链与今天逐字节一致:公告照发、镇长照装,零条记忆。"""
    people = await _town(db_session)
    await _elect(db_session, people)

    assert await election_service.current_mayor(db_session) == "cand-a"
    assert (await db_session.execute(
        select(Memory).where(Memory.source == "civic"))).scalars().all() == []
    posts = (await db_session.execute(select(BulletinPost))).scalars().all()
    assert sorted(p.title for p in posts) == [
        "镇务征询:镇长选举:谁来当下一任镇长?",
        "镇务结果:镇长选举:谁来当下一任镇长?",
    ]
