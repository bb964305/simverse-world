Delivered to the team lead. Here is the complete deliverable (also sent via the team channel), self-contained.

# LLM 成本优化研究 · 方法论评审 + 全量实验路线图

已通读台账 §0 与全部关键代码路径（`llm/client`、`llm/prompt`、`agent` 各 phase、`memory/service`、`personality/evolution`、`forge`、`ws/handlers/chat`、`config`、`media/model_router`），并用 claude-api 参考核实了缓存/批处理事实。

## 一、方法论评审（编号 · 可执行）

**M-1【最高优先，会作废整条缓存+批处理支线】生产端点与真实价目未确认。** 代码硬证据：`config.py` 有 `llm_base_url`/`llm_model` 覆盖位，`model_router.py:126` 注释写明 "same DashScope Anthropic-compatible endpoint"，video=`kimi-k2.5`、portrait=`gemini-3-pro-image-preview`。基本可判定生产走的是**中转/聚合网关**（DashScope 或 one-api/new-api 类），非 Anthropic 原生。后果：(a) Anthropic 列表价只是参考价，网关真实计费可能偏差 >20%；(b) claude-api 已核实 automatic caching 在 Bedrock/Vertex 上就是 ❌，中转站通常直接吞掉 `cache_control` 且 `usage` 不返回 `cache_read_input_tokens` → E-01 引出的"共享大前缀"策略与整条缓存支线净收益可能全为 0；(c) Batch API 在 Bedrock/Vertex/Foundry 均 ❌，中转站几乎必然不暴露 → 批处理支线可能直接作废；(d) 若 usage 无 cache_* 字段，E-03 给 P1-1 的缓存字段建议要降级为 est 影子计量。**→ 入 §修正记录，新增 P0 门实验 E-06。**

**M-2【基线偏低 ≥40%】现有实验漏计三类递归调用。** 静态枚举全部 call site 后，候选 A 只算了 decide+chat，漏了：**daily PLAN**（`plan/basic.py:203`，max_tokens=1200，全系统最大输出，每居民每天 1 次）、**整个 EVOLUTION 子系统**（`evolution.py`：shift=SHIFT_EVAL 500+persona 改写 800+soul 500，drift=DRIFT_EVAL 400+persona 800，二者都从 extract_events 在玩家聊天/居民互聊两条路径同步触发）、**auto-reflection**（15% 概率 generate_reflections 400）。**→ 入 §修正记录，§0.3 场景公式补齐。**

**M-3** est_tokens 从未对真实模型 tokenizer 标定，±25% 是断言非实测；若网关"haiku"实为 Qwen/kimi 代理，CJK 比会漂，且 JSON 密集输出下 `ascii/3.6` 过于乐观。→ 有 key 后用 5-10 payload 对 `usage.input_tokens` 标定（E-07）。**入 §修正记录（待办）。**

**M-4** 比值抵消只在分子分母同字符类构成时成立；E-04（中文模板 vs JSON 合并）构成变了，残差不全抵消 → 跨结构对比附 ascii/cjk/other 拆分。**入 §修正记录。**

**M-5** 用 max_tokens=512 当 output 上界会高估短 JSON 调用成本；各调用改用指令隐含输出长度，cap 只用于"截断风险"这个独立问题。**入 §修正记录。**

**M-6** dev DB（15 居民、2 条 assistant）代表性不足；forge 的萧炎 persona=908 是手写中位 208 的 4.4 倍 → $/居民·天 按 median 与 heavy 分位双档给区间，$/对话用真实轮次分布。

**M-7** 重试（system=2/user=3）与静默吞 JSONDecodeError 的"白付调用"完全没算，中转站 429/5xx 下有效调用量 +10~30% → P1-1 须按 attempt 计费。

**M-8** "haiku via Agent 工具"测的是 haiku 级模型的下界（harness 有自带 system prompt、快照可能≠生产），结论当 go/no-go 非 SLA，报失败模式。**入 §修正记录。**

## 二、全量实验路线图（32 条，交付 a=基线 / b=杠杆 / c=P1-1）

