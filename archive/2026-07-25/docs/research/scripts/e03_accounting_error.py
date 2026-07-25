"""E-03: tokens_used = len(full_reply) 记账口径失真度（复刻 ws/handlers/chat.py 路径）。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from est_tokens import est_tokens

DB = Path(__file__).parents[3] / "backend" / "skills_world_dev.db"

# ① 真实回复样本: 字符数 vs est_tokens
con = sqlite3.connect(DB)
rows = con.execute("SELECT content FROM messages WHERE role='assistant'").fetchall()
print("① 真实 assistant 回复: chars vs est_tokens")
for (c,) in rows:
    print(f"   chars={len(c):>4}  est_tokens={est_tokens(c):>6.0f}  ratio(chars/tok)={len(c)/est_tokens(c):.2f}")

# ② 会话级失真:accounted vs true
SYSTEM = "你" * 208  # persona 稳定前缀 ≈ E-01 median
MEMORY = "记" * 300  # retrieve_context 注入段
USER_MSG = "今天市集上有什么新鲜事吗跟我说说"  # 15 字
REPLY = ("（抬眼看向你）今天市集来了个新商人，卖的香料很特别，我去看了两回。"
         "价格不便宜，但闻着确实提神。你要去的话记得砍价，他开价虚高。回头见。")  # ≈80 字

print("\n② 会话级: accounted(len chars of replies) vs true(in+out est_tokens)")
print(f"{'轮数':>4} {'accounted':>10} {'true_out':>9} {'true_in':>9} {'true_total':>10} {'acct/true':>9}")
for turns in (5, 10, 20, 40):
    chat_messages = []
    accounted = 0
    true_in = true_out = 0.0
    system_tok = est_tokens(SYSTEM) + est_tokens(MEMORY)
    for t in range(turns):
        chat_messages.append(("user", USER_MSG))
        # input = system + 全量历史(含刚 append 的 user 消息)
        true_in += system_tok + sum(est_tokens(m) for _, m in chat_messages)
        chat_messages.append(("assistant", REPLY))
        true_out += est_tokens(REPLY)
        accounted += len(REPLY)
    total = true_in + true_out
    print(f"{turns:>4} {accounted:>10} {true_out:>9.0f} {true_in:>9.0f} {total:>10.0f} {accounted/total:>8.1%}")
