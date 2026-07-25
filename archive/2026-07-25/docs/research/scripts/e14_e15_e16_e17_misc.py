"""E-14 extract 双视角合一 / E-15 plan 降频 / E-16 互聊标准多轮化 / E-17 cap 审计。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from est_tokens import est_tokens

P_IN, P_OUT = 1.0 / 1e6, 5.0 / 1e6

print("== E-14 extract 双视角合一 ==")
d = 460            # 8 轮对话文本 est（E-04 同参）
c = 200            # SBTI 块
two_calls = 2 * (230 + c + 20 + d)
one_call = 260 + 2 * c + 30 + d       # 合并指令稍长 + 双 SBTI + dialog 一次
print(f"两次 extract input={two_calls}  合一 input={one_call}  省 {1-one_call/two_calls:.1%}")

print("\n== E-15 plan 降频 ==")
plan_share = 0.075   # E-11 v2 实测占比
for policy, factor in (("48h 间隔", 0.5), ("仅 heat>0 (~40%居民)", 0.4), ("两者叠加", 0.2)):
    print(f"{policy}: plan 成本×{factor} → 全服省 {plan_share*(1-factor):.1%}")

print("\n== E-16 互聊标准多轮化 ==")
PERSONA, REL, LINE = 241, 40, 40   # est tokens
# 现状（E-02 实测）: 8 轮 input=5477
now = 5477.0
# 重构: 双方各自维护 system(persona+rel+指令~80) + 标准多轮 messages
# speaker 每次调用: system + 自己视角的历史(交替 user/assistant, 每行 LINE)
total = 0.0
for turn in range(8):
    hist_lines = turn  # 之前的行数
    total += (PERSONA + REL + 80) + hist_lines * LINE + 10
print(f"现状 input={now:.0f}  重构={total:.0f}  省 {1-total/now:.1%}")

print("\n== E-17 max_tokens cap 审计 ==")
rows = [
    # (调用点, cap, 指令隐含输出, 说明)
    ("互聊开场 (chat.py:138 turn0)", 100, "30字≈35-50tok", "安全"),
    ("互聊回复 (chat.py:138)", 100, "50字≈55-80tok", "临界:超字数样本会被硬切,半句话进history"),
    ("互聊summary (chat.py:175)", 150, "JSON+2句≈90-110", "偏紧"),
    ("decide (decide/basic.py:111)", 200, "JSON≈50-80", "余量2.5×,可收到120"),
    ("plan (plan/basic.py:203)", 1200, "7槽计划JSON≈600-800", "合理"),
    ("extract_events (service.py:307)", 500, "1-3条JSON≈100-220", "余量2×+"),
    ("update_rel (service.py:380)", 300, "JSON≈120-180", "合理"),
    ("reflections (service.py:423)", 400, "2-3条JSON≈120-200", "余量2×"),
    ("玩家回复 (model_router 流式)", 512, "200字≈220-300", "合理"),
    ("forge quick (forge_service:420)", 4096, "三层全文", "一次性,合理"),
]
for name, cap, implied, verdict in rows:
    print(f"  {name:<38} cap={cap:<5} 隐含={implied:<18} {verdict}")
