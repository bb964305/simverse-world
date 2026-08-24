# Simverse World 贡献指南

这篇文章写给准备修改项目的人。

贡献不只表示写新功能。
修复错误、补测试和改文档也都是贡献。

如果项目还没有在本机运行，请先读[本地开发手册](DEVELOPMENT.md)。

## 一次只解决一个问题

开始前，先用一句话写清目标。

例如：

> 居民详情页在名字为空时，不应该崩溃。

不要在同一次修改中顺手重写无关代码。
小修改更容易检查，也更容易恢复。

## 先确认工作区

运行：

```bash
git status --short
git branch --show-current
```

如果已经有未提交文件，不要直接删除。
它们可能是别人的工作。

不要使用 `git reset --hard` 清理工作区。
不要把真实 `.env`、密钥或用户资料提交进仓库。

## 我应该改哪里

先读[系统结构](ARCHITECTURE.md)。
它说明前端、后端、数据库和后台工人的关系。

可以先按问题类型判断：

| 看到的问题 | 通常从哪里找 |
|---|---|
| 页面、按钮或地图显示 | `frontend/src/` |
| 浏览器请求后端 | `frontend/src/services/` |
| API 行为 | `backend/app/routers/` |
| 业务规则 | `backend/app/services/` 或对应领域目录 |
| 数据表 | `backend/app/models/` |
| 数据库升级 | `backend/alembic/versions/` |
| 后端测试 | `backend/tests/` |
| 项目说明 | `README.md` 或 `docs/` |

先找同类代码。
请沿用已经存在的写法和命名。

## 使用小步测试流程

推荐按下面四步工作：

1. 先写一个会失败的测试。
2. 运行测试，确认它真的失败。
3. 写最少的代码，让测试通过。
4. 再运行测试和真实用户路径。

这叫测试驱动开发，英文简称 TDD。
它能证明测试确实检查了新行为。

### 第一步：写失败测试

后端测试放在 `backend/tests/`。
文件名通常是 `test_功能.py`。

前端测试放在被测代码附近。
文件名通常以 `.test.ts` 或 `.test.tsx` 结尾。

先只运行相关测试。

后端示例：

```bash
cd backend
./.venv/bin/python -m pytest tests/test_health.py -q
```

前端示例：

```bash
cd frontend
npm run test -- src/App.test.tsx
```

失败内容必须和目标问题一致。
如果测试因为拼写或环境错误失败，应先修正测试。

### 第二步：实现最小修改

只修改实现目标需要的文件。

不要留下 `TODO`、`...` 或假数据占位。
不要为了让测试变绿而跳过真实业务规则。

### 第三步：运行相关测试

先重新运行刚才失败的测试。

它通过后，再运行受影响模块的测试。
如果修改公共代码，应运行完整测试组。

### 第四步：走真实用户路径

测试通过不等于用户能正常使用。

请启动真实应用并重复用户操作。
例如：

```text
打开首页 → 注册 → 进入地图 → 打开被修改功能
```

同时检查浏览器和后端日志。
记录真实看到的成功结果。

## 后端修改怎样验证

在 `backend/` 目录运行完整测试：

```bash
./.venv/bin/python -m pytest tests/
```

如果只改了一个功能，可以先运行单个文件：

```bash
./.venv/bin/python -m pytest tests/test_auth.py -q
```

修改 API 时，还要实际请求它。
修改实时消息时，还要检查 WebSocket 连接。

默认测试不会运行需要外部 Lab 或真实 PostgreSQL 的标记测试。
需要这些环境时，应明确选择对应测试。

## 前端修改怎样验证

在 `frontend/` 目录依次运行：

```bash
npm run test
npm run lint
npx tsc --noEmit
npm run build
```

它们分别检查页面行为、代码规则、类型和生产构建。

页面修改还要在真实浏览器中检查：

- 页面能打开。
- 主要按钮能点击。
- 窄屏幕不会盖住重要内容。
- 浏览器控制台没有新错误。
- 前端能连接后端和 WebSocket。

## 文档修改怎样验证

至少运行：

```bash
git diff --check
git status --short
```

然后逐项检查：

- 新链接指向真实文件。
- 命令可以复制运行。
- 当前说明没有混入历史状态。
- 新词第一次出现时有白话解释。
- 没有真实密码、token 或用户 ID。

重要命令要在安全环境真实运行一次。
不要只看文字觉得它应该成功。

## 数据表变化怎样处理

修改数据表时，必须添加 Alembic 迁移。

Alembic 是数据库升级工具。
它让旧数据库安全变成新结构。

流程是：

1. 修改模型。
2. 新建迁移文件。
3. 阅读迁移内容。
4. 在真实 PostgreSQL 上升级。
5. 运行依赖该表的测试和用户路径。

可以从 `backend/` 目录开始：

```bash
./.venv/bin/alembic revision --autogenerate -m "describe the change"
./.venv/bin/alembic upgrade head
```

自动生成的迁移也可能出错。
必须检查 `upgrade()` 和 `downgrade()`。

不要只在 SQLite 上验证迁移。
生产使用 PostgreSQL，并依赖 pgvector 等扩展。

迁移前先备份重要数据库。
迁移失败时，不要继续部署后面的服务。

## 什么时候更新文档

出现下面变化时，要一起更新文档：

- 新增或删除页面。
- API 行为变化。
- 新增环境变量。
- 启动或部署命令变化。
- 功能被关闭或废弃。
- 运维恢复方法变化。

常用位置：

| 变化 | 更新位置 |
|---|---|
| 用户玩法 | `docs/GAMEPLAY.md` |
| 系统关系 | `docs/ARCHITECTURE.md` |
| 本地启动 | `docs/DEVELOPMENT.md` |
| 生产发布 | `docs/DEPLOYMENT.md` |
| 日常运维 | `docs/OPERATIONS.md` |
| 当前进度 | `docs/ROADMAP.md` |

历史记录放进 `archive/`。
不要把旧状态写成当前状态。

## 怎样提交

提交前先看改动：

```bash
git diff
git diff --check
git status --short
```

只暂存本次目标的文件。

提交标题使用下面格式：

```text
类型(范围): 简短说明
```

仓库里的常见例子：

```text
feat(agent): add resident greeting
fix(memory): keep public memory order
test(auth): cover expired token
docs(dev): explain local startup
```

常用类型：

| 类型 | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修复错误 |
| `test` | 增加或修改测试 |
| `docs` | 修改文档 |
| `refactor` | 行为不变的代码整理 |
| `chore` | 工具或维护工作 |

一个提交只完成一个能独立验证的小步骤。
提交信息要说明做了什么。

不要用 `--no-verify` 跳过检查。
不要编造 `Verified-by` 结果。

推送远程仓库会影响其他人。
确认获得授权后再执行 `git push`。

## 提交前清单

- [ ] 目标能用一句话说清。
- [ ] 失败测试先失败过。
- [ ] 相关测试已经通过。
- [ ] 真实用户路径已经走过。
- [ ] 数据表变化有迁移。
- [ ] 迁移在 PostgreSQL 验证过。
- [ ] 文档和环境变量示例已更新。
- [ ] 没有密钥、用户资料或生成文件。
- [ ] `git diff --check` 没有报错。
- [ ] 提交只包含本次目标。

## 需要更多背景

- 系统如何合作：读[系统结构](ARCHITECTURE.md)。
- 本机如何启动：读[本地开发手册](DEVELOPMENT.md)。
- 项目当前进度：读[项目路线图](ROADMAP.md)。
- 看不懂技术词：读[词语表](GLOSSARY.md)。
