# Simverse World 生产版本功能测试报告

> 执行时间：2026-07-24 00:00–00:36（Asia/Shanghai）
> 被测版本：当前生产部署（前端资源 `index-chPSWG70.js`）
> 被测地址：`https://simverse.world` · `https://simverse-api.proxypool.eu.org` · `wss://simverse-api.proxypool.eu.org/ws`
> 执行方式：HTTPS API 冒烟、真实 WebSocket、Playwright Chromium、截图重复加载、有限时长观察

## 1. 发布结论

**结论：有条件通过，不能宣称全功能验收通过。**

登录、核心 REST、权限边界、静态媒体、账号注销、WebSocket 实时链路、快速/深度 Forge、AgentLoop 和前端主要页面均可用。唯一确认的高优先级问题是 **NPC 图片理解不可用**：图片上传和静态 URL 已修复，但带图片的 NPC 对话在相对 URL、绝对 URL 以及两个测试居民上都明确回复“没有图像识别/视觉处理能力”。这直接阻断测试计划中的 M5-5 多模态功能。

该问题不是 P0 基础设施故障，不需要据此判断整个站点不可访问；但在视觉能力是产品承诺的前提下，建议在修复并回归 M5-5 前不要将本版本标记为“全功能通过”。

## 2. 模块结果

| 模块 | 结果 | 证据摘要 |
|---|---|---|
| 基础设施与安全 | PASS | `/health` 200；前端 200；OpenAPI 146 paths；CORS 仅允许正式前端；`/metrics` 未授权 401；未知路径结构化 404；TLS 有效 |
| 认证与账号 | PASS | 注册、登录、错密 401、伪造 token 401；新 disposable 账号注销 200，旧 token 和密码均失效 |
| REST 只读与设置 | PASS | 26 个核心 GET 端点全部 200；设置六分区可读；无赛季排行榜返回 200 空结构 |
| 静态媒体与头像 | PASS | 上传返回 URL，图片 GET 200/45,102 bytes；头像 GET 200/6,873 bytes；上传文件 SHA-256 完全一致 |
| WebSocket 实时 | PASS（9/11；2 项为同一缺陷） | 鉴权、错误帧恢复、双客户端移动广播、对话开始/结束、非法聊天校验、注入防护通过；两项图片理解失败 |
| Forge | PASS | 快速 Forge 完成；深度 Forge 依次经过 extracting/building/validating/refining 并到 `done`，不再卡死 |
| AgentLoop | PASS（观察项） | 约 9 分钟前后已有居民坐标/状态变化；`/feed` 为空，暂记配置/事件窗口观察，不判失败 |
| 前端 E2E | PASS | 15/15 有效检查通过；主要路由、WebSocket、Lab 隐藏、管理员门禁、移动端无横向溢出、资产和网络错误检查均通过 |
| Lab | PASS（按配置禁用） | `/lab/researchers` 200 空列表；普通账号 `lab_enabled=false`；桌面/移动入口均隐藏 |
| 性能抽查 | 观察 | 长连接采样 `/health` p95 325 ms、`/residents` p95 523 ms；冷请求最大约 1.6 s/2.2 s，未见 5xx |

## 3. 关键验证证据

### 3.1 基础设施、TLS、API 和权限

- `GET /health` → `200 {"status":"ok"}`。
- `GET /` → 200 HTML；当前 manifest 载入 `index-chPSWG70.js`、`gameStore-6R2wzXAN.js` 等资源。
- `GET /openapi.json` → 200，标题 `Simverse World API`，146 条路径。
- 从 `https://simverse.world` 发起 CORS 预检 → 200，`access-control-allow-origin` 精确匹配正式前端；恶意 Origin → 400 且无 ACAO。
- `/metrics` 无 token → 401；随机路径 → 404 JSON；OAuth 两个入口均正确 307 跳转。
- 10 个未授权受保护接口全部 401；14 个普通账号 `/admin/*` 读取接口全部 403。
- 错误登录/缺字段 JSON → 422；伪造 token → 401；Unicode、路径样式搜索均未泄露异常；目录穿越探测未返回文件内容。
- TLS：`simverse.world` 证书到期 `2026-10-14`；API 证书到期 `2026-09-24`，均未临期。

### 3.2 账号、静态资源和修复回归

注销回归使用一次性账号：注册 200 → 错误邮箱确认 400 → 正确 `DELETE /settings/account` 200 → 原 token `/users/me` 401 → 原密码登录 401。此前的生产 500 已消失。

使用专用账号上传 `target-full.png`：

- URL：`/static/uploads/images/9eac8af3-6375-4503-ab01-2db10e56bb58.png`
- GET：200，`image/png`，45,102 bytes
- 源文件与下载文件 SHA-256：`72f742f82de50e8a09459dbdb04f9689423a1883eaf822ed14eecb22738ac0d5`

头像生成也返回可访问 PNG：`/static/portraits/eb3d2091-3987-45b5-acd4-523000ad7dec.png`，512×512，6,873 bytes。由此确认此前 `/static/*` 404 的部署问题已修复。

### 3.3 Forge 与后台行为

- 快速路径 Forge ID `b96dc6b3-76ba-4cbd-bd49-52a2fed71c46` 最终 `done`，居民“部署回归图灵0724”已入住。
- 深度路径 Forge ID `0a2cd7d8-67c4-4d53-bc26-29be88f991ee` 依次出现 `extracting → building → validating → refining → done`；稳定复查仍为终态。居民“格蕾丝·霍珀”详情 200，persona/soul/ability 均非空。
- 居民快照从 24 增至 26（增量来自本次测试居民）；既有居民“夜风侦探-46ff1f”坐标由 `(109,27)` 变为 `(112,30)`，状态由 `walking` 变为 `idle`，证明 AgentLoop 在运行。`/feed` 仍为空，暂作为无事件窗口/配置观察项。

