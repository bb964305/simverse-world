"""/polls/open 的对外契约护栏：白名单投影 + 选举标记。

玩家实测 #4/#2/#4b 的根因是 open_polls 把 options_json 原样吐出：前端按
string[] 渲染 dict 触发 React #31 整页崩，同时 _npc_voters 全名单、未落地
建筑坐标、提案人 slug 全部泄漏给未鉴权客户端。这里钉的是「对外只有
label + npc_votes」，杜绝形状与泄漏面再次漂移。
"""
import json
from datetime import datetime, timedelta, UTC

import pytest

from app.models.resident import Resident
from app.models.season import Poll
from app.services import civic_service, election_service, script_service


def _res(slug, name):
    return Resident(slug=slug, name=name, district="town_hall", status="idle",
                    resident_type="npc", creator_id="sys", tile_x=1, tile_y=1)


@pytest.mark.anyio
async def test_open_polls_projects_only_label_and_npc_votes(db_session):
    """一张真 civic poll（带全部内部 blob）投影后只剩两个键。"""
    db_session.add_all([_res("prop", "提案人"), _res("voter", "投票人")])
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "在南苑空地兴建一座邮局",
        [{"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
            "slug": "post_office", "bounds": [44, 100, 48, 106]}}},
         {"label": "暂缓,维持现状", "effect": None}],
        proposer_slug="prop",
    )
    assert poll is not None
    await civic_service.run_npc_voting(db_session)

    out = await script_service.open_polls(db_session)
    assert len(out) == 1
    for opt in out[0]["options"]:
        assert set(opt) == {"label", "npc_votes"}
        assert isinstance(opt["label"], str)
        assert isinstance(opt["npc_votes"], int)


@pytest.mark.anyio
async def test_open_polls_leaks_no_internal_fields(db_session):
    """整个响应序列化后不得出现任何内部键——黑名单挡不住新增键，这里查全文。"""
    db_session.add_all([_res("prop", "提案人"), _res("voter", "投票人")])
    await db_session.commit()
    await civic_service.propose(
        db_session, "在东岸花园兴建一座剧院",
        [{"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
            "slug": "theater", "center": [175, 45]}}},
         {"label": "暂缓,维持现状", "effect": None}],
        proposer_slug="prop",
    )
    await civic_service.run_npc_voting(db_session)

    polls = await script_service.open_polls(db_session)
    blob = json.dumps(polls, ensure_ascii=False)
    for leaked in ("_npc_voters", "_proposer_slug", "_eligible_at_open", "effect", "theater"):
        assert leaked not in blob, f"{leaked} 泄漏到了对外响应里"
    # "175" (from center: [175, 45]) is checked against the parsed `options`
    # substructure only, not the whole blob: the blob also contains poll.id
    # (a uuid4's 32 hex chars) and closes_at.isoformat() (6-digit microseconds),
    # where "175" as a substring turns up by pure chance ~1%/run combined —
    # a flaky assertion that erodes trust in the whole suite once it flips red
    # for no real reason.
    options_blob = json.dumps([p["options"] for p in polls], ensure_ascii=False)
    assert "175" not in options_blob, "175 泄漏到了对外响应的 options 里"


@pytest.mark.anyio
async def test_open_polls_flags_elections(db_session):
    """选举 poll 带 is_election=True，普通议案为 False——前端据此拆区块。"""
    db_session.add_all([_res("a", "候选甲"), _res("b", "候选乙")])
    await db_session.commit()
    await election_service.open_election(db_session, candidate_slugs=["a", "b"])
    await civic_service.propose(
        db_session, "广场是否加装长椅",
        [{"label": "支持", "effect": None}, {"label": "反对", "effect": None}],
    )

    out = await script_service.open_polls(db_session)
    by_election = {p["is_election"]: p["question"] for p in out}
    assert by_election[True].startswith(election_service.ELECTION_TAG)
    assert by_election[False] == "广场是否加装长椅"


@pytest.mark.anyio
async def test_string_options_still_supported(db_session):
    """历史/回滚数据可能是 string[]（test_script_season 就这么造）——不许炸。"""
    db_session.add(Poll(question="谁是凶手？", options_json=["管家", "园丁"],
                        closes_at=datetime.now(UTC) + timedelta(hours=24),
                        status="open"))
    await db_session.commit()

    out = await script_service.open_polls(db_session)
    assert out[0]["options"] == [{"label": "管家", "npc_votes": 0},
                                 {"label": "园丁", "npc_votes": 0}]


