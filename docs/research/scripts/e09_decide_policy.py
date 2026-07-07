"""E-09: decide LLM 调用率蒙特卡洛（现状 vs 计划优先政策 P1）。"""
import random

random.seed(42)

SLOTS = 7           # hourly_slots
HIGH_SLOTS = 2      # max_high_importance (importance>=6 强制执行)
TICKS_PER_SLOT = 3  # 20 actions/day ≈ 7 槽 × ~3 tick
DAYS = 1000

for interrupt_rate in (0.10, 0.20, 0.30):
    now_llm = p1_llm = total = 0
    for _ in range(DAYS):
        high = set(random.sample(range(SLOTS), HIGH_SLOTS))
        for slot in range(SLOTS):
            for _tick in range(TICKS_PER_SLOT):
                total += 1
                if slot in high:
                    continue  # 现状/P1 都不调 LLM
                # 现状: 低重要度槽每 tick 都 LLM
                now_llm += 1
                # P1: 只有中断信号才 LLM
                if random.random() < interrupt_rate:
                    p1_llm += 1
    print(f"中断率={interrupt_rate:.0%}: 现状 LLM 率={now_llm/total:.1%}  "
          f"P1 LLM 率={p1_llm/total:.1%}  decide 调用降幅={1-p1_llm/now_llm:.1%}")

# 全服成本影响（E-06 中位: decide 占 41%）
print("\n换算全服（decide 占 41%）:")
for interrupt_rate, cut in ((0.10, None), (0.20, None), (0.30, None)):
    cut = 1 - interrupt_rate  # P1 在低槽内的降幅≈1-中断率
    print(f"  中断率={interrupt_rate:.0%}: 全服总成本 ≈ 省 {0.41 * cut:.1%}")
