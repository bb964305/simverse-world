# 修复报告：生产 NPC 图片理解不可用（P1-1）

> 分支：`fix/npc-vision`（基于 `port/prod-fixes-onto-044`，worktree `../sv-vision`）
> 日期：2026-07-24
> 关联：`docs/testing/TEST_REPORT_2026-07-24.md` §4 P1-1、`§7` 回归要求
> 状态：代码 + 测试完成，未合并 / 未 push / 未部署

## 1. 结论先行

- 采用**方案 (b) 理解前置 + 混合注入**：图片先经视觉模型转成文字描述，注入原对话链；同时保留原 image block（视觉中转仍受益）。对现有链路侵入最小，主流式路径 `_stream()` 零改动。
- 视觉模型名走新环境变量 `SV_VISION_MODEL`（`os.environ` 读取，**未改 `config.py`**）；未配置时前置逻辑完全休眠，行为与现状一致。
- 失败优雅回落、视觉调用接入 `llm_usage` 计量与预算熔断，均已单测覆盖。
- 改动范围内新增 6 个测试全绿；全量 pytest 无新增失败（相对 lab-v2 基线零回归）。

## 2. 根因确认过程

| 步骤 | 证据 | 结论 |
|---|---|---|
| 读测试报告 P1-1 | 相对/绝对 URL、两个居民均回复"没有视觉能力"；HTTP 与聊天流本身成功 | 不是上传/URL 问题，是图片内容未被模型消费 |
| 读 `model_router.py` | `_inject_image`（旧 :62-96）把图包成 image block；`_stream`（旧 :163-175）**无条件** `resolved_model = settings.effective_model` | image 分支与 no-media 分支用同一个模型，图片路径没有任何"视觉模型"路由 |
| 读 `config.py:93-94` | `effective_model = llm_model or llm_default_model`，单一模型 | 生产实际是 qwen3.7-plus（百炼中转，见 `config.py:100-102` F-02 注释），文本模型 |
| 读 `_inject_image` 源块 | `{"type":"image","source":{"type":"url","url":image_url}}`，`image_url` 是 `/static/uploads/...` 相对路径 | 即便中转有视觉能力，也取不到内网相对 URL |

**根因（已确认）**：图片被包成 image block 后仍发往不具备视觉能力的 `effective_model`，且注入的是中转无法获取的相对 URL —— 图片内容从未进入模型可消费的形态。与报告的高置信推断一致。

## 3. 方案

### 3.1 为什么选 (b) 而非 (a)

- (a) 视觉路由：要把 image 消息整条改道到另一个模型流式，破坏 `_stream()` 与主模型（人格/记忆/预算）的统一路径，侵入大。
- (b) 理解前置：新增一个"图 → 文字描述"的 prepass（完全复用已存在的 `_understand_video` 计量模式），把描述注入回原对话链，主模型/人格/记忆/计量路径**不动**。侵入最小，且描述是纯文本，任何文本模型都能消费。

### 3.2 混合注入

`chat_with_media` 的 image 分支改为：先 `_understand_image()` 拿描述 → `_inject_image_description()` 把描述以「AI 视觉识别」note 形式并入用户消息文本 → 再 `_inject_image()` 保留 image block。这样：
- 文本主模型：靠描述文字"看见"图片内容；
- 视觉中转（若日后启用）：仍收到 image block；
- 两个断言 image block 的既有测试保持绿。

### 3.3 关键设计点

- **模型名来源**：`_vision_model()` 读 `os.environ["SV_VISION_MODEL"]`，空/未设 → 返回 None → 前置休眠（不改 `config.py`，满足约束）。
- **本地图取盘**：`_image_source()` 对 `http(s)://` 直传 url source；对 `/static/uploads/...` 用 `MediaService.get_file_path()` 读盘 + `sniff_image_type()` 嗅探类型 + base64 内联（中转取不到内网 URL）。读不到 → 返回 None → 回落。
- **计量**：`_understand_image()` 成功路径必过 `record_usage(scenario="image", model=<vision>, owner="system", ...)`，透传 meter 的 resident/user/conversation，与 video 前置同款，**不绕过 Meter**。
- **预算熔断**：`_vision_budget_blocked()` 用**独立短会话**查 `background_tier()`，`PLAYER_ONLY`（全局预算耗尽）→ 跳过前置省钱降级；查询异常 → fail-open（不挡对话）；会话不跨 LLM 调用持有。
- **失败回落**：视觉调用任何异常（网络/超时/坏响应/读盘失败）→ 返回空描述 → 只走 image block 的 legacy 行为，对话不崩。
- **计量不漏（对抗评审加固）**：`create()` 计费成功后，`extract_text` 的响应解析用**独立 try** 包裹（防中转返回 `content=None`/`block.text=None` 时在 `record_usage` 之前抛异常而漏计）；`record_usage` 在解析之后无条件执行。
- **预算准确性（对抗评审加固）**：estimated 回退路径（中转不返回 `usage` 时）的 `est_input_tokens` 计入图片粗估 `_VISION_IMAGE_TOKEN_EST=1600`，避免 vision 开销被记成 ~0 导致断路器系统性漏算。
- **读盘防越界（对抗评审加固）**：`_image_source` 只读 `/static/uploads/` 前缀的本地路径，其余（如 `/etc/...`、`../`）在触盘前拒绝，杜绝经前置的任意文件读取。