| ID | 标题 | 假设（预期数字/方向） | 优先级 | 依赖 | 执行者 | 交付 |
|---|---|---|---|---|---|---|
| E-01 | 稳定前缀 vs Haiku 阈值 | 全部<4096(中位208)，per-resident 缓存零收益 | ✅done | - | 主线 | b |
| E-02 | 互聊 history 双注入 | 双注入=23.1%，persona 重复=35.2% | ✅done | - | 主线 | b |
| E-03 | len(reply) 记账失真 | 记账≈真实 3-9%，input 91-97% 二次增长 | ✅done | - | 主线 | a,c |
| E-04 | 收尾 5→1 合并数学 | 收尾 input 降至 40-55% | P1 | - | 主线 | b |
| E-05 | 合并 JSON haiku 可靠性 | 5 试跑≥4 完整可解析 | P1 | E-04 | haiku-agent | b,c |
| **E-06** | **端点/真实价/缓存可行性** | **非 Anthropic 中转；不透传 cache_control、无 Batch；单价偏列表价>20%** | **P0** | 需凭据 | worker+Jimmy | a,b,c |
| **E-07** | est_tokens 标定 | 比值落 0.8-1.2，JSON 密集偏差最大 | P0 | E-06 | 主线 | a |
| **E-08** | 完整调用清单 | 补齐 plan+evolution+reflection 后 $/居民·天 高≥40% | P0 | - | 主线 | a,b |
| **E-09** | $/居民·天基线 | decide 50-70%、plan 15-30%、互聊+evo 10-25% | P0 | E-08 | 主线 | **a** |
| **E-10** | $/次玩家对话基线 | 短对话收尾>50%，长对话 history>80%，超线性 | P0 | E-08,03 | 主线 | **a** |
| **E-11** | 100 居民月成本&扩容悬崖 | 60s×并发5 存吞吐上限，超临界 N tick 被动降频→行为退化 | P0 | E-09 | 主线 | a,c |
| **E-12** | evolution 触发率&占比 | 触发<0.3次/居民/天但单次≈2-4×decide，且改 persona 失效缓存 | P1 | E-08 | 主线 | a,b |
| **E-13** | extract 双视角去重 | dialog 重复计费 2 次，合并省 extract input ~45% | P1 | E-04 | 主线 | b |
| **E-14** | skip-decide 规则回退 | 30-50% tick 可跳 decide，覆盖 tick 零质量风险 | P1 | E-09 | 主线 | b |
| **E-15** | daily plan 降频 | interval 48-72h/仅 heat 阈值，省 50-66% plan | P2 | E-09 | 主线+haiku | b |
| **E-16** | 共享全局前缀>4096 | 盈亏平衡命中率 h*≈12-18%，agent loop 远超 h* | P1 | **E-06**,01 | 主线 | b |
| **E-17** | 玩家对话增量前缀缓存 | 20 轮 input 降 40-60%（缓解 O(n²) 最强杠杆） | P1 | **E-06**,03 | 主线 | b |
| **E-18** | 静默缓存失效审计 | ≥3 处 builder 在稳定前缀注入变化内容 | P2 | - | worker | b,c |
| **E-19** | 分级模型映射 | 背景 JSON 调用占次数>70% 可降档 | P1 | E-08,06 | 主线 | b,c |
| **E-20** | decide JSON haiku vs sonnet | haiku 已≥90%，换 sonnet 边际<5% 不值 3× | P2 | E-19 | 双 agent | b |
| **E-21** | SBTI 着色块必要性 | 去块省记忆调用 input 15-25%，importance 漂移<0.1 | P2 | - | haiku-agent | b |
| **E-22** | 决策 prompt 压缩 | decide input 降 20-35% 不影响合法率 | P2 | E-09 | 主线+haiku | b |
| **E-23** | 互聊协议重构（多轮化） | persona 计费一次/轮，互聊 input 降 30-45%→>50%(缓存) | P1 | E-02,16 | 主线 | b |
| **E-24** | history 截断/滚动摘要 | O(n²)→O(n)，40 轮省 55-70%（端点无关兜底） | P1 | E-03,10 | 主线+haiku | b |
| **E-25** | max_tokens 右尺寸/截断 | 互聊回复 cap=100 对"50字"有截断风险；plan 1200/extraction 3000 虚高 | P1 | - | 主线 | b,c |
| **E-26** | 字数 vs cap 谁先约束 | haiku 遵守字数，触 cap 0-1/10 | P2 | E-25 | haiku-agent | b |
| **E-27** | JSON 解析失败率&白付 | 裸失败 5-15%，剥离围栏后<3% | P1 | - | haiku-agent | b,c |
| **E-28** | 重试放大系数 | 有效调用放大 10-30%，须按 attempt 计费 | P2 | E-06 | 主线 | c |
| **E-29** | forge 单角色一次性成本 | deep≈output 上限 11.7k，≈数十天 agent-loop 量级 | P1 | E-08 | 主线 | a |
| **E-30** | forge 两套实现双花审计 | pipeline.py 与 forge_service.py 若都在生产=2× forge | P2 | - | worker | a,c |
| **E-31** | P1-1 计量字段设计 | 落 {model,call_type,in/out,cache_*,attempts,parse_ok,scenario} | P0 | E-03,06,08 | Opus | **c** |
| **E-32** | 预算熔断+分级路由稿 | per-user 日预算+熔断降级（背景先停/降档，对话保质） | P1 | E-09,10,11,19 | Opus | **c** |

