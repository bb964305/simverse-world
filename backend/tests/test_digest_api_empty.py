"""E2E-08 读取侧: /digest/latest 与 /digest?date= 不得把空正文行返回给前端。

背景（生产实测，与本文件用例一一对应）：digests 表里现在有 5 条
scope='village' 且 content_md 长度为 0 的行（2026-07-17/24/25/26/28）——
生成侧上一批次（test_digest_empty_guard.py 覆盖）已经堵住了新的空行落库，
且存量空行可以被回填脚本 UPDATE 回真实正文；但回填之前，这些行依然原样
躺在库里。

在这个空窗期，两个端点原样把空行序列化后返回给前端：
- ``/digest/latest`` 只按 date desc 取一条，完全不看 content_md 是否为空
  → 2026-07-28 那天最新、也最空，被原封不动地返回。
- ``/digest?date=<空行的日期>`` 同样把空壳对象返回。

前端 DigestModal 用 `digest ?` 三元判断是否渲染正文区，非 null 的空壳对象
会走进渲染分支，`<ReactMarkdown>{''}</ReactMarkdown>` 渲染出一片空白——
「还没有日报」的空态文案永远不会出现。

本文件锁死读取侧的修法：两个端点复用 ``digest_service.has_real_digest_body``
判断「是否有实质正文」，没有就当成不存在处理（返回 None），而不是自己再写
一套 ``content_md == ''`` ——避免两套口径漂移。
"""
from datetime import date

import pytest
from sqlalchemy import select

from app.models.digest import Digest


def _empty_row(day: date) -> Digest:
    """只有标题行、正文长度为 0 —— 与生产实测的 5 条空行同构。"""
    return Digest(scope="village", date=day, user_id="",
                  title=f"{day} 村落日报", content_md="", stats_json={})


def _real_row(day: date, body: str = "小镇今天很热闹，大家都在广场上聊天。") -> Digest:
    return Digest(scope="village", date=day, user_id="",
                  title=f"{day} 村落日报", content_md=f"# {day} 村落日报\n{body}",
                  stats_json={})


@pytest.mark.anyio
async def test_latest_returns_null_when_the_only_row_is_empty(client, db_session):
    db_session.add(_empty_row(date(2026, 7, 28)))
    await db_session.commit()

    r = await client.get("/digest/latest")
    assert r.status_code == 200
    assert r.json()["digest"] is None


@pytest.mark.anyio
async def test_latest_skips_the_newest_empty_row_and_returns_the_previous_real_one(client, db_session):
    """直接复刻生产现状：07-28 空、07-27 有内容 —— /digest/latest 应该回退到 07-27。"""
    db_session.add(_real_row(date(2026, 7, 27)))
    db_session.add(_empty_row(date(2026, 7, 28)))
    await db_session.commit()

    r = await client.get("/digest/latest")
    assert r.status_code == 200
    body = r.json()["digest"]
    assert body is not None
    assert body["date"] == "2026-07-27"


@pytest.mark.anyio
async def test_by_date_returns_null_for_an_existing_but_empty_row(client, db_session):
    db_session.add(_empty_row(date(2026, 7, 26)))
    await db_session.commit()

    r = await client.get("/digest?date=2026-07-26")
    assert r.status_code == 200
    assert r.json()["digest"] is None


@pytest.mark.anyio
async def test_by_date_returns_the_row_when_it_has_a_real_body(client, db_session):
    db_session.add(_real_row(date(2026, 7, 25)))
    await db_session.commit()

    r = await client.get("/digest?date=2026-07-25")
    assert r.status_code == 200
    body = r.json()["digest"]
    assert body is not None
    assert body["date"] == "2026-07-25"


@pytest.mark.anyio
async def test_all_real_rows_behaves_as_before_regression_guard(client, db_session):
    """全是有正文的行时，行为应与修改前一致：latest 拿最新一条，按日期查也照常。"""
    db_session.add(_real_row(date(2026, 7, 20), "第一天的正文"))
    db_session.add(_real_row(date(2026, 7, 21), "第二天的正文"))
    await db_session.commit()

    r = await client.get("/digest/latest")
    assert r.json()["digest"]["date"] == "2026-07-21"

    r2 = await client.get("/digest?date=2026-07-20")
    assert r2.json()["digest"] is not None
    assert r2.json()["digest"]["date"] == "2026-07-20"

    # sanity: db_session 里确实还留着两行（没有被这轮修改误删）
    rows = (await db_session.execute(select(Digest))).scalars().all()
    assert len(rows) == 2