@pytest.mark.anyio
async def test_polls_open_endpoint_is_anonymous_and_clean(client, db_session):
    """HTTP 层同样干净——生产泄漏就是匿名 curl 拿到的。"""
    db_session.add_all([_res("prop", "提案人"), _res("voter", "投票人")])
    await db_session.commit()
    await civic_service.propose(
        db_session, "旱季供水改造",
        [{"label": "赞成", "effect": {"type": "system_config", "key": "x",
                                       "value": 1}},
         {"label": "反对", "effect": None}],
        proposer_slug="prop",
    )
    await civic_service.run_npc_voting(db_session)

    resp = await client.get("/polls/open")
    assert resp.status_code == 200
    body = resp.text
    assert "_npc_voters" not in body and "_proposer_slug" not in body
    assert "system_config" not in body
    polls = resp.json()["polls"]
    assert polls and set(polls[0]["options"][0]) == {"label", "npc_votes"}


# ── /polls/propose:自由文本的入口闸 ────────────────────────────────────

async def _token(db, email: str) -> str:
    """一个普通(非 admin)登录用户的 Bearer token —— propose 只要求这一个条件。"""
    from app.models.user import User
    from app.services.auth_service import create_token

    user = User(name="p", email=email, is_admin=False, is_banned=False)
    db.add(user)
    await db.commit()
    return create_token(user.id)


def _body(topic: str = "广场是否加装长椅", label: str = "支持") -> dict:
    return {"topic": topic, "options": [{"label": label}, {"label": "反对"}]}


@pytest.mark.anyio
async def test_propose_rejects_oversized_free_text(client, db_session):
    """topic / label 无长度上限 = 一个 Bearer token 就能把全镇 prompt 灌爆。

    这些字符串不止落进 polls 表:它们进每位 NPC 的 system prompt 与 decide
    prompt,还经 ``_clerk_announce`` 广播成 14 人的**持久记忆**——写进去就擦不掉。
    读侧的截断是兜底,入口这道才是「不该收下」。topic 的上限对齐
    ``Poll.question`` 的 ``String(300)``:再宽就是留给 PG 去报 DataError。
    """
    from app.routers.polls import TOPIC_MAX_CHARS, OPTION_LABEL_MAX_CHARS

    auth = {"Authorization": f"Bearer {await _token(db_session, 'p1@t.co')}"}

    assert (await client.post("/polls/propose", headers=auth,
                              json=_body(topic="议" * (TOPIC_MAX_CHARS + 1))
                              )).status_code == 422
    assert (await client.post("/polls/propose", headers=auth,
                              json=_body(label="项" * (OPTION_LABEL_MAX_CHARS + 1))
                              )).status_code == 422
    assert (await client.post("/polls/propose", headers=auth, json={
        "topic": "选项灌爆", "options": [{"label": f"{i}"} for i in range(500)],
    })).status_code == 422

    ok = await client.post("/polls/propose", headers=auth, json=_body(
        topic="议" * TOPIC_MAX_CHARS, label="项" * OPTION_LABEL_MAX_CHARS))
    assert ok.status_code == 200, "顶格的合法输入必须收得下"


@pytest.mark.anyio
async def test_propose_is_rate_limited(client, db_session):
    """限流:``app/rate_limit.py`` 的 ``default_limits=[]`` —— 没挂 ``@limiter.limit``
    的路由就是**完全不限**。开公投是写操作 + 全镇广播,一个脚本能开到天亮。"""
    from app.config import settings as app_settings

    auth = {"Authorization": f"Bearer {await _token(db_session, 'p2@t.co')}"}
    limit = app_settings.rest_rate_limit_propose_per_minute

    codes = [(await client.post("/polls/propose", headers=auth,
                                json=_body(topic=f"第 {i} 号议案"))).status_code
             for i in range(limit + 1)]
    assert codes[:limit] == [200] * limit, f"限额内不该被挡:{codes}"
    assert codes[limit] == 429, f"超出限额必须 429:{codes}"