## 3.4 对抗评审（adversarial review）结论

派独立 subagent 逐条核验 7 项声明：**5 项直接成立**（失败回落 / 预算熔断跳过 & fail-open & 不跨会话持连接 / 本地图 base64 & http 直传 / 描述注入位置 & 顺序 / 无循环导入 & 既有测试全绿）。修复 2 项：
- **Claim 1（计量可绕过）**：成功计费的 `create()` 后若 `extract_text().strip()` 抛异常会跳过 `record_usage` → 已按上文独立 try 修复，新增 `test_malformed_vision_response_still_metered` 断言畸形响应仍计量。
- **预算计量 bug**：图片 token 未计入估算 → 已加 `_VISION_IMAGE_TOKEN_EST` 并断言 `input_tokens >= 1600`。

低危提示已处理：读盘越界加前缀白名单（`test_non_upload_local_path_refused`）。**video 路径存在同构的"提取失败漏计"问题，但属预存、超出本次 scope，已在 §8 记录建议后续单独修**，本次不动 video 代码以免影响既有 video 测试。

## 4. 改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `backend/app/media/model_router.py` | +~185 行 | 模块 docstring 更新；import `base64`/`os`/`BudgetTier`/`background_tier`/`async_session`；`chat_with_media` image 分支加前置；新增 `_inject_image_description`/`_vision_model`/`_image_source`/`_vision_budget_blocked`/`_understand_image`；常量 `_VISION_SYSTEM`/`_VISION_PROMPT`/`_VISION_IMAGE_TOKEN_EST`；对抗评审加固（计量独立 try、图片 token 估算、读盘前缀白名单） |
| `backend/app/llm/metering.py` | +1/-1 | `SCENARIOS` 集合加 `"image"`（与 `"video"` 并列） |
| `backend/tests/test_vision_understanding.py` | 新增 | 8 个测试：描述注入+保留 block / 失败回落 / 未配置 legacy / 计量 scenario=image / 预算 PLAYER_ONLY 跳过 / E2E 断言 NPC 回复含图中颜色+物体 / 畸形响应仍计量 / 非上传路径拒绝读盘 |
| `backend/uv.lock` | 变更 | `uv sync --extra dev` 在裸 worktree 补齐 dev 依赖（pytest 等），非业务改动 |

**未改动**：`config.py`（只读约束）、`.env.example`、`service.py`、`ws/handlers/chat.py`、任何既有测试。

## 5. 收口时需写入 `config.py` / `.env.example` 的配置清单

> 本次按约束用 `os.environ` 读 `SV_VISION_MODEL`，未改 config。收口（正式合并线）建议二选一，**避免踩 `test_env_example_consistency` 一致性门**：

| 配置项 | 建议默认 | 说明 |
|---|---|---|
| `SV_VISION_MODEL` | `""`（空=禁用前置） | 视觉理解模型 id（如 `qwen-vl-max` / `qwen2.5-vl-72b-instruct` 等中转支持的视觉模型） |

两种收口姿势（`tests/test_env_example_consistency.py` 只识别 `KEY=` 行、跳过 `#` 注释）：

1. **提升为 Settings 字段（推荐，与 portrait/tts/video_llm_model 一致）**：在 `config.py` 加 `vision_llm_model: str = ""`，`.env.example` 加 `VISION_LLM_MODEL=`，并把 `model_router._vision_model()` 改读 `settings.vision_llm_model`。这样两条一致性断言都过。
   - 可选：`vision_llm_base_url` / `vision_llm_api_key`（若视觉模型与主 LLM 不同端点/密钥；当前实现复用 `get_client("system")` 即主系统端点）。
