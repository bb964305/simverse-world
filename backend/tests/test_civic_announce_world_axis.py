"""开票公告里的截止日必须与居民所在的时间轴同轴 —— 世界轴。

``civic_service.propose`` 拼的那句公告经 ``_clerk_announce`` →
``broadcast_civic_memory`` 落成**全体自治居民的一等记忆**。记忆段是不带日期戳的
裸 ``- {content}``(``app/llm/prompt.py::format_memory_context``),所以这句话会被
原样 retrieve 回每个 NPC 的「## 记忆」里,**永久**有效。

旧文案写的是 ``poll.closes_at.date()`` —— **真实**时间轴上的绝对日期;而同一份
prompt 里的「今天」走 ``world_clock``(k=4,今天两者相隔约两年)。NPC 照字面读 =
这张正在议的票两年前就截止了。这与「小镇现况」段刚修掉的是同一类缺陷(见
``town_facts_service._closes_in_world_days`` 的长注释),只是这一处落的是永久记忆,
错得更久、也更难回收。

**换算**(按哪根轴数)复用那一批的 ``_closes_in_world_days``,不另起第二份口径;
**措辞**(怎么说)另走 ``_announce_closes_text``。两者分家是因为时态语域不同:
「小镇现况」段每次读取现算,说「还有 N 天」是对的;这一句写进永久记忆后没有日期戳
去校正它,原点必须写进话里(「自本次公告起 N 天」)。见下面那组 deictic 断言。
"""
import re
from datetime import UTC, datetime

import pytest

from app import world_clock
from app.config import settings
from app.services import civic_service

#: 锚点与 tests/test_town_facts_service.py::frozen_clock 同一个:距 ``world_epoch``
#: 恰好 220 天 3 小时,×4 之后世界时间落在 **2028-05-30 12:00**(世界日正中)。
#: 日界线离得远,下面的期望值量的才是换算本身,而不是量日差的取整。
#:
#: 刻意用 UTC 表示:sqlite 存 ``DateTime(timezone=True)`` 会把 offset 丢掉只留墙上
#: 时间,而 ``world_clock._as_zone`` 按「naive = UTC」解读 —— 拿 +08 的时刻造数据会
#: 在读回时凭空多出 8 小时(= 32 个世界小时,足够把日差顶偏一天)。生产写入侧本来
#: 就是 ``datetime.now(UTC)``。
_NOW = datetime(2026, 8, 8, 19, 0, tzinfo=UTC)


@pytest.fixture
def frozen_clock(monkeypatch):
    """把**两根轴的 now 一起**钉死,期望值才是手算出来的常数。

    只钉 ``world_clock.now_real`` 不够:``propose`` 写 ``closes_at`` 用的是它自己
    模块里的 ``datetime.now(UTC)``(civic_service.py:82)。两个 now 不同源的话,
    倒计时就成了「测试跑了多久」的函数 —— 世界时钟每 6 个真实小时跨一次日界线,
    毫秒级的偏差恰好落在界线上就会把 12 抖成 11。
    """
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW if tz is None else _NOW.astimezone(tz)

    monkeypatch.setattr(settings, "world_clock_k", 4)
    monkeypatch.setattr(settings, "world_epoch", "2026-01-01T00:00:00+08:00")
    monkeypatch.setattr(settings, "civic_polls_enabled", True)
    monkeypatch.setattr(world_clock, "now_real", lambda: _NOW)
    monkeypatch.setattr(civic_service, "datetime", _Frozen)
    return _NOW


@pytest.fixture
def announced(monkeypatch):
    """截获镇务公告正文。被测的就是 ``propose`` 拼出来的这一串真串。

    截在 ``_clerk_announce`` 而不是读公告板:那一层把异常整个吞掉
    (``logger.warning`` 后继续),落库失败会让断言红在「没有公告」上,指不到真正
    的缺陷。这里要钉的是**文案本身**。
    """
    bodies: list[str] = []

    async def _spy(db, title, body, **kw):
        bodies.append(body)

    monkeypatch.setattr(civic_service, "_clerk_announce", _spy)
    return bodies


async def _propose(db):
    return await civic_service.propose(
        db, "是否在东岸花园兴建剧院",
        [{"label": "赞成兴建"}, {"label": "暂缓,维持现状"}],
        days=3,
    )


