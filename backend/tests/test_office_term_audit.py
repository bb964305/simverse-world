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


# ── Task 5: 空缺超过一个夜间周期 → 红旗输入 ─────────────────────────

@pytest.mark.anyio
async def test_overdue_vacancies_flags_only_stale_empty_offices(db_session):
    now = datetime.now(UTC)
    db_session.add_all([
        # 空缺 30 小时 → 超过一个夜间周期
        Office(office_key="mayor", institution="town_hall",
               fill_strategy="election", holder_slug=None,
               updated_at=now - timedelta(hours=30)),
        # 同样是民选缺位,但只空了 2 小时 → 还在允许窗口内(时间阈值本身的边界)
        Office(office_key="deputy_mayor", institution="town_hall",
               fill_strategy="election", holder_slug=None,
               updated_at=now - timedelta(hours=2)),
        # 在任 → 无论多久都不算空缺
        Office(office_key="postman", institution="post_office",
               fill_strategy="seed", holder_slug="luo-xiaozhou",
               updated_at=now - timedelta(days=90)),
    ])
    await db_session.commit()

    flags = await office_audit.overdue_vacancies(db_session, now=now)
    assert [f["office_key"] for f in flags] == ["mayor"]
    assert flags[0]["fill_strategy"] == "election"
    assert flags[0]["vacant_hours"] == pytest.approx(30.0, abs=0.05)
    assert flags[0]["vacant_since"] == (now - timedelta(hours=30)).isoformat()

    # 阈值可调:放宽到 48 小时后没有红旗
    assert await office_audit.overdue_vacancies(
        db_session, max_vacant_hours=48.0, now=now) == []


@pytest.mark.anyio
async def test_overdue_vacancies_ignores_labour_offices_vacant_forever(db_session):
    """生产的真实形态:迁移 046 seed 出四行,doctor 连 backfill 都没有,
    holder 恒 NULL、updated_at 停在迁移那一刻。它们没有任何自动回填路径
    (trigger_backfill 只认 fill_strategy == "election"),所以不得成为永久红旗
    ——否则探针每晚恒定 2~3 面红旗,镇长空缺淹没在噪声里。"""
    now = datetime.now(UTC)
    db_session.add_all([
        Office(office_key="doctor", institution="clinic",
               fill_strategy="appointment", holder_slug=None,
               updated_at=now - timedelta(days=90)),
        Office(office_key="postman", institution="post_office",
               fill_strategy="seed", holder_slug=None,
               updated_at=now - timedelta(days=90)),
    ])
    await db_session.commit()

    assert await office_audit.overdue_vacancies(db_session, now=now) == []
    # 显式放开策略白名单时才看得到它们(收口若要扩面,这就是入口)
    widened = await office_audit.overdue_vacancies(
        db_session, strategies=frozenset({"election", "appointment", "seed"}),
        now=now)
    assert [f["office_key"] for f in widened] == ["doctor", "postman"]


@pytest.mark.anyio
async def test_overdue_vacancies_shape_is_probe_consumable(db_session):
    """形状契约:钉死探针收口时会依赖的字段集(本线不接线,只能靠这条测试
    保证交接面不漂)。"""
    now = datetime.now(UTC)
    db_session.add(Office(office_key="mayor", institution="town_hall",
                          fill_strategy="election", holder_slug=None,
                          updated_at=now - timedelta(hours=30)))
    await db_session.commit()

    flag = (await office_audit.overdue_vacancies(db_session, now=now))[0]
    assert set(flag) == {"office_key", "fill_strategy",
                         "vacant_since", "vacant_hours"}
    json.dumps(flag)          # 探针要 JSON 序列化,不许塞 datetime 进去


@pytest.mark.anyio
async def test_overdue_vacancies_empty_world(db_session):
    assert await office_audit.overdue_vacancies(db_session) == []


