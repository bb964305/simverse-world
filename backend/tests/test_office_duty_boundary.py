"""S10 —— ``offices`` 与 ``meta_json['duty']`` 的边界、读法方向与双向漂移网。

**两个概念,不是同一概念的两套存储**:

- ``offices``(``OFFICE_DEFS`` 4 键)带任期 / 机构 / 权限,运行时只有 ``mayor``
  被写(选举 → ``install_mayor`` → ``OfficeService.appoint``);
- ``meta_json['duty']``(seed 11 键)带 ``prompt_hint`` / ``perks``,是「营生」,
  不是「官职」——它决定 WORK 产出、prompt 口吻与各类系数。

重叠只有 ``{town_clerk, postman}``,来自迁移 046 的一次性快照拷贝,之后**零
同步**。所以本文件不做「定权威源 + 回填」(那是数据变更,与开闸同车触红线),
只做三件事:钉死边界、钉死两个方向各自的入口、张一张双向漂移网。

**方向**(rev1 把这条写反了,R2 更正):

- 「按 key 反查持有人」= ``duty_service.find_duty_resident(db, key)``;
- 「按人读营生」= ``duty_service.get_duty(resident)`` / ``duty_key(resident)``。

两个方向的唯一入口都在 ``duty_service``,所以守卫只有一条判据:业务代码不许
手写 ``meta_json['duty']['key']`` 这条原始链(不论是拿去 ``== "lecturer"``
反查,还是拿去读自己的营生)。守卫仿
``tests/test_civic_standing_write_entrypoints.py`` 的 AST 扫描 + guard-of-the-
guard 姿势,**不设任何文件路径豁免**——``duty_service`` 自己能通过是因为它的
``get_duty`` 只读到 ``['duty']`` 为止,``duty_key`` 读的是 ``get_duty()`` 的
返回值,两处结构上都落不进判据里(见
``test_duty_service_itself_needs_no_path_exemption``)。
"""
import ast
import inspect
import pathlib
import warnings

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.services import duty_service
from app.services.office_service import OFFICE_DEFS, OfficeService

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _res(slug, name, duty=None, **kw) -> Resident:
    d = dict(slug=slug, name=name, district="town_hall", status="idle",
             resident_type="npc", creator_id="sys", tile_x=70, tile_y=56,
             meta_json={"duty": duty} if duty else None)
    d.update(kw)
    return Resident(**d)


def _seed_duty_keys() -> set[str]:
    """seed 侧的 11 个营生键。

    这里直接读 ``PRESET_CHARACTERS`` 的字典,不走 ``duty_service.get_duty``:
    那个访问器读的是 ``resident.meta_json`` 属性,而 seed 条目是纯 dict——它是
    营生的**定义处**,不是 Resident 的读点。
    """
    from seed.preset_characters import PRESET_CHARACTERS

    return {
        duty["key"]
        for c in PRESET_CHARACTERS
        if (duty := (c.get("meta_json") or {}).get("duty"))
    }


# ── 边界:两个概念,重叠恰为 2 键 ─────────────────────────────────────────

def test_offices_and_duty_are_two_concepts_overlapping_on_two_keys():
    office_keys = set(OFFICE_DEFS)
    duty_keys = _seed_duty_keys()
    assert office_keys == {"mayor", "town_clerk", "postman", "doctor"}
    assert len(duty_keys) == 11
    assert office_keys & duty_keys == {"town_clerk", "postman"}
    # 两侧各自的专属项:offices 有 mayor/doctor 这种「官职」,duty 有 cafe_host
    # 这种「营生」。任一侧被并进另一侧,这条就红。
    assert office_keys - duty_keys == {"mayor", "doctor"}
    assert "cafe_host" in duty_keys - office_keys


def test_mayor_is_never_a_duty():
    """镇长是选出来的官职,不是谁的营生。``meta_json['duty']['key']`` 里出现
    ``mayor`` = 有人把官职塞进了营生命名空间,``duty_service.on_work`` 会开始
    给镇长发营生工资,``find_duty_resident('mayor')`` 也会绕过
    ``election_service.current_mayor`` 这个唯一权威源(全局设计决策 4)。"""
    assert "mayor" not in _seed_duty_keys()
    assert "mayor" not in duty_service._WORK_HANDLERS


OVERLAP_KEYS = frozenset(OFFICE_DEFS) & _seed_duty_keys()


# ── 方向:两个入口,签名互为反向 ──────────────────────────────────────────

