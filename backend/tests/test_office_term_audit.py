"""F3 Task 4/5/6 — 卸任财政审计。

只读汇总:每个数字都是 SELECT,唯一的写是自己那一行 system_config。
镇财政没有流水表(transactions.user_id 是 users.id 硬 FK,见
app/models/town_treasury.py),所以可审计面就是 S1-5 留下的余额 +
updated_at + system_config 戳,加上 S2-5 的财政政策行与推动它们的公决。

存储用 system_config:本线不允许迁移(§5 独占文件没有 models/migrations)。
value 是 String(2000) 且 ConfigService.set 用 json.dumps(ensure_ascii=True),
一个汉字 6 字节——所以每个 payload 落盘前必须过 _fit。
"""
from datetime import datetime, timedelta, UTC

import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.office import Office
from app.models.policy import Policy
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.season import Poll
from app.models.system_config import SystemConfig
from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.services.office_service import OfficeService
from app.tasks import office_audit


def _res(slug, name, meta=None, rtype="npc"):
    return Resident(
        slug=slug, name=name, district="central_plaza", status="idle",
        resident_type=rtype, creator_id="sys", tile_x=70, tile_y=56,
        meta_json=meta,
    )


@pytest.mark.anyio
async def test_collect_fiscal_audit_shape_and_numbers(db_session):
    started = datetime.now(UTC) - timedelta(days=5)
    ended = datetime.now(UTC)

    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=250,
                     updated_at=ended - timedelta(hours=2)),
        ResidentTreasury(resident_slug="ex-mayor", balance_sc=88),
        Office(office_key="mayor", institution="town_hall",
               fill_strategy="election", holder_slug=None),
        # 任内的财政政策改动
        Policy(key="tax_rate", value="0.2", tier="simple_majority",
               procedure="civic_poll", group="fiscal", version=3,
               updated_by="poll:p1", created_at=started,
               updated_at=started + timedelta(days=1)),
        # 任期之前的改动——不得计入
        Policy(key="npc_default_wage_sc", value="7", tier="simple_majority",
               procedure="civic_poll", group="fiscal", version=2,
               updated_by="poll:p0", created_at=started - timedelta(days=30),
               updated_at=started - timedelta(days=20)),
        # 任内经公决**通过**的财政议案(won 落在赞成项上,
        # 与 civic_service._close_one 的落库形状一致)
        Poll(question="镇务征询:把税率提到 0.2",
             options_json=[
                 {"label": "赞成", "won": True, "final_votes": 5, "effect": {
                     "type": "policy", "key": "tax_rate", "value": 0.2}},
                 {"label": "反对", "effect": None},
             ],
             closes_at=started + timedelta(days=1), status="closed"),
        # 财政议案但被否决:赞成项照样带着财政 effect,只是没赢——不得计入,
        # 否则「任内通过的财政议案」会把加税失败也算成政绩
        Poll(question="镇务征询:把税率提到 0.9",
             options_json=[
                 {"label": "赞成", "effect": {
                     "type": "policy", "key": "tax_rate", "value": 0.9}},
                 {"label": "反对", "won": True, "final_votes": 9,
                  "effect": None},
             ],
             closes_at=started + timedelta(days=1, hours=1), status="closed"),
        # 非财政议案——不得计入
        Poll(question="镇务征询:在南苑空地兴建一座邮局",
             options_json=[{"label": "赞成", "won": True, "effect": {
                 "type": "dynamic_location", "data": {"slug": "post_office"}}}],
             closes_at=started + timedelta(days=2), status="closed"),
    ])
    await db_session.commit()

    payload = await office_audit.collect_fiscal_audit(
        db_session, office_key="mayor", holder_slug="ex-mayor",
        term_started_at=started, term_ended_at=ended,
    )

    assert payload["schema_version"] == office_audit.AUDIT_SCHEMA_VERSION
    assert payload["office_key"] == "mayor"
    assert payload["fill_strategy"] == "election"
    assert payload["holder_slug"] == "ex-mayor"
    assert payload["town_balance_sc_end"] == 250
    assert payload["holder_balance_sc_end"] == 88
    assert payload["mayor_wage_multiplier"] == settings.election_mayor_wage_bonus
    assert [c["key"] for c in payload["fiscal_policy_changes"]] == ["tax_rate"]
    assert payload["fiscal_policy_changes"][0]["version"] == 3
    # 只认赢的那一项:被否决的加税提案(0.9 那张)不得计入
    assert payload["fiscal_polls_passed"] == 1
    assert payload["fiscal_poll_questions"] == ["镇务征询:把税率提到 0.2"]
    # 任期长度按世界日,不是真实日(k=4 → 5 真实日 = 20 世界日)
    assert payload["term_world_days"] == pytest.approx(
        5 * settings.world_clock_k, abs=0.01)
    assert payload["truncated"] is False


