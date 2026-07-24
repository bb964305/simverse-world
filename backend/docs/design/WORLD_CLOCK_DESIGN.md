# 世界时钟设计说明（agent-T / 设计阶段）

> 结论先行：新建 `app/world_clock.py` 作为**唯一时间换算入口**；时间语义锚**东八区 Asia/Shanghai (UTC+8)**，废除 UTC 锚；世界时 = `WORLD_EPOCH + k×真实流逝`，`k=WORLD_CLOCK_K=4`（1 真实天 = 4 世界天，每 6 真实小时一个完整昼夜）。凡"居民作息/星期节律/日报叙事/每晚梦境/未来年龄任期"读**世界时**；凡"LLM 预算日结/cron 运维/TTL 限流冷却/日志时间戳/TLS"保持**真实时**。**daily-action 计数保持真实天锚定**（否则真实日花费 ×4 击穿预算，见 §4）。

## 1. 两类时间归属表

| 语义项 | 代码位置 | 归属 | 换算入口调用 |
|---|---|---|---|
| 居民作息调度（wake/sleep/peak/social 小时） | `loop.py:150-151` `current_hour/current_weekday`；喂 `scheduler.get_activity_probability`(:169) / `should_tick`(:192) | **世界时** | `world_hour()` / `world_weekday()` |
| 睡眠居民代谢唤醒窗口 | `loop.py:156` `_metabolize_sleepers(current_hour, current_weekday)` | **世界时** | 同上（随上游传入） |
| 周末作息（Sat/Sun 睡懒觉+社交位） | `scheduler.py:29-44` `_apply_weekend(weekday)` | **世界时** | `world_weekday()` |
| 计划生成/去重日期 key | `plan/basic.py:80,86,246` `generated_date`；`plan/basic.py:155` "昨天"；`decide/basic.py:293-296` `today_key` | **世界时** | `world_date_key()` |
| 集市日 weekday=5 判定 | `event_templates.py:79` `day.weekday()==market_day_weekday` | **世界时**（选哪天办市集）；活动 active 窗口仍真实时（见 §5 注） | `world_weekday()` |
| 节日/holiday 排期 | `event_templates.py:58-71` `today + timedelta` | **世界时**（日历日） | `world_date_key()` / `now_world().date()` |
| 日报/叙事日期、`world_time` 展示 | `schemas.py:81` `get_world_time()`（当前 `datetime.now()`） | **世界时** | 重写为读 `now_world()` |
| 每晚梦境 / 每日 digest 的"当天"戳 | `dream_service.py:52,100,124`；`digest_service` | **世界时**（日期 key/叙事）；**触发**仍随 cron 真实日（见 §5） | `world_date_key()` |
| 周日 A1 目标评估 / 周一关系衰减 | `nightly_cron.py:183,221` `datetime.now(UTC).weekday()` | **世界时**（改世界周 index 门，见 §5） | `world_weekday()` / `world_week_index()` |
| 未来年龄 / 任期接口 | 预留 | **世界时** | `now_world()` |
| ——分界—— | | | |
| LLM 预算日结 `budget_global_daily_usd` | `config.py:78`；llm_usage 日结 | **真实时** | 不改 |
| **daily-action 计数 reset** | `tick.py:24-26` `_daily_key` | **真实时**（保持真实天，§4） | 不改 |
| nightly cron 触发周期（每真实 24h） | `nightly_cron.py:21-25,360-364` | **真实时**（仅改锚点小时，§5） | 真实 `datetime.now(tz)` |
| TTL / 限流 / 冷却（Redis）、日志戳、TLS | 各处 | **真实时** | 不改 |

## 2. `world_clock` 模块 API（唯一入口，纯函数）

```python
# app/world_clock.py  —— 全局唯一换算入口；禁止各处自行 datetime.now() 做世界时换算
from datetime import datetime
ZONE = "Asia/Shanghai"                       # config.TIMEZONE
K: int = settings.world_clock_k              # =4
WORLD_EPOCH: datetime                        # tz-aware Asia/Shanghai，见 §3

def now_world() -> datetime: ...             # tz-aware(Asia/Shanghai) = real_to_world(now_real())
def now_real() -> datetime: ...              # datetime.now(tz=Asia/Shanghai)（真实时的东八区表达）
def real_to_world(dt: datetime) -> datetime: # WORLD_EPOCH + K*(dt - WORLD_EPOCH)
def world_to_real(dt: datetime) -> datetime: # WORLD_EPOCH + (dt - WORLD_EPOCH)/K
def world_hour() -> int: ...                 # now_world().hour  → 喂 scheduler
def world_weekday() -> int: ...              # now_world().weekday() (Mon=0..Sun=6)
def world_date_key() -> str: ...             # now_world().strftime("%Y-%m-%d")
def world_week_index() -> int: ...           # 自 EPOCH 起的世界周序号（§5 周任务门）
def next_beijing_morning_real(hour: int) -> datetime:  # 下一次真实的北京 hour:00（cron 锚，§5）
def seconds_until_world_hour(h: int) -> float:         # 备用：距下个世界整点 h 的真实秒
```
所有入参/出参 tz-aware；`world_*` 系列即整个系统读世界时的唯一来源，`schemas.get_world_time()` 改为薄封装 `now_world()`。

## 3. `WORLD_EPOCH` 取值策略

