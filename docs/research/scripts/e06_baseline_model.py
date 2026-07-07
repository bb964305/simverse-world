"""E-06: 全服 LLM 成本基线模型（参数来自 E-01..05 实测 + config.py 频率参数）。"""

P_IN, P_OUT = 1.0 / 1e6, 5.0 / 1e6  # haiku-4.5 $/token

# —— 单调用尺寸（est_tokens，来源见台账）——
PLAN = (900, 700)          # in, out_actual（max 1200，计划 JSON 7 槽实际 ~700）
DECIDE = (700, 120)        # decision prompt（system 400 + user 300），JSON 输出 ~120
CHAT_TURN_IN_AVG = 5477 / 8   # E-02: 8 轮总 input 5477（含双注入现状）
CHAT_TURN_OUT = 45
CHAT_WRAPUP = (3198, 600)  # E-04 现状 5 调用收尾
PLAYER_MSG_20T = (26250 / 20, 1340 / 20)  # E-03: 20 轮会话平摊每条 in/out


def daily_cost(residents, chats_per_resident, decide_llm_rate, player_msgs_per_day):
    plan = residents * (PLAN[0] * P_IN + PLAN[1] * P_OUT)
    decide = residents * 20 * decide_llm_rate * (DECIDE[0] * P_IN + DECIDE[1] * P_OUT)
    # 每场互聊: 8 轮(说话人合计) + 收尾 5 调用；chats_per_resident 按发起方计
    chat_convo = 8 * (CHAT_TURN_IN_AVG * P_IN + CHAT_TURN_OUT * P_OUT) \
        + CHAT_WRAPUP[0] * P_IN + CHAT_WRAPUP[1] * P_OUT
    chats = residents * chats_per_resident * chat_convo
    player = player_msgs_per_day * (PLAYER_MSG_20T[0] * P_IN + PLAYER_MSG_20T[1] * P_OUT)
    total = plan + decide + chats + player
    return plan, decide, chats, player, total


print(f"{'居民':>4} {'聊/居/天':>8} {'decide率':>8} {'玩家条/天':>9} | "
      f"{'plan':>8} {'decide':>8} {'互聊':>8} {'玩家':>8} {'日合计':>9} {'月合计':>9}")
for residents in (15, 100):
    for chats in (1, 2, 4):
        for rate in (0.5, 1.0):
            pm = 50 if residents == 15 else 300
            p, d, c, pl, t = daily_cost(residents, chats, rate, pm)
            print(f"{residents:>4} {chats:>8} {rate:>8.0%} {pm:>9} | "
                  f"${p:>7.3f} ${d:>7.3f} ${c:>7.3f} ${pl:>7.3f} ${t:>8.3f} ${t*30:>8.2f}")

# 占比(中位情形)
p, d, c, pl, t = daily_cost(15, 2, 1.0, 50)
print(f"\n中位情形(15居民/2聊/100%decide/50玩家条): "
      f"plan {p/t:.0%} decide {d/t:.0%} 互聊 {c/t:.0%} 玩家 {pl/t:.0%}")
p, d, c, pl, t = daily_cost(100, 2, 1.0, 300)
print(f"100居民同参数: plan {p/t:.0%} decide {d/t:.0%} 互聊 {c/t:.0%} 玩家 {pl/t:.0%} 月=${t*30:.2f}")
