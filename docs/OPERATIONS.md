# Simverse World 运维手册

这篇文章写给日常照看生产服务的人。

它回答五个问题：

1. 服务现在健康吗？
2. 出错时先看哪里？
3. 数据怎样备份？
4. 新版本怎样回滚？
5. 功能开关怎样保持安全？

第一次发布请先读[部署说明](DEPLOYMENT.md)。

## 当前生产状态

普通游戏服务继续运行。

Lab 实验执行已经关闭。
原 ARM 服务器已经不可用。

当前 Lab 状态：

| 部分 | 状态 |
|---|---|
| 实验楼参观页面 | 保留 |
| 历史信息只读查看 | 保留 |
| 新实验执行 | 已关闭 |
| 实验结果发布 | 已关闭 |
| `lab-runner` | 已停止 |

完整证据见：

- [2026-08-24 Lab 停用报告](reports/ops-lab-shutdown-2026-08-24.md)

## 每次检查从这里开始

连接服务器后进入：

```bash
cd /opt/skills-world/deploy
```

先记录时间：

```bash
date -u +%Y-%m-%dT%H:%M:%SZ
```

再运行：

```bash
docker compose ps
curl -fsS http://localhost:8100/health
```

成功标志：

- `db` 是 `healthy`。
- `redis` 是 `healthy`。
- `api` 是 `healthy`。
- `agent-worker` 是 `healthy`。
- `hosted-agent-worker` 是 `healthy`。
- `/health` 返回成功。

`bootstrap` 成功后会退出。
它的退出码应为 `0`。

## 从公网再检查一次

服务器本机健康不代表玩家能访问。

再检查公网 API：

```bash
curl -fsS https://simverse-api.proxypool.eu.org/health
```

然后用浏览器打开：

```text
https://simverse.world
```

至少完成：

```text
登录 → 进入地图 → 打开一个普通功能
```

这样可以同时检查前端、外层转发和 API。

## 怎样看日志

先看最近十分钟：

```bash
docker compose logs --since=10m api agent-worker hosted-agent-worker
```

只看最后一百行：

```bash
docker compose logs --tail=100 api
```

持续观察 API：

```bash
docker compose logs --follow api
```

按 `Ctrl+C` 结束观察。
这不会停止容器。

### 日志里先找什么

先找反复出现的内容：

- 数据库连接失败。
- Redis 连接失败。
- 迁移失败。
- 进程不断重启。
- WebSocket 连接失败。
- 后台心跳过期。
- 同一个请求大量返回错误。

一次旧错误不一定表示现在仍故障。
请同时看时间和重复次数。

不要把完整 token 或用户资料复制进报告。

## 怎样检查单个容器

查看所有状态：

```bash
docker compose ps -a
```

查看 API 健康细节：

```bash
docker inspect deploy-api-1 --format '{{json .State.Health}}'
```

查看重启次数：

```bash
docker inspect deploy-api-1 --format '{{.RestartCount}}'
```

容器名可能因 Compose 项目名不同而变化。
先用 `docker compose ps` 确认真实名字。

## 怎样检查数据库迁移

先看迁移容器：

```bash
docker compose ps -a bootstrap
docker compose logs --tail=100 bootstrap
```

再看数据库记录的版本：

```bash
docker compose exec -T db \
  psql -U postgres -d skills_world \
  -c "select version_num from alembic_version;"
```

正常结果：

- `bootstrap` 退出码为 `0`。
- 日志没有异常堆栈。
- `alembic_version` 有一个当前版本号。

迁移失败时先停止发布。
不要反复重跑不知道影响的数据迁移。

## 怎样备份数据库

先创建只有管理员可读的目录：

```bash
install -d -m 700 /opt/skills-world/backups
```

为本次备份选择唯一文件名。
例如：

```text
/opt/skills-world/backups/skills_world-20260824T070000Z.dump
```

执行备份：

```bash
docker compose exec -T db \
  pg_dump -U postgres -d skills_world -Fc \
  > /opt/skills-world/backups/skills_world-20260824T070000Z.dump
```

检查文件不是空的：

```bash
test -s /opt/skills-world/backups/skills_world-20260824T070000Z.dump
```

检查备份目录能被读取：

```bash
docker compose exec -T db pg_restore --list \
  < /opt/skills-world/backups/skills_world-20260824T070000Z.dump
```

命令成功不等于恢复一定成功。
重要发布前要在隔离数据库试恢复。

### 还要备份什么

数据库之外，还要保存：

- 生产 `.env` 的受控备份。
- 用户上传文件。
- 已发布静态文件。
- 当前 Git 提交号。
- 当前可恢复镜像信息。

配置备份可能包含秘密。
不要把它放进 Git 或聊天记录。

## 数据库恢复规则

恢复会覆盖数据。
它是高风险动作。

恢复前必须：

1. 停止会继续写数据的服务。
2. 再备份一次当前故障数据库。
3. 确认恢复文件的时间和来源。
4. 在隔离数据库完成试恢复。
5. 获得项目负责人批准。

恢复后必须检查：

- 迁移版本。
- 关键表是否存在。
- 关键数据行数是否合理。
- 登录和地图用户路径。
- 后台工人是否恢复心跳。