## 三、执行批次（可并行）

- **批次 0（门·立刻）**：E-06（**需 Jimmy 给部署机 .env，硬阻塞**）、E-08（纯静态）、E-18/E-30（只读审计，两 worker 并行）。E-07 待凭据。
- **批次 1（基线·E-08 后）**：E-09/E-10/E-11/E-12/E-29 主线脚本并行 → 出**交付物 a**。
- **批次 2（真实模型试跑·与批次1完全并行）**：E-05/E-20/E-21/E-26/E-27 全 haiku/sonnet-agent 并行。
- **批次 3（杠杆·基线后）**：E-04→E-13/E-14/E-15/E-22/E-23/E-25 token 数学并行。
- **批次 4（缓存·E-06=支持才做）**：E-16/E-17；若不支持则整批跳过，预算转投 E-24。
- **批次 5**：E-24、E-19。 **批次 6（汇总·Opus）**：E-31/E-32。

## 四、最高信息价值实验（5 条）

1. **E-06 端点/价格/缓存真相** — 唯一能作废半个 campaign 的实验。不先做可能精算几天缓存省钱，而中转站静默丢 `cache_control`、省钱全为 0。直接决定 a/b/c 三交付物可信度。**建议立刻向 Jimmy 索要部署机 `.env`。**
2. **E-08+E-09 完整基线** — 头条数字本身；漏计 plan+evolution+reflection 会让基线系统性偏低 ≥40%，所有 % 杠杆都乘这个基数，基数错则排序全错。
3. **E-17 玩家对话增量前缀缓存** — 直击 E-03 最痛的 O(n²) history 重发（40 轮≈$0.086/会话），长会话 40-60% 节省（前提 E-06 放行）。
4. **E-24 history 截断/滚动摘要** — 同一问题的端点无关兜底，与 E-17 构成"二选一必中一个"的对冲，长会话问题必有解。
5. **E-14 skip-decide** — decide 是频次最高的调用，30-50% tick 可跳且零质量风险，很可能是 agent loop 单条最大节省。

**额外补的老兵实验**（候选未写）：E-11 扩容悬崖、E-12 evolution 隐藏成本+缓存失效连锁、E-28 重试放大、E-29/E-30 forge 峰值与潜在双花、E-27 白付调用——都是"只看平均态"时最易漏的尖峰/最坏情况成本。

下一步我可以直接把 M-1..M-8 写进台账 §修正记录，或起草 E-06 探针脚本 + 给 Jimmy 的凭据索要清单——等你定。