# 生产部署与 P0/P1 基线报告（2026-08-15）

## 结论

2026-08-15 已将小镇消费与场所激活 P0-P3 代码暗上至 vm212，并将数据库迁移到单头 `067_market_economy_loop`。随后按独立配置变更完成 P0/P1：执行一次性发展金、开启世界日消费，并取得幂等、每日 claim、余额与健康证据。

本次没有开启 P2 玩家市场或 P3 实验执行：`MARKET_PLAYER_ENABLED=false`、`LAB_ENABLED=false`。既有商队链路保持原生产配置，不以代码部署替代后续 P2 灰度验收。

## 发布与恢复材料

| 项目 | 生产证据 |
|---|---|
| 后端节点 | `vm212`，API、agent worker、hosted-agent worker 均健康 |
| 数据库迁移 | `067_market_economy_loop (head)` |
| 前端入口 | `https://simverse.world`，入口资源 `/assets/index-DxFVMPfF.js` |
| Cloudflare Worker 版本 | `8c8bb867-8a71-46ff-9e04-dcb2084c8252` |
| 数据库备份 | `/opt/skills-world/deploy/db-backup-p0p3-20260815-074642.sql.gz` |
| 数据库备份 SHA-256 | `30885244a779ef8a9ccee9ba34d072c7d250845570d41b210ad509d013269442` |
| 后端代码备份 | `/opt/skills-world/backend.bak-p0p3-20260815-074642` |
| Compose 备份 | `/opt/skills-world/deploy/docker-compose.yml.bak-p0p3-20260815-074642` |
| P1 开闸前 `.env` 备份 | `/opt/skills-world/deploy/.env.bak-p0p1-world-day-20260815-080004` |

数据库备份已通过 `gzip` 完整性检查，大小约 449 MiB。P1 已产生正式经济账目，因此回滚优先关闭 `NPC_TRADE_WORLD_DAY_ENABLED` 并保留迁移和补贴审计记录，不反向扣回发展金。

## P0/P1 基线

时间均为 UTC。补贴前快照取于 `2026-08-15T07:57:16.680093+00:00`；补贴后、世界日消费前快照取于 `07:59:31`；首个世界日消费后快照取于 `08:01:07`。

| 指标 | 补贴前 | 补贴后、消费前 | 首个世界日消费后 |
|---|---:|---:|---:|
| 居民数 | 14 | 14 | 14 |
| 居民余额合计 | 574 SC | 665 SC | 654 SC |
| 居民余额中位数 | 2 SC | 12 SC | 12 SC |
| 最低余额 | 0 SC | 12 SC | 6 SC |
| 最高余额 | 250 SC | 250 SC | 235 SC |
| 低于 5 SC 人数 | 8 | 0 | 0 |
| 贫困比例 | 57.14% | 0% | 0% |
| 可负担最便宜进口货人数 | 6 | 14 | 13 |
| 可购买居民作品人数 | 5 | 5 | 6 |
| 镇库余额 | 45 SC | 45 SC | 46 SC |
| 日工资预算 | 4 SC | 4 SC | 4 SC |
| 镇库工资 runway | 11.25 世界日 | 11.25 世界日 | 11.5 世界日 |
| 运营告警 | 无 | 无 | 无 |

生产工资 runway 目标为 7 天；镇库原有 45 SC 已高于目标 28 SC，因此本批没有向镇库增发货币。

## 一次性发展金证据

- 固定批次 key：`town-liquidity-v1`。
- 批次 ID：`81d4bfa1-223a-4e9b-90e5-b18a2e9d87ed`。
- 8 位居民共获得 91 SC，全部补至最低 12 SC；镇库补助为 0 SC。
- 数据库中批次恰好 1 行、grant 恰好 8 行、grant 合计 91 SC。
- 通过正式管理员 API 重复提交后返回同一批次，没有再次发钱，证明请求幂等。

| 居民 | 补贴前 | 补贴后 | 增量 |
|---|---:|---:|---:|
| a-lan | 3 | 12 | 9 |
| bai-xing | 0 | 12 | 12 |
| chen-yu | 0 | 12 | 12 |
| gu-mingyuan | 1 | 12 | 11 |
| gu-wanzhou | 0 | 12 | 12 |
| jiang-lin | 1 | 12 | 11 |
| su-xiaoman | 0 | 12 | 12 |
| zhao-qiwen | 0 | 12 | 12 |

## 世界日消费证据

`NPC_TRADE_WORLD_DAY_ENABLED=true` 作为迁移部署后的独立配置变更开启，仅重建 API 与 agent worker。

- 当前世界日为 `2028-06-25`。
- claim 键 `npc_trade_world_day:2028-06-25` 在数据库中恰好 1 行。
- 首次 pass 日志为 `3 purchases for 25 SC (tax 1)`。
- 同日手动重放返回 `bought=0`、`spent=0`、`tax=0`、`claimed=false`。
- 重放后 claim 仍为 1 行，发展金批次仍为 1 行。

这证明世界日消费采用“先 claim、后执行”的单次语义，nightly 不会在同一世界日重复消费。

## 发布后功能与健康检查

- 生产 OpenAPI 已包含 `/admin/economy/operations`、`/admin/economy/bootstrap`、`/markets/current`、`/lab/status`。
- 生产前端包含管理端“居民经济运行与开闸状态”“一次性发展补助”，以及玩家端“参观 & 状态”“商队集市”“集市大厅”入口。
- `embedding_backfill`、`economy`、`hosted_agent` 等后台 loop 均为健康状态；日志未发现实际 ERROR。检索命中的 `failed=0` 是普通 INFO 字段。
- 前端构建和 Cloudflare 发布成功。构建期 `npm audit` 报告 9 个依赖告警（1 low、8 high），本次未阻断发布，应另开依赖治理变更处理，避免与经济开闸同车。

## 当前开关与下一观察窗

| 开关 | 当前生产值 | 说明 |
|---|---:|---|
| `NPC_TRADE_WORLD_DAY_ENABLED` | `true` | P1 已开闸并完成首日单次执行验证 |
| `MARKET_PLAYER_ENABLED` | `false` | P2 玩家购买行为未开放 |
| `LAB_ENABLED` | `false` | P3 实验执行未开放；访客/状态能力随代码部署 |

进入 P2 前至少连续观察 3 个世界日，并逐日记录余额中位数、贫困比例、可消费人数、镇库 runway、消费笔数及 claim 数。若余额中位数连续下降、贫困比例超过 50%、runway 低于 3 天、同日 claim 超过 1 或出现负库存/重复收据，立即关闭世界日消费并停止推进下一阶段。
