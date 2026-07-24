# 市政厅 + 实验楼终端 只读面板 — 交付报告

> Society Expansion Plan §10 先行项：两个**只读**前端面板 + 两个前端小债。
> 分支 `feat/town-m1-m6-20260724`（worktree `/Volumes/data/dev/sv-townhall`，基于 M1–M6 提交 `5172f0e`）。
> **未合并、未 push**（等用户拍板）。

## 结论先行

| 项 | 状态 | 证据 |
|---|---|---|
| 后端只读 router `townhall.py` | ✅ | 5/5 单测绿；live HTTP 200 |
| `main.py` 仅注册（+2 行） | ✅ | `git diff 5172f0e --stat` = `1 file changed, 2 insertions(+)` |
| TownHallPanel（政策/职位/议案/选举） | ✅ | 真机截图渲染真实种子数据 |
| LabTerminalPanel（只读运行监视） | ✅ | 截图 + 断言 0 写按钮 |
| 公告栏分页（小债①） | ✅ | 截图第 1/2 页，每页 10 条 |
| 集市日折后价标签（小债②） | ✅ | 截图每件商品划线原价 + 折后价 + 集市日徽章 |
| 前端 tsc / eslint / vitest | ✅ | 0 / 0 / 103 passed(24 files) |
| 后端 townhall 单测 | ✅ | 5 passed |
| 无新表 / 无迁移 / 无写接口 | ✅ | diff 仅新增 router + 前端组件 |
| 未碰 gameStore 字段语义 / GameScene 主渲染 / 红区 | ✅ | diff 清单见下 |

## 变更清单（`git diff 5172f0e --stat`，共 21 文件 +1337/-9）

### 后端（仅 2 文件涉既有代码，其一是 +2 行注册）
- `backend/app/routers/townhall.py`（新增 168 行）— 只读 `GET /townhall/overview` + `GET /townhall/market-day`
- `backend/app/main.py`（**+2 行**）— import + include_router，无其他改动
- `backend/tests/test_townhall.py`（新增 131 行）— 5 个 anyio 测试

### 前端
- `src/services/api/townhall.ts`（+67）、`api/townhall.test.ts`（+43）、`services/api.ts`（+1 barrel）
- `src/components/TownHallPanel.tsx`（+199）、`.test.tsx`（+90）
- `src/components/LabTerminalPanel.tsx`（+149）、`.test.tsx`（+73）
- `src/components/Pager.tsx`（+49，抽取复用）、`.test.tsx`（+40）
- `src/components/BulletinBoard.tsx`（+17/-；分页）、`.test.tsx`（+84）
- `src/components/ShopModal.tsx`（+23/-；折后价标签）、`.test.tsx`（+65）
- `src/components/TopNav.tsx`（+27/-；两入口 + 互斥 overlay lane）、`.test.tsx`（+27）
- `src/game/phaserBridge.ts`（+4 事件表注释）

> `backend/skills_world_dev.db`、`backend/uv.lock` 的工作树改动来自本地验证时的种子/依赖解析，**已 `git checkout` 还原，未进任何提交**。

## 提交（一 step 一 commit，8 个）

```
92802bb feat(townhall): read-only /townhall overview + market-day router, registered
215307b feat(townhall): frontend townhall api client
1b762e4 feat(ui): extract reusable Pager component
141d65a feat(bulletin): client-side pagination for the announcement board
13cb2e5 feat(shop): show market-day discounted price label (display only)
30c082f feat(townhall): read-only TownHallPanel with policy/office/poll/result tabs
91513e4 feat(townhall): read-only LabTerminalPanel (player-only lab run monitor)
603b959 feat(townhall): wire TownHall + LabTerminal panel entries into TopNav
```

## Verified-by（本轮终审重跑，非缓存）

| 检查 | 命令 | 结果 |
|---|---|---|
| main.py 仅 +2 行 | `git diff 5172f0e --stat -- backend/app/main.py` | `1 file changed, 2 insertions(+)` |
| 后端 townhall 单测 | `uv run --extra dev python -m pytest tests/test_townhall.py -q` | `5 passed, 1 warning in 2.40s` |
| 前端类型 | `npx tsc -b` | exit 0 |
| 前端 lint | `npm run lint` (eslint .) | exit 0 |
| 前端单测 | `npx vitest run` | `Test Files 24 passed (24) / Tests 103 passed (103)` |

