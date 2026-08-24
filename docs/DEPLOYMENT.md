# Simverse World 部署说明

这篇文章说明怎样把后端新版本放到服务器。

它写给负责发布的人。
日常检查和故障恢复请看[运维手册](OPERATIONS.md)。

## 当前最重要的提醒

普通游戏服务可以部署。

Lab 实验执行已经关闭。
原 ARM 服务器也已经不可用。

不要启动 `lab` 或 `lab-production` profile。
profile 是一组需要主动选择的额外服务。

恢复 Lab 前，必须先有新的运行平台和完整验收。

## 先看懂发布入口

生产后端配置在：

- [`deploy/backend/docker-compose.yml`](../deploy/backend/docker-compose.yml)
- [`deploy/backend/.env.example`](../deploy/backend/.env.example)
- [`deploy/backend/deploy.sh`](../deploy/backend/deploy.sh)

日期版详细步骤在：

- [2026-08-15 小镇发布手册](runbooks/2026-08-15-town-p0-p3-rollout.md)

脚本默认把文件放到服务器的：

```text
/opt/skills-world
```

后端服务由 Docker Compose 管理。
它负责按顺序启动容器。

## 默认生产服务

普通发布会使用六个服务：

| 服务 | 白话用途 | 正常状态 |
|---|---|---|
| `db` | 保存账号、居民和世界数据 | `healthy` |
| `redis` | 传递实时消息和后台任务 | `healthy` |
| `bootstrap` | 只执行数据库迁移 | 成功后退出 |
| `api` | 接收网页和 Agent 请求 | `healthy` |
| `agent-worker` | 推动居民行动和定时任务 | `healthy` |
| `hosted-agent-worker` | 管理托管 Agent；开关关闭时保持休眠 | `healthy` |

`bootstrap` 退出不代表失败。
退出码为 `0` 才表示迁移成功。

默认服务关系是：

```text
db 健康 ─┐
         ├→ bootstrap 成功 → api 健康 → hosted-agent-worker 健康
redis 健康 ────────────────→ agent-worker 健康
```

## Lab 额外服务

Lab 服务不属于默认发布。

### `lab` profile

这个 profile 只增加：

| 服务 | 白话用途 |
|---|---|
| `lab-runner` | 从队列取出实验任务并安排执行 |

它过去用于独立运行实验任务。
当前必须保持停止。

### `lab-production` profile

这个 profile 包含六个参考服务：

| 服务 | 白话用途 |
|---|---|
| `lab-runtime` | 保存一次实验会话的运行状态 |
| `lab-egress` | 只允许经过检查的外网访问 |
| `lab-executor` | 在隔离环境执行代码工具 |
| `artifact-ingest` | 接收实验产生的文件 |
| `artifact-scanner` | 扫描文件是否安全 |
| `artifact-cleanup` | 清理过期或隔离文件 |

这六个服务只是参考拓扑。
拓扑表示服务之间怎样连接。

它们还需要独立身份、固定镜像、网络规则、扫描器和签名信任。
只有 Compose 文件不等于可以安全上线。

### 什么时候才可以重新启用

下面条件必须全部满足：

- 项目负责人批准重新开放。
- 新的 ARM 或替代执行平台已经可用。
- 所有专用密钥已经重新配置。
- 镜像和运行身份已经核验。
- 网络隔离和文件扫描已经通过。
- Staging 实际任务已经成功。
- 关闭开关和回滚方法已经演练。

少一项都继续保持关闭。

## 公网请求怎样进入项目

生产 API 只监听服务器本机的 `127.0.0.1:8100`。
它不会直接开放到整个互联网。

Cloudflare 或 Nginx 可以放在外层。
它们只负责把公网请求送到前端或 API。

它们不保存游戏数据。
它们也不能代替 API、数据库或后台工人。

当前前端发布脚本在：

- [`deploy/frontend/deploy.sh`](../deploy/frontend/deploy.sh)

前端和后端可以分开发布。
两边的 API 地址必须相互匹配。

## 安全发布顺序

每次发布都按这个顺序：

```text
备份 → 同步 → 迁移 → 重建 → 健康检查 → 用户路径
```

任何一步失败，都先停止。
不要带着已知失败继续后面的步骤。

## 第一步：备份

先记录当前版本：

```bash
git rev-parse HEAD
```

在服务器记录当前容器：

```bash
cd /opt/skills-world/deploy
docker compose ps
```

再备份：

- `/opt/skills-world/deploy/.env`
- PostgreSQL 数据库。
- 用户上传文件。
- 当前镜像或可恢复的 Git 提交号。

数据库备份文件必须不是空文件。
最好在隔离数据库试恢复一次。

## 第二步：同步文件

这条命令会修改远程服务器文件。
运行前逐项确认：

