# M1–M6 合并 + 部署方案（待拍板）

> 生成：2026-07-24。本地验收已全绿（见 `TEST_REPORT_M1-M6_2026-07-24.md`）。
> 本方案覆盖：提交切分 → 叠到生产线 → 复验 → 部署 → 回写 master。**尚未执行任何不可逆动作。**

## 0. 关键事实（决定"怎么合怎么部"）

| 事实 | 证据 |
|---|---|
| **生产跑的是 `port/prod-fixes-onto-044`（8a0449c），不是 master** | port/044 = master + 1 prod-fix 提交，alembic **045**；master alembic **044** 且相对 port/044 无独有提交；生产已含 static-404/deep-forge/账号删除500 修复 + alembic 045 |
| **master 落后生产** | master 缺 8a0449c、alembic 低一级。合并 M1-M6 进 master 再部署 master = 生产降级（045→044）+ 丢 prod-fix，**不可取** |
| **M1-M6 = 纯应用代码，零 schema 迁移** | 工作树无任何 `alembic/versions` 改动；所有 M 表（treasuries/relations/goals/dynamic_locations/system_config/polls）均既有已 tracked |
| **叠加冲突面极小** | HEAD vs port/044 仅 3 文件 DIFFER：`config.py`(+135)、`nightly_cron.py`(±25)、`.env.example`(+149)，全为 lab/realism 追加；其余（含 `preset_characters.py`+1170）**IDENTICAL**，M1-M6 改动干净落地 |
| **本地验收在 HEAD 老线（alembic 040）上跑的** | 需在 port/044 基线复验一次，确认与 lab/realism 无交互回归 |

## 1. 提交切分（Phase 1，可逆）

**纳入 M1-M6 提交**（应用代码 + 测试 + 文档）：
- 改：`backend/app/agent/chat.py`、`agent/phases/execute/basic.py`、`agent/prompts.py`、`config.py`、`routers/polls.py`、`services/{daily_quest,digest,encounter,gossip,shop_effects,shop_service}.py`、`tasks/{event_cron,event_templates,nightly_cron}.py`、`seed/{preset_characters,seed_residents}.py`、`tests/test_preset_import.py`、`.env.example`、`docs/PROGRESS.md`
- 新：`services/{arc,civic,duty,election}_service.py`、`scripts/memory_recall_eval.py`、`seed/reset_builtin_residents.py`、`tests/{test_duty_service,test_m1_economy,test_m2_arcs,test_m3_civic,test_m4_recall_eval,test_m5_space,test_m6_election}.py`、`docs/KICKOFF_PROMPT_M1-M6_TEST.md`、`docs/testing/TEST_REPORT_M1-M6_2026-07-24.md`

**排除（噪音/无关）**：
- `backend/skills_world_dev.db`（dev 库，deploy 也 exclude `*.db`）、`skills_world_dev.db.bak.*`、`backend/_to_delete/`、`Fable5-提示词指南.md`
- `backend/uv.lock`（+pillow 属 portrait 修复，非 M1-M6；port/044 已有 portrait，不需要）
- `tmp/`（已 gitignore）

**动作**：在当前 HEAD 切分支 `feat/town-m1-m6-20260724`，`git add` 上述精确清单，提交：
`feat(town): 小镇扩展 M1–M6 — economy/arcs/civic/space/election (零迁移, 门控默认on)`
提交尾附 `Verified-by:` 本地验收摘要（1311 baseline + T1-T10 + 边界 4/4）。

## 2. 叠到生产线（Phase 2，可逆，本地）

1. `git worktree add ../sv-m1m6-integ port/prod-fixes-onto-044` → 新分支 `integ/m1-m6-on-prod`
2. `git cherry-pick <M1-M6 commit>`；预期仅 `config.py`/`nightly_cron.py`/`.env.example` 冲突 → 加性解决（M-flag 块 + M-job 块 + M env 各留一份，保留 port/044 的 lab/realism 内容）
3. 其余文件（含 preset_characters +1170）自动干净应用

## 3. 生产基线复验（Phase 3，硬门，本地）

在 `integ/m1-m6-on-prod` 上、独立 sqlite + dummy key：
- `uv run pytest -q`（全量，须 0 failed；此时含 lab/realism 用例，基线数会 >1311）
- 6 个 M 系定向（须 40/54 全绿）
- `M_MODE=full uv run python tmp/m_harness.py` + `M_MODE=alloff` + `tmp/m_boundary.py`（须 8/8 + 1/1 + 4/4）
- **不绿不部署**（走 verify-before-done）

## 4. 部署 vm212（Phase 4，不可逆·对外·需单独授权）

- 目标：`simverse-api.proxypool.eu.org` 后端（vm212）——**这是对外生产**，且**正在 realism burn-in**（记忆红线"burn-in 期间不部署"）。部署即把 M1-M6 叠到在观测的 realism 世界。
- **部署前先核对 vm212 实际运行 SHA** = port/044(8a0449c)，避免叠错基线。
- 动作：`deploy/backend/deploy.sh <user@vm212>` → rsync backend + `docker compose up -d --build` + `/health`。M1-M6 零迁移 → alembic 保持 045。
- 冒烟：`/health` 200；`/openapi.json` paths；开一晚 nightly 或 admin force → 确认议案/选举真开出；登录+WS 基本链路。
- 回滚点：部署前记录 vm212 当前 commit / 镜像；异常则 `git checkout 8a0449c` 重部署。

## 5. 回写 remote main（Phase 5，不可逆·对外）

master 落后生产。为让 master 代表"生产 + M1-M6"：
- `git checkout master`（注意 master 在 `simverse-world-master-merge` worktree，需先确认无并发改动/抢锁）
- `git merge --no-ff integ/m1-m6-on-prod`（一并把 8a0449c prod-fix 带进 master，master 追平生产 + 得 M1-M6）
- `git push origin master`（代理大包可能假超时，分块/重试）

## 6. 待确认项（阻断执行）

1. **部署目标性质**：vm212 = 对外生产（proxypool 后端）还是可折腾测试环境？记忆里两种说法都有。
2. **burn-in 覆盖**：是否同意在 realism burn-in 期间部署 M1-M6（覆盖"不部署"红线）？还是只做 Phase 1-3+5（合并回 master）、Phase 4 部署延后？
3. **P2（内置居民缺 sbti）**：上线前补 or 记 backlog 后补？（非阻断，机制 fail-safe）
4. **master worktree 抢锁**：`simverse-world-master-merge` 是否有正在进行的工作？动 master 前需确认。
