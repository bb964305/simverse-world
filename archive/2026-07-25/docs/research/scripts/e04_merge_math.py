"""E-04: 互聊收尾 5 调用 vs 1 合并调用的 token 数学（复刻真实模板）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from est_tokens import est_tokens

# —— 真实素材（尺寸对齐）——
PERSONA = "我是一个典型居民角色，性格鲜明，有自己的口头禅和行为方式。" * 8  # ≈ median 208 tok
SBTI_COLORING = """该居民的 SBTI 人格类型为 OJBK（无所谓人），性格维度如下：
- 自尊自信(S1): 中
- 自我清晰度(S2): 中
- 核心价值(S3): 中
- 依恋安全感(E1): 中
- 情感投入度(E2): 高
- 边界与依赖(E3): 中
- 世界观倾向(A1): 低
- 规则与灵活度(A2): 中
- 人生意义感(A3): 中
- 动机导向(Ac1): 中
- 决策风格(Ac2): 中
- 执行模式(Ac3): 高
- 社交主动性(So1): 中
- 人际边界感(So2): 中
- 表达与真实度(So3): 中

请根据以上性格特征来着色记忆的表述方式和重要性评估。
例如：E2(情感投入度)高的居民，事件记忆的 importance 会偏高；
A1(世界观倾向)低的居民，反思时倾向悲观解读。"""

DIALOG_LINE = "李明轩: 这个话题很有意思，我觉得市集上新来的商人卖的东西确实不错，下次一起去看看吧。"
DIALOG = "\n".join(DIALOG_LINE for _ in range(8))  # 8 轮对话文本
REL_CURRENT = "上次和对方聊过市集的见闻，印象是个健谈的人，关系友好，信任度中等。"
EVENT_SUMMARIES = "- 两人约好下次一起去市集看新商人的货\n- 聊到了香料价格偏高的问题"

# —— 现状 5 调用（模板 token 按真实 app/memory/prompts.py + agent/prompts.py 结构）——
EXTRACT_SYS_TMPL = 230   # EXTRACT_EVENTS_SYSTEM 骨架(不含 coloring)
UPDATE_SYS_TMPL = 260    # UPDATE_RELATIONSHIP_SYSTEM 骨架
SUMMARY_SYS = 80         # CHAT_SUMMARY_SYSTEM

d = est_tokens(DIALOG)
c = est_tokens(SBTI_COLORING)
rel = est_tokens(REL_CURRENT)
ev = est_tokens(EVENT_SUMMARIES)

calls = {
    "extract_events(甲)": EXTRACT_SYS_TMPL + c + 20 + d,
    "extract_events(乙)": EXTRACT_SYS_TMPL + c + 20 + d,
    "update_rel(甲)": UPDATE_SYS_TMPL + c + 20 + rel + ev,
    "update_rel(乙)": UPDATE_SYS_TMPL + c + 20 + rel + ev,
    "summary": SUMMARY_SYS + 20 + d,
}
out_budget = {"extract_events(甲)": 500, "extract_events(乙)": 500,
              "update_rel(甲)": 300, "update_rel(乙)": 300, "summary": 150}
# 实际输出估计(按指令产出，非 max_tokens 上限)
out_actual = {"extract_events(甲)": 120, "extract_events(乙)": 120,
              "update_rel(甲)": 150, "update_rel(乙)": 150, "summary": 60}

print("== 现状 5 调用 ==")
tot_in = tot_out = 0
for k, v in calls.items():
    print(f"  {k:<22} in={v:>6.0f}  out≈{out_actual[k]}")
    tot_in += v
    tot_out += out_actual[k]
print(f"  合计 in={tot_in:.0f} out={tot_out}  total={tot_in + tot_out:.0f}")

# —— 合并 1 调用 ——
# system: 合并指令(提取+关系+总结说明, 双居民 coloring 各一份) ≈ 480 + 2c
# user: dialog 一次 + 双方当前关系 + 双方名字/SBTI
merged_in = 480 + 2 * c + d + 2 * rel + 40
merged_out = 2 * 120 + 2 * 150 + 60  # 同等内容量
print("\n== 合并 1 调用 ==")
print(f"  in={merged_in:.0f} out={merged_out}  total={merged_in + merged_out:.0f}")

print("\n== 对比 ==")
print(f"  input:  {merged_in / tot_in:.1%} of 现状 (省 {1 - merged_in / tot_in:.1%})")
print(f"  total:  {(merged_in + merged_out) / (tot_in + tot_out):.1%} of 现状 (省 {1 - (merged_in + merged_out) / (tot_in + tot_out):.1%})")
print(f"  RTT: 5 次串行 → 1 次")

# 按 haiku 单价折算每场对话收尾成本
P_IN, P_OUT = 1.0 / 1e6, 5.0 / 1e6
cost_now = tot_in * P_IN + tot_out * P_OUT
cost_merged = merged_in * P_IN + merged_out * P_OUT
print(f"\n  Haiku 单场收尾成本: 现状 ${cost_now:.6f} → 合并 ${cost_merged:.6f}")
