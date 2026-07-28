"""#6-1/#6-2 村落日报正文为空 + 空行被幂等永久钉死。

compose_digest 绕过 app.llm.client.chat() 直调 messages.create，于是
thinking 没被关掉（chat() 是全仓唯一会加 thinking={"type":"disabled"} 的
地方），800 的 max_tokens 被推理吃光，响应里没有可用 text block → 返回空串。
generate_village_digest 拿到空串后无条件落库，而 (scope,date,user_id) 唯一
约束 + 「行存在就早返回」的幂等让这一天永远是空的。

生产实证：2026-07-17/24/25/26 四天 content_md 长度为 0，且四天全部落在
output_tokens=801（max_tokens 触顶）的样本里。

CRIT-2（final whole-branch review 升级）：上一轮的守卫 `if not
content.strip()` 挡不住「只有标题行」——``"# 今日头条"`` 这种字符串本身非
空，会骗过守卫落库，然后被幂等的「已经有正文」早返回永久钉死，且不像全空
那样能从库里一眼查出来，比原 bug 更隐蔽。本文件同时钉住：新守卫改用「去掉
标题行之后是否还有 ``DIGEST_MIN_BODY_CHARS`` 字以上的实质正文」判断，且
早返回判据必须与之保持同一把尺子（否则回填脚本挑出来的行会在早返回这里被
悄悄放过）。
"""
from datetime import date, datetime, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, func

from app.models.digest import Digest
from app.models.memory import Memory


async def _material(db, day):
    """给 gather_material 一点素材，避免走冷启动兜底分支。

    时间戳必须落在 ``day`` 当天：gather_material 按 [day 00:00, day+1 00:00)
    UTC 窗口过滤 Memory.created_at。用 now() 去喂一个查历史日期的窗口永远
    查不到，has_material 恒为 False，测试就会静默地走冷启动兜底而根本不调用
    compose_digest —— 那样这两条用例测的就不是它们声称要测的东西了。
    """
    db.add(Memory(resident_id="r1", type="event", content="今天大家聊得很开心",
                  importance=0.9, source="chat_resident",
                  created_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC)))
    await db.commit()


@pytest.mark.anyio
async def test_compose_disables_thinking_and_raises_the_token_budget(db_session):
    """走 chat() 包装，且显式锁定 model —— 不靠 background_model 恰好相等。"""
    from app.config import settings
    from app.services import digest_service as ds

    captured = {}

    async def _fake_chat(system_prompt, messages, model=None, max_tokens=None, **kw):
        captured.update(system=system_prompt, messages=messages,
                        model=model, max_tokens=max_tokens, kw=kw)
        return "# 今日头条\n小镇很热闹"

    with patch.object(ds, "llm_chat", _fake_chat):
        title, content = await ds.compose_digest(date(2026, 7, 28), {
            "events": [], "chats": ["聊得开心"], "shifts": [], "arc_lines": [],
            "heat_top": [], "stats": {},
        })

    assert title == "今日头条" and "热闹" in content
    assert captured["max_tokens"] == ds.DIGEST_MAX_TOKENS >= 2000
    assert captured["model"] == settings.effective_model
    assert captured["kw"]["owner"] == "system"
    assert captured["kw"]["meter"].scenario == "digest"


@pytest.mark.anyio
async def test_empty_compose_result_is_not_persisted(db_session):
    """空正文不许落库 —— 一旦落了，幂等会把这一天永久钉死。"""
    from app.services import digest_service as ds

    await _material(db_session, date(2026, 7, 24))
    with patch.object(ds, "compose_digest", AsyncMock(return_value=("t", "   "))):
        with pytest.raises(ds.DigestComposeEmpty):
            await ds.generate_village_digest(db_session, date(2026, 7, 24))

    n = (await db_session.execute(
        select(func.count()).select_from(Digest))).scalar()
    assert n == 0, "空正文落库了 —— 这一天从此再也不会自愈"


