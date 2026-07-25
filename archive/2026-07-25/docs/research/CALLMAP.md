Mapping complete. Here is the exhaustive LLM call-site ground truth for the cost model. Every claim cites `file:line` relative to `/Volumes/data/dev/simverse-world/backend`.

---

# Simverse Backend — LLM 调用点全景（成本建模用）

## 0. 入口与模型解析（先读这个，决定每次调用的 model / owner / max_tokens）

所有 Anthropic 文本调用最终走两条路：`app/llm/client.py` 的 `chat()`(非流式) / `stream_chat()`(流式)，或者绕过 wrapper 直接 `client.messages.create(...)` / `client.messages.stream(...)`。

- `chat()` `client.py:77-100`：`model = model or settings.effective_model`；`max_tokens = max_tokens or settings.llm_max_tokens(512)`；`system=...`；**默认 `owner="system"`**；`thinking` 默认 disabled（`settings.llm_thinking=False`）。**不传 temperature。**
- `stream_chat()` `client.py:103-123`：同上，但 `max_tokens` 永远是 `settings.llm_max_tokens(512)`（不接受覆盖）；**默认 `owner="user"`**；可注入用户自定义 key（`user_config`）。**不传 temperature。**
- `effective_model` = `llm_model or llm_default_model` = 默认 **`claude-haiku-4-5-20251001`** (`config.py:36,44-46`)。
- `get_client(owner)` `client.py:28-62`：`owner="system"` 用系统 key；`owner="user"` 且提供 `user_config.api_key` 时用用户 key，否则回落系统 key。

关键结论：**除了「玩家-NPC 聊天流式回复」「玩家-玩家自动回复」「Forge build/extract/validate/refine 阶段」用 `owner="user"` 外，其余全部 `owner="system"`，且几乎全部用默认 haiku 模型。** 配置里定义了 `system_llm_temperature=0.3 / user_llm_temperature_chat=0.7 / user_llm_temperature_forge=0.5`（`config.py:62,67-68`），但**没有任何调用点把 temperature 传进 messages API** —— 全部走 provider 默认值，建模时可忽略 temperature 差异。

---

## 1. 全部调用点清单（按场景归类）

### A. Resident tick 流水线（`app/agent/`，全部 owner=system, 默认 haiku）

| # | file:line | 阶段 | prompt builder | max_tokens | 流式 | 触发频率 |
|---|-----------|------|----------------|-----------|------|---------|
| A1 | `app/agent/phases/plan/basic.py:203` | plan | `PLAN_SYSTEM_PROMPT`/`PLAN_USER_PROMPT` 内联于同文件 `:20-66`，builder `_generate_plan` `:125-203` | 1200 | 否 | **每居民每「游戏内日」一次**（`daily_plans_json.generated_date != today` 才生成，`:82-90`） |
| A2 | `app/agent/phases/decide/basic.py:111` | decide | `build_decision_prompt` `app/agent/prompts.py:50-108`（`DECISION_SYSTEM`/`DECISION_USER` `:4-47`） | 200 | 否 | **每 tick 至多一次**，但高优先级计划(importance≥6)会 force-execute 跳过 LLM (`decide/basic.py:31-37`)；`spontaneous` 变体有几率直接产出 CHAT 不调 LLM (`spontaneous.py:27-43`) |
| A3 | `app/memory/service.py:423`（`generate_reflections`） | memorize→reflect | `REFLECT_SYSTEM/USER` `app/memory/prompts.py`，builder `service.py:401-423` | 400 | 否 | 仅 `ReflectiveMemorizePlugin`，且 `memory_created` 为真时以 **0.15 概率** 触发 (`memorize/reflective.py:19,25-28`) |

perceive (`perceive/basic.py`, `perceive/social.py`)、execute (`execute/basic.py`)、memorize-basic (`memorize/basic.py`) **全部 rule-based，无 LLM**。`decide/cautious.py`、`decide/spontaneous.py` 继承 basic，只是概率性改写结果，不额外增加 LLM 调用。

### B. Resident-Resident 自主对话（`app/agent/chat.py`，owner=system, 默认 haiku）

