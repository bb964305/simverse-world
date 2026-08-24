# 文档整理验收报告

日期：2026-08-24

## 一句话结论

当前公开文档的结构、链接和白话表达检查通过。

真实注册、新手引导和 WebSocket 路径也通过。

但是项目不能称为“全部测试通过”。
后端仍有 47 个 Lab 测试失败。

浏览器实例也不可用。
所以本次没有拿到真实页面截图和浏览器控制台证据。

## 这次检查了什么

检查分成四部分：

1. 文档结构和链接。
2. 本地真实启动。
3. 注册到新手引导的 API 路径。
4. 前端和后端完整测试。

API 是前端和后端约定的请求接口。

## 文档静态检查

本机共扫描 57 个 Markdown 文件：

- 1 个根 README。
- 45 个已跟踪的公开 `docs/` 文件。
- 11 个不发布的本机内部笔记。

内部笔记位于 `docs/superpowers/`。
它们被 `.gitignore` 明确排除。

公开文档不会链接这些本机文件。

### 检查结果

| 检查 | 结果 |
|---|---|
| Git 空白和行尾 | 通过 |
| 当前入口中的旧技术说法 | 0 个 |
| 常见占位词 | 0 个 |
| 密钥、token 和用户 ID 样式 | 0 个 |
| 本地链接 | 57 个文件中 0 个坏链接 |
| 文档总目录 | 每个公开 Markdown 都有入口 |
| README 第一屏 | 在线地址和四类身份都存在 |
| 文档目录第一屏 | 只有四个选择 |
| 六条固定阅读路径 | 全部能走到终点 |
| 危险操作表 | 风险、保护、成功标志和恢复入口齐全 |

### 句子长度

每个当前入口抽取前 20 句。
至少 18 句不超过 35 个字符。

结果：

| 文件 | 短句数 |
|---|---|
| 根 `README.md` | 19 / 20 |
| `docs/README.md` | 20 / 20 |
| `START_HERE.md` | 20 / 20 |
| `GAMEPLAY.md` | 19 / 20 |
| `ARCHITECTURE.md` | 20 / 20 |
| `DEVELOPMENT.md` | 20 / 20 |
| `CONTRIBUTING.md` | 20 / 20 |
| `DEPLOYMENT.md` | 19 / 20 |
| `OPERATIONS.md` | 18 / 20 |
| `GLOSSARY.md` | 20 / 20 |
| `ROADMAP.md` | 19 / 20 |

## 真实启动环境

本机已有 SSH 隧道占用：

- `5432`。
- `6379`。

没有停止或修改这些现有隧道。

本次另开一个隔离 Redis：

```text
127.0.0.1:16379
```

后端使用新的临时 SQLite 文件。
后台任务保持关闭。

Lab 明确设置为关闭：

```text
LAB_ENABLED=false
```

第一次启动暴露了两个文档问题：

- 本机代理让普通 `curl` 没有直连后端。
- 本机旧 `.env` 让 Lab 被读成开启。

开发手册已经修正：

- 健康检查增加 `--noproxy '*'`。
- 启动命令增加 `LAB_ENABLED=false`。
- 启动命令增加只用于本地的 JWT 值。

修正后重新使用全新数据库启动。

## 真实后端路径

最终路径没有出现 4xx 或 5xx。

| 步骤 | 结果 |
|---|---|
| `GET /health` | HTTP `200`，`status=ok` |
| `POST /auth/register` | HTTP `200` |
| 注册返回的 Lab 开关 | `false` |
| 引导前 `GET /onboarding/check` | HTTP `200`，需要引导 |
| `POST /onboarding/skip` | HTTP `200`，创建默认居民 |
| 引导后再次检查 | HTTP `200`，不再需要引导 |
| `GET /users/me` | HTTP `200`，Lab 仍为 `false` |

测试账号和临时 token 没有写进仓库。

第一次注册曾使用保留域名。
邮箱校验器按设计返回 HTTP `422`。

随后改用标准示例域名重新验证。
最终用户路径全部返回 HTTP `200`。

## 真实 WebSocket 路径

客户端连接：

```text
ws://127.0.0.1:8000/ws
```

token 只放在第一条认证消息中。
它没有放进 URL 或日志。

收到的消息类型：

```text
auth_ok,daily_reward,spawn_position
```

这证明：

- WebSocket 连接成功。
- 登录身份通过认证。
- 每日奖励消息到达。
- 地图出生位置到达。

## 前端运行结果

Vite 8.0.5 成功启动在：

```text
http://localhost:5173
```

| 请求 | 结果 |
|---|---|
| `/` | HTTP `200`，根节点存在 |
| `/play` | HTTP `200`，应用外壳存在 |
| `/src/main.tsx` | HTTP `200`，转换后的入口模块存在 |

### 前端自动验证

原始结果：

```text
Test Files  66 passed (66)
Tests       323 passed (323)
```

下面检查也成功退出：

- `npm run lint`。
- `npx tsc --noEmit`。
- `npm run build`。

生产构建成功。
构建器只报告大文件警告，没有构建错误。

## 浏览器限制

浏览器运行时返回空列表：

```text
[]
```

按照浏览器工具规则，没有改用未授权的浏览器工具。

因此本次没有验证：

- 页面真实像素渲染。
- 浏览器控制台红色错误。
- 浏览器网络面板。
- 从按钮点击进入 `/play` 的画面。

这些项目仍需要可用浏览器后补验。

## 后端测试基线

后端完整默认命令收集到：

```text
4307 collected
57 deselected
4250 selected
```

最终原始摘要：

```text
47 failed, 4202 passed, 1 skipped, 57 deselected
```

运行时间约 5 分 57 秒。

47 个失败都在 `tests/test_lab_*.py`。
普通账号、新手引导、地图、居民和经济测试没有出现在失败清单。

主要失败组包括：

- Lab 预算用量接线。
- Lab v2 运行时和控制面。
- Lab 结果与制品协议。
- Lab release gate。
- Lab terminal writer 清单。

### 单独复现

停止本次前后端服务后，单独运行：

```text
tests/test_lab_budgets_wiring.py::test_default_limits_do_not_terminate_happy_path
```

结果：

```text
1 failed, 1 warning
```

失败内容是：

```text
expected used_egress_requests = 2
actual used_egress_requests = 0
```

所以它不是前后端并行启动造成的干扰。

## 当前判断

可以确认：

- 公开文档静态质量检查通过。
- 本地启动命令经过真实修正和复验。
- 注册、新手引导和居民创建通过。
- WebSocket 认证和出生位置通过。
- 前端测试、规则、类型和构建通过。

不能确认：

- 浏览器中的完整画面和控制台无错。
- 后端完整测试全绿。

Lab 执行已经关闭。
这降低了玩家受到 Lab 失败影响的风险。

但关闭功能不等于测试失败可以忽略。

## 下一步

1. 决定修复还是归档已关闭 Lab 的旧测试。
2. 修复后重新运行后端 4250 项默认测试。
3. 提供浏览器实例。
4. 在浏览器完成注册、引导和 `/play`。
5. 检查控制台和网络面板没有新错误。

在这五步完成前，不应声称整个项目全绿。