@pytest.mark.anyio
async def test_an_existing_empty_row_is_refilled_not_short_circuited(db_session):
    """存量空行（生产有 4 天）必须能被重新生成填回去，走 UPDATE 而非 INSERT。"""
    from app.services import digest_service as ds

    day = date(2026, 7, 25)
    db_session.add(Digest(scope="village", date=day, user_id="",
                          title=f"{day} 村落日报", content_md="", stats_json={}))
    await db_session.commit()
    await _material(db_session, day)

    with patch.object(ds, "compose_digest",
                      AsyncMock(return_value=(
                          "补写的头条",
                          "# 补写的头条\n有内容了，这是重新生成之后的真实正文，长度足够超过守卫阈值。",
                      ))):
        d = await ds.generate_village_digest(db_session, day)

    assert d.title == "补写的头条" and "有内容了" in d.content_md
    # 唯一约束还在 → 必须是 UPDATE，不是第二行
    n = (await db_session.execute(
        select(func.count()).select_from(Digest))).scalar()
    assert n == 1


@pytest.mark.anyio
async def test_a_nonempty_row_still_short_circuits(db_session):
    """有正文的才算完成 —— 幂等语义不能被上一条改坏。"""
    from app.services import digest_service as ds

    day = date(2026, 7, 26)
    db_session.add(Digest(scope="village", date=day, user_id="", title="原标题",
                          content_md="# 原标题\n原来的正文，这一段长度足够，不会被判定为只有标题的退化情况。",
                          stats_json={}))
    await db_session.commit()

    compose = AsyncMock(return_value=("不该被调用", "不该被调用"))
    with patch.object(ds, "compose_digest", compose):
        d = await ds.generate_village_digest(db_session, day)

    compose.assert_not_awaited()
    assert d.title == "原标题"


@pytest.mark.anyio
async def test_title_only_digest_body_is_rejected(db_session):
    """CRIT-2 的直接回归：只有标题行的正文（``"# 今日头条"``）必须像空正文
    一样被拒绝——旧守卫 ``if not content.strip()`` 会放它过去，因为这个
    字符串本身非空。
    """
    from app.services import digest_service as ds

    await _material(db_session, date(2026, 7, 27))
    with patch.object(ds, "compose_digest", AsyncMock(return_value=("今日头条", "# 今日头条"))):
        with pytest.raises(ds.DigestComposeEmpty):
            await ds.generate_village_digest(db_session, date(2026, 7, 27))

    n = (await db_session.execute(
        select(func.count()).select_from(Digest))).scalar()
    assert n == 0, "只有标题行的正文落库了 —— 这一天会被幂等永久钉死且没有 error 日志"


@pytest.mark.anyio
async def test_a_title_only_existing_row_is_also_refilled_not_short_circuited(db_session):
    """存量「只有标题行」的行（旧守卫会放过的坏数据）同样不能被早返回放过。

    早返回判据必须和落库前守卫用同一把尺子（has_real_digest_body）：如果
    早返回继续用松判据 ``.strip()``，回填脚本挑出的这类行会在这里被悄悄
    短路，永远填不回去——那样回填脚本的整个存在理由就落空了。
    """
    from app.services import digest_service as ds

    day = date(2026, 7, 17)
    db_session.add(Digest(scope="village", date=day, user_id="",
                          title=f"{day} 村落日报", content_md="# 今日头条", stats_json={}))
    await db_session.commit()
    await _material(db_session, day)

    with patch.object(ds, "compose_digest",
                      AsyncMock(return_value=(
                          "补写的头条",
                          "# 补写的头条\n这次真的有实质正文内容了，长度超过阈值。",
                      ))):
        d = await ds.generate_village_digest(db_session, day)

    assert d.title == "补写的头条" and "实质正文" in d.content_md
    n = (await db_session.execute(select(func.count()).select_from(Digest))).scalar()
    assert n == 1


@pytest.mark.anyio
async def test_pin_digest_bulletin_is_idempotent_for_the_same_content(db_session):
    """升级为必修的那条：_pin_digest_bulletin 对同一天（同一份标题+正文）
    幂等——同一天被回填两次只应留一条置顶 ``kind="digest"`` 公告。旧
    docstring 声称「idempotent per day for free」，其实没有：BulletinPost
    没有 day 列，第二次调用会盲目 unpin+insert，产生两条置顶公告。
    """
    from app.services import digest_service as ds
    from app.models.bulletin_post import BulletinPost

    day = date(2026, 7, 25)
    digest = Digest(scope="village", date=day, user_id="", title="标题A",
                    content_md="# 标题A\n正文内容一致，用来模拟同一天被回填两次。", stats_json={})
    db_session.add(digest)
    await db_session.commit()
    await db_session.refresh(digest)

    await ds._pin_digest_bulletin(db_session, digest)
    await ds._pin_digest_bulletin(db_session, digest)  # 模拟同一天被回填两次

    posts = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "digest"))).scalars().all()
    assert len(posts) == 1
    assert posts[0].pinned is True


