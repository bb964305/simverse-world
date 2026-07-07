"""E-19 重试放大 / E-20 Forge 单角色成本。"""
P_IN, P_OUT = 1.0 / 1e6, 5.0 / 1e6

print("== E-19 重试放大 ==")
for fail in (0.02, 0.05, 0.10):
    # 网络/5xx 重试：期望 attempt 数（retries=2, 系统调用为主）
    attempts = sum(fail ** k for k in range(3))
    # 解析失败：结构性失败 3%（付全款零产出），无自动重试（静默吞掉→行为默认值）
    parse_waste = 0.03
    amplification = attempts * (1 + parse_waste)
    print(f"  失败率={fail:.0%}: E[attempts]={attempts:.3f}, 叠加解析浪费 3% → 有效成本 ×{amplification:.3f}")

print("\n== E-20 Forge 单角色成本 (@Haiku 列表价, 输出按 cap×0.5 折) ==")
scenarios = {
    "legacy /forge/quick": [(2000, 4096 * 0.5), (600, 100)],
    "legacy 5步引导": [(1500, 750), (2000, 1000), (1500, 750), (800, 100), (400, 50), (600, 100)],
    "pipeline quick": [(400, 100), (2500, 1000), (2500, 1000), (2500, 1000)],
    "pipeline deep": [(400, 100), (6000, 1500), (6000, 1000), (6000, 1000), (6000, 1000),
                      (8000, 1000), (2000, 500), (2000, 500), (5000, 1250), (5000, 1250), (5000, 1250)],
}
for name, calls in scenarios.items():
    c = sum(i * P_IN + o * P_OUT for i, o in calls)
    print(f"  {name:<22} {len(calls):>2} 次调用  ${c:.4f}")
print("  （对照：居民常驻成本 ≈ $0.059/居民·天，E-11 v2 中位）")
