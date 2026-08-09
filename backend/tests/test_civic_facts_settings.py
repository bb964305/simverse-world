"""世界公共记忆(civic public memory)的六个闸门 —— 默认即"全关"。

两个总闸(事实层 / 记忆广播)默认 False,所以整批代码可以随部署暗上,开闸是之后
两次各自独立的 deploy/.env 变更(红线:行为开闸与代码变更不同车)。四个数值旋钮
的默认值在这里钉死:后续 step 的有界 fail-open 上限、importance 分档都按这几个
数写断言,改默认值等于一次静默的行为变更,必须先来改这张表。

键名在两份 .env.example 里的 parity 不在本文件重复断言 ——
tests/test_env_example_consistency.py 的 invariant 1/2 管
backend/.env.example,test_governance_knobs_exist_in_deploy_env_example_too
凭 CIVIC_ 前缀自动把这六个键纳入 deploy/backend/.env.example 的 parity。
"""
from app.config import Settings

#: (字段名, 默认值) —— 顺序与 app/config.py 里那一块逐行对应。
GATES = [
    ("civic_facts_enabled", False),
    ("civic_facts_cache_ttl_seconds", 60.0),
    ("civic_facts_max_stale_seconds", 600.0),
    ("civic_memory_broadcast_enabled", False),
    ("civic_memory_importance", 0.9),
    ("civic_memory_notice_importance", 0.6),
]


def test_six_gates_exist_with_documented_defaults():
    fields = Settings.model_fields
    for name, default in GATES:
        assert name in fields, f"闸门缺失:{name} 不在 Settings 里"
        actual = fields[name].default
        # 连类型一起比:False == 0.0 在 Python 里为真,只比值的话把 bool 闸门
        # 写成 0.0(或反过来)这道断言看不出来。
        assert type(actual) is type(default) and actual == default, (
            f"{name} 的默认值必须是 {default!r}({type(default).__name__}),"
            f"实得 {actual!r}({type(actual).__name__})")


def test_both_master_gates_default_off():
    """两个总闸默认关 —— 本批只暗上代码,不改任何世界的行为。"""
    fields = Settings.model_fields
    assert fields["civic_facts_enabled"].default is False
    assert fields["civic_memory_broadcast_enabled"].default is False


def test_max_stale_is_looser_than_cache_ttl():
    """陈旧上限必须严格大于快照 TTL,否则有界 fail-open 那层没有意义。

    TTL 是"多久重取一次",max_stale 是"取不到时旧快照最多还能用多久"。若
    max_stale <= TTL,S2 的失败分支永远直接返回 {}(退化成 fail-closed),
    fail-open 这个设计就名存实亡。
    """
    fields = Settings.model_fields
    assert (fields["civic_facts_max_stale_seconds"].default
            > fields["civic_facts_cache_ttl_seconds"].default)
