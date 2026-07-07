"""E-02: 居民互聊 history 双重注入浪费量化（复刻 app/agent/chat.py 路径）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from est_tokens import est_tokens

# 复刻 app/agent/prompts.py 模板（只保留尺寸相关结构）
CHAT_INITIATE_SYSTEM = """你是 {initiator_name}，一个 Simverse World 的居民（SBTI：{sbti_type} {sbti_name}）。
你主动走向 {target_name} 并开始对话。

你的人格：
{persona_md}

你对 {target_name} 的记忆：
{relationship_memory}

请用中文，以符合你人格的方式开场白。保持简短（30字以内）。
"""

CHAT_REPLY_SYSTEM = """你是 {responder_name}，一个 Simverse World 的居民（SBTI：{sbti_type} {sbti_name}）。
{initiator_name} 正在和你对话。

你的人格：
{persona_md}

你对 {initiator_name} 的记忆：
{relationship_memory}

对话历史：
{history}

请用中文，以符合你人格的方式回应。保持简短（50字以内）。
"""

PERSONA = "# Persona Layer\n\n## 身份卡\n" + "我是一个典型居民角色，性格鲜明，有自己的口头禅和行为方式。" * 8  # ≈ median 208 tokens
REL = "上次和对方聊过市集的见闻，印象是个健谈的人，关系友好。"
REPLY = "这个话题很有意思，我觉得市集上新来的商人卖的东西确实不错，下次一起去看看吧，顺便聊聊别的。"  # ≈40 中文字

NAME_A, NAME_B = "李明轩", "王雨桐"


def simulate(turns=8, double_inject=True):
    dialog_lines = []
    total_in = 0.0
    total_out = 0.0
    for turn in range(turns):
        speaker = NAME_A if turn % 2 == 0 else NAME_B
        listener = NAME_B if turn % 2 == 0 else NAME_A
        history = "\n".join(dialog_lines[-6:])
        if turn == 0:
            system = CHAT_INITIATE_SYSTEM.format(
                initiator_name=speaker, sbti_type="OJBK", sbti_name="无所谓人",
                target_name=listener, persona_md=PERSONA, relationship_memory=REL)
            user = "开始对话"
        else:
            if double_inject:  # 现状：history 进 system 也进 user
                system = CHAT_REPLY_SYSTEM.format(
                    responder_name=speaker, sbti_type="OJBK", sbti_name="无所谓人",
                    initiator_name=listener, persona_md=PERSONA,
                    relationship_memory=REL, history=history)
                user = history
            else:  # 方案 B：system 不含 history，只在 user 出现一次
                system = CHAT_REPLY_SYSTEM.format(
                    responder_name=speaker, sbti_type="OJBK", sbti_name="无所谓人",
                    initiator_name=listener, persona_md=PERSONA,
                    relationship_memory=REL, history="（见对话内容）")
                user = history
        total_in += est_tokens(system) + est_tokens(user)
        reply = REPLY
        total_out += est_tokens(reply)
        dialog_lines.append(f"{speaker}: {reply}")
    return total_in, total_out


a_in, a_out = simulate(double_inject=True)
b_in, b_out = simulate(double_inject=False)
print(f"方案A(现状双注入): input={a_in:.0f} output={a_out:.0f}")
print(f"方案B(单次注入):   input={b_in:.0f} output={b_out:.0f}")
print(f"重复注入浪费: {a_in - b_in:.0f} tokens = 总输入的 {(a_in - b_in) / a_in * 100:.1f}%")

# 附带:8轮对话总输入中 persona 重复注入的占比(每轮都带全量 persona)
persona_tok = est_tokens(PERSONA)
print(f"\npersona 单份={persona_tok:.0f} tokens, 8 轮重复注入共 {persona_tok * 8:.0f} tokens "
      f"= 方案A总输入的 {persona_tok * 8 / a_in * 100:.1f}%")
