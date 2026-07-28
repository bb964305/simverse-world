"""玩家提交的居民正文的统一内容守卫（07-27B E2）。

**为什么单开一个模块。** 词表 `SENSITIVE_WORDS` 与判定 `_has_sensitive` 长在
`shop_effects.py` 里——那是「改名卡的预检」的上下文，居民导入路径要用它得
`from app.services.shop_effects import _has_sensitive` 这样跨语境地借。结果是
四条写入路径里只有一条借了，而且只查了四个字段中的三个。

本模块不复制词表（那会漂移），只把「哪些字段算正文、脏了怎么办」收成一个
调用点，让「有没有接守卫」变成可静态断言的事实。

**范围。** 只做已有词表的机械匹配，**不扩词表**——扩词表、举报、申诉是内容
治理（07-27B E3）的活，不在本条范围内。这里保证的是「已经写下的规则在每条
路径上都真的生效」，不是「规则够不够」。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.services.shop_effects import _has_sensitive

#: 玩家可写、且会流向 NPC prompt / 公开名录 / Markdown 预览的正文字段。
#: 新增可写正文字段时必须同步加进来——`tests/test_resident_content_guard.py`
#: 会按这个元组逐字段验证守卫，漏加就等于漏过滤。
RESIDENT_TEXT_FIELDS = ("name", "ability_md", "persona_md", "soul_md")


def assert_resident_content_clean(**fields: str | None) -> None:
    """任一正文字段命中词表就抛 400。

    只检查 :data:`RESIDENT_TEXT_FIELDS` 里列出的字段；调用方多传的键被忽略，
    这样 handler 可以直接把整个请求体摊开传进来而不必自己筛。

    `None` 与空串一律放行——`PUT /residents/{slug}` 的部分更新会把未提交的
    字段传成 `None`，把它当成「空内容」而不是「非法内容」。

    错误信息刻意不回显命中的词：回显等于把词表当成 oracle 送给调用方，几次
    试探就能把整张表反推出来。
    """
    for name in RESIDENT_TEXT_FIELDS:
        value = fields.get(name)
        if value and _has_sensitive(value):
            raise HTTPException(
                status_code=400, detail="content contains disallowed words")