| # | file:line | 作用 | prompt builder | max_tokens | 频率 |
|---|-----------|------|----------------|-----------|------|
| B1 | `app/agent/chat.py:138` | 每轮对白 | `_build_chat_system` `:45-68` → `CHAT_INITIATE_SYSTEM`/`CHAT_REPLY_SYSTEM` `app/agent/prompts.py:117-144` | 100 | **每对话 num_turns 次**，num_turns=clamp(agent_chat_max_turns=8,[3,8]) → 默认 8 |
| B2 | `chat.py:144`→`memory/service.py:307` | initiator 事件抽取 | `EXTRACT_EVENTS_SYSTEM/USER` `memory/prompts.py` | 500 | 每对话 1 次 |
| B3 | `chat.py:150`→`memory/service.py:307` | target 事件抽取 | 同上 | 500 | 每对话 1 次 |
| B4 | `chat.py:159`→`memory/service.py:380` | initiator 关系更新 | `UPDATE_RELATIONSHIP_SYSTEM/USER` | 300 | 有抽出记忆才调（0/1 次） |
| B5 | `chat.py:165`→`memory/service.py:380` | target 关系更新 | 同上 | 300 | 有抽出记忆才调（0/1 次） |
| B6 | `chat.py:175` | 对话总结(广播用) | `CHAT_SUMMARY_SYSTEM/USER` `prompts.py:146-159` | 150 | 每对话 1 次 |

### C. 人格进化 hooks（`app/personality/evolution.py`，owner=system, 默认 haiku）—— 挂在 B2/B3 及玩家聊天的 extract_events 内部

在 `memory/service.py:332-349` `extract_events` 末尾条件触发，且有 guard（24h shift 冷却 / 月度预算 / drift 需 ≥15 事件）：

| # | file:line | 作用 | max_tokens | 触发条件 |
|---|-----------|------|-----------|---------|
| C1 | `evolution.py:148`（`evaluate_shift`） | 剧变评估 | 500 | 抽出记忆里有 importance≥0.9 (`service.py:336-339`)，且过 guard |
| C2 | `evolution.py:73`（`evaluate_drift`） | 渐变评估 | 400 | `count_events_since_last_reflection ≥ 15` (`service.py:344-347`)，且过 guard |
| C3 | `evolution.py:268`（`_sync_text` persona） | 重写 persona_md | 800 | C1 或 C2 产生了 validated 变更时**必调** |
| C4 | `evolution.py:293`（`_sync_text` soul） | 重写 soul_md | 500 | 仅 shift 且变更命中 `_SOUL_DIMENSIONS={S3,A3}` (`:289-291`) |

进化链是稀疏但重尾的：一旦触发，单次可叠加 2–4 个额外调用（eval + persona sync + 可选 soul sync）。

### D. 玩家-NPC WebSocket 聊天（`app/ws/handlers/chat.py`）

| # | file:line | 作用 | model / owner | max_tokens | 流式 |
|---|-----------|------|--------------|-----------|------|
| D1 | `chat.py:174`→`media/model_router.py:167`（`_stream`） | 主回复 | `effective_model`, **owner=user** | `llm_max_tokens=512` | **是** |
| D2 | `media/model_router.py:128`（`_understand_video`） | 视频理解（发视频时才有，先于 D1） | **`video_llm_model="kimi-k2.5"`**, owner=system | 512 | 否 |
| — | 图片：`model_router.py:41-44` 走 D1 同一路径，仅在最后一条 user message 注入 image block（无额外调用） | | | | |

system prompt 由 `assemble_system_prompt` `app/llm/prompt.py:40-69` 组装（soul_md+persona_md+ability_md+记忆上下文，末尾"回复不超过200字"）。

聊天**结束**后台任务 `extract_chat_memories` `chat.py:307-375`（`>=2` 条消息才跑）：
- D3 `chat.py:336`→`service.py:307` extract_events max=500（+ 嵌入 + 上述 C 进化 hooks）
- D4 `chat.py:345`→`service.py:380` update_relationship max=300（有事件才调）
- D5 `chat.py:355`→`service.py:423` generate_reflections max=400（仅 `event_count>=15`）