@pytest.mark.anyio
async def test_pin_digest_bulletin_still_repins_for_a_genuinely_different_digest(db_session):
    """幂等不能误伤正常场景：内容真的变了（换了一天，或回填时重新
    composed 出了不同文本），仍要 unpin 旧的、pin 新的。
    """
    from app.services import digest_service as ds
    from app.models.bulletin_post import BulletinPost

    d1 = Digest(scope="village", date=date(2026, 7, 24), user_id="", title="标题A",
                content_md="# 标题A\n第一天的正文内容，足够长，避免被判定为标题行。", stats_json={})
    d2 = Digest(scope="village", date=date(2026, 7, 25), user_id="", title="标题B",
                content_md="# 标题B\n第二天的正文内容，同样足够长，避免被判定为标题行。", stats_json={})
    db_session.add_all([d1, d2])
    await db_session.commit()

    await ds._pin_digest_bulletin(db_session, d1)
    await ds._pin_digest_bulletin(db_session, d2)

    pinned = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "digest", BulletinPost.pinned.is_(True))
    )).scalars().all()
    assert len(pinned) == 1 and pinned[0].title == "标题B"
    total = (await db_session.execute(
        select(func.count()).select_from(BulletinPost))).scalar()
    assert total == 2


@pytest.mark.anyio
async def test_pin_digest_bulletin_recovers_when_already_two_pins_exist(db_session):
    """final review Important 回归：``script_service.settle_due_seasons`` 的
    赛季落幕公告插入一条 ``kind="digest", pinned=True`` 的帖子，却不 unpin
    任何已置顶的 digest（见 script_service.py 里那段 ``db.add(BulletinPost(
    kind="digest", ..., pinned=True, ...))``）。默认 28 天季长下，部署后第
    一次赛季落幕就会让库里同时存在 ≥2 条 ``kind="digest" AND pinned=True``
    的行——这是本测试预置的库状态。

    修复前的 ``scalar_one_or_none()`` 遇到这种状态会抛
    ``sqlalchemy.exc.MultipleResultsFound``；抛在无差别 unpin UPDATE 之前，
    所以那条本可以自愈的 UPDATE 永远跑不到，两条置顶行永久卡死，此后每天
    的日报都不再能上公告板。函数必须：不抛异常、且收敛到恰好 1 条置顶
    （新日报）。
    """
    from app.services import digest_service as ds
    from app.models.bulletin_post import BulletinPost

    # 模拟赛季落幕后的库状态：两条 kind="digest" pinned=True 的历史帖子
    # （一条是赛季落幕公告，一条是更早的一条村落日报置顶帖）。
    db_session.add_all([
        BulletinPost(kind="digest", author_user_id=None, pinned=True,
                     title="赛季一 · 赛季落幕", content_md="# 赛季一 · 赛季落幕\n落幕公告正文。"),
        BulletinPost(kind="digest", author_user_id=None, pinned=True,
                     title="旧日报", content_md="# 旧日报\n更早一天的历史正文。"),
    ])
    await db_session.commit()

    digest = Digest(scope="village", date=date(2026, 7, 28), user_id="", title="今日新日报",
                    content_md="# 今日新日报\n今天的新日报正文，长度足够超过守卫阈值。", stats_json={})
    db_session.add(digest)
    await db_session.commit()
    await db_session.refresh(digest)

    # 修复前：这一行直接抛 MultipleResultsFound（不经过任何 try/except 吞掉）。
    await ds._pin_digest_bulletin(db_session, digest)

    posts = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "digest", BulletinPost.pinned.is_(True))
    )).scalars().all()
    assert len(posts) == 1, "两条历史置顶行必须收敛到恰好一条，而不是继续卡在 2 条"
    assert posts[0].title == "今日新日报"


