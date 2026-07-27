"""F2 Task 9 —— 声誉是社会属性，不是政治权利。

reputation_service.recompute 是 civic_membership 收口时漏掉的第 11 处 type
读点（裸的 resident_type == "npc"）。不改的后果：被降级者退出夜间声誉重算、
分数永久冻结在降级前那一刻，而 election_service.py:53-60 的候选排序读的正是
这个冻结值；将来「违规扣声誉」若先改档位再扣分，扣分会因这行字面量永不生效。

全仓 resident_type 字面量分类（F2 开工核查）：
  半状态源  reputation_service.py:74           → 本任务改成 is_autonomous
  第三族    home_decor.py:56 / map_data.py:475 → != "player"，刻意不动
  展示层    admin/residents.py:38（标签）/ :299（preset 删除守卫）
  回退值    resident_sprite_publish_service.py:217（精灵模板缺省）
  创建路径  forge ×3 / routers/residents ×2 / onboarding ×1（关键字实参）
"""
import ast
import pathlib

import pytest

from app.config import settings
from app.models.resident import Resident
from app.services import civic_membership as cm
from app.services.reputation_service import recompute, score_from_meta

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _res(slug, rtype):
    return Resident(slug=slug, name=slug, district="central_plaza",
                    status="idle", resident_type=rtype, creator_id="sys",
                    tile_x=70, tile_y=56,
                    mood_json={"valence": 0.4, "arousal": 0.2, "label": "calm"},
                    meta_json={"sbti": {"dimensions": {"Ac1": "H"}}})