@pytest.mark.anyio
async def test_overdue_vacancies_never_flags_an_occupied_election_office(db_session):
    """假阳性回归锁(评审 fix round 1 追加):去掉 holder_slug IS NULL 谓词后,
    在任镇长会被误判成空缺红旗——比漏报一个空缺更危险,brief 的立意是
    「别让噪声淹没真正该看的镇长空缺」,而假阳性正是更糟的一种噪声,一旦
    出现会让人开始无视红旗。这条只锁 holder_slug 这一半谓词:fill_strategy
    谓词已经被 test_overdue_vacancies_ignores_labour_offices_vacant_forever
    锁住了(该用例里的 doctor/postman 都是 holder_slug=None,不会触达
    holder_slug 谓词)。"""
    now = datetime.now(UTC)
    db_session.add(Office(office_key="mayor", institution="town_hall",
                          fill_strategy="election", holder_slug="he-qiaoyun",
                          updated_at=now - timedelta(hours=90)))
    await db_session.commit()

    assert await office_audit.overdue_vacancies(db_session, now=now) == []


# ── Task 6: 出缺路径接线 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_term_check_records_audit_for_departing_holder(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=175),
    ])
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1

    audits = await office_audit.list_term_audits(db_session, office_key="mayor")
    assert len(audits) == 1
    assert audits[0]["holder_slug"] == "ex-mayor"
    assert audits[0]["town_balance_sc_end"] == 175
    assert audits[0]["term_started_at"] is not None


@pytest.mark.anyio
async def test_vacate_audits_only_when_asked(db_session):
    """默认 audit=False → 既有调用方(admin/测试/F2 之外的路径)行为不变。"""
    db_session.add(_res("ex-mayor", "前镇长"))
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")
    assert await svc.vacate("mayor") is True
    assert await office_audit.list_term_audits(db_session) == []

    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")
    assert await svc.vacate("mayor", audit=True) is True
    audits = await office_audit.list_term_audits(db_session)
    assert len(audits) == 1
    assert audits[0]["holder_slug"] == "ex-mayor"
    # fix round 1(评审):锁住 vacate() 预读的 term_started_at 真的被传给了
    # 审计——硬编码成 None 不会被 brief 给定的任何断言抓到(M6 幸存变异)。
    assert audits[0]["term_started_at"] is not None


@pytest.mark.anyio
async def test_vacate_audit_noop_when_office_already_vacant(db_session):
    """没有真正出缺就不该有审计行(guard UPDATE rowcount==0)。"""
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "someone", fill_strategy="election")
    await svc.vacate("mayor")
    assert await svc.vacate("mayor", audit=True) is False
    assert await office_audit.list_term_audits(db_session) == []


@pytest.mark.anyio
async def test_audit_failure_does_not_break_vacate(db_session, monkeypatch):
    db_session.add(_res("ex-mayor", "前镇长"))
    await db_session.commit()

    async def _boom(db, **kwargs):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(office_audit, "collect_fiscal_audit", _boom)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")
    assert await svc.vacate("mayor", audit=True) is True
    assert await svc.get_holder("mayor") is None


@pytest.mark.anyio
async def test_audit_write_failure_leaves_session_usable(db_session, monkeypatch):
    """上一条的 _boom 进门就 raise,session 从没脏过,加不加 rollback 都会绿。
    真实故障形状是异常来自落盘那一步的 flush/commit(ConfigService.set 内部
    有 db.commit()):那时 session 停在 needs-rollback 状态,后续任何语句都抛
    PendingRollbackError——F2 拿到 vacate 的 True 之后还要在同一个 session 上
    改档位 / 写历史行 / 断言 / 广播,那些写会全军覆没(spec §4.3 要防的半途状态)。
    """
    from app.services.config_service import ConfigService

    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=10),
        # 它的主键待会儿被拿去制造 flush 期冲突(SystemConfig.key 是 PK)
        SystemConfig(key="probe-dup", value="1", group="probe",
                     updated_by="test"),
    ])
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")

    async def _boom_in_flush(self, key, value, *, group, updated_by):
        self._db.add(SystemConfig(key="probe-dup", value="2", group="probe",
                                  updated_by="test"))
        await self._db.flush()    # IntegrityError:主键冲突,炸在 flush 里

    monkeypatch.setattr(ConfigService, "set", _boom_in_flush)
    assert await svc.vacate("mayor", audit=True) is True
    # 关键断言:session 仍可用。没有 rollback 这里会抛 PendingRollbackError。
    assert await svc.get_holder("mayor") is None
    assert await office_audit.list_term_audits(db_session) == []