### E. 玩家-玩家自动回复（`app/services/player_chat_service.py`）

| # | file:line | 作用 | model / owner | max_tokens | 流式 |
|---|-----------|------|--------------|-----------|------|
| E1 | `player_chat_service.py:33`（`generate_auto_reply` → `stream_chat`） | 目标玩家 persona 自动回复 | `effective_model`, **owner=user** | 512 | 是（内部拼成整串） |

触发：`app/ws/handlers/player_chat.py:57`，仅当目标 resident `reply_mode=="auto"`（`player_chat_service.py:108-121`）。system prompt = `assemble_system_prompt(resident)`（无记忆注入）`:29`。**每条发出的消息最多 1 次。**

### F. Forge 角色生成 —— 存在两套并行实现

#### F-legacy：`app/services/forge_service.py`（被 `/forge/answer`、`/forge/quick` 端点使用，`routers/forge.py:74,122`）；全部 `get_client()`=system, `effective_model`

5 步引导管线 `run_generation_pipeline` `:262-370`：
- F1 ability `forge_service.py:282` max=1500
- F2 persona `:292` max=2000
- F3 soul `:303` max=1500
- F4 `_score_quality` `:540` max=200
- F5 `_assign_district` `:579` max=100
- F6 `compute_sbti`（见 G1）

Quick 管线 `run_quick_pipeline` `:373-520`：
- F7 单次抽三层，**在子进程里** `client.messages.create` `forge_service.py:420` max=**4096**（`:394-443`，为绕开 uvicorn event-loop TLS 问题）；评分走 fallback 不调 LLM
- F6 `compute_sbti`（见 G1）

prompt 来自 `app/llm/forge_prompts.py`（`ABILITY_/PERSONA_/SOUL_/SCORING_/DISTRICT_/QUICK_EXTRACT_*`）。

#### F-pipeline：`app/forge/pipeline.py`（被 `/forge/deep-start` 使用，`routers/forge.py:157,177`）；prompt 全在 `app/forge/prompts.py`

`start()` 无论 quick/deep 都先跑：
- F8 Router `app/forge/router_stage.py:18` max=200, **system_client**

Quick 模式（`_run_quick` `pipeline.py:133-150`）：
- F9×3 BuildStage `app/forge/build_stage.py:57` max=2000（ability/persona/soul 各一次）, **user_client**

Deep 模式（`_run_deep` `pipeline.py:152-228`）：
- Research 阶段 `app/forge/research_stage.py`：**无 LLM**，是 SearXNG 网页检索（6 维 × 2 query = 12 次 HTTP，`research_stage.py:56-70`）
- F10 Extraction `app/forge/extraction_stage.py:14` max=3000, user_client
- F9×3 Build（同上）max=2000×3
- F11 Validation `app/forge/validation_stage.py:20` max=2000, user_client
- Refinement `app/forge/refinement_stage.py`：F12 optimizer `:28` max=1000 + F13 creator `:36` max=1000 + F14×3 `_refine_layer` `:77` max=2500×3（ability/persona/soul）

### G. 按需/独立 LLM 服务（owner=system, effective_model）

| # | file:line | 作用 | prompt | max_tokens | 调用方 |
|---|-----------|------|--------|-----------|--------|
| G1 | `app/services/sbti_service.py:205`（`compute_sbti`） | 15 维人格评级 | `SBTI_ANALYSIS_SYSTEM/USER` 内联 `:122-177` | 200 | forge×2、`routers/residents.py:145`(import) `:251`(编辑时重算)、`routers/admin/residents.py:144` |
| G2 | `app/services/sprite_service.py:108`（`match_sprite_by_persona`） | 从 persona 抽外貌属性 | `SPRITE_MATCH_SYSTEM_PROMPT` `:96-101` | 100 | `routers/sprites.py:25` |
| G3 | `app/services/skill_import_service.py:75`（`convert_to_standard`） | 非标准 Skill → 三层 | `CONVERSION_SYSTEM_PROMPT` `:42-51` | 4096 | 导入非标准格式时（标准格式直接解析，不调 LLM `:67-68`） |