### 3.4 WebSocket 与前端

WebSocket 真实测试结果：

- malformed 首帧和伪造 token 均 close `4001`；合法鉴权收到 `auth_ok`、`spawn_position`。
- 认证后发送非法帧会断开；新连接可重新鉴权，恢复链路通过。
- 双客户端移动广播通过；缺少 `text` 的聊天帧返回 `Invalid message format`；结束对话返回 `chat_ended`。
- 提示词注入测试未泄露 `sk-`、`JWT_SECRET`、`LLM_API_KEY` 或系统提示词标记。

浏览器有效结果为 15/15 PASS：落地页、登录、游戏 Canvas/WebSocket、`/forge`、`/profile`、`/seasons`、`/debates`、`/capsules`、`/graph`、管理员门禁、移动端布局、242–244 个静态资产和 console/page/request/5xx 巡检均通过。游戏画面以截图作为最终判据；3 次独立桌面加载在 25 秒时亮度比均为 `0.861`，控制台错误为空。首次 12 秒采样有一次较暗（`0.1097`）但随后恢复，属于启动时序/采样现象；WebGL `readPixels` 背缓冲检查不作为验收依据。

## 4. 缺陷

### P1-1：生产 NPC 图片理解不可用

**复现：**在已确认可 GET 的图片 URL 上，通过 WS `chat_msg` 携带 `media_type=image`：

1. 相对 URL 回复：“只能看到文本信息，没有图像识别能力”；
2. 绝对 URL 诊断重试回复：“没有视觉处理模块”；
3. 另一测试居民重复测试回复“无法处理图像数据”。

HTTP 请求和聊天流本身均成功结束，因此这不是上传失败或 URL 格式问题，而是视觉内容没有被模型消费。

**高置信根因推断：**`backend/app/media/model_router.py:46-49` 将图片包装成 Anthropic image block，但 `:163-175` 仍统一使用 `settings.effective_model`；`backend/app/config.py:80-81` 也只有单一 effective model。生产实际模型/中转当前不具备或未启用视觉输入能力。

**建议：**为图片路径配置明确的 vision-capable 模型，或增加图片理解 prepass 后把结构化描述注入主模型；补充一个包含已知颜色/物体的生产或 staging E2E 断言，不能只检查 HTTP 200 和非空文本。

本轮是测试任务，未修改业务源码。

## 5. 场景覆盖与边界

| 场景 | 结果 | 说明 |
|---|---|---|
| 正常路径 | PASS | 登录、进入世界、WS 移动/聊天、Forge、媒体、主要页面 |
| malformed 输入 | PASS | REST 422、WS 4001、非法聊天帧结构化错误 |
| 中断/重连 | PASS | 非法认证后重新连接；认证后非法帧断开并恢复；双客户端广播 |
| 提示词注入 | PASS | 无敏感信息泄露 |
| 重复/波动加载 | PASS | 3 次独立 `/play`，晚时截图全部稳定；无 console error |
| 假成功语义 | PASS | 同时核对 HTTP、响应体、终态字段、静态字节数和 SHA-256 |
| stale Forge 状态 | 部分覆盖 | 随机旧 UUID 返回 404；旧部署中的具体会话 ID 不可复用，惰性清扫未直接复测 |
| Lab cancel/resume | SKIP | 生产 `lab_enabled=false`，未创建会产生冻结资金的任务；无副作用替代验证为只读接口和入口门控 |
| 注册限流/并发购买 | 未在本轮重跑 | 上一轮报告有证据，本轮重点集中于已部署修复回归，不能把旧证据当作本轮结果 |
| 脏工作树/测试副作用 | 已控制 | 发现已有业务改动后未回滚、未覆盖；所有生产写入均使用专用账号或测试居民 |

所有网络和 LLM 等待均设置了有限超时；深度 Forge 在终态前持续轮询，没有把中间 200 响应误判为成功。

## 6. 测试数据与清理

已清理：本轮一次性账号（注销回归账号）已删除，旧 token/密码均失效。

仍留在生产、便于后台审计/清理的测试数据：

- 居民：`部署回归图灵0724`、`格蕾丝-霍珀`；
- 媒体：上述测试图片和生成头像 URL；
- 对话：WebSocket 多模态、注入和移动测试产生的消息/计费记录；
- 专用回归账号 A/B 仍保留，未执行破坏性清理。

本地保留截图和 JSON 证据目录：`tmp/production-qa-20260724/`。一次性 Python harness 已删除。

## 7. 文件变更与后续回归

- 本轮有意新增：`docs/testing/TEST_REPORT_2026-07-24.md`。
- 本轮未修改任何生产业务源码；当前工作树中原有的 backend/seed、agent/service、测试和数据库改动均保留。
- 修复 P1-1 后，至少重跑 `WS-08` 相对 URL、绝对 URL诊断和已知颜色/物体断言；同时复查模型路由、计量和预算路径。
- 如需清理生产测试数据，应使用管理后台按测试居民/专用邮箱精确删除，避免误伤真实居民。

证据文件：`tmp/production-qa-20260724/ws-results.json`、`tmp/production-qa-20260724/browser-results-pixel-pass.json`、`tmp/production-qa-20260724/browser-results-structure-pass.json`、`tmp/production-qa-20260724/game-repeat-results.json`。