@pytest.mark.anyio
async def test_term_check_audit_failure_does_not_break_vacate_or_backfill(
    db_session, monkeypatch,
):
    """brief 给定的 Step 1 测试只锁了 vacate(audit=True) 侧的审计失败,没有
    覆盖 term_check 自动接线这一侧——而这恰恰是 brief 正文点名要求「自己论证
    位置、并用测试锁住」的那条:record_term_audit 插在 term_check 循环里
    self.db.commit()(出缺 + 清遗留)之后、trigger_backfill(补选)之前。

    与姊妹测试 test_term_check_backfill_failure_does_not_break_vacate
    (tests/test_office_backfill.py)对称:那条锁「补选失败不撕毁出缺」,
    这条锁「审计失败既不撕毁出缺,也不拦住紧随其后的补选」——审计调用放在
    vacate 的 commit() 之后,所以它失败时只回滚它自己尚未提交的工作,
    出缺本身已经落盘,补选调用也在它之后正常触发。
    """
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    db_session.add_all([_res("a", "甲"), _res("b", "乙"), _res("c", "丙")])
    await db_session.commit()

    async def _boom(db, **kwargs):
        raise RuntimeError("audit backend down")

    # 挂 collect_fiscal_audit(record_term_audit 内部调用的第一步),让
    # record_term_audit 自己的 fail-open(try/except + _rollback_quietly)
    # 走到——而不是直接替掉 record_term_audit 本身(那样绕过了它自己的
    # 兜底,只是在测「term_check 有没有 try/except」而不是在测「审计模块
    # 自己的 fail-open 是否真的兜住了」)。
    monkeypatch.setattr(office_audit, "collect_fiscal_audit", _boom)

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "a", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1
    # 出缺没被审计失败撕毁
    assert await svc.get_holder("mayor") is None
    # 补选也没被拦住(它在审计调用之后,审计的失败/rollback 不该波及它)
    polls = (await db_session.execute(
        select(Poll).where(Poll.status == "open")
    )).scalars().all()
    assert len(polls) == 1


@pytest.mark.anyio
async def test_term_check_audit_term_ended_at_follows_injected_clock(
    db_session, monkeypatch,
):
    """term_check 的 ``now`` 是可注入的冻结时钟(测试脚手架的核心契约,见
    test_office_backfill.py 里所有用 ``now=datetime.now(UTC)+timedelta(...)``
    的用例)。写审计时若漏传 ``term_ended_at=now``,record_term_audit 会悄悄
    落到它自己的默认值——真实墙钟时间——而不是循环判定"到期"时用的那个
    ``now``。brief 给定的 test_term_check_records_audit_for_departing_holder
    只断言了 term_started_at is not None,没有钉住 term_ended_at 必须等于注入
    的 now,这条补上这个盲点。"""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    db_session.add_all([
        _res("ex-mayor", "前镇长"),
        TownTreasury(key=TOWN_KEY, balance_sc=1),
    ])
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election", term_days=7)
    frozen_future = datetime.now(UTC) + timedelta(days=365)
    assert await svc.term_check(now=frozen_future) == 1

    audits = await office_audit.list_term_audits(db_session, office_key="mayor")
    recorded_ended = datetime.fromisoformat(audits[0]["term_ended_at"])
    assert abs((recorded_ended - frozen_future).total_seconds()) < 5
