"""E-11: 基线 v2 —— 补进化/反思/玩家收尾/作息门控（修正-3）。"""
P_IN, P_OUT = 1.0 / 1e6, 5.0 / 1e6

# E-06 原参数
PLAN = (900, 700)
DECIDE = (700, 120)
CHAT_TURN_IN_AVG = 5477 / 8
CHAT_TURN_OUT = 45
CHAT_WRAPUP = (3198, 600)
PLAYER_MSG_20T = (26250 / 20, 1340 / 20)

# v2 新增
REFLECT = (700, 200)            # generate_reflections: 20 events + 10 rels 注入
EVO_SHIFT = (1800, 1500)        # shift 评估500out + persona 800 + soul 500(加权0.4) ≈ in 3×600
EVO_DRIFT = (1200, 1000)        # drift 评估 + persona 改写
PLAYER_WRAPUP = (1400, 250)     # 对话全文重发×1(extract) + 关系(300cap→150) ；反思按 0.2 概率并入
SHIFT_RATE = 0.05               # 次/居民日（importance≥0.9 且过 24h 冷却+月预算 guard）
DRIFT_RATE = 0.10               # 次/居民日（≥15 事件累计）
REFLECT_RATE = 0.15             # memorize 概率（仅 reflective 配置的居民，按 1/3 居民算）
SCHED_GATE = 0.75               # should_tick 作息门控：夜间/睡眠居民不 tick


def cost(tok_in, tok_out):
    return tok_in * P_IN + tok_out * P_OUT


def baseline(residents, chats, player_msgs, player_convos, v2=False):
    decide_n = 20 * (SCHED_GATE if v2 else 1.0)
    c = {}
    c["plan"] = residents * cost(*PLAN)
    c["decide"] = residents * decide_n * cost(*DECIDE)
    convo = 8 * cost(CHAT_TURN_IN_AVG, CHAT_TURN_OUT) + cost(*CHAT_WRAPUP)
    c["互聊"] = residents * chats * convo
    c["玩家消息"] = player_msgs * cost(*PLAYER_MSG_20T)
    if v2:
        c["玩家收尾"] = player_convos * cost(*PLAYER_WRAPUP)
        c["反思"] = residents * decide_n * REFLECT_RATE / 3 * cost(*REFLECT)
        c["进化"] = residents * (SHIFT_RATE * cost(*EVO_SHIFT) + DRIFT_RATE * cost(*EVO_DRIFT))
    total = sum(c.values())
    return c, total


for n, chats, pm, pc in ((15, 2, 50, 5), (100, 2, 300, 30)):
    c1, t1 = baseline(n, chats, pm, pc, v2=False)
    c2, t2 = baseline(n, chats, pm, pc, v2=True)
    print(f"\n== {n} 居民 ==")
    print(f"  v1(E-06口径)  ${t1:.3f}/天")
    print(f"  v2(修正-3口径) ${t2:.3f}/天   上浮 {t2/t1-1:+.1%}")
    for k, v in c2.items():
        print(f"    {k:<6} ${v:.4f}  ({v/t2:.1%})")
    print(f"  月成本 v2: ${t2*30:.2f}")
