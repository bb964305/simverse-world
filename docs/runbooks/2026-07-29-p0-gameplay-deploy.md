# P0 玩家实测修复批次 · 部署 runbook（2026-07-29）

**本批次零迁移。** alembic head 仍是 `051_add_civic_standing_history`，生产 `alembic current` 部署前后应当一致。这满足「迁移/数据变更 与 开闸/行为变更 不得同一次变更」的红线——本次只发行为变更。

存量数据处置（辩论退款、空日报回填、幽灵票重置）是**单独一次变更**，在代码上线并观察之后进行，见 §5。

---

## 1. 部署前必读：这次上线会立刻发生什么

代码一起来就有行为，不需要开任何闸。逐条推演过：

### 第一个 event_cron tick（≤60 秒内）

| 动作 | 结果 |
|---|---|
| `ensure_active_season` | **创建「第 1 季」**（28 天）。从此 `add_points` 不再 `return 0`，玩家聊天/委托/探索开始真正记分——这是 E7 的正确落地 |
| `drive_due_debates` 的超期兜底 | 辩论 `1c00ba36`（07-26 建，卡 &gt;24h）→ `_auto_draw_refund` → **玩家 stawky@linux.do 的 10 SC 全额退回**，辩论置 `settled/draw` |

超期兜底**先于** `run_live` 执行，所以不会为这场过期辩论再烧一轮 LLM。

### 第一个 nightly cron（次日 07:00 北京 = 23:00 UTC）

| 动作 | 结果 |
|---|---|
| `generate_village_digest` | 当天日报走 `chat()` 包装（thinking 关闭 + 2000 tokens）→ **玩家几天来第一次看到有正文的日报** |
| `close_due_polls` | 生产 3 张 poll 的 `closes_at` 已被推到 07-31，**今晚不关**。真正的考验是 07-31 之后的第一次夜间 cron |
| `maybe_open_seasonal_election` | 已有 open 选举 poll → 返回 None，不会撞车 |
| `run_npc_voting` | 3 张 poll 的 `_npc_voters` 是存量 `list` 格式 → 名册升级成 dict、幽灵 slug 移出名册，但 **`npc_votes` 计数一个都不减**（旧格式没存票的归属，减错票比留着更糟）→ 见 §5.3 |

### 不会发生的事（部署前请确认预期一致）

- **四天空日报（07-17/24/25/26）不会自动回填。** `generate_village_digest` 的唯一 cron 调用方恒为「今天」。回填要跑 §5.2 的脚本。
- **两张建筑议案的幽灵票计数不会自动清。** 它们的 `effect.type` 是 `dynamic_location`，不在 `_PERSON_TYPES` 里，不走结票归零。要跑 §5.3 的脚本。

### 已知的节奏变更

`SEASON_LENGTH_DAYS` 默认值定为 **28**（不是 14），理由是：开季后 `maybe_open_seasonal_election` 的 season 分支会永久接管，季长即选举节奏。设成 28 让镇长选举保持与 `ELECTION_INTERVAL_DAYS=28` 一致的现状，同时把季末 top-3 自动派彩（200/120/80 SC）的频率压到 28 天一次。**这是首次让该派彩路径可达**，请纳入经济观察窗口。

---

## 2. 回滚四件套（部署前必须全做完）

```bash
# (1) DB 备份
ssh vm212 'cd /opt/skills-world/deploy && docker compose exec -T db \
  pg_dump -U postgres skills_world | gzip > /root/skills_world-pre-p0-$(date +%Y%m%d-%H%M).sql.gz && \
  ls -lh /root/skills_world-pre-p0-*.sql.gz | tail -1'

# (2) 代码目录备份
ssh vm212 'cp -a /opt/skills-world/backend /opt/skills-world/backend.bak-p0-$(date +%Y%m%d-%H%M) && \
  ls -d /opt/skills-world/backend.bak-* | tail -1'

# (3) 镜像 tag
ssh vm212 'cd /opt/skills-world/deploy && \
  docker tag $(docker compose images -q api | head -1) skills-world-api:rollback-p0'

# (4) compose 备份
ssh vm212 'cp /opt/skills-world/deploy/docker-compose.yml \
  /opt/skills-world/deploy/docker-compose.yml.bak-p0'
```

---

## 3. 发代码

**绝不用 `deploy/backend/deploy.sh`** —— 它的 `rsync --delete` exclude 漏了 `.env` 和 `tmp/`，跑一次就删掉生产 `.env`（G1 未修）。

```bash
cd /Volumes/data/dev/simverse-world
git archive --format=tar master backend | ssh vm212 'tar -x -C /opt/skills-world'
```