- `user@server` 是准备发布的服务器。
- 数据库、`.env` 和上传文件已经备份。
- 服务器上的 `.env` 已准备好，且不会被覆盖。
- 当前版本和回退版本已经记下。

少一项都不要执行。

推荐从仓库根目录运行：

```bash
./deploy/backend/deploy.sh user@server
```

把 `user@server` 换成真实 SSH 地址。

脚本会：

1. 同步后端代码。
2. 同步 Compose 和 Dockerfile。
3. 保留服务器上的 `.env`。
4. 构建并启动默认服务。
5. 检查本机 API 健康。

先读脚本，再在新服务器运行。
不要把示例地址直接复制到生产。

## 第三步：迁移数据库

默认发布由 `bootstrap` 执行：

```text
alembic upgrade head
```

它必须在 `api` 启动前成功。

检查：

```bash
cd /opt/skills-world/deploy
docker compose ps -a bootstrap
docker compose logs --tail=100 bootstrap
```

成功标志：

- `bootstrap` 退出码为 `0`。
- 日志没有迁移错误。
- 数据库版本是最新 head。

## 第四步：重建服务

部署脚本实际使用：

```bash
docker compose up -d --build --wait --wait-timeout 300
```

`--wait` 会等待健康检查。
它不能代替后面的真实用户检查。

不要添加 Lab profile。
当前发布只启动默认服务。

## 第五步：健康检查

在服务器运行：

```bash
curl -fsS http://localhost:8100/health
docker compose ps
docker compose logs --since=10m api agent-worker hosted-agent-worker
```

检查结果：

- `/health` 成功。
- `db`、`redis`、`api` 和两个 worker 健康。
- `bootstrap` 成功退出。
- 没有持续重复的新错误。
- `lab-runner` 没有运行。

还要从公网检查健康地址。
这样才能同时验证外层转发。

## 第六步：走用户路径

健康接口成功还不够。

至少走一遍：

```text
打开首页 → 登录 → 进入地图 → 打开一个普通功能 → 退出
```

如果修改了实时功能，还要检查 WebSocket。
如果修改了后台任务，还要等一次真实任务完成。

记录时间、版本和看到的结果。

## 危险动作表

| 动作 | 风险 | 动手前保护 | 成功标志 | 失败后的恢复入口 |
|---|---|---|---|---|
| `rsync --delete` | 删除服务器上源目录没有的文件 | 先看同步范围；排除 `.env`、上传文件和本地数据 | 只更新计划内代码；配置仍存在 | 停止发布；从代码快照或备份恢复文件 |
| `alembic upgrade head` | 改变数据表，旧程序可能不兼容 | 先做并验证数据库备份；阅读迁移 | `bootstrap` 退出码为 `0`；版本到 head | 停止新服务；按迁移设计回退或从备份恢复 |
| `docker compose up --build` | 新镜像可能无法启动，旧容器会被替换 | 记录旧提交和镜像；确认 `.env` 没变 | 所有默认服务达到预期状态 | 用旧提交或旧镜像重新启动 |
| 数据库备份恢复 | 覆盖当前数据，恢复点后的新数据会丢失 | 先保存当前故障库；在隔离库试恢复；确认目标库 | 表、行数和关键用户路径通过 | 停止写入；保留两份备份；由负责人选择正确时间点 |

这些动作都不能只看命令退出码。
还要检查数据和真实用户路径。

## `.env` 规则

示例文件只能当清单使用。
不要直接把示例密钥用于生产。

生产 `.env` 必须留在服务器。
发布脚本明确排除它。

发布前检查：

- `JWT_SECRET` 是足够长的随机值。
- `POSTGRES_PASSWORD` 不是 `changeme`。
- CORS 只包含真实前端地址。
- API 密钥没有写进 Git。
- Lab 开关保持关闭。

当前 Lab 相关值至少应满足：

```text
LAB_ENABLED=false
LAB_EXECUTOR_ENABLED=false
LAB_ARTIFACT_PIPELINE_ENABLED=false
LAB_EGRESS_ENABLED=false
```

运行时 Redis 开关也必须保持关闭。
只改 `.env` 不是完整关闭。

## 发布后记录什么

每次发布记录：

- 发布日期和时间。
- Git 提交号。
- 操作人。
- 数据库备份位置。
- 迁移结果。
- 容器状态。
- 内网和公网健康结果。
- 实际走过的用户路径。
- 是否需要回滚。

不要在记录里放密钥、token 或完整用户资料。

## 发布失败时怎么办

先停止继续操作。

然后按顺序判断：

1. 文件同步是否正确。
2. `.env` 是否仍存在。
3. 数据库迁移是否成功。
4. 哪个容器不健康。
5. 内网健康是否成功。
6. 公网转发是否成功。

恢复步骤请读[运维手册](OPERATIONS.md)。
复杂小镇发布请使用[日期版发布手册](runbooks/2026-08-15-town-p0-p3-rollout.md)。
