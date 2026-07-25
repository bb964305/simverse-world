# Simverse World 生产环境全功能测试方案

> 版本:v1.0 · 日期:2026-07-23 · 适用部署:simverse.world 最新生产版本
> 测试性质:部署后验收测试(Post-Deployment Acceptance)+ 全功能冒烟/回归

---

## 1. 目标与范围

对已部署到生产服务器的最新版本进行**全功能验证**,确认:

1. 基础设施(API / 数据库 / Redis / agent-worker / lab-runner / 前端静态站点)全部健康;
2. 159 个 REST 接口 + WebSocket 实时通道的核心链路可用;
3. LLM 重度功能(NPC 对话、锻造、居民自主行为、多模态)真实可用;
4. Lab 实验模块按配置预期工作;
5. 安全边界(认证、权限、限流)未被本次部署破坏。

**不在本次范围**:LinuxDo / GitHub OAuth 三方登录(需真实三方账号配合,仅验证跳转端点可达)、移动端适配、压力测试。

## 2. 被测环境

| 组件 | 地址 / 说明 |
|---|---|
| 前端 | `https://simverse.world`(Cloudflare Workers,SPA) |
| API | `https://simverse-api.proxypool.eu.org`(FastAPI,2 uvicorn workers) |
| WebSocket | `wss://simverse-api.proxypool.eu.org/ws`(首条消息 `{"type":"auth","token":...}` 鉴权) |
| 数据库 | PostgreSQL 16 + pgvector(容器内,仅 127.0.0.1) |
| Redis | Redis 8(WS 跨进程总线 / 限流 / 队列) |
| agent-worker | 独立进程:居民自主行为 AgentLoop + heat/event/nightly cron + embedding 回填 |
| lab-runner | 独立进程:Lab 运行队列消费者(适配器由 LAB_ADAPTER 决定) |

## 3. 测试策略与分层

自底向上分 8 层执行,前一层不通过则后续层大概率失真,先修再测:

| 层 | 内容 | 手段 |
|---|---|---|
| L0 基础设施 | 健康检查、TLS、CORS、前端资产加载 | curl / 脚本 |
| L1 只读 API 冒烟 | 全部公开 GET 接口 + 鉴权保护验证 | 自动化脚本 |
| L2 账号与核心写入 | 注册→登录→Onboarding→角色→设置 | 自动化脚本(专用测试账号) |
| L3 WebSocket 实时 | 连接鉴权、出生点、移动、每日奖励、在线广播 | websockets 脚本 |
| L4 LLM 重度功能 | NPC 对话、快速/引导/深度锻造、头像、TTS | 脚本 + 人工判读回复质量 |
| L5 经济/社交/内容 | 商店、投资、辩论、委托、公告、胶囊、投票等 | 自动化脚本 |
| L6 Lab 与后台任务 | Lab 任务链路、居民自主行为观察 | 脚本 + 时间窗观察 |
| L7 前端 E2E | 真实浏览器走通注册→进入世界→对话→锻造 | Playwright,截图留证 |

## 4. 测试账号与数据策略

- 专用测试账号:`svtest_<日期>_<随机>@sv-test.dev`,统一前缀便于后续在管理后台按邮箱清理;
- 所有写入类用例只操作测试账号自己名下的数据(角色、对话、购买、任务);
- **不修改**任何 `/admin/*` 全局配置(经济参数、系统配置、事件),管理后台仅做只读与权限测试;
- LLM 成本控制:生产设有预算断路器(全局/单用户/单锻造请求日预算)。本方案 LLM 类用例预计消耗 < $0.5,处于单用户日预算内;深度蒸馏锻造只跑 1 次;
- 限流注意:注册接口 5 次/分钟、锻造 10 次/分钟,脚本已按此节奏执行,并顺带验证 429 行为。

## 5. 详细测试用例

优先级定义:**P0** 阻断性(不通过=部署失败);**P1** 主功能;**P2** 增强/边缘。

