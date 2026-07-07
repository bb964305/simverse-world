"""E-18: 分级模型映射——按"玩家可见/背景"拆调用与 token，三方案成本对比。"""
H_IN, H_OUT = 1.0, 5.0     # $/MTok haiku
S_IN, S_OUT = 3.0, 15.0    # sonnet

# (类别, 调用次数/天@15居民, in tok/次, out tok/次)  基于 E-11 v2 中位参数
FLOWS = [
    ("decide",        "背景", 15 * 14.3, 700, 120),
    ("plan",          "背景", 15 * 1, 900, 700),
    ("互聊对白",       "背景", 15 * 2 * 8, 5477 / 8, 45),
    ("互聊收尾",       "背景", 15 * 2 * 5, 3198 / 5, 120),
    ("反思",           "背景", 15 * 14.3 * 0.05, 700, 200),
    ("进化",           "背景", 15 * 0.15, 1500, 1200),
    ("玩家流式回复",    "玩家可见", 50, 1313, 67),
    ("玩家收尾",       "背景", 5, 1400, 250),
]

n_bg = tok_bg = n_pv = tok_pv = 0.0
cost = {"全Haiku": 0.0, "玩家Sonnet+背景Haiku": 0.0, "全Sonnet": 0.0}
for name, cls, n, tin, tout in FLOWS:
    tk = n * (tin + tout)
    if cls == "背景":
        n_bg += n
        tok_bg += tk
    else:
        n_pv += n
        tok_pv += tk
    h = n * (tin * H_IN + tout * H_OUT) / 1e6
    s = n * (tin * S_IN + tout * S_OUT) / 1e6
    cost["全Haiku"] += h
    cost["全Sonnet"] += s
    cost["玩家Sonnet+背景Haiku"] += s if cls == "玩家可见" else h

print(f"背景调用: {n_bg:.0f} 次/天 ({n_bg/(n_bg+n_pv):.1%}), token {tok_bg:.0f} ({tok_bg/(tok_bg+tok_pv):.1%})")
print(f"玩家可见: {n_pv:.0f} 次/天 ({n_pv/(n_bg+n_pv):.1%})")
base = cost["全Haiku"]
for k, v in cost.items():
    print(f"{k:<18} ${v:.3f}/天  ({v/base:+.1%} vs 全Haiku)" if k != "全Haiku" else f"{k:<18} ${v:.3f}/天  (基准)")
