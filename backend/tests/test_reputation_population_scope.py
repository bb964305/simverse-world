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


def test_no_bare_npc_literal_comparison_survives_in_app():
    """结构性守卫：任何 `resident_type == "npc"` / `!= "npc"` 都是半状态源。

    成员判定必须走 Resident.is_autonomous（人口）或 Resident.is_civic_voter
    （政治），字面量只许出现在 civic_membership 的常量定义里。
    """
    offenders = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            is_type_read = (
                (isinstance(left, ast.Attribute) and left.attr == "resident_type")
                or (isinstance(left, ast.Name) and left.id == "resident_type")
            )
            if not is_type_read:
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.Eq, ast.NotEq)):
                    continue
                if (isinstance(comparator, ast.Constant)
                        and comparator.value == "npc"):
                    offenders.append(
                        f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
    assert offenders == [], (
        "裸的 resident_type 与 \"npc\" 比较 = 半状态源，改走 "
        f"is_autonomous / is_civic_voter：{offenders}")