前端另行部署到 CF Workers（`frontend/deploy.sh`；`wrangler deploy` 会假超时，实际约 42s 成功，用 bundle 哈希核实）。

---

## 4. 重启与验证

容器重建**独占一次变更**，不与数据操作混做。

```bash
# --wait：等 api 的 healthcheck 真正通过再返回，而不是"容器创建完"就返回
ssh vm212 'cd /opt/skills-world/deploy && docker compose up -d --build --wait api agent-worker'

# 迁移版本必须与部署前一致（本批次零迁移）
ssh vm212 'cd /opt/skills-world/deploy && docker compose exec -T api alembic current'
```

### ⚠️ 这条命令会连带触发 bootstrap

`api` 的 `depends_on` 里有 `bootstrap: condition: service_completed_successfully`，所以 `up api` **必然先跑一次 bootstrap 容器**。bootstrap 会执行 `reset_builtin_residents`——就是 2026-07-25 删掉 12 个玩家角色的那类脚本。

2026-07-29 那次部署实测是安全的 no-op（日志 `No old built-in residents found.` + `Seeded 0 new residents.`，居民数 `npc=11/player=2` 未变），因为当前 NPC 全在 `PRESET_CHARACTERS` 名册里。**但这不是永久保证**：一旦世界里出现不在名册里的 system NPC，这次部署就会删掉它们。

每次部署后**必须**立刻核对：

```bash
ssh vm212 'cd /opt/skills-world/deploy && docker compose logs --tail=30 bootstrap 2>&1 | grep -iE "found|seeded|removed|delete"'
ssh vm212 'docker exec deploy-db-1 psql -U postgres -d skills_world -c \
  "SELECT resident_type, count(*) FROM residents GROUP BY resident_type;"'
```

对不上就立刻用 §2 的 DB 备份回滚。

### healthcheck 能做什么、不能做什么

`api` 服务此前是**唯一没有 healthcheck 的服务**（其余 9 个都有），于是 `docker compose up -d` 在容器**创建**后就返回，而容器的 CMD 要先跑完 `alembic upgrade head` 才轮到 uvicorn 绑端口。这中间端口已发布但连接被拒——cloudflared 把它转成一个**没有 CORS 头**的失败响应，浏览器则把它显示成 CORS 错误。E2E-06 最初被误判成"CORS 配置不一致"就是这么来的（实测证明 CORS 头在所有能抵达 FastAPI 的路径上都稳定正确）。

加上 healthcheck 之后：

- **能**：`--wait` 会等到 API 真的能应答才返回，部署脚本不再"假完成"；容器状态里能看到 `healthy`/`unhealthy`；未来接监控有了抓手。
- **不能**：**这不是零停机部署**。单容器 + 固定发布端口，`stop → start` 的间隙依然存在，只是从"停旧→构建→起新→迁移→就绪"缩短到"停旧→起新→迁移→就绪"（构建本来就在旧容器还活着的时候完成）。要真正消除窗口需要蓝绿（两个 API 服务 + 可切换的上游），那是独立的改造。

**所以部署仍应挑低峰期。**

探测命令用 python 而非 curl（镜像 `python:3.12-slim` 只额外装了 `gcc libpq-dev`，没有 curl/wget），并且显式 `ProxyHandler({})` 禁用代理——urllib 会读 `http_proxy` 环境变量，容器一旦继承到代理变量，回环探测就会被路由出去，把健康的 API 报成 unhealthy（本地已实测复现：清掉 `NO_PROXY` 后旧写法退出码 1、新写法 0）。

### 上线后 5 分钟内核查

```bash
# 赛季真的开了
ssh vm212 'docker exec deploy-db-1 psql -U postgres -d skills_world -c \
  "SELECT id,title,status,starts_at,ends_at FROM seasons;"'

# 辩论押金真的退了
ssh vm212 'docker exec deploy-db-1 psql -U postgres -d skills_world -c \
  "SELECT status,winner,settled_at FROM debates; \
   SELECT amount,payout FROM debate_stakes; \
   SELECT reason,amount FROM transactions WHERE reason LIKE '\''debate%'\'' ORDER BY created_at DESC LIMIT 5;"'

# 内部字段不再出网
curl -s https://simverse-api.proxypool.eu.org/polls/open | grep -c '_npc_voters'   # 期望 0

# 玩家能投票了（前端）：进 /seasons，确认渲染出中文 label 且不是「页面出错了」
```

### 次日核查

