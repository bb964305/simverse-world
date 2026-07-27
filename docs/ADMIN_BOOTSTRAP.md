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

这两个账号**理论上不该有余额**，但目前只有 `coin_service.reward_creator_passive`
真正按 `NON_USER_CREATOR_IDS`（两个 id 都挡）跳过铸币；其余几处铸币/分账口径各不
相同，都还没收紧（收紧是单独排期的任务：`shop_effects.py` 那两处判断跟
`_skim_town_tax` 共用一个 `if`，改动会牵动镇税抽成，需要自己的经济行为测试）：

- `shop_effects.py` 的 `gift_share:`（约第 233 行）与 `tip_share:`（约第 297 行）
  只判断裸字面量 `"system"`——恰好挡住 `ADMIN_CREATOR_ID`（值本身就是
  `"system"`），但挡不住 `SYSTEM_CREATOR_ID`（UUID），内置 NPC 会从这两处漏进
  余额。
- `lab_terminalization_service.py` 的 `lab_reward:` 分账（约第 201 行，lab 功能线，
  默认关闭）同样只判断裸字面量 `"system"`，同样挡不住 `SYSTEM_CREATOR_ID`。
- `ws/handlers/rating.py` 的 `good_rating:` 奖励（约第 92 行）完全没有哨兵判断，
  两个哨兵账号都能从这里直接拿到余额。
- `investment_service.py`（约第 55 行）也只判断裸字面量 `"system"`，但那里只发
  通知、不铸币，影响较小。

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