2. **保持纯 env 变量**：`.env.example` 里只能以**注释**形式写 `# SV_VISION_MODEL=qwen-vl-max`（不能写成 `SV_VISION_MODEL=`，否则 `test_every_example_key_is_a_settings_field` 因它不是 Settings 字段而失败）。

> 视觉模型定价：若 `SV_VISION_MODEL` 不在 `app/llm/pricing.py::_PER_MTOK`，`compute_cost` 会回落 Haiku 档（$1/$5，见 pricing.py 头注），spend 仍被计入而非归零；如需精确成本，收口时按中转实际视觉模型资费补一行 `_PER_MTOK`。

## 6. 生产验证步骤

前置：在生产/staging 后端环境设 `SV_VISION_MODEL=<中转支持的视觉模型id>` 并重启（或收口后走 `VISION_LLM_MODEL`）。

1. **相对 URL（WS-08 复测）**：上传图片得 `/static/uploads/images/<uuid>.png`；WS `chat_msg` 带 `media_type=image` + 该相对 URL。断言：NPC 回复**含图中已知颜色/物体**，不再出现"没有视觉能力"。
2. **绝对 URL 诊断**：同图换 `https://simverse-api.proxypool.eu.org/static/...` 绝对 URL 重试，断言同上。
3. **计量核对**：查 `llm_usage` 该对话窗口出现 `scenario='image'` 且 `model=<vision>` 的行（`cost_usd > 0`），另有 `scenario='player_chat'` 行 —— 证明前置未绕过计量。
4. **失败回落**：临时设 `SV_VISION_MODEL=<不存在的模型>` 或断视觉端点，发带图消息，断言对话仍正常返回文本（走 legacy），不崩、不 5xx。
5. **预算降级**：当全局日预算触 100%（`BudgetTier.PLAYER_ONLY`）时发带图消息，断言无新增 `scenario='image'` 计量行，但玩家可见回复仍产出。
6. **回归**：`SV_VISION_MODEL` 未配置时发带图消息，确认行为与本次修复前一致（仅 image block，不新增视觉调用）—— 保证灰度可回滚。

## 7. 测试证据

```
# 范围内（新增 + 既有 media 相关）
uv run pytest tests/test_vision_understanding.py tests/test_model_router.py \
  tests/test_media_chat_integration.py tests/test_chat_optimizations.py -q
→ 17 passed, 1 warning

# 修复范围内（新增 + 既有 media/计量相关）
uv run pytest tests/test_vision_understanding.py tests/test_model_router.py \
  tests/test_media_chat_integration.py tests/test_chat_optimizations.py \
  tests/test_llm_usage.py -q
→ 39 passed, 1 warning

# 全量（相对基线核对，对抗评审加固前）
uv run pytest -q -p no:cacheprovider
→ 55 failed, 1557 passed, 25 skipped, 11 deselected, 17 errors
  全部 FAILED/ERROR 均为 test_lab_* 与 tests/integration/*_postgres.py
  （需 redis/testcontainers 的预存 lab-v2 基线，含 test_env_example_consistency
   的 lab_egress/lab_artifact 陈旧键 —— 与本次改动无关）。
  非 lab/postgres 的新增失败：0。
```

TDD red 证据：实现前 `test_vision_understanding.py` 5 failed / 1 passed（未配置 legacy 用例天然通过，因当时代码正是该行为）。

## 8. 后续 / 遗留

- **video 前置同构漏计（预存，超本次 scope）**：`_understand_video`（`model_router.py`）同样在 `extract_text(resp)` 抛异常时会跳过 `record_usage`。本次为控制 scope 未改 video 代码（避免影响既有 video 测试）。建议后续单独一个小 PR 用同样的"独立 try + 计量后置"姿势修 video 路径。
- **视觉模型定价行**：收口时若确定 `SV_VISION_MODEL` 具体模型，按中转实际视觉资费在 `app/llm/pricing.py::_PER_MTOK` 补一行，避免回落 Haiku 档导致成本口径偏差。
- **既有测试环境变量隔离**：`tests/test_model_router.py::test_image_routes_to_main_model_with_image_block` 未隔离 `SV_VISION_MODEL`；当前因未配置而行为不变，但若 CI 全局设了该变量控制流会变（属既有测试的脆弱性，本次未改既有测试）。新测试已用 `monkeypatch` 规避。