### H. 非 Anthropic 辅助调用（成本单列，勿混入 token 模型）

- **Embedding**：`app/memory/embedding.py:17,58` → 本地 **Ollama** `/api/embed`（`ollama_embed_model="qwen3-embedding:4b"`, 1024 维）。**每条抽出的 event 记忆各调一次** `memory/service.py:320`。自托管，非按 token 计费。
- **Portrait**：`app/services/portrait_service.py:66` → httpx POST 到 **Gemini image**（`portrait_llm_model="gemini-3-pro-image-preview"`）。图像生成计价，非文本 token。调用方 `routers/avatar.py:42`、`routers/settings.py:270`。
- 注意：`dashboard.py:129`、`research_stage.py:59`、`github_auth.py`、`linuxdo_auth.py`、`settings_service.py:284`、`forge_monitor.py:107` 里的 `get_client()` 是 **`app/http.py` 的 httpx client，不是 LLM**。

---

## 2. 关键场景的精确调用序列

### 一次 Resident-Resident 对话（`resident_chat` `chat.py:71-217`，默认 num_turns=8）
1. 前置检查（冷却/忙碌）→ 无 LLM
2. **8 次**对白 `chat.py:138`（max=100，交替 initiator/target）
3. `extract_events(initiator)` `:144`→`service.py:307`（max=500）+ 每条记忆 1 次 Ollama 嵌入 + 条件进化 hooks(C1/C2→C3/C4)
4. `extract_events(target)` `:150`（同上）
5. `update_relationship(initiator)` `:159`（max=300，有记忆才调）
6. `update_relationship(target)` `:165`（max=300，有记忆才调）
7. `summary` `:175`（max=150）

**基线（不含进化）：8 + 2 + (0–2) + 1 = 11–13 次 haiku 调用/对话。** 进化触发时每居民再 +2–4 次。

### 一次 Resident tick（`resident_tick` `tick.py:32-70`，phases 顺序 perceive→plan→decide→execute→memorize）
- perceive：0
- plan：0（计划新鲜）或 1（每游戏日首次，max=1200）
- decide：0（高优先级计划 force-execute / spontaneous 直出 CHAT）或 1（max=200）
- execute：0
- memorize：0（basic）或 15% × 1（reflective, max=400）
- **典型 tick = 0–1 次 LLM**；若 decide 产出 `CHAT_RESIDENT`，`loop.py:172` 会另起一整场 `resident_chat`（+11–13 次）。日上限 `agent_max_daily_actions=20` 个 action/居民 (`tick.py:29`)。

### 一次玩家-NPC 聊天消息（`handle_chat_msg` `chat.py:112-236`）
- （无媒体）**1 次流式** `chat.py:174`（effective_model, owner=user, max=512）
- （图片）同 1 次流式 + image block
- （视频）**先 1 次 kimi-k2.5** 理解 `model_router.py:128`（max=512）**再 1 次主流式**
- 结束时后台：extract_events(500) + update_relationship(300, 条件) + reflections(400, 仅≥15事件) + 嵌入 + 条件进化

### 一次玩家-玩家消息（`handle_player_chat` `player_chat.py:16-73`）
- manual 模式：0 LLM（转发/入队）
- auto 模式：**1 次流式** `generate_auto_reply` `player_chat_service.py:33`（effective_model, owner=user, max=512）

### Forge（两条路都要建模，取决于端点）
- `/forge/quick`（legacy F7）：**1 次(max=4096) + compute_sbti 1 次(max=200)** = 2 次
- `/forge/answer` 5 步（legacy）：F1–F5 = **5 次 + compute_sbti = 6 次**
- `/forge/deep-start` quick：Router(200) + Build×3(2000) + compute_sbti(200) via `_create_resident`? —— 注意 `pipeline.py:94-131` 的 `_create_resident` **未调 compute_sbti**，只写 `meta_json={"origin":"forge"}`。所以 pipeline-quick = **1 + 3 = 4 次**
- `/forge/deep-start` deep：Router(200) + Extraction(3000) + Build×3(2000) + Validation(2000) + Refine[optimizer(1000)+creator(1000)+apply×3(2500)] = **1+1+3+1+5 = 11 次**，Research 阶段 0 LLM（12 次 SearXNG HTTP）