> 后端全量回归（`python -m pytest -q`）本会话早前已跑：1316 passed / 1 skipped / 11 deselected（deselected = 需 redis/testcontainers 的 lab-v2 集），相对 base **零新增失败**。

## verify-before-done：真机运行时证据

方法：起真实后端（127.0.0.1:8000，`create_all` 建 M1–M6 当前 schema）+ 前端 dev（localhost:5173，命中 CORS 白名单，规避本机 1082 代理劫持）；用无依赖的 CDP 驱动（Node 全局 `WebSocket` 驱动系统 Chrome 150 headless）注入真实登录 token → 打开 `/play` → 点导航入口触发 bridge 事件 → 截图。种子数据：现任镇长赵启文、两个 duty 持有者、进行中议案「广场是否加装长椅」、已结束选举「镇长选举」（赵启文 7 票胜）、14 条公告、active 集市日事件（0.9 折）、一个 running lab 委托。

后端 live 响应（真实 HTTP）：
- `GET /townhall/market-day` → `{"active":true,"discount":0.9,"weekday":5}`
- `GET /townhall/overview` → 200，mayor=赵启文、duties×2、open_polls 含议案且**不含**选举、recent_election winner=赵启文/7 票、finances 投影 config
- `GET /lab/tasks?scope=mine` → 200，返回 running 委托「调研：小镇咖啡馆选址」

### 截图

**1. TownHall — 政策 & 财政**（config 投影：居民日薪🪙5、每餐🪙2、集市日折扣×0.9、集市日周5、议案时长3天、选举周期28天、镇长津贴🪙1.2）
![政策](img/townhall/02-townhall-policy.png)

**2. TownHall — 在任职位**（赵启文/公告与登记处、何巧云/杂货铺掌柜）
![职位](img/townhall/03-townhall-office.png)

**3. TownHall — 议案投票**（广场是否加装长椅：加装 / 维持原样）
![议案](img/townhall/04-townhall-poll.png)

**4. TownHall — 选举结果**（当选：赵启文 · 7 票）
![选举](img/townhall/05-townhall-result.png)

**5. LabTerminal — 委托列表**（顶部横幅「只读运行状态 · 无发布 / 放款 / 审批操作」）
![lab列表](img/townhall/06-labterminal-list.png)

**6. LabTerminal — 选中委托看运行状态**（执行中 / 运行状态：运行中 / 适配器 mock）。DOM 断言：对话框内匹配 `发布|放款|批准|拒收|取消委托|审批` 的按钮 = **`[]`（零写按钮）**
![lab运行](img/townhall/06b-labterminal-run.png)

**7. Shop — 集市日折后价标签**（每件商品：划线原价 + 折后价 + 「集市日」徽章；如🪙10→🪙9。结算逻辑未改）
![商店](img/townhall/07-shop-marketday.png)

**8. Bulletin — 分页第 1/2 页**（公告 #01–#10，恰好 10 条 + 分页器）
![公告1](img/townhall/08-bulletin-page1.png)

**9. Bulletin — 第 2/2 页**（剩余公告 #11–#14）
![公告2](img/townhall/09-bulletin-page2.png)

## 偏差记录

1. **基线**：计划原写基于 `port/prod-fixes-onto-044`，实际 rebase 到 **M1–M6（`5172f0e`）** —— §10 面板依赖 M1–M6 的 civic/election/duty 数据模型，044 线无这些表。（用户先前已确认 rebase on M1-M6。）
2. **Node 版本**：CI 用 Node 22，本地验证用 **Node v25**（用户先前已批准）。
3. **截图工具**：项目未装 Playwright/Puppeteer。为不新增依赖，用**零依赖 CDP 驱动**（Node 全局 WebSocket + 系统 Chrome headless）完成真机截图，脚本为一次性验证产物、未入库。
4. **CORS**：后端 `cors_origins` 默认仅 `localhost:5173`；验证时前端固定跑在该 origin，未改后端配置。
5. **LAB_ENABLED**：实验楼终端入口受 `settings.lab_enabled` 门控，验证时以 `LAB_ENABLED=true` 起后端以展示该入口（仅本地验证环境，未改 .env / 未入库）。

## 明确未做（遵计划排除项）

- 未建 policies / offices / town_treasury 表，无迁移。
- 未做 §7.1 全量仪表盘（政体曲线/gini/舆情 —— M1–M6 无数据源）。
- 未改 ShopModal 结算路径、未改 BulletinBoard 后端 cursor 语义。
- 未合并、未 push、未碰红区文件 / gameStore 字段语义 / GameScene 主渲染。