@pytest.mark.anyio
async def test_collect_fiscal_audit_never_writes(db_session):
    """只读汇总:跑一次审计不得改动任何余额。"""
    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=333),
        ResidentTreasury(resident_slug="ex-mayor", balance_sc=44),
    ])
    await db_session.commit()

    await office_audit.collect_fiscal_audit(
        db_session, office_key="mayor", holder_slug="ex-mayor",
        term_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    assert (await db_session.execute(
        select(TownTreasury.balance_sc).where(TownTreasury.key == TOWN_KEY)
    )).scalar_one() == 333
    assert (await db_session.execute(
        select(ResidentTreasury.balance_sc)
        .where(ResidentTreasury.resident_slug == "ex-mayor")
    )).scalar_one() == 44


@pytest.mark.anyio
async def test_record_and_list_term_audit(db_session):
    started = datetime.now(UTC) - timedelta(days=3)
    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=120),
    ])
    await db_session.commit()

    payload = await office_audit.record_term_audit(
        db_session, office_key="mayor", holder_slug="ex-mayor",
        term_started_at=started,
    )
    assert payload is not None

    key = office_audit.audit_key("mayor", "ex-mayor", started)
    row = (await db_session.execute(
        select(SystemConfig).where(SystemConfig.key == key)
    )).scalar_one()
    assert row.group == office_audit.AUDIT_GROUP
    assert row.updated_by == "office_term_audit"
    assert len(row.value) <= 2000
    assert json.loads(row.value)["holder_slug"] == "ex-mayor"

    listed = await office_audit.list_term_audits(db_session, office_key="mayor")
    assert len(listed) == 1
    assert listed[0]["holder_slug"] == "ex-mayor"
    assert await office_audit.list_term_audits(
        db_session, office_key="postman") == []


@pytest.mark.anyio
async def test_record_term_audit_without_holder_is_noop(db_session):
    assert await office_audit.record_term_audit(
        db_session, office_key="mayor", holder_slug=None,
        term_started_at=None) is None
    assert (await db_session.execute(
        select(SystemConfig).where(
            SystemConfig.group == office_audit.AUDIT_GROUP)
    )).scalars().all() == []


def test_fit_keeps_payload_under_system_config_limit():
    """value 是 String(2000) 且 ConfigService 用 ensure_ascii=True 序列化:
    汉字每字 6 字节,中文议案标题是真正会撑爆列宽的部分。"""
    payload = {
        "schema_version": 1,
        "office_key": "mayor",
        "holder_slug": "he-qiaoyun",
        "fiscal_policy_changes": [
            {"key": "tax_rate", "value": "0.25", "version": i,
             "updated_by": f"poll:{i}",
             "updated_at": datetime.now(UTC).isoformat()}
            for i in range(60)
        ],
        "fiscal_poll_questions": ["镇务征询:关于税率的第若干号提案" * 6] * 8,
    }
    out = office_audit._fit(payload)
    assert len(json.dumps(out)) <= 2000
    assert out["truncated"] is True
    assert len(out["fiscal_policy_changes"]) <= office_audit._MAX_POLICY_CHANGES
    assert len(out["fiscal_poll_questions"]) <= office_audit._MAX_POLL_QUESTIONS
    # 不可裁剪的标识字段必须原样保留
    assert out["office_key"] == "mayor"
    assert out["holder_slug"] == "he-qiaoyun"


def test_audit_key_fits_system_config_key_column():
    key = office_audit.audit_key(
        "mayor", "x" * 200, datetime(2026, 7, 27, 3, 4, 5, tzinfo=UTC))
    assert len(key) <= 200
    assert key.startswith("office_audit:mayor:")
    assert key.endswith("20260727T030405")