---

## 3. 配置值（成本模型输入，`app/config.py`）

- `agent_tick_interval = 60` 秒/轮 (`:90`)
- `agent_max_concurrent = 5`（`asyncio.Semaphore`，`loop.py:82`）(`:91`)
- `agent_max_daily_actions = 20` /居民/游戏日 (`:92`, 用于 `tick.py:29`)
- `agent_chat_max_turns = 8`（对话轮数上限，clamp 到 [3,8]，`chat.py:104`）(`:93`)
- `agent_chat_cooldown = 1800` 秒（同一对居民再聊冷却）(`:94`)
- `llm_max_tokens = 512`（默认输出上限，流式恒用此值）(`:37`)
- `llm_default_model = claude-haiku-4-5-20251001` (`:36`)
- `llm_thinking = False`（thinking 关闭）(`:38`)
- `user_llm_concurrency = 5`（配置存在，但代码未见 semaphore 实际使用）(`:71`)
- temperature：`system=0.3 / user_chat=0.7 / user_forge=0.5`（`:62,67-68`）**均未接入调用** 
- `video_llm_model = kimi-k2.5` (`:77`)；`portrait_llm_model = gemini-3-pro-image-preview` (`:56`)
- tick 轮内还有调度 `should_tick`（SBTI 作息，`loop.py:92`）与 `status.not_in(["sleeping"])` 过滤（`loop.py:73-74`），并非每居民每轮都进入 tick。

---

## 4. Prompt 注入规模（上下文膨胀边界，影响 input token）

- **玩家-NPC / 玩家-玩家 system prompt**（`assemble_system_prompt` `prompt.py:40-69`）：全量注入 `soul_md + persona_md + ability_md`（无截断），外加记忆块 `format_memory_context`（`:4-37`：1 条 relationship 全文 + reflections 全部 + events 全部）。记忆检索规模由 `retrieve_context` 默认 `max_events=10, max_reflections=3` 决定（`service.py:200-201,213-216`）。历史消息 `ctx.chat_messages` **无长度上限**，整段会话累积（`chat.py:167,197`）——长会话是 input token 的主要增长源。
- **tick decide**（`build_decision_prompt` `prompts.py:50-108`）：注入最近记忆 `memories[:8]`（`prompts.py:83`）、附近居民列表、今日行动 `today_actions[-10:]`（`:86`）。上游 `_load_memories` 取 `limit=10`（`decide/basic.py:117`）。
- **plan**（`plan/basic.py:141-201`）：persona 截断 `[:200]`（`:182`）、recent 记忆 top5、relationships top3、yesterday top5（`:145-159`）—— 规模有界。
- **resident-resident 对白**：persona_md 全量 + relationship 全文 + `history=dialog_lines[-6:]`（`chat.py:126`，只带最近 6 行）—— 每轮上下文小而恒定。
- **Forge extraction/build**：注入 `research_text`（deep 模式可能很大，含 12 条检索结果全文，`research_stage.py:72-89`）；skill 转换截断 `[:8000]`（`skill_import_service.py:84`）；sprite 截断 `[:2000]`（`sprite_service.py:112`）。
- 记忆软上限：`evict_memories(max_events=500)` `service.py:164` —— 单居民 event 记忆最多 500 条，但检索每次只取 ≤10 条进 prompt。

---

## 5. 现有 token 计量

**几乎没有真实 token 计量。** 唯一一处：`app/ws/handlers/chat.py:212` `fresh_conv.tokens_used += len(full_reply)` —— 用**回复字符数**当代理，写入 `Conversation.tokens_used`（`app/models/conversation.py:16`）。**没有任何地方读取 `response.usage.input_tokens/output_tokens`**（grep 全仓 `usage.`/`input_tokens`/`output_tokens` 无命中）。`token_cost_per_turn`（`models/resident.py:17`，默认 1）是**灵魂币计费**单位，与真实 token 无关，用于 `charge()`（`chat.py:122,138`、`player_chat_service.py:110`）。

