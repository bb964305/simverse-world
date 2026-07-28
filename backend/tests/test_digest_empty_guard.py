"""#6-1/#6-2 村落日报正文为空 + 空行被幂等永久钉死。

compose_digest 绕过 app.llm.client.chat() 直调 messages.create，于是
thinking 没被关掉（chat() 是全仓唯一会加 thinking={"type":"disabled"} 的
地方），800 的 max_tokens 被推理吃光，响应里没有可用 text block → 返回空串。
generate_village_digest 拿到空串后无条件落库，而 (scope,date,user_id) 唯一
约束 + 「行存在就早返回」的幂等让这一天永远是空的。

生产实证：2026-07-17/24/25/26 四天 content_md 长度为 0，且四天全部落在
output_tokens=801（max_tokens 触顶）的样本里。
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
                      AsyncMock(return_value=("补写的头条", "# 补写的头条\n有内容了"))):
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
                          content_md="# 原标题\n原来的正文", stats_json={}))
    await db_session.commit()

    compose = AsyncMock(return_value=("不该被调用", "不该被调用"))
    with patch.object(ds, "compose_digest", compose):
        d = await ds.generate_village_digest(db_session, day)

    compose.assert_not_awaited()
    assert d.title == "原标题"


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