### M0 基础设施(L0)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M0-1 | API 健康 | `GET /health` → 200 `{"status":"ok"}` | P0 |
| M0-2 | 前端可达 | `GET https://simverse.world/` → 200,HTML 含 app 挂载点;JS/CSS 资产 200 | P0 |
| M0-3 | TLS 证书 | 两域名证书有效且未临期(<14 天告警) | P0 |
| M0-4 | OpenAPI | `GET /openapi.json` → 200,路径数 ≈159,作为回归基线比对 | P1 |
| M0-5 | CORS | 带 `Origin: https://simverse.world` 预检 → `access-control-allow-origin` 正确 | P0 |
| M0-6 | /metrics 保护 | `GET /metrics` 无 token → 401(若线上设置了 METRICS_TOKEN)或有意识确认其公开状态 | P1 |
| M0-7 | 静态媒体 | 上传目录挂载可用:任一已存在头像/媒体 URL 可 200 | P1 |

### M1 认证与账号(L2)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M1-1 | 注册 | `POST /auth/register` 新邮箱 → 200 返回 token | P0 |
| M1-2 | 重复注册 | 同邮箱再注册 → 4xx 明确报错 | P1 |
| M1-3 | 登录 | `POST /auth/login` → 200 token;`GET /users/me` → 200 用户信息 | P0 |
| M1-4 | 错误密码 | 登录错密码 → 401 | P0 |
| M1-5 | 无 token 访问 | 抽样 10 个受保护接口无 Authorization → 401 | P0 |
| M1-6 | 伪造 token | 随机字符串 token → 401 | P0 |
| M1-7 | OAuth 端点 | `GET /auth/github/login`、`/auth/linuxdo/login` → 302 跳转或明确的未配置错误(不做全流程) | P2 |
| M1-8 | 注册限流 | 1 分钟内 >5 次注册 → 429 | P1 |
| M1-9 | 改密码 | `POST /settings/account/password` → 200,旧密码失效、新密码可登录 | P1 |
| M1-10 | 注销账号 | 测试尾声 `DELETE /settings/account` → 200,token 失效(兼做数据清理) | P2 |

### M2 Onboarding 与玩家角色(L2)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M2-1 | 引导检查 | 新账号 `GET /onboarding/check` → 提示需要创建角色 | P0 |
| M2-2 | 精灵模板 | `GET /sprites/templates` → 返回 25 个模板 | P1 |
| M2-3 | 创建角色 | `POST /onboarding/create-character`(name+sprite_key)→ 200;`/users/me` 关联 player_resident | P0 |
| M2-4 | 预设加载 | `POST /onboarding/load-preset`(另一账号)→ 200 | P2 |
| M2-5 | 跳过引导 | `POST /onboarding/skip`(另一账号)→ 200 | P2 |

### M3 居民/角色系统(L1/L2)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M3-1 | 居民列表 | `GET /residents` → 200,含预设 NPC(约 25 个村民) | P0 |
| M3-2 | 居民详情 | `GET /residents/{slug}` → 200,persona/soul/ability、SBTI、状态字段完整 | P0 |
| M3-3 | 角色卡 | `GET /residents/{slug}/card` → 200 | P1 |
| M3-4 | 导出/导入 | `GET .../export` → 200;`POST /residents/import-card` round-trip → 200 | P1 |
| M3-5 | 版本历史 | `GET /residents/{slug}/versions` → 200 | P2 |
| M3-6 | 编辑自己角色 | `PUT /residents/{slug}`(自己的)→ 200;编辑他人角色 → 403 | P1 |
| M3-7 | 居民目标 | `GET /residents/{slug}/goals` → 200 | P2 |
| M3-8 | 世界位置 | `GET /world/locations` → 200,约 20 个命名位置 | P1 |
| M3-9 | 搜索 | `GET /search?q=...` → 200 命中居民 | P1 |
| M3-10 | 家装 | `GET/PUT /residents/{slug}/home/decor`(自己)→ 200 | P2 |

