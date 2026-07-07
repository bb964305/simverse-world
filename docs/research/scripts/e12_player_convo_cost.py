"""E-12: $/次玩家对话（逐条流式 + 结束收尾），短/长会话对比。"""
P_IN, P_OUT = 1.0 / 1e6, 5.0 / 1e6
SYSTEM = 208 + 300
USER_T, REPLY_T = 15, 80


def convo_cost(turns):
    msgs, stream_in, stream_out = [], 0.0, 0.0
    for _ in range(turns):
        msgs.append(USER_T)
        stream_in += SYSTEM + sum(msgs)
        msgs.append(REPLY_T)
        stream_out += REPLY_T
    dialog_full = sum(msgs)
    # 收尾: extract(模板230+SBTI 200+dialog) + 关系(模板260+SBTI+事件~60) [+反思 0.2×(700)]
    wrap_in = (430 + dialog_full) + (520 + 60) + 0.2 * 700
    wrap_out = 120 + 150 + 0.2 * 200
    stream_c = stream_in * P_IN + stream_out * P_OUT
    wrap_c = wrap_in * P_IN + wrap_out * P_OUT
    return stream_c, wrap_c, stream_in, dialog_full


print(f"{'轮数':>4} {'流式$':>9} {'收尾$':>9} {'总$':>9} {'收尾占比':>8} {'history重发占比':>12}")
base5 = None
for t in (5, 10, 20, 40):
    s, w, sin, dlg = convo_cost(t)
    tot = s + w
    # history 重发 = 流式 input 里除 system 与"当轮新消息"以外的部分
    resend = sin - t * (SYSTEM + USER_T + (REPLY_T if t > 1 else 0))
    if base5 is None:
        base5 = tot
    print(f"{t:>4} ${s:>8.5f} ${w:>8.5f} ${tot:>8.5f} {w/tot:>7.1%} {resend*P_IN/tot:>11.1%}  (×{tot/base5:.1f} vs 5轮)")