- 定义：`WORLD_EPOCH` 是**真实与世界时重合的锚点**（该瞬间两钟相等），取一个**北京整点、且为 00:00 的固定常量**，如 `2026-01-01T00:00:00+08:00`。
- 推导：世界午夜每 `24h/k = 6` 真实小时到来一次 → 对齐到北京 **00:00 / 06:00 / 12:00 / 18:00**；世界正午（world 12:00）落在真实北京 03:00 / 09:00 / 15:00 / 21:00。
- 白天登录保证：昼夜周期仅 6 真实小时，**任何 ≥6h 的会话必见完整昼夜**；且北京白天/傍晚登录高峰（约 09:00、15:00、21:00）恰逢世界正午，登录即见白天。
- 部署：用**固定常量**（非启动时刻对齐），保证 loop / tick / API 多进程各自算出的世界时一致、且重启不漂移。默认 `2026-01-01T00:00:00+08:00`；如需把某真实高峰精确挪到 world 正午，只做**整小时相位**微调，不改 k。
- config 新增：`WORLD_CLOCK_K=4`、`WORLD_EPOCH="2026-01-01T00:00:00+08:00"`、`TIMEZONE="Asia/Shanghai"`。**旧 `agent_time_scale=1.0`(config.py:184) 全仓未被引用**，本设计以 `WORLD_CLOCK_K` 取代它，建议删除或标注废弃。

## 4. `AGENT_MAX_DAILY_ACTIONS` 语义与成本核算（含 reset 核实）

- **核实结论**：daily-action 计数**按真实日 reset**。`tick.py:24-26` `_daily_key()` 用 `datetime.now().strftime("%Y-%m-%d")` 做 Redis key 前缀（`sv:daily_actions:{date}:{rid}`），跨真实午夜自动换 key 归零，2 天 TTL 清理旧 key（`tick.py:20-21,29-39`）。config.py:181 注释写"per in-game day"**名不副实**——当前无世界日概念，实为真实日。
- **若改按世界天 reset**：key 每 6 真实小时翻一次 → 每真实天 4 个世界天 × cap 20 = **每居民每真实天 80 行动**（×4）。按 config.py:77 基线（15 居民 × 20 行动/真实天 ≈ 命中 $1.5 全局上限，隐含 ≈$0.005/行动）：改世界天后 ≈ **$6/真实天**，**击穿 `BUDGET_GLOBAL_DAILY_USD=1.5` 约 4 倍**，全局熔断器长期停在 RULE_ONLY，世界大部分时间无 LLM。
- **建议（不擅改预算上限）**：daily-action 计数**保持真实天锚定**（`_daily_key` 不动，它本质是花费护栏，语义上就是"每真实天预算配额"）。若产品坚持"每世界天"语义，则须把 `agent_max_daily_actions` 20→**5**（5×4=20/真实天，花费持平），此为建议值，**待 burn-in 实测 $/行动校准**后定。当前设计选前者：**counting 不变，仅作息/日期读世界时**。

## 5. nightly_cron 触发时刻改造

- 现状：`nightly_cron_loop`(:360-364) 用 `_seconds_until_next_run(datetime.now(UTC))`，RUN_HOUR=0/RUN_MINUTE=30 → 真实 UTC 00:30 = 北京 08:30。
- 改造：**真实 24h 周期不变**，仅把锚点换成**北京清晨整点**——传入 `now_real()`（Asia/Shanghai）替 `datetime.now(UTC)`，`RUN_HOUR` 设北京清晨（如 6 或 7）。日报在北京清晨可读。可用 `next_beijing_morning_real(RUN_HOUR)` 表达。
- 周任务门（周日 A1 / 周一关系衰减 `:183,221`）：cron 仍每真实日触发，但**判定改世界周**——用 `world_week_index()` 存 Redis，序号跨越才跑（世界周 = 7 世界天 = 1.75 真实天，不能再用 `weekday()==k` 等值判定，否则漏跑/错跑）。
- 每晚 dream / digest：cron 真实日触发不变，输出**戳世界日期**；接受"1 真实日承载 4 世界日→梦境非每个世界夜都做"的压缩（若要每世界夜做需 ×4 触发，与 §4 预算冲突，暂不做，待校准）。
- 集市/节日 active 窗口注：**选哪天办**用世界 weekday（世界时决策），但 `WorldEvent.starts_at/ends_at` 的 active 判定沿用真实时窗（玩家看到连续在线的活动），此接缝细节留实现阶段。

## 6. 给 agent-R 的口径边界

- **tick 仍每 60 真实秒**（`agent_tick_interval=60`, `loop.py:88-102`）——**k 不改变 tick 频率**。故所有 **per-tick 参数 = 真实时，随 k=4 保持不变**：`realism_energy_*`、`realism_satiety_decay`、`realism_social_*`、`realism_contagion_rate` 等 Task10/11 代谢与情绪 per-tick 项。
- **需随 k=4 调整的"每世界天/世界小时"语义**：
  - 关系衰减 `realism_rel_familiarity_decay=0.95 / _affinity_decay=0.98`（注释"×/week"）、`realism_rel_decay_idle_days=30` → **世界周/世界天**语义，配合 §5 世界周 index 门，避免真实日跑 4 次过度衰减。
  - 周末/节日：`realism_weekend_*`、`realism_festival_*` 走 `world_weekday()`。
  - importance 窗口 `realism_importance_window=100` 若按"条/时间"混合需复核。
- **关键副作用（R 必复核）**：作息门改世界小时后，一个世界日仅约 **240 真实 tick** 处于清醒窗（world 06–22），而非旧 1440——per-tick 的 energy/satiety 阈值（`realism_needs_critical=0.25`、`realism_eat_restore=0.5`）要重算：确认居民在缩短的清醒真实时长内仍能正常触发 EAT/入睡，不会饿死或永不困。此项 **burn-in 实测校准**。