### M4 WebSocket 实时通道(L3)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M4-1 | 鉴权成功 | 连接后发 `{"type":"auth","token":JWT}` → 收到 `auth_ok` + `spawn_position` | P0 |
| M4-2 | 鉴权失败 | 错误 token → close code 4001 | P0 |
| M4-3 | 每日奖励 | 当日首连 → 收到 `daily_reward`(金额/余额/连击) | P1 |
| M4-4 | 移动同步 | 发 `move` → 第二连接收到位置广播;断线后位置持久化 | P1 |
| M4-5 | 在线列表 | 双连接互见 `player_joined` / `online_players` / `player_left` | P1 |
| M4-6 | WS 限流 | 1 分钟内 >20 条消息 → 被限流且连接不崩 | P2 |

### M5 NPC 对话与多模态(L4,LLM)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M5-1 | 开始对话 | WS `start_chat`(空闲 NPC)→ 状态变 chatting;广播 `resident_status` | P0 |
| M5-2 | LLM 回复 | `chat_msg` "你好,你是谁?" → 收到符合该 NPC 人设的中文回复,时延 < 60s | P0 |
| M5-3 | 记忆写入 | 对话中告知独特事实→ `end_chat` → 稍后再开新对话询问 → NPC 能回忆(三层记忆链路) | P1 |
| M5-4 | 评分 | `rate_chat`(1-5)→ 200/ack | P2 |
| M5-5 | 图片理解 | `POST /api/media/upload` 上传图片 → `chat_msg` 带 media_url → NPC 描述图片内容(qwen 路由) | P1 |
| M5-6 | 视频理解 | 同上传视频(kimi 路由)→ NPC 回应视频内容 | P2 |
| M5-7 | 玩家私聊 | 双账号 `player_chat` 互发 → 对方实时收到;离线消息重连后送达 | P1 |
| M5-8 | 自动回复 | `set_reply_mode` auto → 离线玩家收到私聊时 LLM 按人设代答 | P2 |
| M5-9 | TTS | `POST /tts` → 200 音频或明确"未配置"错误(视 TTS_BASE_URL 配置) | P2 |

### M6 锻造 Forge(L4,LLM)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M6-1 | 快速锻造 | `POST /forge/quick`(name+raw_text)→ 生成角色,persona/soul/ability 非空且与素材相关 | P0 |
| M6-2 | 引导锻造 | `POST /forge/start` → `GET /forge/status/{id}` 轮询 → `POST /forge/answer` 交互 → 完成 | P1 |
| M6-3 | 深度蒸馏 | `POST /forge/deep-start` → 轮询 `deep-status`:调研(SearXNG)→提取→构建→验证→精炼各阶段推进至完成,产出质量人工判读 | P1 |
| M6-4 | 锻造限流 | 超过 10 次/分钟 → 429 | P2 |
| M6-5 | AI 头像 | `POST /avatar/generate` → 200 返回像素风头像 URL(视 PORTRAIT 配置,未配置则明确报错) | P1 |
| M6-6 | 精灵匹配 | `POST /sprites/match` → 200 返回匹配模板 | P2 |
| M6-7 | 技能导入 | `POST /settings/character/import`(.skill/md 文件)→ 200 | P2 |

### M7 经济系统(L5)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M7-1 | 初始余额 | 新账号 `/users/me` → Soul Coin 初始值 + 每日登录奖励入账 | P0 |
| M7-2 | 商店目录 | `GET /shop/catalog` → 200 商品列表 | P1 |
| M7-3 | 购买 | `POST /shop/purchase` 便宜商品 → 200,余额减少,`GET /shop/inventory` 出现 | P1 |
| M7-4 | 余额不足 | 购买超额商品 → 4xx,余额不变(原子扣款回归,重点!本次改动过 charge 逻辑) | P0 |
| M7-5 | 流水 | `GET /profile/transactions` → 上述交易可见、金额正确 | P1 |
| M7-6 | 目标投资 | `GET /residents/{slug}/goals` → `POST /goals/{id}/invest` → 200 扣款入账 | P1 |
| M7-7 | 辩论押注 | `GET /debates` → `stake`/`vote` → 200(无进行中辩论则记 SKIP) | P2 |
| M7-8 | 委托 | `GET /commissions` → `accept`/`abandon` → 状态流转正确 | P2 |

