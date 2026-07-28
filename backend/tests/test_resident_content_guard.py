"""居民正文的敏感词过滤必须覆盖**每一条写入路径的每一个字段**（07-27B E2）。

**现状与真正的洞。** `_has_sensitive` 在居民侧只有一处调用
（`routers/residents.py:158`，import-card 路径），而且只查 `name` / `persona_md` /
`soul_md`——**漏了 `ability_md`**。外部审查报告只点到了这个漏字段，但穷举写入
路径后发现问题大得多：multipart `/residents/import`、`PUT /residents/{slug}`
与 `POST /onboarding/create-character` 三条路径**整条零过滤**。

**为什么这不是洁癖。** 这些正文会进三个地方：NPC 的 prompt、公开名录、以及
`ResidentEditor` 的 Markdown live preview（后者叠加 `rehype-raw` 且全站无 CSP，
是 07-27B E1 那条 XSS 链的载荷入口）。更要紧的是**排序**：F2 的公民权晋升一旦
开闸，玩家创作居民会成为一等公民，它们的正文将进入议政文本与选举材料。所以本
条必须先于 `CIVIC_PROMOTION_MODE=on`。

词表本身（`shop_effects.SENSITIVE_WORDS`）是既有的、很短的，本条不扩充它——
扩词表是内容治理的活（E3），这里只保证**已有的词在每条路径上都真的生效**。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.content_guard import (
    RESIDENT_TEXT_FIELDS,
    assert_resident_content_clean,
)
from app.services.shop_effects import SENSITIVE_WORDS

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
DIRTY = sorted(SENSITIVE_WORDS)[0]


# ── ① 守卫本身 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("field", RESIDENT_TEXT_FIELDS)
def test_guard_rejects_every_text_field(field):
    """四个字段任意一个脏，都必须被拒——含此前漏掉的 `ability_md`。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        assert_resident_content_clean(**{field: f"前缀{DIRTY}后缀"})
    assert ei.value.status_code == 400


def test_guard_accepts_clean_content():
    assert_resident_content_clean(
        name="阿岚", ability_md="会修钟表", persona_md="温和", soul_md="想家")


def test_guard_tolerates_none():
    """PUT 只提交部分字段，未提交的是 None，不能因此炸。"""
    assert_resident_content_clean(name="阿岚", ability_md=None,
                                  persona_md=None, soul_md=None)


# ── ② 每条写入路径都真的调用了它 ──────────────────────────────────────

#: 会写入居民正文的 handler → 所在文件。静态断言比行为测试更能防复发：
#: 新增一条写入路径而忘了接守卫，这里会红，而行为测试只覆盖已知路径。
WRITE_HANDLERS = {
    "residents.py": ("resident_import", "import_resident", "edit_resident"),
    "onboarding.py": ("create_character",),
}


@pytest.mark.parametrize("filename,handlers", sorted(WRITE_HANDLERS.items()))
def test_every_write_path_calls_the_guard(filename, handlers):
    """静态解析：每个写入 handler 的函数体里必须出现守卫调用。"""
    tree = ast.parse((ROUTERS / filename).read_text(encoding="utf-8"))
    missing = []
    for name in handlers:
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == name), None)
        assert fn is not None, f"{filename} 里找不到 handler {name}——请更新本清单"
        called = any(
            isinstance(c, ast.Call)
            and getattr(c.func, "id", getattr(c.func, "attr", None))
            == "assert_resident_content_clean"
            for c in ast.walk(fn))
        if not called:
            missing.append(name)
    assert not missing, (
        f"{filename} 的这些写入 handler 没有调用 assert_resident_content_clean，"
        f"玩家可以从它们绕过词表: {missing}")
