"""默认测试门到底跑什么——`markers` 与 `addopts` 的一致性闸（07-27B F1）。

**这道闸存在的理由。** `pyproject.toml` 里有两处各自声明「这类测试存在」和
「默认门跑什么」：`[tool.pytest.ini_options].markers` 与同段的 `addopts`。
`lab_oci` 单独落地那次两处是同步的；后来 `lab_postgres` / `lab_redis` /
`lab_staging` / `lab_capacity` 四个 marker 陆续加进 `markers`，**没有一个同步进
`addopts` 的 `-m` 表达式**，于是 45 条需要真 PG / Redis / staging 环境的测试
一直待在默认门里。CI 没有这些环境，它们就成了常驻的红。

口径分叉这种事没人会主动去查，所以把它做成断言：**新增 marker 必须显式选边**
——要么进 `-m` 的排除项，要么写进 `DEFAULT_GATE_MARKERS` 并说明为什么它可以在
没有外部依赖的机器上跑。忘了选边就红在这里，而不是三周后红在 master 上。

纯静态检查：只读 `pyproject.toml`，不 import app、不连库。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

#: 声明了 marker 但**仍然留在默认门里**的，写在这里并给出理由。
#: 判据是「在一台只有 Python 和本仓依赖的机器上能不能跑过」——能，才可以进。
DEFAULT_GATE_MARKERS: dict[str, str] = {}


def _ini() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))[
        "tool"]["pytest"]["ini_options"]


def _declared_markers() -> set[str]:
    # markers 条目形如 "lab_oci: real-container OCI isolation evidence (...)"
    return {m.split(":", 1)[0].strip() for m in _ini().get("markers", [])}


def _excluded_markers() -> set[str]:
    """`addopts` 的 `-m` 表达式里被 `not X` 排掉的 marker 名。"""
    addopts = _ini().get("addopts", "")
    m = re.search(r"-m\s+'([^']*)'|-m\s+\"([^\"]*)\"", addopts)
    if not m:
        return set()
    expr = m.group(1) or m.group(2) or ""
    return set(re.findall(r"\bnot\s+([A-Za-z_][A-Za-z0-9_]*)", expr))


def test_every_declared_marker_picks_a_side():
    """每个 marker 要么被默认门排除，要么显式登记为「默认门可以跑」。

    这一条红，意味着有人加了 marker 却没决定默认门要不要跑它——正是 45 条
    环境依赖测试混进默认门的那个漏子。
    """
    undecided = sorted(
        _declared_markers() - _excluded_markers() - set(DEFAULT_GATE_MARKERS))
    assert not undecided, (
        "这些 marker 既不在 addopts 的排除项里，也不在 DEFAULT_GATE_MARKERS 中——"
        "请选边：需要外部环境就加进 `-m 'not ...'`，能裸机跑就登记并说明理由: "
        f"{undecided}")


def test_exclusions_reference_real_markers():
    """`-m` 里排除的名字必须是真声明过的 marker。

    排一个不存在的名字（打错字、或 marker 已改名）不会报错，只会静默地什么都
    不排——排除项就这样悄悄失效。
    """
    unknown = sorted(_excluded_markers() - _declared_markers())
    assert not unknown, (
        f"addopts 排除了未声明的 marker（打错字或已改名）: {unknown}")


def test_default_gate_allowlist_stays_justified():
    """`DEFAULT_GATE_MARKERS` 里的名字必须仍然是声明过的 marker。

    marker 删掉后白名单里的残留条目会一直放行一个不存在的名字，下次同名 marker
    出现时直接跳过选边。
    """
    stale = sorted(set(DEFAULT_GATE_MARKERS) - _declared_markers())
    assert not stale, (
        f"DEFAULT_GATE_MARKERS 里有已不存在的 marker，删掉它们: {stale}")
