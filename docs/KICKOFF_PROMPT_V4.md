# Kickoff V4 — 推送部署 + 前端收尾

你在 `/Volumes/data/dev/simverse-world`(React 19 + Vite + zustand 前端,FastAPI + SQLAlchemy async 后端)。按任务顺序执行,每个任务独立提交,完成后在 `docs/PROGRESS.md` 对应条目打勾并记录偏差。

## 当前仓库状态(2026-07-10)

- `master` == `feat/rate-limiting-p1` == `95e5040`,本地领先 origin/master 145 个提交,**未推送**
- 最近提交:`fee7126` P1-3 慢查询/索引/分页;`71cbf4b` admin llm-usage 成本端点;`bc8f1fe`/`25d7833`/`180d857`/`95e5040` 四批前端(赛季+辩论页、动态/周报/关注/公告帖子、目标卡/TTS/连登弹窗/事件横幅、Admin 成本面板)
- 工作区应只有 docs/*.md 几个未跟踪文件;`worktree-*` 分支已清理,**不要再用并行 worktree 代理**——上次并行实现同一批功能造成大面积重复劳动
- 验证基线:`npx tsc --noEmit` 干净;`npx eslint src` = 7 errors/3 warnings(全部预存,不许新增);后端 `pytest` 选择性全绿。frontend build 若报 rolldown binding 缺失,是平台 optional deps 问题,`npm i` 重装即可(PROGRESS 里记录的 Node v25 问题请用 v20/v22)

## 任务 0:推送 + 部署 vm212 🔥(最优先)

1. `git push origin master feat/rate-limiting-p1`
2. 部署后端:`./deploy/backend/deploy.sh <user@vm212>`(rsync + docker compose up -d --build;不会覆盖远端 `/opt/skills-world/deploy/.env`)
3. **迁移是本次部署的核心风险**:013(llm_usage)→030(perf indexes)在真 Postgres + pgvector 上从未跑过全链(沙盒无 pgvector,PROGRESS 多处标注留 vm212 复验)。容器起来后进 api 容器跑 `alembic upgrade head`,逐条盯输出;失败时不要盲目 downgrade,先把报错和当前 `alembic_version` 记下来再处理
4. 健康检查:deploy.sh 末尾探 `localhost:8000` 是老端口,vm212 实际是 **8100**,手动 `curl http://localhost:8100/health`
5. E2E 冒烟(玩家交互链路允许,遵守下面约束):注册/登录 → WS 连接 → 找居民聊 2 轮 → `GET /admin/llm-usage/summary`(admin token)看计量有没有写入 → 前端如也部署,浏览器过一眼新页面
6. **硬约束**:vm212 的 LLM key 是百炼 Coding Plan,条款禁止后端自动化调用——`AGENT_ENABLED=false` 必须保持,禁止跑批量脚本烧配额;别动远端 `.env` 的其它值
7. 前端部署在 Cloudflare(`deploy/frontend/deploy.sh` + wrangler),一并发布

## 任务 1:新前端运行时验证

沙盒里只做了 tsc/eslint/build 级验证,没起过浏览器。本机起后端(sqlite dev 库即可)+ `npm run dev`,把四批新 UI 走一遍:

- `/seasons`:无赛季空态、投票(投过再投的 400 态)、排行榜 around_me
- `/debates`:列表/详情、押注边界(10-200)、voting 期免费投票、settled/draw 横幅;有条件的话造一场 live 辩论验证 `debate_turn` WS 实时追加
- Profile 📡 动态(游标分页、取消关注)、📅 本周回顾(懒生成慢时的"生成中"态)
- NpcTooltip 关注按钮 + 🎯 目标卡;ChatDrawer 🔊 TTS(429 配额态);TopNav 🔥 连登弹窗、📣 事件横幅(投放一个 admin 世界事件验证 start/end WS);公告板 帖子 tab 筛选/分页;Admin 💸 LLM 成本面板
- 发现的 bug 直接修,修完保持 tsc/eslint 基线,单独提交 `fix(frontend): runtime findings from local E2E`

## 任务 2:mood emoji(需要动后端,小)

PROGRESS L100 欠账:`resident_status` WS 广播加 `mood_label` 字段 + NpcTooltip 显示 emoji。

- 后端:mood 存在 resident 的 JSON 列(见 `app/services/` 里 mood/heat_cron 相关代码),在 `ws/handlers/chat.py`、`ws/handlers/connection.py`、`tasks/heat_cron.py`、`agent/loop.py` 广播 `resident_status` 的位置带上 `mood_label`;补一个小测试
- 前端:`components/NpcTooltip.tsx` 状态行加 emoji 映射(calm 😌 / happy 😊 / excited 🤩 / sad 😔 / angry 😠 / anxious 😰,以后端实际 label 集为准);`services/ws.ts` 无需新分支(NpcTooltip 数据走 bridge `npc:nearby`,评估是否需要在 GameScene 里把 mood_label 透传进 ResidentData)

## 任务 3:大件前端(按价值排序,每个独立提交)

先读对应后端 router/service 拿准 API 形状,再动前端。前端约定:**全 inline style + global.css CSS 变量**(--bg-card/--border/--accent-red 等,深色主题)、页面用具名导出 + App.tsx `lazy(() => import().then(m => ({default: m.XxxPage})))`、API 走 `services/api.ts` 的 `apiFetch<T>`(自动带 token/15s 超时/401 登出)、WS 用 `onWSMessage`、Phaser↔React 走 `game/phaserBridge.ts` 的 bridge 事件。

1. **商店 UI(D2)+ EconomyPanel 通胀曲线**:`routers/shop.py`;商品列表/购买/库存,tip 商品已被打赏用
2. **探索图鉴 ExplorationCodex(E8)**:`routers/exploration.py`;lore 收集进度 + 小地图剪影(已覆盖 8 个位置)
3. **时间胶囊 CapsuleComposer(E7)**:`routers/capsules.py`;写信全屏信纸 UI + capsule_ticket 计费(首封免费后 10 SC)
4. **合影 PhotoBooth(E10)**:后端 `group photo log` 已就绪;前端主导,`GameScene` 用 `renderer.snapshot` 截图合成
5. (可选小件)SoulCard `<canvas>` 卡片图(C1)、admin EventsPanel 世界事件投放 UI(A2,api 在 `routers/admin/events.py`)

## 任务 4:收尾

- `docs/PROGRESS.md`:给任务 1-3 完成项打勾、记偏差,格式照现有条目(commit 号 + 验证结果 + 偏差)
- 全部完成后再次推送;如 vm212 前端也要更新,重跑 wrangler 部署
- 提交规范照仓库现状:`feat(frontend): ...`,commit body 带 `Verified-by:`