@pytest.mark.anyio
async def test_open_announcement_counts_down_on_the_world_axis(
        db_session, frozen_clock, announced):
    """3 个真实日的投票窗 = 居民感知里的 12 个世界日。"""
    poll = await _propose(db_session)

    assert poll is not None
    assert announced, "开票必须出一条镇务公告"
    assert "自本次公告起 12 天截止" in announced[0], \
        f"k=4,3 个真实日 = 12 个世界日,与居民的「今天」同轴;实际:{announced[0]}"


#: 以**说话时刻**为原点的指示性措辞。它们在「小镇现况」段是对的(那一段每次读取
#: 现算),在这里是错的:这句话写一次、被反复 retrieve,而记忆段不带日期戳。
_DEICTIC = ("还有", "今天", "明天")


@pytest.mark.anyio
async def test_open_announcement_uses_no_read_time_deictic_wording(
        db_session, frozen_clock, announced):
    """倒计时的原点必须写进话里,不能是「读到这句话的那一刻」。

    公告落成永久记忆后,``format_memory_context`` 把它渲染成不带日期戳的裸
    ``- {content}`` —— 没有任何东西会告诉 NPC 这句话是什么时候说的。「还有 12 天
    截止」于是永远自称「现在」:一个月后被检索回来,听着仍像眼下还剩 12 天。
    「自本次公告起 12 天」不随读取时刻漂移,因为原点是公告本身。
    """
    await _propose(db_session)

    body = announced[0]
    for word in _DEICTIC:
        assert word not in body, \
            f"指示性措辞「{word}」会随时间腐坏,不能进永久记忆:{body}"


@pytest.mark.anyio
async def test_open_announcement_carries_no_real_axis_date(
        db_session, frozen_clock, announced):
    """公告正文里不得留下任何真实轴的绝对日期。

    黑名单式地查「2026-08-11」挡不住换个写法的回归,所以直接查形状:整句话里一个
    ``YYYY-MM-DD`` 都不该有。这一段进的是永久记忆,而记忆段没有日期戳去校正它。
    """
    await _propose(db_session)

    body = announced[0]
    assert not re.search(r"\d{4}-\d{2}-\d{2}", body), \
        f"绝对日期漏进了永久记忆:{body}"
    assert "2026" not in body, f"真实轴的年份漏进了永久记忆:{body}"


#: (``_closes_in_world_days`` 的返回值, 折出来的那半句)。分档边界一个不漏。
_ANNOUNCE_WORDING = (
    (0, "于公告当天截止"),
    (1, "于公告次日截止"),
    (2, "自本次公告起 2 天截止"),
    (12, "自本次公告起 12 天截止"),
)


@pytest.mark.parametrize("days,expected", _ANNOUNCE_WORDING)
def test_announce_wording_by_bucket(days, expected):
    assert civic_service._announce_closes_text(days) == expected


def test_announce_wording_clamps_at_the_read_side_ceiling():
    """顶格那一档说「以上」而不是一个确切数字。

    ``_closes_in_world_days`` 把值夹在 ``POLL_CLOSES_IN_MAX_DAYS``,被夹住的真值
    可能大得多 —— 报确切数字就是编。中文的「以上」含本数,恰好顶格时这句也为真。
    """
    from app.services.town_facts_service import POLL_CLOSES_IN_MAX_DAYS

    want = f"自本次公告起 {POLL_CLOSES_IN_MAX_DAYS} 天以上截止"
    assert civic_service._announce_closes_text(POLL_CLOSES_IN_MAX_DAYS) == want
    assert civic_service._announce_closes_text(POLL_CLOSES_IN_MAX_DAYS + 5) == want


@pytest.mark.parametrize("junk", [None, -1, -7, True, "12", {}, [1], 3.5])
def test_announce_wording_degrades_to_silence(junk):
    """认不出的形状 → 空串,整句就不提截止。

    负数**刻意**也走这一支:开票公告不该宣称一张刚开出来的票已经过期。它是调用方
    传了非法窗口的信号,少半句好过编一句自相矛盾的话(``propose`` 那侧据此整段
    跳过截止从句)。``True`` 是 ``int`` 的子类,必须单独挡掉。
    """
    assert civic_service._announce_closes_text(junk) == ""