不要在不确定目标库时执行恢复命令。

## 怎样回滚代码

回滚表示把程序换回上一个可用版本。

推荐顺序：

1. 停止继续发布。
2. 记录当前失败版本和日志。
3. 确认上一个已验证提交。
4. 检查新迁移是否向后兼容。
5. 从控制机器重新同步旧提交。
6. 用旧代码重建默认服务。
7. 检查健康和真实用户路径。

如果数据库结构不兼容旧代码，不能只回滚镜像。
请先由开发者判断迁移恢复方式。

回滚成功标志：

- 默认容器健康。
- 公网健康接口成功。
- 登录和地图成功。
- 后台工人心跳正常。
- 新错误不再持续出现。

## 功能开关怎么管理

功能开关让代码保留，但暂时不让用户使用。

常见开关：

| 开关 | 控制内容 | 安全默认值 |
|---|---|---|
| `LAB_ENABLED` | Lab 部署入口 | `false` |
| Redis `sv:lab:enabled` | Lab 运行时 | `0` |
| `LAB_EXECUTOR_ENABLED` | Lab 代码执行器 | `false` |
| `LAB_ARTIFACT_PIPELINE_ENABLED` | Lab 文件流水线 | `false` |
| `LAB_EGRESS_ENABLED` | Lab 受控联网 | `false` |
| `RESIDENT_SPRITE_ENABLED` | 居民精灵生成 | `false` |
| `HOSTED_AGENT_RUNNER_ENABLED` | 托管 Agent 控制器 | `false` |
| `AGENT_SELF_REGISTRATION_ENABLED` | 外部 Agent 自助注册 | `false` |

改 `.env` 后，相关容器必须重新创建。
否则进程可能继续使用旧值。

先在 API 容器中核对程序真实读取的值。
不要只看文件内容。

## Lab 关闭状态怎样复查

先检查部署开关：

```bash
grep '^LAB_ENABLED=' .env
```

期望：

```text
LAB_ENABLED=false
```

检查 Redis 开关：

```bash
docker compose exec -T redis redis-cli GET sv:lab:enabled
```

期望：

```text
0
```

检查执行容器：

```bash
docker compose ps -a lab-runner
```

期望是停止状态。

检查 API 真实设置：

```bash
docker compose exec -T api python -c \
  "from app.config import settings; print(settings.lab_enabled)"
```

期望：

```text
False
```

最后检查活动任务为零。
这项检查要使用只读数据库查询或管理接口。

如果任何一项不符合，先停止 Lab：

```bash
docker compose stop lab-runner
docker compose exec -T redis redis-cli SET sv:lab:enabled 0
```

然后恢复 `.env` 中的 `LAB_ENABLED=false`。
重新创建 API 后再检查一次。

不要启动旧 ARM 执行链。

## 危险动作表

| 动作 | 风险 | 动手前保护 | 成功标志 | 恢复入口 |
|---|---|---|---|---|
| `docker compose down -v` | 删除数据库和其他命名卷 | 不要在生产运行；先确认命令不带 `-v` | 本动作没有普通运维成功场景 | 立即停止；从已验证备份恢复 |
| 覆盖生产 `.env` | 丢失密码和开关，服务可能错误开放 | 先做受控备份；逐项比较；绝不复制示例密钥 | 容器读取正确值，健康和权限检查通过 | 恢复配置备份并重新创建容器 |
| 用根目录 Compose 当生产环境 | 启动普通 PostgreSQL 16 和 Redis 7，版本与生产不同 | 只在 `/opt/skills-world/deploy` 操作；先看当前目录 | `pgvector` PostgreSQL 16 和 Redis 8 健康 | 停止错误服务；用生产 Compose 恢复 |
| 没有备份就降级数据库 | 可能永久丢字段和数据 | 先做并试验备份；阅读 `downgrade()`；获得批准 | 版本和数据都符合目标，用户路径通过 | 停止写入；从变更前备份恢复 |

## 常见故障判断

### API 不健康

先看：

```bash
docker compose logs --tail=200 api bootstrap db redis
```

常见原因是迁移失败、数据库失败或配置缺失。

### 页面能开，但没有实时更新

检查 Redis 和 WebSocket。

```bash
docker compose ps redis
docker compose logs --tail=100 redis api
```

### 居民不行动

检查 `agent-worker`：

```bash
docker compose ps agent-worker
docker compose logs --tail=200 agent-worker
```

它的健康检查会检查关键后台循环的心跳。

### 公网失败，但本机健康

问题通常在外层转发、域名或证书。

不要先重建数据库。
先检查 Cloudflare 或 Nginx 到本机 API 的连接。

## 每次事件要记录什么

- UTC 时间。
- 影响了哪些用户功能。
- 当时的 Git 提交号。
- 容器状态。
- 关键日志的短摘要。
- 做过的动作。
- 每个动作的真实结果。
- 是否回滚。
- 最后走过的用户路径。

不要只写“已经修好”。
要写清怎样证明它恢复了。

复杂发布仍以[日期版发布手册](runbooks/2026-08-15-town-p0-p3-rollout.md)为准。
