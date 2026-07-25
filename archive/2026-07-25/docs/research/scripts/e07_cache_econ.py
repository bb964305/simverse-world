"""E-07: Haiku 共享大前缀缓存收支模型。

设计：前缀 P = 全局垫料 G + 该调用的稳定内容 S（persona/模板等），P >= 4096 才会被缓存。
现状每次调用付 S + V（V=易变部分）。缓存化后：
  未命中（写）: (G + S) * 1.25 + V
  命中（读）:   (G + S) * 0.1  + V
Δ/调用 = 现状 - 缓存化 = S + V - [hit*(0.1P + V) + (1-hit)*(1.25P + V)]
       = S - P*(0.1*hit + 1.25*(1-hit))
"""

FLOWS = {
    # name: (S 稳定 tokens, V 易变 tokens, 每居民每天调用次数)
    "decide":        (450, 250, 20),
    "chat_turn":     (350, 335, 16),   # 8轮×2居民视角
    "player_chat":   (550, 760, 3.3),  # 50条/15居民
    "wrapup_merged": (700, 674, 2),    # E-04 合并后
    "plan":          (600, 300, 1),
}
P_IN = 1.0 / 1e6

print(f"{'flow':<14} {'S':>5} {'P':>5} | " + " | ".join(f"hit={h:.0%} Δtok/调用" for h in (0.5, 0.8, 0.95)))
for name, (S, V, freq) in FLOWS.items():
    P = max(4096, S)  # 全局垫料补足到 4096
    cells = []
    for hit in (0.5, 0.8, 0.95):
        eff = P * (0.1 * hit + 1.25 * (1 - hit))
        delta = S - eff  # >0 省钱
        cells.append(f"{delta:>+9.0f}")
    print(f"{name:<14} {S:>5} {P:>5} | " + " | ".join(cells))

print("\n全服 Δ$/天（15 居民，hit=95%，全部 flow 缓存化）:")
total = 0.0
for name, (S, V, freq) in FLOWS.items():
    P = max(4096, S)
    eff = P * (0.1 * 0.95 + 1.25 * 0.05)
    delta_tok = (S - eff) * freq * 15
    total += delta_tok * P_IN
    print(f"  {name:<14} Δ${delta_tok * P_IN:+.4f}/天")
print(f"  合计 Δ${total:+.4f}/天（负=更贵）")

# 对照：假想模型阈值=1024（如 sonnet-4.5 类），P=max(1024,S)
print("\n对照：若最小前缀阈值=1024（P=max(1024,S)），hit=95%:")
for name, (S, V, freq) in FLOWS.items():
    P = max(1024, S)
    eff = P * (0.1 * 0.95 + 1.25 * 0.05)
    print(f"  {name:<14} Δtok/调用 {S - eff:+.0f}")