@pytest.mark.anyio
async def test_generate_village_digest_still_reaches_the_bulletin_after_a_season_finale(db_session):
    """端到端版本：走 generate_village_digest（生产的真实调用路径），而不是
    直接调 _pin_digest_bulletin。这里的 ``_pin_digest_bulletin`` 调用被
    generate_village_digest 自己的 ``except Exception: logger.warning(...)``
    包着（这层包裹本身没变，也不该变——bulletin 失败不该打断日报生成），所以
    这条测试即便在修复前也不会向上抛异常；它钉住的是可观测的最终状态：
    修复前置顶数会停在 2（unpin 从未跑到），修复后必须是 1。
    """
    from app.services import digest_service as ds
    from app.models.bulletin_post import BulletinPost

    db_session.add_all([
        BulletinPost(kind="digest", author_user_id=None, pinned=True,
                     title="赛季一 · 赛季落幕", content_md="# 赛季一 · 赛季落幕\n落幕公告正文。"),
        BulletinPost(kind="digest", author_user_id=None, pinned=True,
                     title="旧日报", content_md="# 旧日报\n更早一天的历史正文。"),
    ])
    await db_session.commit()

    day = date(2026, 7, 28)
    await _material(db_session, day)
    with patch.object(ds, "compose_digest",
                      AsyncMock(return_value=(
                          "今日新日报",
                          "# 今日新日报\n今天的新日报正文，长度足够超过守卫阈值。",
                      ))):
        await ds.generate_village_digest(db_session, day)

    posts = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "digest", BulletinPost.pinned.is_(True))
    )).scalars().all()
    assert len(posts) == 1, "修复前：MultipleResultsFound 被吞掉，unpin 跑不到，置顶数仍是 2"
    assert posts[0].title == "今日新日报"


@pytest.mark.anyio
async def test_cold_start_fallback_is_still_allowed_to_persist(db_session):
    """冷启动兜底文案不是「空」—— 它有正文，必须照常落库。"""
    from app.services import digest_service as ds

    d = await ds.generate_village_digest(db_session, date(2026, 7, 9))
    assert "静悄悄" in d.content_md
    n = (await db_session.execute(
        select(func.count()).select_from(Digest))).scalar()
    assert n == 1


@pytest.mark.anyio
async def test_no_module_bypasses_the_chat_wrapper_in_digest(db_session):
    """结构守卫：digest 路径不得再直调 SDK。

    匹配的是调用形式 ``.messages.create(`` 而不是裸子串 ``messages.create``：
    后者会把 docstring 里解释「为什么不能这么调」的那句话一起命中，逼着文档
    为了绕开测试而扭曲措辞。守卫要防的是调用，不是提及。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "services"
           / "digest_service.py").read_text(encoding="utf-8")
    assert ".messages.create(" not in src, (
        "digest 路径必须走 app.llm.client.chat()——它是全仓唯一会加 "
        "thinking={'type':'disabled'} 的地方")


@pytest.mark.parametrize("degenerate_title_only", [
    "# 今日头条",
    "\n# 今日头条",
    "  \n# 今日头条",
])
def test_has_real_digest_body_rejects_title_only_content_with_leading_whitespace(degenerate_title_only):
    """Minor 回归：``_digest_body`` 曾经用 ``body.lstrip().startswith("#")``
    判断（忽略前导空白），却用 ``body.splitlines()[1:]``（不忽略前导空白）
    切分——两处标准不一致。``"\\n# 今日头条"`` 这种带前导换行的退化行，
    lstrip() 后的判断认为第一行是标题行，但 splitlines()[1:] 砍掉的其实是
    原字符串里那个前导空行，标题行原封不动地留在结果里，长度变成 6，骗过
    ``has_real_digest_body`` 的守卫——可达性不高（compose_digest 的返回值
    已 .strip()），但同一个判据同时被 generate_village_digest 的早返回和
    refill_empty_digests.py 的 find_targets 使用，库里若真有这种退化行就永远
    挑不出来、填不回去。三种前导空白形态都必须和干净的 "# 今日头条" 一样被
    判为「没有实质正文」。
    """
    from app.services import digest_service as ds
    assert ds.has_real_digest_body(degenerate_title_only) is False
