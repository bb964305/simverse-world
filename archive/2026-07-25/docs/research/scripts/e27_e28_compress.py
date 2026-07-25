"""E-27 decide prompt 压缩 / E-28 玩家聊天记忆注入规模敏感性。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from est_tokens import est_tokens

print("== E-27 decide prompt 压缩 ==")
sys_now = """你是一个游戏 NPC 居民的自主决策引擎。你的任务是根据居民当前的状态、周围环境和记忆，选择最符合角色人格的下一个行动。

居民信息：
- 姓名：李明轩
- 当前位置：市集（热闹的交易场所）
- 人格类型（SBTI）：EHAC（热心肠）
- 当前状态：idle

输出严格 JSON 格式，不要输出其他内容：
{"action": "<ACTION_TYPE>", "target_slug": "<居民slug或null>", "target_tile": [x, y] 或 null, "reason": "<一句话理由，15字以内>"}

可用的 action 类型：WORK, STUDY, WANDER, REFLECT, JOURNAL, OBSERVE, CHAT_RESIDENT, GO_HOME, VISIT_DISTRICT, GOSSIP

规则：
- CHAT_RESIDENT 需要在 nearby_residents 中选一个空闲居民，填入 target_slug
- WANDER/VISIT_DISTRICT 填入 target_tile（使用地点入口坐标），其余为 null
- GO_HOME 不需要 target_tile（自动导航到你的家）
- GOSSIP 需要 target_slug，内容由后续流程生成
- 社交类型低（So1=L）的居民，倾向于选择 REFLECT/JOURNAL/OBSERVE
- 行动力高（Ac3=H）的居民，倾向于 WORK/STUDY/WANDER
- 当天已执行 5 个行动，上限 20

你在市集里，这里特别适合：WORK, CHAT_RESIDENT"""
user_now = """当前游戏世界时间：上午 10:30（morning）

附近的居民：
- 王雨桐（wang-yutong）：idle，距离约 3 格
- 陈老板（chen-laoban）：working，距离约 8 格

最近的记忆：
""" + "\n".join(f"- [action] 记忆条目内容大约在二十个汉字左右第{i}条" for i in range(8)) + """

今天已做的事：
""" + "\n".join(f"- 做过的事情第{i}条" for i in range(10)) + """

请选择下一个行动。

你原本计划在这个时段 WORK（上午是市集生意最好的时候），但你可以根据当前情况改变主意。"""

sys_c = """你是 NPC 决策引擎。按人格选下一个行动，只输出 JSON：
{"action":"...","target_slug":"...或null","target_tile":[x,y]或null,"reason":"15字内"}
李明轩@市集 EHAC(热心肠) idle | 今日 5/20 个行动
动作: WORK,STUDY,WANDER,REFLECT,JOURNAL,OBSERVE,CHAT_RESIDENT,GO_HOME,VISIT_DISTRICT,GOSSIP
要点: CHAT/GOSSIP 需选空闲邻居填 slug；WANDER/VISIT 填坐标；市集适合 WORK,CHAT_RESIDENT"""
user_c = """时间: 上午10:30(morning)
附近: 王雨桐(wang-yutong,idle,3格) 陈老板(chen-laoban,working,8格)
最近记忆:
""" + "\n".join(f"- 记忆条目内容大约在二十个汉字左右第{i}条" for i in range(5)) + """
今日已做: """ + "、".join(f"事{i}" for i in range(5)) + """
计划: 本时段 WORK(生意最好)，可改主意。选下一个行动。"""

a = est_tokens(sys_now) + est_tokens(user_now)
b = est_tokens(sys_c) + est_tokens(user_c)
print(f"现状 input={a:.0f}  压缩后={b:.0f}  省 {1-b/a:.1%}")
print(f"全服换算(decide 占 32%): 省 {0.32*(1-b/a):.1%}")

print("\n== E-28 记忆注入规模 ==")


def memory_block(n_events, n_refl):
    parts = ["### 关于当前对话对象", "认识很久的老朋友关系描述大约三十个字的样子啦", "印象标签：健谈, 靠谱"]
    parts += ["### 你最近的思考"] + [f"- 反思内容大约二十五个汉字左右的一条思考记录第{i}" for i in range(n_refl)]
    parts += ["### 相关的过往经历"] + [f"- 事件记忆内容大约三十个汉字左右的一条过往经历记录条目第{i}" for i in range(n_events)]
    return "\n".join(parts)


m10 = est_tokens(memory_block(10, 3))
m5 = est_tokens(memory_block(5, 2))
msg_in_now = 208 + m10 + 805
msg_in_cut = 208 + m5 + 805
print(f"记忆段: 10ev+3refl={m10:.0f} tok → 5ev+2refl={m5:.0f} tok")
print(f"20轮会话单条消息 input: {msg_in_now:.0f} → {msg_in_cut:.0f}  省 {1-msg_in_cut/msg_in_now:.1%}")
print(f"记忆段占单条 input: {m10/msg_in_now:.1%}")
