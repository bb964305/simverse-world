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

两者都由 bootstrap 服务（`alembic upgrade head && python -m seed.reset_builtin_residents`）
建出来；`POST /admin/residents/presets` 另外会在插入前自愈调用一次，所以即使 seed 没跑过
也不会外键违约。

这两个账号**永远不该有余额**：`coin_service.reward_creator_passive` 跳过
`NON_USER_CREATOR_IDS` 里的每一个 id。想确认历史上有没有被误铸过：

    docker compose exec api python scripts/audit_system_minting.py

该脚本是纯只读的，只报数不改数据。

## 相关

- 端点与风险全貌：`docs/plans/2026-07-27-admin-immediate-fixes.md`
- 立绘审核后台的运维手册：`docs/RESIDENT_SPRITE_OPERATIONS.md`
