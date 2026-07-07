"""E-08: 玩家聊天 history 策略成本曲线（A 全量 / B 滑窗10 / C 滑窗6+滚动摘要）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from est_tokens import est_tokens

SYSTEM_TOK = 208 + 300          # persona + memory 段（E-03 同参）
USER_TOK = est_tokens("今天市集上有什么新鲜事吗跟我说说")
REPLY_TOK = 80                   # ≈80 字中文回复
SUMMARY_TOK = 150                # 滚动摘要驻留尺寸
SUMMARY_CALL_IN = 900            # 每次摘要调用输入（10条消息+指令）
SUMMARY_EVERY = 10               # 每 10 轮摘要一次


def run(turns, policy):
    msgs = []            # (tok,) per message
    total_in = 0.0
    summary_resident = 0
    extra_calls_in = 0.0
    for t in range(turns):
        msgs.append(USER_TOK)
        if policy == "A":
            hist = sum(msgs)
        elif policy == "B":
            hist = sum(msgs[-10:])
        else:  # C
            hist = sum(msgs[-6:]) + summary_resident
        total_in += SYSTEM_TOK + hist
        msgs.append(REPLY_TOK)
        if policy == "C" and (t + 1) % SUMMARY_EVERY == 0:
            extra_calls_in += SUMMARY_CALL_IN
            summary_resident = SUMMARY_TOK
    return total_in + extra_calls_in


print(f"{'轮数':>4} {'A全量':>9} {'B滑窗10':>9} {'C窗6+摘要':>10} {'B省':>7} {'C省':>7}")
for turns in (5, 10, 20, 40, 80):
    a = run(turns, "A")
    b = run(turns, "B")
    c = run(turns, "C")
    print(f"{turns:>4} {a:>9.0f} {b:>9.0f} {c:>10.0f} {1-b/a:>6.1%} {1-c/a:>6.1%}")
