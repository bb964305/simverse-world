# Simverse World 本地开发手册

这篇文章写给第一次运行代码的开发者。

它提供一条简单路线，也提供完整路线。
生产部署请看后续的部署说明。

技术词可以在[词语表](GLOSSARY.md)中查到。

## 先选一条路线

| 路线 | 适合谁 | 能验证什么 |
|---|---|---|
| 最短体验 | 第一次运行项目的人 | 登录、新手引导、地图和实时连接 |
| 完整开发 | 修改数据库功能的人 | PostgreSQL、pgvector、Redis 和迁移 |

第一次运行，请先选“最短体验”。

## 需要准备什么

请先安装：

- Git。
- Python 3.11 或更新版本。
- Node.js 20.19 或更新版本。
- 也可以使用 Node.js 22.12 或更新版本。
- npm。
- Docker 和 Docker Compose。

先检查版本：

```bash
python3 --version
node --version
npm --version
docker --version
docker compose version
```

Vite 8 要求使用上面列出的较新 Node.js 版本。
Node 版本太旧时，前端无法可靠构建。

## 路线一：最短体验

这条路线使用 SQLite 保存本地数据。

SQLite 是一个单文件数据库。
它适合快速启动，但不代表生产行为。

Redis 仍然需要启动。
没有 Redis，实时消息会失效。

### 1. 启动 Redis

在项目根目录运行：

```bash
docker compose up -d redis
```

检查它是否健康：

```bash
docker compose ps redis
```

状态应包含 `healthy`。

### 2. 启动后端

在项目根目录运行：

```bash
cd backend
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

然后启动 API：

```bash
DATABASE_URL=sqlite+aiosqlite:///./simverse-dev.db \
  AUTO_CREATE_TABLES=true \
  RUN_BACKGROUND_TASKS=false \
  REDIS_URL=redis://localhost:6379/0 \
  DEBUG=true \
  ./.venv/bin/uvicorn app.main:app --reload --port 8000
```

这条命令只用于本地开发。
`DEBUG=true` 不能放进生产环境。

保持这个终端继续运行。

### 3. 启动前端

另开一个终端。

在项目根目录运行：

```bash
cd frontend
npm install
VITE_API_URL=http://localhost:8000 npm run dev
```

前端通常会显示这个地址：

```text
http://localhost:5173
```

### 4. 走一遍真实路径

先检查后端：

```bash
curl http://localhost:8000/health
```

浏览器再完成下面的路径：

```text
打开首页 → 注册 → 选择或跳过居民 → 进入 /play
```

成功标志：

- `/health` 返回成功状态。
- 注册后没有网络错误。
- 新手引导能创建默认居民。
- `/play` 能看到地图和顶部导航。
- 浏览器能建立实时连接。

### 5. 这条路线不能验证什么

它不能代表 PostgreSQL 的真实行为。

它也不会运行数据库迁移。
表是由开发模式自动创建的。

没有真实 LLM 密钥时：

- 登录和地图可以使用。
- 普通数据页面可以打开。
- AI 聊天可能失败或返回降级说明。
- 炼化和头像生成不能完整工作。

## 路线二：完整数据库开发

这条路线使用 pgvector PostgreSQL 16。
它还会启动 Redis 8。

根目录 Compose 不是同一套环境：

| Compose | PostgreSQL | Redis | 用途 |
|---|---|---|---|
| 根 `docker-compose.yml` | 普通 PostgreSQL 16 | Redis 7 | 简单本地基础服务 |
| `deploy/backend/docker-compose.yml` | pgvector PostgreSQL 16 | Redis 8 | 接近生产的数据库环境 |

两套 Compose 会占用相同端口。
不要同时启动它们。

### 1. 停止简单环境

如果刚才启动过根 Redis，请运行：

```bash
docker compose stop redis
```

### 2. 准备完整数据库

在项目根目录运行：

```bash
cd deploy/backend
test ! -f .env || { echo "deploy/backend/.env 已存在，请使用其中的密码"; exit 1; }
cp .env.example .env
docker compose up -d db redis
```

模板中的本地数据库密码是 `changeme`。

如果 `.env` 已经存在，不要覆盖它。
请把真实密码写进自己的 `DATABASE_URL`。

只启动 `db` 和 `redis`。
不要用模板密钥启动生产 API。

### 3. 安装后端并执行迁移

继续运行：

```bash
cd ../../backend
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

执行迁移：

```bash
DATABASE_URL=postgresql+asyncpg://postgres:changeme@localhost:5432/skills_world \
  REDIS_URL=redis://localhost:6379/0 \
  DEBUG=true \
  ./.venv/bin/alembic upgrade head
```

启动后端：

