"""E-13: tick 吞吐上限排队模型（60s 轮询 × 5 并发信号量）。"""

ROUND = 60.0     # tick 轮间隔(s)
CONC = 5         # semaphore
CAPACITY = ROUND * CONC  # 每轮可用并发秒 = 300s

# tick 类型墙钟(s): decide LLM ~2s；plan 6s(1200tok 输出)；互聊 8×2.5 + 收尾5×2 = 30s
T_DECIDE, T_PLAN, T_CHAT = 2.0, 6.0, 30.0

print(f"每轮并发预算 = {CAPACITY:.0f} 并发秒\n")
print(f"{'居民数':>6} {'互聊率':>6} {'每轮需求(s)':>11} {'利用率':>7} {'实际tick间隔(s)':>13}")
for n in (15, 50, 100, 150, 200, 300):
    for chat_rate in (0.0, 0.05, 0.10):
        # 每轮: 全员 decide + 1/1440 plan(日一次摊到分钟) + chat_rate 互聊
        demand = n * (T_DECIDE + T_PLAN / 1440 + chat_rate * T_CHAT)
        util = demand / CAPACITY
        eff_interval = ROUND * max(1.0, util)
        print(f"{n:>6} {chat_rate:>6.0%} {demand:>11.0f} {util:>7.0%} {eff_interval:>13.0f}")
    print()