### M8 社交与内容(L5)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M8-1 | 动态流 | `GET /feed` → 200,有近期世界事件(证明 agent-worker 在产出) | P0 |
| M8-2 | 关注 | `POST/DELETE /follows/{slug}` → feed 个性化生效 | P2 |
| M8-3 | 公告栏 | `GET /bulletin`、`GET/POST /bulletin/posts` → 发帖可见 | P1 |
| M8-4 | 时间胶囊 | `POST /capsules` → `GET /capsules` 列表可见 | P2 |
| M8-5 | 投票 | `GET /polls/open` → `vote` → 200(无开放投票记 SKIP) | P2 |
| M8-6 | 关系图谱 | `GET /graph/relationships` → 200,NPC 间存在关系边 | P1 |
| M8-7 | 赛季 | `GET /seasons/current` + `leaderboard` → 200 | P1 |
| M8-8 | 日报/周报 | `GET /digest/latest`、`/digest/weekly/me` → 200(nightly cron 产物) | P1 |
| M8-9 | 通知 | `GET /notifications` → `POST /notifications/read` → 未读数归零 | P1 |
| M8-10 | 成就 | `GET /achievements` → 200;注册/首聊后有成就解锁记录 | P2 |
| M8-11 | 每日任务 | `GET /daily/quest` → 200 当日任务 | P2 |
| M8-12 | 探索图鉴 | `GET /exploration/me` → 200;WS 移动到新位置后图鉴更新 | P2 |
| M8-13 | 创作者统计 | `GET /creator/stats` → 200 | P2 |

### M9 设置面板(L2)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M9-1 | 读取全部设置 | `GET /settings` → 200,6 大分区齐全 | P0 |
| M9-2 | 各分区 PATCH | account/character/interaction/privacy/economy 各改一字段 → 200 且回读生效 | P1 |
| M9-3 | 人设编辑 | `PUT /settings/character/persona` → 200 | P1 |
| M9-4 | 自定义 LLM | `PATCH /settings/llm` + `POST /settings/llm/test` → 按 ALLOW_USER_CUSTOM_LLM 配置返回 200 或明确禁用 | P2 |

### M10 管理后台(L1/安全)

> 无管理员凭据时,本模块全部用例转为**权限负向测试**;如提供管理员账号则追加只读正向测试。

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M10-1 | 权限拒绝 | 普通测试账号访问全部 `/admin/*`(抽样 ≥15 个)→ 403/401,绝不能 200 | P0 |
| M10-2 | 仪表盘 | (管理员)`GET /admin/dashboard/health|stats|trends` → 200,服务健康项全绿 | P1 |
| M10-3 | LLM 用量 | (管理员)`GET /admin/llm-usage/summary` → 本次测试的调用有计量记录 | P1 |
| M10-4 | SearXNG 健康 | (管理员)`GET /admin/forge/searxng-health` → healthy | P1 |
| M10-5 | Lab 状态 | (管理员)`GET /admin/lab/status` → 队列/开关状态与部署预期一致 | P1 |

### M11 Lab 实验模块(L6)

> 线上行为取决于 `LAB_ENABLED` 与 `LAB_ADAPTER`(默认 mock)。若 LAB_ENABLED=false,预期为明确的 403/404/禁用报错,同样记为符合预期。

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M11-1 | 研究员列表 | `GET /lab/researchers` → 200 列表(或明确禁用) | P1 |
| M11-2 | 创建任务 | `POST /lab/tasks`(小额 reward_sc)→ 200,余额被冻结(coin_hold) | P1 |
| M11-3 | 任务执行 | 轮询 `GET /lab/tasks/{id}` + `GET /lab/runs/{run_id}/steps` → 状态机推进(queued→running→review) | P1 |
| M11-4 | 产物 | `GET /lab/artifacts/{id}` + download → 200 | P2 |
| M11-5 | 验收/拒绝 | `accept-result` → 赏金分账(创作者分成/平台费);或 `reject-result` → 仲裁流程 | P2 |
| M11-6 | 取消 | 新建任务后 `cancel` → 冻结金额退回 | P1 |