@pytest.mark.anyio
async def test_recompute_covers_the_world_population_not_the_electorate(
        db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", True)
    db_session.add_all([_res("builtin", cm.CIVIC_MEMBER_TYPE),
                        _res("demoted", cm.UGC_RESIDENT_TYPE)])
    await db_session.commit()

    assert await recompute(db_session) == 2, (
        "被降级者必须留在夜间声誉重算里，否则分数永久冻结在降级前那一刻")


@pytest.mark.anyio
async def test_recompute_skips_player_avatars(db_session, monkeypatch):
    """人口口径 = is_autonomous：玩家化身是注册成员但不是自治居民。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    db_session.add_all([_res("builtin", cm.CIVIC_MEMBER_TYPE),
                        _res("avatar", cm.PLAYER_RESIDENT_TYPE)])
    await db_session.commit()
    assert await recompute(db_session) == 1


@pytest.mark.anyio
async def test_demoted_resident_score_keeps_moving(db_session, monkeypatch):
    """回归意义上的断言：降级后再跑一次重算，分数确实被更新了。"""
    monkeypatch.setattr(settings, "rep_enabled", True)
    r = _res("demoted", cm.UGC_RESIDENT_TYPE)
    db_session.add(r)
    await db_session.commit()

    await recompute(db_session)
    await db_session.refresh(r)
    block = (r.meta_json or {}).get("reputation")
    assert block is not None, "被降级者必须拿到新的 reputation 投影"
    assert "score" in block and "updated_at" in block and "samples" in block
    # mood_valence=0.4 × rep_mood_weight，EMA 从 0 起步 → 分数必然为正
    assert score_from_meta(r.meta_json) > 0.0


# ── 守卫探测器 ───────────────────────────────────────────────────────────
#
# 抽出成独立函数，让「全仓真实扫描」与「喂源码文本的 hermetic 单元测试」共用
# 同一份判定逻辑，两边不会各写一份而漂移。
#
# ⚠️ 评审 Important finding（本轮修复对象）：旧实现只认 `node.left`，漏检
# 「字面量在左」的 Yoda 写法（`"npc" == resident.resident_type`）与经
# `getattr(obj, "resident_type", ...)` 间接读取的同类比较——两者的
# `node.left` 分别是 `Constant("npc")` 与 `ast.Call`，都不满足旧判定只认
# `ast.Attribute`/`ast.Name` 的条件，会静默通过守卫。


def _is_resident_type_read(node: ast.expr) -> bool:
    """`node` 是否读取了 `resident_type`（直接：属性 / 裸名）。

    TODO(评审 Important finding)：还不认 `getattr(obj, "resident_type", ...)`
    间接读取——这正是本轮 guard-of-the-guard 要钉住的第二个缺口。
    """
    if isinstance(node, ast.Attribute) and node.attr == "resident_type":
        return True
    if isinstance(node, ast.Name) and node.id == "resident_type":
        return True
    return False


def _is_npc_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == "npc"


def _npc_literal_offenders(tree: ast.AST, label: str) -> list[str]:
    """结构性扫描：目前只看 `node.left`。

    TODO(评审 Important finding)：Yoda 写法（`"npc" == resident.resident_type`）
    的 `resident_type` 读取落在 `node.comparators` 里，`node.left` 是常量
    `"npc"`——只查 `left` 会静默放过它。
    """
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not _is_resident_type_read(left):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            if _is_npc_constant(comparator):
                offenders.append(f"{label}:{node.lineno}")
    return offenders


def _offenders_in_source(source: str, label: str = "<source>") -> list[str]:
    """给 hermetic 单元测试用：喂源码文本，不碰真实文件。"""
    return _npc_literal_offenders(ast.parse(source, filename=label), label)


_GUARD_FAILURE_MESSAGE = (
    "裸的 resident_type 与 \"npc\" 比较（含字面量在左的 Yoda 写法、"
    "getattr(obj, \"resident_type\", ...) 间接读取）= 半状态源，改走 "
    "is_autonomous / is_civic_voter：{}"
)


def test_no_bare_npc_literal_comparison_survives_in_app():
    """结构性守卫：任何 `resident_type` 与 `"npc"` 的 `==`/`!=` 比较都是半状态
    源——不论字面量写在哪一侧，也不论是直接属性读还是 `getattr()` 间接读取。

    成员判定必须走 Resident.is_autonomous（人口）或 Resident.is_civic_voter
    （政治），字面量只许出现在 civic_membership 的常量定义里。
    """
    offenders = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        label = str(path.relative_to(BACKEND_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=label)
        offenders.extend(_npc_literal_offenders(tree, label))
    assert offenders == [], _GUARD_FAILURE_MESSAGE.format(offenders)


# ── Guard-of-the-guard（评审 Important finding）─────────────────────────
#
# 喂源码文本给探测器，不碰真实文件——hermetic，跑得快，且直接钉住"这个探测器
# 认不认识这种句法形状"，与"全仓当下有没有这种代码"（上面那条真实扫描）是两
# 件事。

_EVASIVE_SHAPES = {
    "attr-eq": 'if resident.resident_type == "npc":\n    pass\n',
    "attr-ne": 'if resident.resident_type != "npc":\n    pass\n',
    "bare-name-eq": 'if resident_type == "npc":\n    pass\n',
    "yoda-attr-eq": 'if "npc" == resident.resident_type:\n    pass\n',
    "yoda-attr-ne": 'if "npc" != resident.resident_type:\n    pass\n',
    "getattr-eq": 'if getattr(resident, "resident_type", None) == "npc":\n    pass\n',
    "yoda-getattr-eq": 'if "npc" == getattr(resident, "resident_type", None):\n    pass\n',
    "getattr-no-default-ne": 'if getattr(resident, "resident_type") != "npc":\n    pass\n',
}


@pytest.mark.parametrize("source", _EVASIVE_SHAPES.values(),
                        ids=_EVASIVE_SHAPES.keys())
def test_guard_catches_every_evasive_shape(source):
    """评审指出的两种绕过写法（Yoda / getattr 间接读取）各自单独钉住，附直接
    形态（属性 / 裸名，两侧任意）做完整覆盖对照。"""
    offenders = _offenders_in_source(source)
    assert offenders, f"guard failed to flag an evasive shape: {source!r}"


_EXEMPT_SHAPES = {
    "membership-constant-assign": (
        'CIVIC_VOTER_TYPES = frozenset({"npc"})\n'),
    "creation-keyword-arg": (
        'Resident(resident_type="npc")\n'),
    "membership-in-operator": (
        'x = resident_type in ("preset", "npc", UGC_RESIDENT_TYPE)\n'),
    "compare-against-name-not-literal": (
        'x = Resident.resident_type == UGC_RESIDENT_TYPE\n'),
}


@pytest.mark.parametrize("source", _EXEMPT_SHAPES.values(),
                        ids=_EXEMPT_SHAPES.keys())
def test_guard_does_not_flag_mechanism_exempt_shapes(source):
    """加宽比较符两侧的判定之后，重新确认这四类"按机制豁免"的写法（
    civic_membership 自身的常量定义 / 创建路径的关键字实参 / `in (...)` 成员
    测试 / 比较对象是符号引用而非字面量）依旧不被误报——加宽比较项一侧，正是
    最容易意外开始命中这些豁免写法的改法。"""
    assert _offenders_in_source(source) == []