```bash
DATABASE_URL=postgresql+asyncpg://postgres:changeme@localhost:5432/skills_world \
  REDIS_URL=redis://localhost:6379/0 \
  DEBUG=true \
  ./.venv/bin/uvicorn app.main:app --reload --port 8000
```

如果你使用了别的密码，请同步修改两条命令。

### 4. 启动前端

另开一个终端：

```bash
cd frontend
npm install
VITE_API_URL=http://localhost:8000 npm run dev
```

### 5. 完整环境成功标志

- PostgreSQL 和 Redis 都是 `healthy`。
- `alembic upgrade head` 成功结束。
- `/health` 返回成功状态。
- 注册、新手引导和地图可以使用。
- 服务器日志没有数据库连接错误。

## 环境变量怎么读

环境变量是启动时交给程序的设置。

模板在：

- `backend/.env.example`
- `frontend/.env.example`
- `deploy/backend/.env.example`

### 本地必须知道的变量

| 变量 | 白话说明 |
|---|---|
| `DATABASE_URL` | 数据库地址和密码 |
| `REDIS_URL` | Redis 地址 |
| `DEBUG` | 是否使用本地开发安全规则 |
| `VITE_API_URL` | 前端要连接的后端地址 |

### 按需填写的变量

| 变量 | 什么时候需要 |
|---|---|
| `LLM_API_KEY` | 使用自定义 AI 服务时 |
| `ANTHROPIC_API_KEY` | 使用 Anthropic 密钥时 |
| `LLM_BASE_URL` | 使用自定义模型地址时 |
| `SEARXNG_URL` | 炼化需要联网调研时 |
| `VITE_SENTRY_DSN` | 需要前端错误上报时 |

### 危险变量

| 变量 | 风险 |
|---|---|
| `JWT_SECRET` | 泄露后可能伪造登录凭证 |
| `POSTGRES_PASSWORD` | 泄露后可能读取数据库 |
| 各种 `API_KEY` | 泄露后可能产生费用 |
| `AUTO_CREATE_TABLES` | 只能给开发环境使用 |
| `DEBUG` | 生产必须为 `false` |

不要提交真实 `.env` 文件。
不要把密钥贴进报错或截图。

## 运行后端测试

在 `backend/` 目录运行：

```bash
./.venv/bin/python -m pytest tests/
```

默认测试会排除外部环境测试。
被排除的测试包括：

- 真实 Lab 容器测试。
- 真实 Lab PostgreSQL 测试。
- 真实 Lab Redis 测试。
- 活的 Staging 测试。
- Lab 容量测试。
- 经济并发 PostgreSQL 测试。

这些测试需要单独环境和明确标记。

## 运行前端验证

在 `frontend/` 目录依次运行：

```bash
npm run test
npm run lint
npx tsc --noEmit
npm run build
```

四条命令分别检查：

1. 组件和页面测试。
2. 代码规则。
3. TypeScript 类型。
4. 生产构建。

只运行其中一条，不能代表全部通过。

## 常见问题

### 端口已经被占用

常用端口是：

- 前端 `5173`。
- 后端 `8000`。
- PostgreSQL `5432`。
- Redis `6379`。

先停止旧服务，再重新启动。
不要随意杀掉不属于本项目的进程。

### 迁移提示没有 vector 扩展

你可能启动了根目录的普通 PostgreSQL。

完整迁移需要 pgvector 镜像。
请使用完整数据库开发路线。

### SQLite 迁移失败

SQLite 路线不应该运行 Alembic。

它使用 `AUTO_CREATE_TABLES=true` 建表。
迁移行为必须在 PostgreSQL 验证。

### 页面显示网络错误

先检查后端 `/health`。

再检查 `VITE_API_URL` 是否正确。
前端地址还必须在后端 CORS 列表中。

CORS 是浏览器的跨站安全规则。

### 地图打开但实时内容不更新

请检查 Redis 是否健康。

也要检查 WebSocket 是否连接成功。
浏览器控制台会显示连接错误。

### AI 居民不回答

请检查 LLM 密钥和模型地址。

还要查看预算是否已经用完。
没有密钥时，基础地图仍应能打开。

### 登录时提示 JWT 配置错误

本地命令需要 `DEBUG=true`。

生产环境不能这样处理。
生产必须使用足够长的随机密钥。

## 怎样停止本地服务

前端和后端终端可以按 `Ctrl+C`。

停止根目录 Redis：

```bash
docker compose stop redis
```

停止完整数据库环境：

```bash
cd deploy/backend
docker compose stop db redis
```

不要运行 `docker compose down -v`。
它会删除数据库卷里的数据。

## 下一步读什么

- 想知道代码放哪里：读 [系统结构](ARCHITECTURE.md)。
- 想开始改代码：读 [贡献指南](CONTRIBUTING.md)。
- 想查技术词：读 [词语表](GLOSSARY.md)。
