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

换算(按哪根轴数)与措辞(怎么说)都复用那一批的两个函数,不在这里另起第三份口径。
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
    assert "还有 12 天截止" in announced[0], \
        f"k=4,3 个真实日 = 12 个世界日,与居民的「今天」同轴;实际:{announced[0]}"


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
