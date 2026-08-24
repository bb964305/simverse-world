# 小镇 P0-P3 上线与回滚手册

> 状态：P0/P1 已于 2026-08-15 按本文完成生产部署与开闸，证据见 [`../reports/ops-deploy-p0-p1-2026-08-15.md`](../reports/ops-deploy-p0-p1-2026-08-15.md)。P2/P3 行为闸仍关闭；迁移与后续开闸继续分车。

## 目标与边界

| 阶段 | 本批交付 | 安全边界 |
|---|---|---|
| P0 | 管理端经济运营快照、开关矩阵、贫困与工资 runway 告警 | 只读，不改变经济行为 |
| P1 | 一次性居民/镇库发展金；世界日消费 pass | 补贴按固定 key 幂等；每日先 claim 后消费，最多执行一次 |
| P2 | 商队 `MarketSession`、进口收藏、鉴定/定制服务、玩家交易 UI | 到访 + trading 双门；共享库存 CAS；玩家/到访/offer 限购；请求幂等 |
| P3 | 实验楼访客模式、封闭测试准入、成果进入市场候选审核 | 访客入口常开；只有完成任务的 clean + verified 产物可提名；管理员审核后仅展示，不执行产物代码 |

## 0. 暗上：只部署代码与迁移

1. 备份数据库，并记录当前镜像、`.env` 与 `alembic_version`。
2. 部署代码，执行 `alembic upgrade head`，期望单头为 `067_market_economy_loop`。
3. 保持下列新闸为 `false`：

   ```dotenv
   NPC_TRADE_WORLD_DAY_ENABLED=false
   MARKET_PLAYER_ENABLED=false
   LAB_ENABLED=false
   LAB_BETA_USER_IDS=[]
   ```

4. 保持既有经济闸原值，不要在迁移车里修改：`NPC_TRADE_ENABLED`、`TAX_CARRY_ENABLED`、`ITEM_STOCK_GUARD_ENABLED`、`CARAVAN_ENABLED`、`CARAVAN_LIFECYCLE_ENABLED`。
5. 验收 `/health`、`/health/loops`；管理端 `GET /admin/economy/operations` 应显示新 `economy` loop 与完整开关矩阵。

暗上回滚：恢复旧镜像。没有执行补贴或翻行为闸时，可按变更窗口策略降回 066；一旦已产生 P1-P3 业务数据，优先关闸保留迁移，不做破坏性 downgrade。

## 1. P0/P1：建立基线并注入最低流动性

1. 管理端读取 `GET /admin/economy/operations` 与 `GET /admin/economy/bootstrap`，保存以下基线：居民余额中位数、低于保留金人数、可消费人数、镇库余额、日工资预算与 runway。
2. 人工复核预览后调用：

   ```http
   POST /admin/economy/bootstrap
   Content-Type: application/json

   {"confirm": true}
   ```

3. 默认把自治居民补至 `12 SC`，把镇库补至 `7` 天保守工资预算。批次 key 为 `town-liquidity-v1`，重复调用只返回原批次，不重复发钱。
4. 对账：`economy_bootstrap_batches` 恰好 1 行；`economy_bootstrap_grants` 金额之和等于居民余额增量；镇库增量等于批次 `town_grant_sc`。
5. 已稳定运行 NPC 贸易后，再单独开启 `NPC_TRADE_WORLD_DAY_ENABLED=true`。新 loop 每 60 秒检查世界日期，但同一世界日只有 claim 胜者执行；此时 nightly 不再重复跑消费 pass。

P1 回滚：先关闭 `NPC_TRADE_WORLD_DAY_ENABLED`。发展金是已审计的经济事实，不反向扣回；通过降低后续工资/商队预算校准货币总量。

## 2. P2：商队市场分级开闸

按以下顺序，每一步单独重建容器并观察一个完整周期：

1. `MARKET_DAY_VENUE=market_hall`，确认地图集市坐标与 NPC 到访路径。
2. `ITEM_STOCK_GUARD_ENABLED=true`，确认所有库存消费者统一读写 `items.stock`。
3. `NPC_TRADE_ENABLED=true` 与 `TAX_CARRY_ENABLED=true`，观察居民内部交易守恒。
4. `CARAVAN_ENABLED=true`，观察外来收购、摊位费与进口货入库。
5. `CARAVAN_LIFECYCLE_ENABLED=true`，确认 `caravan_visits` 依次经过 waiting → inbound → trading → outbound → departed，重启不重复到访。
6. 最后开启 `MARKET_PLAYER_ENABLED=true`。

玩家验收：

- 靠近集市建筑按 `E` 或从顶部导航打开货单。
- 非 trading 阶段只可预览；trading 阶段显示共享余量与倒计时。
- 购买进口货后扣款一次、库存减一、家园库存出现对应收藏；同一幂等键重试不重复扣款。
- 同一玩家在同次到访对同一 offer 只能购买一次；售罄或阶段切换返回明确错误。
- 鉴定服务仅对玩家拥有的在售居民作品开放；定制灯饰每次到访限量。

P2 回滚顺序：`MARKET_PLAYER_ENABLED=false` → `CARAVAN_LIFECYCLE_ENABLED=false` → `CARAVAN_ENABLED=false` → `NPC_TRADE_WORLD_DAY_ENABLED=false`。已成交收据保留；不要删除购买记录。必要时只把仍在架的 `import_good` 设为 inactive。

## 3. P3：实验楼访客与封闭测试

1. 不论 Lab 开关如何，实验楼建筑与“参观 & 状态”页保持可进入，逐项显示部署、运行时、适配器、协议、扫描链和并发检查。
2. 先配置明确的内测用户 ID：

   ```dotenv
   LAB_BETA_USER_IDS=["user-id-1","user-id-2"]
   ```

   空列表沿用历史开放语义；生产封闭测试不得留空。
3. Codex Adapter 使用 v1 成本结算路径时，保持 `LAB_TERMINALIZER_V2_ENABLED=false`。配置 Adapter endpoint 后再开启 `LAB_ENABLED=true`，并以 `docker compose --profile lab up -d` 启动独立 runner。
4. 用内测账号跑通“报价 → 托管 → 执行 → 直播 → clean/verified 产物 → 验收”。非内测账号只能参观，发布应被拒绝。
5. 玩家可把自己的已完成、安全产物提名为商品/服务/合同候选；管理员在 Lab Runs 面板批准或拒绝。批准项只进入集市展示候选池，不直接成为可购买/可执行产品。

P3 回滚：关闭 `LAB_ENABLED` 或运行时 Redis kill switch；访客页与历史产物读取保持可用。撤销市场候选使用审核状态，不删除产物审计链。

## 持续观测与停止线

- 每日记录居民中位余额、贫困比例、可消费人数、镇库 runway、NPC/玩家市场成交量与 Lab 候选状态。
- 任一条件触发立即停止下一阶段：余额中位数连续下降、贫困比例超过 50%、镇库 runway 低于 3 天、库存出现负数/重复收据、同世界日消费执行两次、未扫描产物进入候选。
- 所有开关保持代码默认关闭；生产开闸必须是独立配置变更，并附开闸前后快照。
