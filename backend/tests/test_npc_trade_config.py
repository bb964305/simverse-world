"""M-A 经济内生化的 8 个配置键与默认值（Step 1）。

三个闸门是**互相独立**的：NPC_TRADE_ENABLED（C1 餐费入账 + C2 消费 pass +
C3 委托接单/结算）、CARAVAN_ENABLED（C4 外来商队）、TAX_CARRY_ENABLED
（C5 分数税账）。默认必须全 False——vm212 的 TOWN_TREASURY_ENABLED 已在产
开启，carry 若不单独设闸，「随代码暗上」就等于迁移 + 在产玩家路径行为变更
同车（红线 feedback-no-migration-with-flag-flip）。开闸是 deploy/.env 的
单独变更。
"""
from app.config import Settings


def test_npc_trade_gates_default_off():
    s = Settings(_env_file=None)
    assert s.npc_trade_enabled is False
    assert s.caravan_enabled is False
    assert s.tax_carry_enabled is False
    assert s.item_stock_guard_enabled is False   # 加固闸:迁移 056 暗上后单独翻


def test_npc_trade_tuning_defaults():
    s = Settings(_env_file=None)
    assert s.npc_trade_buy_prob == 0.25
    assert s.npc_trade_reserve_sc == 5          # 保留金，兼作贫困线
    assert s.npc_trade_max_buys_per_night == 2  # 全镇每晚成交上限


def test_caravan_defaults():
    s = Settings(_env_file=None)
    assert s.caravan_stall_fee_sc == 5   # 摊位费（第二税源，不依赖 tax_rate）
    assert s.caravan_budget_sc == 30     # 每个集市日的收购预算


def test_npc_trade_env_override(monkeypatch):
    monkeypatch.setenv("NPC_TRADE_ENABLED", "true")
    monkeypatch.setenv("CARAVAN_BUDGET_SC", "12")
    s = Settings(_env_file=None)
    assert s.npc_trade_enabled is True
    assert s.caravan_budget_sc == 12