def test_the_two_lookup_directions_are_distinct_entrypoints():
    """rev1 把 ``find_duty_resident`` 说成「duty 的唯一合读入口」——方向写反
    了。它吃 key 吐人(要 db,是协程),而按人读营生吃 resident 吐 dict(纯函
    数,不查库)。签名本身就是方向,钉住签名就钉住了方向。"""
    reverse = duty_service.find_duty_resident
    assert inspect.iscoroutinefunction(reverse)
    assert list(inspect.signature(reverse).parameters) == ["db", "key"]

    for fn in (duty_service.get_duty, duty_service.duty_key):
        assert not inspect.iscoroutinefunction(fn), f"{fn.__name__} 不该查库"
        assert list(inspect.signature(fn).parameters) == ["resident"]


# ── 守卫:业务代码不许手写 meta_json['duty']['key'] 原始链 ────────────────

def _member_read(node: ast.expr, name: str) -> ast.expr | None:
    """把 ``x[name]`` 与 ``x.get(name, ...)`` 归一成同一件事,返回被读的 ``x``。

    两种句法在语义上都是「从这个映射里取 name 这一项」,守卫必须同时认识——
    只认下标就会被 ``.get()`` 绕过,反之亦然。
    """
    if isinstance(node, ast.Subscript):
        idx = node.slice
        if isinstance(idx, ast.Constant) and idx.value == name:
            return node.value
        return None
    if (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == name):
        return node.func.value
    return None


def _strip_or_default(node: ast.expr) -> ast.expr:
    """剥掉 ``(x or {})`` 这层兜底,露出真正被读的 ``x``。

    ``((r.meta_json or {}).get("duty") or {}).get("key")`` 与
    ``(r.meta_json or {}).get("duty", {}).get("key")`` 是同一件事的两种写法,
    不剥这一层,前者就能从守卫底下溜过去。
    """
    while (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
           and node.values):
        node = node.values[0]
    return node


def _duty_key_chain_offenders(tree: ast.AST, label: str) -> list[str]:
    """判据:同一条表达式链上先读 ``duty`` 再读 ``key``。

    这一条同时封住两个方向的绕过——``== "lecturer"`` 的手写反查,和读自己营生
    时绕开 ``duty_key()``。判据只看链的形状,不看根是不是 ``meta_json``:换个
    局部变量名(``meta["duty"]["key"]``)是同一种绕过。

    豁免全部落在结构上,没有一条靠文件路径:

    - ``duty_service.get_duty(r).get("key")``——``.get("key")`` 的底座是
      ``get_duty(...)`` 调用(``func.attr == "get_duty"``),不是 ``.get("duty")``;
    - ``get_duty`` 自己只读到 ``['duty']`` 为止,链上没有 ``key``;
    - seed 的 ``(c.get("meta_json") or {}).get("duty")`` 与
      ``meta.get("duty") == duty`` 整块比较,同样读不到 ``key``。
    """
    offenders = []
    for node in ast.walk(tree):
        base = _member_read(node, "key")
        if base is None:
            continue
        if _member_read(_strip_or_default(base), "duty") is not None:
            offenders.append(f"{label}:{node.lineno}")
    return offenders


def _offenders_in_source(source: str, label: str = "<source>") -> list[str]:
    """给 guard-of-the-guard 用:喂源码文本,不碰真实文件。"""
    return _duty_key_chain_offenders(ast.parse(source, filename=label), label)