建模含义：要精确成本必须自行按各调用点的 `max_tokens` 上限 + prompt 组装规模估算，仓库里没有可直接采信的历史 token 用量数据。

---

需要我把这份清单转成一张"每场景 → 调用次数 × max_tokens × 模型 × owner"的可计算成本表（含默认触发概率与频率系数）供直接代入定价吗？

=====

我已完成全部映射。按你要的 4 个优先项返回精炼版（跳过 chat.py / prompts.py / llm/prompt.py / ws/handlers/chat.py 细节）。路径相对 `/Volumes/data/dev/simverse-world/backend`。

# LLM 调用点报告（成本模型 ground truth）

所有调用默认走 `app/llm/client.py:chat()`(非流式, `client.py:77`) / `stream_chat()`(流式, `client.py:103`)，模型默认 `effective_model = claude-haiku-4-5-20251001`(`config.py:36,45`)，`thinking` 关闭，**不传 temperature**（配置里的 temperature 全部未接入）。

## (1) Resident tick 逐阶段 LLM 序列

`loop.py:_tick_round` → `tick.py:resident_tick:32` → `registry.get_phases` 顺序执行 perceive→plan→decide→execute→memorize：

| 阶段 | file:line | 是否 LLM | max_tokens | 触发条件 |
|------|-----------|---------|-----------|---------|
| perceive | `phases/perceive/basic.py:20`、`social.py:22` | **否**（DB 查邻近居民） | — | — |
| plan | `phases/plan/basic.py:203` | 是 | 1200 | 仅 `daily_plans_json.generated_date != today`，即**每居民每游戏日一次**(`plan/basic.py:82-90`) |
| decide | `phases/decide/basic.py:111` | 是 | 200 | **每 tick ≤1 次**；但 importance≥6 的计划 force-execute 跳过 LLM(`decide/basic.py:31-37`)；`spontaneous.py:27-43` 变体有几率直出 CHAT 不调 LLM |
| execute | `phases/execute/basic.py:20` | **否**（寻路/移动/状态） | — | — |
| memorize | `phases/memorize/basic.py:57` | **否** | — | rule-based 写 event 记忆 |
| memorize(reflective) | `memorize/reflective.py:28`→`memory/service.py:423` | 是 | 400 | 仅 `ReflectiveMemorizePlugin`，`memory_created` 且 **概率 0.15**(`reflective.py:19,25`) |

**典型 tick = 0–1 次 LLM(decide)**，偶尔 +1(plan，每日一次) 或 +1(reflection，15%)。若 decide 产出 `CHAT_RESIDENT`，`loop.py:172` 另起整场 resident-resident 对话（11–13 次，见下）。

## (2) MemoryService LLM 调用（`app/memory/service.py`，owner=system, haiku）

| 方法 | file:line | max_tokens | 检索/注入规模 | 触发 |
|------|-----------|-----------|--------------|------|
| `extract_events` | `service.py:307` | 500 | 输入=对话全文；输出每条 event 再调 1 次 Ollama 嵌入(`service.py:320`) | 每次对话结束（resident-resident ×2，玩家聊天 ×1） |
| `update_relationship_via_llm` | `service.py:380` | 300 | 注入当前关系全文 + event_summaries 列表 | 有抽出 event 才调 |
| `generate_reflections` | `service.py:423` | 400 | 注入 `event limit=20` + `relationship limit=10`(`service.py:403-404`) | `count_events_since_last_reflection ≥ 15` |
| `retrieve_context` | `service.py:193` | **无 LLM**（纯 DB） | 默认 `max_events=10, max_reflections=3`(`service.py:200-201`) + 1 条 relationship；`_search_events` 无向量则按 importance+recency 取 limit | chat 开始时组 prompt 用 |

