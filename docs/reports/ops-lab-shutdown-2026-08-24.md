# Lab 实验执行停用报告

日期：2026-08-24

服务器：`vm212`

## 一句话结论

Lab 实验执行已经关闭。

普通游戏 API 仍然健康。
Lab 的参观页面和历史信息可以保留。

原 ARM 服务器已经不可用。
这是项目负责人的停用背景说明。

它不是从 vm212 容器状态推断出来的结论。

## 这份报告记录什么

这份报告保存一次真实运维变更的证据。

它记录：

- 变更前的开关和任务状态。
- 保存的配置备份。
- 实际执行的关闭动作。
- 变更后的内网和公网检查。
- 将来允许恢复的条件。

报告不包含 token、用户 ID 或密钥。

## 变更范围

只关闭 Lab 实验执行。

没有关闭：

- 登录。
- 普通地图。
- AI 居民后台工人。
- 托管 Agent 工人。
- Lab 参观页面。
- 已有实验历史的只读查看。

## 变更前证据

第一次核验时间：

```text
2026-08-24T06:33:41Z
```

`Z` 表示 UTC 时间。

当时看到：

| 检查项 | 变更前结果 |
|---|---|
| 服务器目录 | `/opt/skills-world/deploy` |
| 部署开关 | `LAB_ENABLED=true` |
| Lab 适配器 | `LAB_ADAPTER=mock` |
| 测试用户名单 | 已配置；报告不保存用户 ID |
| Redis 运行时开关 | `1`，表示开启 |
| 正在活动的 Lab run | `0` |
| 历史 run | `succeeded: 2` |
| 历史 task | `completed: 1`，`rejected: 1` |
| `lab-runner` | 健康运行 |

run 是一次完整实验运行。
task 是运行中的一项小任务。

活动 run 为 `0`，所以关闭时没有打断正在执行的实验。

## 保存的备份

修改 `.env` 前，保存了这个文件：

```text
/opt/skills-world/deploy/.env.before-lab-disable-20260824T063451Z
```

备份保存在 vm212。
它可能包含生产秘密，不能提交到 Git。

报告只记录文件位置。

## 执行的关闭动作

变更时间约为：

```text
2026-08-24T06:34:51Z
```

按下面顺序执行：

1. 备份生产 `.env`。
2. 把 `LAB_ENABLED` 改为 `false`。
3. 把 Redis 的 `sv:lab:enabled` 改为 `0`。
4. 停止 `lab-runner` 容器。
5. 重新创建 API 容器，让新配置生效。

这里有三层关闭：

| 关闭层 | 结果 | 作用 |
|---|---|---|
| 部署配置 | `LAB_ENABLED=false` | API 不允许新实验 |
| Redis 运行时开关 | `sv:lab:enabled=0` | 运行时暂停取任务 |
| 执行容器 | `lab-runner` 已停止 | 没有进程执行实验 |

三层一起关闭，可以避免单个开关失效。

## 变更后即时检查

关闭后看到：

| 检查项 | 结果 |
|---|---|
| 服务器本机 `/health` | HTTP `200` |
| API 容器中的设置 | `False` |
| Redis 运行时开关 | `0` |
| `lab-runner` | `Exited (0)` |
| 容器重启规则 | `unless-stopped` |
| 活动 Lab run | `0` |
| API 最近错误 | 没有发现新的相关错误 |

`Exited (0)` 表示容器正常停止。
它不是崩溃。

使用已登录请求检查 `GET /users/me`：

```json
{"authenticated": true, "lab_enabled": false}
```

使用已登录请求检查 `GET /lab/status`：

```json
{
  "visitor_open": true,
  "deploy_enabled": false,
  "runtime_enabled": false,
  "publish_allowed": false,
  "blockers": [
    "deploy_disabled",
    "runtime_paused",
    "beta_access_required"
  ]
}
```

这些结果表示：

- 参观页面仍可打开。
- 新实验不能部署。
- 运行时已经暂停。
- 实验结果不能发布。

## 延时复查

第二次只读核验时间：

```text
2026-08-24T07:07:33Z
```

复查结果：

| 检查项 | 结果 |
|---|---|
| `.env` 部署开关 | `LAB_ENABLED=false` |
| Redis 运行时开关 | `0` |
| API 读取到的开关 | `False` |
| `lab-runner` | `Exited (0)`，已停止约 32 分钟 |
| 活动 Lab run | `0` |
| vm212 本机健康 | HTTP `200` |
| 公网 API 健康 | HTTP `200` |

这次复查没有修改服务器。

## 当前用户会看到什么

普通玩家仍可使用普通游戏功能。

进入实验楼时，可以看到只读介绍或历史内容。
不能开始新的实验执行。

前端会从 `/users/me` 读取 `lab_enabled=false`。
因此 Lab 执行入口不会作为可用功能展示。

## ARM 服务器说明

项目负责人已经确认：

> ARM 服务器已经不再可用。

所以旧执行链不能作为恢复方案。

vm212 上的关闭证据只能证明：

- API 已拒绝新 Lab 执行。
- Redis 已暂停 Lab 运行时。
- `lab-runner` 已停止。

它不能证明旧 ARM 主机重新可用。

## 什么时候可以恢复

现在不要恢复。

将来只有下面条件全部满足，才可以讨论恢复：

- 新的 ARM 或替代执行平台可用。
- 项目负责人批准重新开放。
- 新平台身份和密钥已经重新配置。
- 网络隔离、镜像和文件扫描通过核验。
- Staging 真实实验成功。
- 关闭开关和故障恢复完成演练。
- 普通游戏路径不受影响。

旧 `.env` 备份只能帮助查看旧配置。
不能直接把它当成安全恢复按钮。

恢复前必须重新检查其中每个 Lab 设置。

## 如果意外重新开启

立即检查：

1. `.env` 中 `LAB_ENABLED` 是否仍为 `false`。
2. Redis 的 `sv:lab:enabled` 是否仍为 `0`。
3. `lab-runner` 是否仍为停止状态。
4. 活动 Lab run 是否仍为 `0`。
5. `/users/me` 是否仍返回 `lab_enabled=false`。

发现任何一项不符合时，先停止 Lab。
不要在没有新运行平台时尝试执行任务。

日常检查方法见[运维手册](../OPERATIONS.md)。