### M12 居民自主行为与后台任务(L6,观察型)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M12-1 | 自主行动 | 间隔 5-10 分钟两次抓取 `GET /residents` → 有 NPC 位置/状态/活动变化(AgentLoop 存活) | P0 |
| M12-2 | 自主对话 | `GET /feed` / `GET /admin/gossip/recent`(如有权限)→ 近 24h 存在 NPC 间对话摘要 | P1 |
| M12-3 | 热度计算 | 与 NPC 对话后其 heat 上升(heat_cron) | P2 |
| M12-4 | 人格演化痕迹 | `GET /residents/{slug}/versions` → 存在演化历史记录 | P2 |
| M12-5 | 世界事件 | `GET /events/active` → 200(有活动事件或空列表均可,接口可用即过) | P1 |

### M13 前端 E2E(L7,Playwright)

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M13-1 | 落地页 | 打开 `/` → 营销页完整渲染,无 console error,CTA 可点 | P0 |
| M13-2 | 注册登录 UI | 走 UI 注册/登录 → 进入 Onboarding → 创建角色 | P0 |
| M13-3 | 游戏世界 | 进入 GamePage → Phaser 画布渲染、tilemap/精灵加载、WS connected、小地图可见 | P0 |
| M13-4 | 对话 UI | 点击 NPC → 聊天窗打开 → 发消息收到回复 | P1 |
| M13-5 | 锻造页 | ForgePage 加载、发起快速锻造、进度展示 | P1 |
| M13-6 | 其他页面 | Profile / Seasons / Debates / Capsules / Graph 页面路由可达、无白屏 | P1 |
| M13-7 | 管理页门禁 | 普通账号访问 /admin 路由 → 被拒或隐藏 | P1 |
| M13-8 | 控制台巡检 | 全流程收集 console error / 失败网络请求 → 无 5xx、无未捕获异常 | P1 |

### M14 非功能抽查

| # | 用例 | 步骤 / 预期 | 优先级 |
|---|---|---|---|
| M14-1 | 时延采样 | 核心只读接口连续 10 次:p95 < 1.5s;`/health` p95 < 300ms | P1 |
| M14-2 | 错误规范 | 4xx/5xx 返回结构化 JSON 错误,无堆栈泄漏 | P1 |
| M14-3 | 敏感信息 | 响应中无密码哈希、API key、内部连接串 | P0 |
| M14-4 | 幂等/并发 | 同一购买请求并发 ×5 → 只成功一次或余额不出现负数 | P1 |

## 6. 执行顺序与依赖

```
M0 ─→ M1 ─→ M2 ─→ ┬─ M3 / M7 / M8 / M9(并行)
                   ├─ M4 ─→ M5
                   ├─ M6
                   ├─ M10 / M11
                   └─ M12(拉长时间窗观察)
全部 API 层完成后 ─→ M13 前端 E2E ─→ M14 汇总
```

## 7. 通过标准

- **发布判定**:P0 用例 100% 通过;P1 通过率 ≥ 90% 且失败项无数据损坏类问题;P2 仅记录不阻断。
- 任何 P0 失败 → 判定本次部署**不可用**,按"回滚或热修"决策,修复后重跑失败层及其下游层。
- 因环境配置(如 TTS/头像/Lab 未启用)导致的"明确禁用"响应记为 **SKIP(符合预期)**,不算失败。

## 8. 产出物

1. 本方案文档(`TEST_PLAN.md`);
2. 可重复执行的自动化冒烟脚本(`smoke_test.py`,每次部署后可直接重跑);
3. 测试执行报告(`TEST_REPORT.md`):逐用例 PASS/FAIL/SKIP + 证据(响应摘要、截图)+ 问题清单及严重级别。
