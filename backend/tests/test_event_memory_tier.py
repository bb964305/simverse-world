"""world_event 记忆的 importance 分档(REALISM_EVENT_MEMORY_*)。

`write_collective_memories` 一直是 `db.add(Memory(...))` 直写,绕过
`MemoryService.add_memory` 也就绕过了 `_normalize_importance`,importance 是硬编码
的 0.5 / 0.6。而检索候选池 `_fetch_event_candidates` 按 `importance DESC` 静态截前
30(`app/memory/service.py:308`),生产实测每位居民 top-30 第 30 名都在 0.95-1.0 ——
1311 条 world_event 记忆一条都进不去,**写了等于没写**。

修法是**分档**,不是整体抬高:1253/1311(96%)是天气,抬上去就是拿「今天多云」把
只有 30 个坑的候选池灌满,个人记忆被挤光,那比现状更糟。

- **琐事档**(天气 / 集市日节庆)→ 直写,逐字节不变;
- **实质档**(其余)→ 走 `add_memory` 参与分位归一。

T1 只钉两个旋钮的存在与默认值,以及 deploy 模板的 parity:`REALISM_` 不在
`GOVERNANCE_PREFIXES`(只有 `CIVIC_`/`REP_`/`POLIS_OFFICE_`)里,那条自动 parity
覆盖不到本批 —— 运维照 deploy 模板起的环境读不到的键 = 不存在的键。
"""
from app.config import Settings

#: (字段名, 保守默认值)。默认必须是「关 + 与现状逐字节一致」——开闸是另一次
#: 独立的部署变更(红线:行为开闸与代码变更不同车)。
KNOBS = [
    ("realism_event_memory_tiered", False),
    ("realism_event_memory_importance", 0.9),
]


def test_tier_knobs_are_settings_fields_with_conservative_defaults():
    fields = Settings.model_fields
    for name, default in KNOBS:
        assert name in fields, f"旋钮 {name} 不在 Settings 里,REALISM_ 前缀的 env 会被拒"
        assert fields[name].default == default, (
            f"{name} 的默认值必须是关/保守,期望 {default!r},"
            f"实得 {fields[name].default!r}")