进化 hook 挂在 `extract_events` 末尾(`service.py:332-349`)，条件+guard 触发 `app/personality/evolution.py`：shift `evolution.py:148`(500)、drift `:73`(400)、persona 同步 `:268`(800，变更即必调)、soul 同步 `:293`(500，仅 shift 命中 {S3,A3})。稀疏但重尾：触发一次叠加 +2–4 调用。

**一次 resident-resident 对话总量**（`agent/chat.py`，num_turns 默认 8）：对白 8×(max100) + extract_events×2(500) + update_relationship 0–2(300) + summary 1(150) = **11–13 次**，不含进化。

## (3) Forge 管线调用次数 —— 两套并行实现

**F-legacy `app/services/forge_service.py`**（端点 `/forge/answer`、`/forge/quick`），全 owner=system, effective_model：
- 5 步引导 `run_generation_pipeline:262`：ability`:282`(1500) + persona`:292`(2000) + soul`:303`(1500) + score`:540`(200) + district`:579`(100) + `compute_sbti`(200) = **6 次**
- Quick `run_quick_pipeline:373`：**子进程内** 单调 `:420`(max=**4096**) + `compute_sbti`(200) = **2 次**（评分走 fallback 不调 LLM）

**F-pipeline `app/forge/pipeline.py`**（端点 `/forge/deep-start`），prompt 在 `app/forge/prompts.py`，Router 用 system_client，其余 user_client：
- Router `router_stage.py:18`(200) 无论模式都跑
- **Quick 模式**：Router + BuildStage×3 `build_stage.py:57`(2000) = **4 次**（注意 `_create_resident:94` 未调 compute_sbti）
- **Deep 模式**：Router(200) + Extraction `extraction_stage.py:14`(3000) + Build×3(2000) + Validation `validation_stage.py:20`(2000) + Refine[optimizer`:28`(1000)+creator`:36`(1000)+apply×3`:77`(2500)] = **11 次**；Research 阶段 **0 LLM**（`research_stage.py` 是 12 次 SearXNG HTTP）

**独立 SBTI/sprite/skill 调用**：`sbti_service.py:205`(compute_sbti, 200)、`sprite_service.py:108`(match_sprite, 100)、`skill_import_service.py:75`(convert, 4096)，均 owner=system。

## (4) 频率/配置模型（`app/config.py`）

- `agent_tick_interval=60`s(`:90`)、`agent_max_concurrent=5` semaphore(`:91`, `loop.py:82`)
- `agent_max_daily_actions=20` /居民/游戏日(`:92`, `tick.py:29`)
- `agent_chat_max_turns=8`（clamp [3,8]，`chat.py:104`)(`:93`)、`agent_chat_cooldown=1800`s(`:94`)
- `llm_max_tokens=512`（流式恒用此值）(`:37`)、`llm_thinking=False`(`:38`)
- tick 前有 `should_tick`(SBTI 作息) + `status.not_in(["sleeping"])` 过滤(`loop.py:73,92`)，非每居民每轮都进入

## owner / 模型归属（成本归因）
- **owner=user**（可走用户自带 key）：玩家-NPC 流式回复(`media/model_router.py:167`, 512)、玩家-玩家自动回复(`player_chat_service.py:33`, 512)、Forge pipeline build/extract/validate/refine 阶段
- **owner=system**：其余全部（tick、resident-resident、memory、personality、sbti、sprite、forge router、forge-legacy）
- **非 Anthropic**：Ollama 嵌入(`memory/embedding.py:17`)、Gemini 画像(`portrait_service.py:66`)、kimi-k2.5 视频理解(`model_router.py:128`, 512)

## Token 计量现状
**无真实 token 计量。** 唯一一处 `ws/handlers/chat.py:212` 用 `len(full_reply)` 字符数当代理写入 `Conversation.tokens_used`。全仓无 `response.usage` 读取。`token_cost_per_turn`(`models/resident.py:17`) 是灵魂币计费单位，与真实 token 无关。建模须自行按各点 max_tokens + prompt 组装规模估算。

需要我把这些汇成一张「场景 → 次数×max_tokens×模型×owner×频率系数」的可计算成本表吗？