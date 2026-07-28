# 2026-07-28 T1 部署报告 · vm212 049 → 051

> 执行时间 2026-07-28 01:17–01:26 UTC。部署对象 `origin/master` = `2d7c01d`。
> 本文是**生产真值记录**，按 07-27B H3 的约定落在 master，不再留在执行分支上。

## 1. 这次部署把什么送上了生产

生产此前停在 `049_add_policies`，跑的是 2026-07-25 15:32 构建的镜像。**中间积压了三个批次**：

| 批次 | 内容 |
|---|---|
| 管理系统「立刻做」 | 平台密钥读侧掩码、哨兵账号铸币收口、`grant_admin` 脚本、admin 授权扫描 |
| F1 / F2 / F3 | 声誉语义修复、公民权档位（迁移 051）、任期与卸任审计 |
| 07-27B 第一批 | `postpone_open_polls` 脚本、B2 的 FK 修复、CI/测试基线三项 |

**开关状态未变**：F1/F2/F3 的新旋钮全部默认关（`CIVIC_PROMOTION_MODE=off`、`REP_ENABLED=false`、`polis_office_mayor_term_days=0`），`RESIDENT_SPRITE_ENABLED` 保持 false。**部署本身不改变任何运行时行为**——要生效必须显式开闸，且 F2/F3 尚未接进 `nightly_cron`。

## 2. 分步执行，未合批

按「迁移 / 数据变更 / 网络变更不得同一次」的红线拆成三次：

| 步 | 动作 | 结果 |
|---|---|---|
| ① | `alembic upgrade head`（只跑迁移，不跑 seed） | 049 → 050 → 051 |
| ② | `python -m seed.reset_builtin_residents`（哨兵 + 幂等重播种） | 哨兵落地，居民零变化 |
| ③ | compose 换新（D1 端口收窄）+ 重建 api | 公网直连关闭，隧道未断 |

## 3. 前置安全检查

**purge 目标集实测为 0 才敢跑 seed。** `reset_builtin_residents` 会删「不在新名册里的 system NPC」，而 2026-07-25 16:53 的事故正是同类脚本造成的。没有靠推理，直接查库：

```sql
SELECT count(*) FROM residents
WHERE resident_type <> 'player'
  AND slug NOT IN (<PRESET_CHARACTERS 的 11 个 slug>);
-- → 0
```

生产 11 位居民的 slug 与 `PRESET_CHARACTERS` 逐字相同，`slug NOT IN (roster)` 匹配不到任何行，purge 是有保证的 no-op。

## 4. 回滚材料

| 类型 | 位置 |
|---|---|
| 数据库 | `/opt/skills-world/db-backup-0727B-20260728-011708.sql.gz`（26MB，已验 `PostgreSQL database dump complete` + 4 张关键表的 COPY 段） |
| 代码目录 | `/opt/skills-world/backend.bak-0727B-20260728-011708` |
| 镜像 | `deploy-api:rollback-0727B` (`e331d62b1806`)、`deploy-agent-worker:rollback-0727B` (`6cdac37c3953`) |
| compose | `/opt/skills-world/deploy/docker-compose.yml.bak-0727B-20260728-011708` |
| 构建日志 | `/opt/skills-world/build-0727B.log` |

**发代码用 `git archive`，没用 `deploy.sh`** —— 后者的 `rsync --delete` 的 exclude 列表漏了 `.env` 与 `tmp/`（07-27B G1），跑一次就会删掉生产 `.env`。tar 只覆盖不删除，实测部署后 `.env`（818 字节，mtime 仍是 Jul 23）与 `static/` 完好。

## 5. 验收证据

**数据零损**（迁移前后四张表逐字相同；users 的 +1 是哨兵行本身）

```
部署前: users=45 residents=11 polls=3 transactions=224
迁移后: users=45 residents=11 polls=3 transactions=224
seed后: users=46 residents=11 polls=3 transactions=224
        └ system|admin-console@skills.world（恰好 1 行）
```

**迁移链头**

```
$ alembic current
051_add_civic_standing_history (head)
```

**B2 在真 PostgreSQL 上生效**（若不修，任何审过 sprite run 的用户从此无法注销）

```
published_by   -> SET NULL
reviewed_by    -> SET NULL
rolled_back_by -> SET NULL
```

**五项修复确认在产**

| 项 | 判据 | 结果 |
|---|---|---|
| 政治层边界 | `CIVIC_VOTER_TYPES` / `SIM_RESIDENT_TYPES` / `UGC_RESIDENT_TYPE` | `['npc']` / `['npc','resident']` / `resident` |
| C1 密钥掩码 | `_mask("llm.api_key", <哨兵>)` | `********`；`_mask("llm.model","gpt-4")` → `gpt-4` |
| 哨兵铸币收口 | `app/services/system_users.py` | 存在 |
| `install_mayor` 结票复核 | 拒绝日志字符串 | 命中 |
| 流会分支 | `_winner_lost_civic_rights` | 命中 3 处 |

**D1 端口收窄，三条同时成立**

```
公网直连 http://<vm212>:8100/health   200 → 000（拒绝）
宿主回环 http://localhost:8100/health 200
隧道     https://simverse-api.proxypool.eu.org/health 200
前端     https://simverse.world/      200
```

cloudflared 是 `network_mode: host` 的容器、ingress 指向 `http://localhost:8100`，所以回环绑定不影响它——这一点在动手**之前**已从 `docker inspect` 与隧道日志确认，不是事后发现。

**世界仍在运行**

```
/health/loops: enabled=True stale=[]
  heat ok / event ok / nightly ok / agent ok / embedding_backfill ok
政治层边界探针: 居民合计 11；有政治权利 11；算世界人口 11
agent-worker: 持续处理 embedding 与人格演化（monthly budget exhausted 是既有的预算态，非故障）
```

## 6. 这次没做的

- **F2/F3 未接进 `nightly_cron`** —— 代码在库里但夜间链上不会调用它们，运行时是死的。属统一收口，见 ROADMAP 近期优先级 5。
- **D2 `--forwarded-allow-ips=*`** 字面仍在 `Dockerfile:26`。D1 修好后它的安全论证前提重新成立，但仍缺一道跨文件不变量测试锁住「端口一旦放宽就报警」。
- **T2 存量回填**在 vm212 上已无对象（泄漏实例实测为 0），保留脚本供其它环境用。
- **T3 投票分布**样本已于 07-25 取到（33 票），不需要新开 poll。

## 7. 下一个墙钟

三张在途 poll 于 `2026-07-31 23:29:43 UTC` 之后的第一次夜间 cron（= **2026-08-01 23:00 UTC**）关票。本次部署已把 `install_mayor` 的结票复核与流会分支送上生产，**那时的结票会走安全路径**：候选人全部不在籍 → 零写入 + 流会公告，不会出现「公告说某人当选、库里没有镇长」。
