# 管理后台：新环境引导

## 第一个管理员从哪来

`is_admin` 默认 `false`，而唯一的线上写入口 `PATCH /admin/users/{user_id}` 自身就要求
admin 身份——所以新部署的环境里没有任何人能通过界面拿到管理员权限。用脚本：

    docker compose exec api python scripts/grant_admin.py --email you@example.com

默认是 dry-run，只打印将要发生什么。确认无误后加 `--apply`：

    docker compose exec api python scripts/grant_admin.py --email you@example.com --apply

前提是该 email 已经注册过（先在前端正常注册/OAuth 登录一次）。

### 降权

    docker compose exec api python scripts/grant_admin.py --email x@example.com --revoke --apply

脚本拒绝降掉最后一名管理员。注意后台的 `PATCH /admin/users/{id}` 目前**没有**这个约束，
两名管理员互相降权仍可能把数量降到零——真降到零就只能用本脚本救（它按 email 找人，
不需要任何现存管理员）。

## 两个哨兵账号

`residents.creator_id` 是 `users.id` 的外键，有两个非人类所有者，都在
`backend/app/services/system_users.py` 里定义：

| 常量 | 值 | 谁用它 | 建行的地方 |
|---|---|---|---|
| `SYSTEM_CREATOR_ID` | `00000000-…-0001` | seed 内置角色班底 | `seed_residents.ensure_system_user()` |
| `ADMIN_CREATOR_ID` | `system` | admin 控制台建的预设居民 | `system_users.ensure_admin_creator_user()` |

日常部署的 `bootstrap` 现在**只**执行 `alembic upgrade head`，不会再隐式重置花名册。
全新环境迁移完成后，用非破坏性的 `python -m seed.seed_residents` 建
`SYSTEM_CREATOR_ID` 和初始班底；`POST /admin/residents/presets` 会在插入前自愈创建
`ADMIN_CREATOR_ID`，所以不会外键违约。

## 内置花名册重置：默认只预览

重置脚本可能删除居民及其依赖历史，不能绑在日常部署上。独立的 compose 服务受
`ops-roster-reset` profile 隔离，而且默认命令只读：

    docker compose --profile ops-roster-reset run --rm roster-reset

记录预览里的目标数 `N`，停止 `api`/`agent-worker` 并验证数据库备份后，才用同一个
镜像显式覆盖命令：

    docker compose --profile ops-roster-reset run --rm roster-reset \
      python -m seed.reset_builtin_residents --apply --expect-targets N

若实际目标数与 `N` 不同，脚本会在第一笔写入前拒绝执行。正常的
`docker compose up` 不启用这个 profile，也不会运行 reset。

这两个账号**理论上不该有余额**，现在所有铸币/通知口径都统一从
`system_users.NON_USER_CREATOR_IDS` 导入判断，两个哨兵可靠地被挡在外面
（`coin_service.reward_creator_passive`、`shop_effects.py` 的 `gift_share:` /
`tip_share:`、`lab_terminalization_service.py` 的 `lab_reward:` 分账、
`ws/handlers/rating.py` 的 `good_rating:` 奖励、`investment_service.py` 的通知）。
`gift_share:`/`tip_share:` 与 `_skim_town_tax` 共用一个 `if`：narrowing 只跳过
了付给创建者的那笔分成，镇税抽成不受影响，仍按原逻辑照收（各自有独立测试覆盖）。

想确认历史上有没有被误铸过：

    docker compose exec api python scripts/audit_system_minting.py

该脚本是纯只读的，只报数不改数据。

## 配置密钥（api_key 等）：只能轮换，不能从后台清空

`GET /admin/system/groups/{group}` 与 `GET /admin/system/entries` 对 `api_key`/`secret`/
`token`/`password` 结尾的字段（`_SECRET_KEY_SUFFIXES`，`backend/app/routers/admin/system_config.py`）
一律回传掩码字面量 `********`，从不下发真实值——`llm.api_key`、`portrait.api_key` 都在此列。
注意是 `api_key` 而不是裸的 `key`：新加一个叫 `xxx_key`（不带 `api_` 前缀）的字段不会被
自动掩码，得手动加进后缀表或换个名字。

写侧规则（`PUT /admin/system/entry`、`PUT /admin/system/batch` 都一样）：

- 面板保存时该字段留空，或者带着掩码 `********` 原样回传（未改动过的字段就是这样），
  都视为「不修改」，后端跳过写入，DB 里的旧值原样保留。
- 填入一个非空的新字符串才会真正轮换该密钥。

**没有把已存密钥「清空、回退到 `.env` 默认值」的入口**——`system_config.py` 里没有任何
重置/删除某条 config 的端点（`admin/` 下别的模块有自己的 DELETE 端点，比如
`residents.py` 的 `/presets/{resident_id}`，但那些管的是别的资源，跟 config 密钥无关）。
这是刻意的取舍，不打算补。一旦 `SystemConfig` 表里写过一条 `llm.api_key`，这条 DB 覆盖值
就会一直优先于 `.env` 里的默认值；唯一的退回方式是直接操作数据库（删掉那一行，或者把
`value` 手工改回想要的默认字符串）。

## 相关

- 端点与风险全貌：`docs/plans/2026-07-27-admin-immediate-fixes.md`
- 立绘审核后台的运维手册：`docs/RESIDENT_SPRITE_OPERATIONS.md`