def test_no_handwritten_duty_key_chain_in_app_or_seed():
    """全仓真实扫描。命中即意味着有人在 ``duty_service`` 之外重写了营生读法
    ——两个方向的入口就此各自多出一条影子实现,``offices`` 优先级
    (``duty_service.py:88-99``)、``"duty": None`` 的兜底、未来任何读法变更都
    不会跟着走。"""
    offenders = []
    for sub in ("app", "seed"):
        for path in (BACKEND_ROOT / sub).rglob("*.py"):
            rel = str(path.relative_to(BACKEND_ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            offenders.extend(_duty_key_chain_offenders(tree, rel))
    assert offenders == [], (
        "按人读营生走 duty_service.get_duty/duty_key,按 key 反查持有人走 "
        f"duty_service.find_duty_resident,别手写 meta_json 链:{offenders}")


def test_duty_service_itself_needs_no_path_exemption():
    """守卫的最高风险点:营生读法的定义处会不会被自己的守卫误伤?不会,而且不
    需要任何文件级豁免——两条纯结构事实:

    1. ``get_duty`` 的链读到 ``.get("duty")`` 就结束,后面没有 ``key``;
    2. ``duty_key`` 的 ``.get("key")`` 底座是 ``get_duty(resident)`` 调用,不是
       一次 ``duty`` 成员读。

    两条都是 AST 节点形状判定,不是 ``if path == "duty_service.py"``。
    """
    path = BACKEND_ROOT / "app" / "services" / "duty_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _duty_key_chain_offenders(tree, "duty_service.py") == []


_EVASIVE_CHAIN_SHAPES = {
    "subscript-chain": 'r.meta_json["duty"]["key"]\n',
    "get-chain-with-default": '(r.meta_json or {}).get("duty", {}).get("key")\n',
    "get-chain-or-default": '((r.meta_json or {}).get("duty") or {}).get("key")\n',
    "mixed-get-then-subscript": 'r.meta_json.get("duty", {})["key"]\n',
    "mixed-subscript-then-get": 'r.meta_json["duty"].get("key")\n',
    # 局部变量根:换个名字不换本质。
    "local-variable-root": 'meta["duty"]["key"]\n',
    # 手写反查:plan 点名的那一种(civic_service.py:864 的原形状)。
    "reverse-lookup-literal-compare": (
        'if (r.meta_json or {}).get("duty", {}).get("key") == "lecturer":\n'
        '    pass\n'),
}


@pytest.mark.parametrize("source", _EVASIVE_CHAIN_SHAPES.values(),
                         ids=_EVASIVE_CHAIN_SHAPES.keys())
def test_guard_catches_every_evasive_chain_shape(source):
    assert _offenders_in_source(source), f"守卫漏了一种绕过形态: {source!r}"


_EXEMPT_CHAIN_SHAPES = {
    # 两个方向的官方入口。
    "sanctioned-by-person-get-duty": 'duty_service.get_duty(r).get("key")\n',
    "sanctioned-by-person-duty-key": 'duty_service.duty_key(r)\n',
    "sanctioned-reverse-lookup": 'find_duty_resident(db, "town_clerk")\n',
    # get_duty 的返回值再读别的键——town_facts_service._read_duties 的真实形状。
    "get-duty-result-title": 'duty_service.get_duty(r).get("title")\n',
    # duty_service.get_duty 自身:链读到 ['duty'] 为止。
    "get-duty-implementation": (
        'return ((getattr(resident, "meta_json", None) or {}).get("duty")) or {}\n'),
    # seed 的两处真实先例:整块 duty 字典,读不到 key。
    "seed-duty-block-read": '(c.get("meta_json") or {}).get("duty")\n',
    "seed-duty-block-compare": 'meta.get("duty") == duty\n',
    # 同一形状但读的是别的命名空间——civic_service 的 SBTI 链。
    "sbti-chain": '(r.meta_json or {}).get("sbti", {}).get("dimensions", {})\n',
    # 与营生无关的 "key" 读点。
    "unrelated-key-read": 'payload["key"]\n',
    "unrelated-key-get": 'o.get("key")\n',
}


@pytest.mark.parametrize("source", _EXEMPT_CHAIN_SHAPES.values(),
                         ids=_EXEMPT_CHAIN_SHAPES.keys())
def test_guard_does_not_flag_structurally_exempt_shapes(source):
    assert _offenders_in_source(source) == []


# ── 双向漂移网(重叠 2 键)─────────────────────────────────────────────────

class OfficeDutyDriftWarning(UserWarning):
    """``offices`` 空缺但营生有人在做——今天生产恰是这一态,不判红。"""


async def _duty_holders(db) -> dict[str, list[str]]:
    """按人读一遍全镇营生,聚成 ``key → [slug]``。

    走 ``duty_service.duty_key``(按人读方向的官方入口)而不是
    ``find_duty_resident``:后者在 ``polis_office_enabled`` 时**先查 offices**
    (``duty_service.py:88-99``),拿它做漂移网等于用 offices 校验 offices,
    两侧一致是必然的,网就空转了。
    """
    residents = (await db.execute(
        select(Resident).where(Resident.is_autonomous).order_by(Resident.slug)
    )).scalars().all()
    out: dict[str, list[str]] = {}
    for r in residents:
        key = duty_service.duty_key(r)
        if key:
            out.setdefault(key, []).append(r.slug)
    return out


async def check_overlap_drift(db) -> list[str]:
    """重叠 2 键上的双向漂移网。返回**硬冲突**列表(非空即红)。

    - ``offices.holder_slug`` 非空且不在该营生的持有人里 → 硬冲突:两张表对
      「谁是文书 / 谁是邮差」给出互相矛盾的答案,而
      ``find_duty_resident`` 会按 offices 那份回答(S2-1 索引优化),事实层与
      公告署名会一起跟着错人。
    - ``offices`` 空而营生有人 → 只 warning:这不是矛盾,是 S2-1 的索引优化对
      这两键**永久失效**(回落 O(N) 扫描),今天生产就是这一态。判红等于逼出
      一次回填,而回填是数据变更,与开闸同车触红线。
    """
    conflicts: list[str] = []
    holders = await _duty_holders(db)
    svc = OfficeService(db)
    for key in sorted(OVERLAP_KEYS):
        office_holder = await svc.get_holder(key)
        duty_slugs = holders.get(key, [])
        if office_holder is None:
            if duty_slugs:
                warnings.warn(
                    f"offices['{key}'] 空缺,但营生持有人是 {duty_slugs}"
                    "——S2-1 的 offices 索引优化对这一键失效,回落全表扫描",
                    OfficeDutyDriftWarning, stacklevel=2,
                )
            continue
        if office_holder not in duty_slugs:
            conflicts.append(
                f"{key}: offices={office_holder!r} duty={duty_slugs!r}")
    return conflicts


@pytest.mark.anyio
async def test_todays_production_shape_warns_but_is_not_a_conflict(db_session):
    """今天的生产态:``offices.town_clerk/postman`` 恒为 NULL,营生那边赵启文
    与骆小舟一直在做。两键各出一条 warning,零硬冲突。"""
    db_session.add_all([
        _res("zhao-qiwen", "赵启文", {"key": "town_clerk", "title": "公告与登记处"}),
        _res("luo-xiaozhou", "骆小舟", {"key": "postman", "title": "邮差"}),
    ])
    await db_session.commit()

    with pytest.warns(OfficeDutyDriftWarning) as caught:
        conflicts = await check_overlap_drift(db_session)
    assert conflicts == []
    assert {w.message.args[0].split("'")[1] for w in caught} == set(OVERLAP_KEYS)


@pytest.mark.anyio
async def test_offices_agreeing_with_duty_is_clean(db_session):
    """回填之后应有的样子:两侧指向同一个人 → 零冲突、零 warning。"""
    db_session.add_all([
        _res("zhao-qiwen", "赵启文", {"key": "town_clerk", "title": "公告与登记处"}),
        _res("luo-xiaozhou", "骆小舟", {"key": "postman", "title": "邮差"}),
    ])
    await db_session.commit()
    svc = OfficeService(db_session)
    await svc.appoint("town_clerk", "zhao-qiwen", fill_strategy="seed")
    await svc.appoint("postman", "luo-xiaozhou", fill_strategy="seed")

    with warnings.catch_warnings():
        warnings.simplefilter("error", OfficeDutyDriftWarning)
        assert await check_overlap_drift(db_session) == []


@pytest.mark.anyio
async def test_offices_disagreeing_with_duty_is_a_hard_conflict(db_session):
    """真漂移:offices 说文书是甲,营生说是赵启文。``find_duty_resident`` 会按
    offices 那份回答,公告署名从此挂到甲头上。"""
    db_session.add_all([
        _res("zhao-qiwen", "赵启文", {"key": "town_clerk", "title": "公告与登记处"}),
        _res("jia", "甲"),
    ])
    await db_session.commit()
    await OfficeService(db_session).appoint(
        "town_clerk", "jia", fill_strategy="seed")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", OfficeDutyDriftWarning)
        conflicts = await check_overlap_drift(db_session)
    assert len(conflicts) == 1
    assert conflicts[0].startswith("town_clerk:")
    assert "'jia'" in conflicts[0] and "zhao-qiwen" in conflicts[0]


@pytest.mark.anyio
async def test_drift_net_ignores_the_non_overlapping_keys(db_session):
    """网只张在重叠 2 键上。``mayor`` 有 offices 行却永远没有营生持有人——那是
    设计(镇长不是营生),不是漂移;``doctor`` 同理。把它们并进网里,这张网从
    第一天起就恒红,等于没有网。"""
    db_session.add(_res("he-qiaoyun", "何巧云",
                        {"key": "shop_keeper", "title": "杂货补给线"}))
    await db_session.commit()
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "he-qiaoyun", fill_strategy="election")
    await svc.appoint("doctor", "wu-yisheng", fill_strategy="appointment")

    with warnings.catch_warnings():
        warnings.simplefilter("error", OfficeDutyDriftWarning)
        assert await check_overlap_drift(db_session) == []