```bash
# 日报有正文了
ssh vm212 'docker exec deploy-db-1 psql -U postgres -d skills_world -c \
  "SELECT date,title,length(content_md) FROM digests WHERE scope='\''village'\'' ORDER BY date DESC LIMIT 3;"'
```

---

## 5. 存量数据处置（**单独一次变更**，代码观察一晚之后）

三项都已获授权。生产镜像里没有 `backend/scripts/` 的新文件（Dockerfile 的 `COPY . .` 早于它们，api 服务无源码 bind mount），用 heredoc 注入：

```bash
ssh vm212 'cd /opt/skills-world/deploy && docker compose exec -T api python - <参数>' < backend/scripts/<脚本>.py
```

**每个脚本都默认 dry-run，先看差异报告，确认目标集符合预期再加 `--apply`。**

### 5.1 辩论 10 SC 退款

**无需脚本** —— 上线后第一个 event_cron tick 的超期兜底会自动处理（见 §1）。按 §4 的核查命令确认 `payout` 已写、`transactions` 有 `debate_refund:` 流水即可。若未发生，检查 `agent-worker` 是否在跑（`run_background_tasks`）。

### 5.2 四天空日报回填

```bash
# 先看目标集（不调 LLM、不写库）
ssh vm212 '... exec -T api python -' < backend/scripts/refill_empty_digests.py
# 确认列出的正是 2026-07-17/24/25/26 四天，再真跑
ssh vm212 '... exec -T api python - --apply' < backend/scripts/refill_empty_digests.py
```

会真调 LLM 生成正文，注意 `llm_usage` 会多四条 `scenario=digest`。

### 5.3 幽灵票重置（**硬期限：2026-08-01 23:00 UTC 之前**）

三张 poll 的 `closes_at` 是 07-31，但**关票发生在其后的第一次夜间 cron**（每天 23:00 UTC），也就是 **08-01 23:00 UTC**。必须在此之前跑完。

```bash
ssh vm212 '... exec -T api python -' < backend/scripts/reset_legacy_poll_votes.py
ssh vm212 '... exec -T api python - --apply' < backend/scripts/reset_legacy_poll_votes.py
```

脚本只处理 `_npc_voters` 仍是 legacy `list` 格式的 open poll，把 `npc_votes` 清零 + 名册清空，让下一轮 `run_npc_voting` 用**当前**名册从零重投。legacy 格式下所有投票人的归属都是未知的（不只是幽灵的），所以整体重置是唯一在信息论上站得住的订正。

**镇长选举那张**：四个候选人全部已被删除。结票时会走「有效候选均已不在名册上 → 流会」分支，不会再出现「某个不存在的人以 N 票当选」。之后 `election_service.maybe_open_seasonal_election` 会在下一季用当前名册重开一张。

---

## 6. 已知取舍（排障时先看这里）

| 现象 | 这是设计如此 |
|---|---|
| 某天完全没有日报（不是空白，是没有那一行） | LLM 失败/返回只有标题行时抛 `DigestComposeEmpty` 且**不落库**。「缺失」优于「空白」——空行会被 `(scope,date,user_id)` 幂等永久钉死。但当日 anchor 已被 `_claim_run_date` 消费，不会自动重试，需要跑 §5.2 的脚本 |
| Redis 不可用时辩论全判平局 | `vote()` 依赖 Redis 去重，Redis 挂了玩家投不了票 → 票数相等 → 平局 → **全额退款**。资金侧 fail-safe（少赚不亏） |
| 辩论投票窗口偶尔短于 60 分钟 | `_mark_voting_since` 用的是 cron 轮次开始时的 `now`，同一 tick 内多场辩论共用它，靠后的辩论实际窗口会被削掉一些（每场 6 次 LLM 串行）。不影响资金正确性 |
| `event_cron` 日志里 `E3 debate driver step failed` | 可能根因在同一轮的上一个块（C3）——两块共用一个 session。已补 rollback，但排障时值得往上一个块看一眼 |

### 未修的已知隐患（follow-up）

- `settle` / `_finish_draw` / `_auto_draw_refund` / `settle_season` 都是**先 commit 置终态、再循环发钱**。进程在这个窗口内崩溃 → 尾部玩家的退款/派彩永久丢失（重入被幂等挡掉）。这是既存写法，但本批次第一次把它们接到 60s 一轮的自动 cron 上、对着真钱跑。修法是把 `coin_service.reward_pending`（flush-owned、不 commit）纳入同一事务，跨模块改动，单独一张票。
- `ensure_active_season` 与 admin 开季都是 check-then-insert，无 DB 约束防止两个 active season。零迁移下无法加 partial unique index，留待下批次。
