"""P1-S7: 餐费分账的 duty key 从硬编码改读 dining 能力的 host_duty 参数。

只钉「解析出了哪个 duty key」这一件事 —— 转账/赊账/钱包缓存的完整语义由
tests/test_meal_revenue.py(真 sqlite + 真 coin_service)守着,这里不重复。所以本文件
把 coin_service / duty_service / feed_service 全部打桩,断言 find_duty_resident 收到
的 key。

最后两条是同一场景的两面:第三个 dining 地点在旧代码里被静默判成 tavern_hub
(execute/basic.py:56 的 else 分支),新代码在没写 host_duty 时把餐费记入镇库。这是
修复不是回归 —— 今天第三个 dining 地点不存在,所以旧行为不可达。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.location_caps import CAP_DINING
from app.agent.map_data import LOCATIONS
from app.agent.phases.execute import basic as execute_basic
from app.config import settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def temp_location():
    added: list[str] = []

    def _add(slug: str, extra: dict) -> str:
        assert slug not in LOCATIONS, slug
        data = {"name": "临时食堂", "type": "public",
                "bounds": (2, 2, 6, 6), "center": (4, 4), "entrance": (4, 4)}
        data.update(extra)
        LOCATIONS[slug] = data
        added.append(slug)
        return slug

    yield _add
    for slug in added:
        LOCATIONS.pop(slug, None)


@pytest.fixture
def duty_keys(monkeypatch):
    """记录 find_duty_resident 收到的 key;并把整条钱链打桩。"""
    from app.services import coin_service, duty_service, feed_service

    seen: list[str] = []

    async def _find(db, key):
        seen.append(key)
        host = MagicMock()
        host.slug = f"{key}-holder"
        host.id = f"{key}-id"
        host.name = key
        return host

    monkeypatch.setattr(duty_service, "find_duty_resident", _find)
    monkeypatch.setattr(duty_service, "set_wallet_cache", lambda db, r, b: None)
    monkeypatch.setattr(coin_service, "treasury_transfer", AsyncMock(return_value=True))
    monkeypatch.setattr(coin_service, "treasury_debit", AsyncMock(return_value=True))
    monkeypatch.setattr(coin_service, "treasury_balance", AsyncMock(return_value=100))
    monkeypatch.setattr(feed_service, "push", AsyncMock())
    monkeypatch.setattr(settings, "npc_trade_enabled", True)
    return seen


def _diner(tile):
    r = MagicMock()
    r.id = "diner-id"
    r.slug = "diner"
    r.name = "食客"
    r.tile_x, r.tile_y = tile
    return r


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("slug,expected", [("cafe", "cafe_host"),
                                           ("tavern", "tavern_hub")])
async def test_the_two_authored_diners_resolve_the_same_key_either_way(
        flag, slug, expected, duty_keys, monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", flag)
    await execute_basic._charge_meal(
        AsyncMock(), _diner(LOCATIONS[slug]["center"]))
    assert duty_keys == [expected]


@pytest.mark.parametrize("flag", [False, True])
async def test_no_duty_lookup_outside_a_dining_location(
        flag, duty_keys, monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", flag)
    await execute_basic._charge_meal(
        AsyncMock(), _diner(LOCATIONS["central_plaza"]["center"]))
    assert duty_keys == []


async def test_legacy_misroutes_a_third_diner_to_tavern_hub(
        temp_location, duty_keys, monkeypatch):
    """闸关 = 旧行为原样保留(含这个已知的错付缺陷)。"""
    temp_location("t_canteen", {"category": "dining",
                               "capabilities": {CAP_DINING: {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    await execute_basic._charge_meal(AsyncMock(), _diner((4, 4)))
    assert duty_keys == ["tavern_hub"]


async def test_third_dining_location_pays_the_town_instead_of_the_void(
        temp_location, duty_keys, monkeypatch):
    """闸开 + 没写 host_duty:不查 duty、不错付,且餐费进镇库而**不是被销毁**。

    treasury_debit 是纯销毁(无对手方),treasury_transfer 才守恒 —— 把「错付」修成
    「蒸发」在守恒维度上是净退步,生产工资已改镇库支出,这是闭环货币里的单向漏斗。
    """
    from app.services import coin_service, treasury_service
    taxed: list[tuple[int, str]] = []

    async def _tax(db, amount, reason="", **kw):
        taxed.append((amount, reason))

    monkeypatch.setattr(coin_service, "treasury_debit_pending",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(treasury_service, "tax_pending", _tax)
    temp_location("t_canteen", {"category": "dining",
                               "capabilities": {CAP_DINING: {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    await execute_basic._charge_meal(AsyncMock(), _diner((4, 4)))
    assert duty_keys == []                          # 不错付给 tavern_hub
    assert coin_service.treasury_debit.await_count == 0   # 零净销毁
    assert [a for a, _ in taxed] == [settings.npc_meal_cost_sc]


async def test_declared_host_duty_is_honored(
        temp_location, duty_keys, monkeypatch):
    temp_location("t_canteen", {
        "capabilities": {CAP_DINING: {"host_duty": "canteen_cook"}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    await execute_basic._charge_meal(AsyncMock(), _diner((4, 4)))
    assert duty_keys == ["canteen_cook"]
