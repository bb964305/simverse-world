# Simverse World 文档整理计划

> 日期：2026-08-24
> 目标：让第一次接触项目的人，用小学程度的中文，也能知道项目是什么、怎么玩、怎么运行、代码在哪里、线上如何维护。
> 范围：只整理 Markdown 文档，不改变游戏代码、数据库结构和线上配置。
> 事实来源：当前仓库代码、环境变量模板、Docker Compose、测试配置、现有运行手册，以及 2026-08-24 对 vm212 的只读/运维核验。

## 一、写作规则

所有新手文档都遵守下面的规则：

1. 每篇开头先回答“这是什么、谁要看、看完会什么”。
2. 先说白话，再写技术名词。
3. 第一次出现缩写时，马上解释中文意思。
4. 一句话只说一件主要事情。
5. 普通说明句尽量不超过 35 个汉字。
6. 命令和生产证据放在单独代码块或高级文档中。
7. 状态只使用：未开始、开发中、代码完成、已部署、已开启、已验证、已关闭、已废弃。
8. 不把“代码已经写好”说成“线上已经开放”。
9. 历史计划不删除，但必须在总目录中标成历史资料。
10. 所有危险操作都写明风险、成功标志和恢复办法。

## 二、文档地图

### 2.1 新手入口

```text
README.md                 项目首页：一句话介绍、在线地址、身份入口
docs/README.md            文档总目录：阅读顺序和资料分类
docs/START_HERE.md        第一次阅读：用一个小故事认识项目
docs/GAMEPLAY.md          玩家手册：从注册到进入小镇
docs/ARCHITECTURE.md      系统说明：四个部分如何合作
docs/DEVELOPMENT.md       本地开发：启动、测试和排错
docs/CONTRIBUTING.md      贡献指南：修改、验证和提交
docs/DEPLOYMENT.md        部署说明：把新版本放到服务器
docs/OPERATIONS.md        运维手册：检查、备份和恢复
docs/GLOSSARY.md          词语表：把技术词翻成白话
docs/ROADMAP.md           唯一现行路线图：现在和下一步
```

### 2.2 高级资料

- `docs/AGENT_PLAYERS.md`：外部 Agent 接入。
- `docs/ADMIN_BOOTSTRAP.md`：第一个管理员账号。
- `docs/ADMIN_CONSOLE_DESIGN.md`：管理后台设计。
- `docs/RESIDENT_SPRITE_OPERATIONS.md`：居民形象发布。
- `docs/PARALLEL_WORKSTREAMS_2026-07-25B.md`：历史并行批次，只供追溯。
- `docs/PARALLEL_WORKSTREAMS_2026-07-27.md`：历史并行批次，只供追溯。
- `docs/PARALLEL_WORKSTREAMS_2026-07-27B.md`：历史并行批次，只供追溯。
- `docs/design/`：玩法和视觉设计。
- `docs/superpowers/2026-08-09-M-A-handoff.md`：经济专项交接。
- `docs/superpowers/2026-08-09-civic-public-memory-handoff.md`：公共记忆专项交接。
- `docs/superpowers/plans/`：专项执行计划，只供维护者继续工作。
- `docs/superpowers/specs/`：专项规格，只供维护者核对设计。
- `docs/runbooks/`：按日期执行的生产操作。
- `docs/reports/`：生产证据和调查结果。
- `docs/plans/`：实施计划，不代表功能已经上线。
- `docs/marketing/`：宣传素材制作稿。
- `archive/`：只读历史快照，不能当作当前说明。

### 2.3 固定阅读路径

| 身份 | 阅读顺序 | 终点 |
|---|---|---|
| 玩家 | `README → START_HERE → GAMEPLAY` | 知道如何进入小镇和使用主要玩法 |
| 开发者 | `README → START_HERE → DEVELOPMENT → CONTRIBUTING → ARCHITECTURE` | 能启动、修改并验证项目 |
| 运维人员 | `README → DEPLOYMENT → OPERATIONS → docs/runbooks/2026-08-15-town-p0-p3-rollout.md` | 能安全发布、检查和恢复 |
| 管理员 | `README → ADMIN_BOOTSTRAP → ADMIN_CONSOLE_DESIGN` | 能进入后台并理解管理区 |
| 外部 Agent 开发者 | `README → AGENT_PLAYERS` | 能理解接入方式和安全边界 |
| 历史研究者 | `docs/README → docs/reports/ops-deploy-p0-p1-2026-08-15.md → docs/plans/2026-08-17-location-capability.md → archive/2026-07-25/README.md` | 能区分报告、计划和历史快照 |

`docs/README.md` 的前 30 行只展示四个选择：我要玩、我要开发、我要运维、查看全部文档。

### 2.4 文件职责

| 文件 | 负责什么 | 不负责什么 |
|---|---|---|
| `README.md` | 项目简介和入口 | 不放完整命令和路线图副本 |
| `START_HERE.md` | 项目故事和角色关系 | 不讲代码细节 |
| `GAMEPLAY.md` | 普通玩家操作 | 不重复管理员和 Agent 接口细节 |
| `ARCHITECTURE.md` | 系统分块和数据流 | 不教部署 |
| `DEVELOPMENT.md` | 本地启动和测试 | 不讲生产发布 |
| `CONTRIBUTING.md` | 修改和提交规则 | 不重复系统原理 |
| `DEPLOYMENT.md` | 发布顺序和服务图 | 不处理故障 |
| `OPERATIONS.md` | 检查、故障、备份和恢复 | 不记录产品待办 |
| `ROADMAP.md` | 当前功能状态和下一步 | 不堆命令、SQL 和长日志 |
| `GLOSSARY.md` | 技术词的白话解释 | 不保存运行状态 |

## 三、执行步骤

### Step 1：建立白话词语表

失败检查：`test -f docs/GLOSSARY.md` 当前应失败。

新建 `docs/GLOSSARY.md`。每个词条都包含“一句白话”和“一个项目例子”。至少解释 Resident、NPC、Agent、LLM、API、REST API、WebSocket、数据库、Redis、worker、profile、bootstrap、迁移、健康检查、回滚、Forge、Soul Coin、Lab、Hosted Agent、功能开关和 vm212。

验收：文件存在；上述词条全部出现；后续新手文档第一次使用技术词时链接本词语表。

提交：`docs(glossary): explain project terms in plain Chinese`

### Step 2：写第一次阅读手册

失败检查：`test -f docs/START_HERE.md` 当前应失败。

新建 `docs/START_HERE.md`。用“玩家走进一座不停生活的小镇”的故事解释玩家、居民、世界、服务器、管理员和外部 Agent。前 30 行回答“这是什么、谁看、看完会什么”。结尾只给玩家、开发者和运维三个下一步。

验收：不出现未解释的缩写；普通读者能用两句话复述项目；文件不包含部署命令。

提交：`docs(start): introduce Simverse with a simple story`

### Step 3：写玩家手册

失败检查：`test -f docs/GAMEPLAY.md` 当前应失败。

新建 `docs/GAMEPLAY.md`。按编号写官网、注册登录、创建居民、进入地图、移动聊天、记忆关系、炼化、商店、委托、赛季、辩论、市政厅、集市、公开小镇 `/town` 和只读查看页 `/watch`。只用一小节区分普通玩家、管理员和外部 Agent，再链接高级资料。

必须明确：集市购买可能关闭；实验楼只读参观仍可见，但执行已关闭；居民形象生成默认关闭；政治功能可能受线上开关控制。

验收：固定路径 `官网 → 登录 → onboarding → /play` 完整；条件功能没有被写成人人可用；每个主要页面都有一句用途。

提交：`docs(gameplay): add a beginner player guide`

### Step 4：写系统结构说明

失败检查：`test -f docs/ARCHITECTURE.md` 当前应失败。

新建 `docs/ARCHITECTURE.md`。用四个盒子说明前端、FastAPI 后端、PostgreSQL/Redis、后台 worker。解释 REST API 与 WebSocket 的区别。列出真实目录和主要 API 分组。所有技术词第一次出现时链接词语表。

验收：开发者能找到页面、路由、服务、模型、迁移、测试和部署目录；文档不包含生产操作步骤；数据流图与 `backend/app/main.py`、`frontend/src/App.tsx` 和生产 Compose 一致。

提交：`docs(architecture): explain the system in four parts`

### Step 5：写本地开发手册

失败检查：`test -f docs/DEVELOPMENT.md` 当前应失败。

新建 `docs/DEVELOPMENT.md`。提供两条路径：

1. 最短体验使用 SQLite、`AUTO_CREATE_TABLES=true` 和 Redis。文档必须给出下面这组完整命令：

   ```bash
   docker compose up -d redis
   cd backend
   python3 -m venv .venv
   ./.venv/bin/pip install -e ".[dev]"
   DATABASE_URL=sqlite+aiosqlite:///./simverse-dev.db \
     AUTO_CREATE_TABLES=true \
     RUN_BACKGROUND_TASKS=false \
     REDIS_URL=redis://localhost:6379/0 \
     ./.venv/bin/uvicorn app.main:app --reload --port 8000
   ```

   另开终端启动前端：

   ```bash
   cd frontend
   npm install
   VITE_API_URL=http://localhost:8000 npm run dev
   ```

   此路径不跑 Alembic，也不代表 PostgreSQL 行为。
2. 完整开发使用 PostgreSQL/pgvector 和 Redis。根 Compose 是普通 PostgreSQL 16 + Redis 7；生产 Compose 才是 pgvector PostgreSQL 16 + Redis 8。两套 Compose 不能混用。下面命令只用于全新的本地环境；如果 `deploy/backend/.env` 已经存在，命令必须停止，读者应使用该文件中的真实密码组成 `DATABASE_URL`，不得覆盖：

   ```bash
   cd deploy/backend
   test ! -f .env || { echo "deploy/backend/.env 已存在，请使用其中的密码"; exit 1; }
   cp .env.example .env
   docker compose up -d db redis

   cd ../../backend
   python3 -m venv .venv
   ./.venv/bin/pip install -e ".[dev]"
   DATABASE_URL=postgresql+asyncpg://postgres:changeme@localhost:5432/skills_world \
     REDIS_URL=redis://localhost:6379/0 \
     DEBUG=true \
     ./.venv/bin/alembic upgrade head
   DATABASE_URL=postgresql+asyncpg://postgres:changeme@localhost:5432/skills_world \
     REDIS_URL=redis://localhost:6379/0 \
     DEBUG=true \
     ./.venv/bin/uvicorn app.main:app --reload --port 8000
   ```

写明 Python 3.11+；Node 20.19+ 或 22.12+。环境变量按“必须、按需、危险”分组。解释没有真实 LLM 密钥时哪些页面可开、哪些 AI 行为不能用。

开发验证命令必须完整写出：

```bash
cd backend
./.venv/bin/python -m pytest tests/

cd ../frontend
npm run test
npm run lint
npx tsc --noEmit
npm run build
```

验收：最短体验能启动 API、WebSocket、前端并走注册到地图；完整开发能在 pgvector PostgreSQL 上迁移；前端 test、lint、tsc、build 命令都能执行；后端说明 pytest 默认排除外部环境 marker。

提交：`docs(dev): add a tested local development guide`

### Step 6：写贡献指南

失败检查：`test -f docs/CONTRIBUTING.md` 当前应失败。

新建 `docs/CONTRIBUTING.md`。解释如何找文件、先写失败测试、实现、运行真实验证、添加迁移、更新文档和使用 Conventional Commits。把“改哪里”链接到架构文档，不重复目录说明。

验收：后端、前端、文档各有一组最小验证命令；迁移步骤明确要求真实 PostgreSQL；提交示例符合仓库已有格式。

提交：`docs(contributing): explain the change and review workflow`

### Step 7：写部署说明

失败检查：`test -f docs/DEPLOYMENT.md` 当前应失败。

新建 `docs/DEPLOYMENT.md`。分别解释默认生产图和实验 profile：

- 默认服务：db、redis、bootstrap、api、agent-worker、hosted-agent-worker。
- `lab`：lab-runner。
- `lab-production`：lab-runtime、lab-egress、lab-executor、artifact-ingest、artifact-scanner、artifact-cleanup。

写出“备份 → 同步 → 迁移 → 重建 → 健康检查 → 用户路径”的发布顺序。链接 `deploy/backend/deploy.sh`、`.env.example` 和日期 runbook。说明 Cloudflare/Nginx 只负责把公网请求送到 API 或前端。

危险动作表必须逐项覆盖：`rsync --delete`、`alembic upgrade head`、`docker compose up --build`、数据库备份恢复。每项写风险、保护、成功标志和恢复入口。

验收：维护者能说出默认服务和六个 `lab-production` 服务的用途；实验 profile 有启用和关闭条件；部署说明不处理日常故障。

提交：`docs(deploy): add a safe production deployment overview`

### Step 8：保存 2026-08-24 实验楼停用证据

失败检查：`test -f docs/reports/ops-lab-shutdown-2026-08-24.md` 当前应失败。

新建只读/运维核验报告。记录 vm212 的 UTC 时间、变更前开关、活动 run 数、备份文件、变更动作、容器状态、内网与公网健康检查。敏感 token、用户 ID 和密钥不得出现。

验收：报告能证明 `LAB_ENABLED=false`、Redis `sv:lab:enabled=0`、Lab Runner `exited (0)`、活动 run 为 0；报告记录“项目所有者确认 ARM 服务当前不可用，这是本次关停背景”，不得把容器状态误写成 ARM 不可用的独立技术证明；回滚只恢复配置，不代表 ARM 已恢复。

提交：`docs(ops): record the Lab shutdown evidence`

### Step 9：写运维手册

失败检查：`test -f docs/OPERATIONS.md` 当前应失败。

新建 `docs/OPERATIONS.md`。写健康检查、loop 心跳、日志、迁移版本、备份、回滚和功能开关。链接 Step 8 的证据报告。明确实验楼建筑和历史只读页仍可能出现，但发布、执行和 ARM 路径已关闭。

禁止操作必须包括：`docker compose down -v`、覆盖生产 `.env`、把本地 Compose 用于生产、无备份降级数据库。每项写风险、保护、成功标志和恢复办法。

验收：公网 `/health`、登录用户 `lab_enabled=false`、`/lab/status` 三项证据都能从报告找到；故障处理只在本文件或 runbook，不与部署步骤重复。

提交：`docs(operations): add health backup and recovery guidance`

### Step 10：归档旧路线图

失败检查：`rg -n '状态基线：2026-07-27|ARM staging' docs/ROADMAP.md` 当前应命中。

只把旧 `docs/ROADMAP.md` 原文保存为 `archive/2026-08-24/ROADMAP.before-documentation-rewrite.md`。

验收：归档文件与改写前的 `docs/ROADMAP.md` 内容完全相同。

提交：`docs(archive): preserve the pre-rewrite roadmap`

### Step 11：说明路线图归档

失败检查：`test -f archive/2026-08-24/README.md` 当前应失败。

只新建 `archive/2026-08-24/README.md`。写明归档日期、来源、只读状态、保存原因和新的权威路线图入口。

验收：读者不会把归档当作当前计划；链接能回到 `docs/ROADMAP.md`。

提交：`docs(archive): explain the roadmap snapshot`

### Step 12：重写当前路线图

失败检查：`rg -n '状态基线：2026-07-27|ARM staging' docs/ROADMAP.md` 当前应命中。

只重写 `docs/ROADMAP.md`，最后更新时间为 2026-08-24。

第一屏只放“已经能用、条件开放、已经关闭、接下来做什么”。实验楼执行必须标为“已关闭”，ARM 服务必须标为“不可用”。历史上曾有 ARM staging 可以写，但必须同时出现“历史”或“已关闭”。

路线图状态只能使用：未开始、开发中、代码完成、已部署、已开启、已验证、已关闭、已废弃。

验收：当前路线图没有 2026-07-27 基线；表格状态全部来自固定词；SQL、哈希和长日志只留在报告、runbook 或归档；旧路线图关键章节能从 Step 10 的归档打开。

提交：`docs(roadmap): publish a clear current status`

### Step 13：建立文档总目录

失败检查：`test -f docs/README.md` 当前应失败。

新建 `docs/README.md`。前 30 行只展示“我要玩、我要开发、我要运维、查看全部文档”四个选择。加入 2.3 的六条固定阅读路径。每个现有高级文档标明“适合谁、用途、不负责什么、当前性”；plans、reports、runbooks、marketing、archive 标明当前性。

验收：所有 2.3 路径都能从总目录点击；高级文档不会出现在玩家主路径；旧路线图归档有明确入口；前 30 行只显示四个选择；高级目录放在后段；玩家、开发者、运维三条主路径各自只有一个清楚终点；其他文件只能从“高级资料”或“历史资料”进入。三个 `PARALLEL_WORKSTREAMS` 文件以及 `docs/superpowers/` 下每个现有 Markdown 文件都列出链接、读者、用途、不负责的内容和当前性。

提交：`docs(index): add role-based documentation navigation`

### Step 14：重写项目首页

失败检查：README 当前仍命中 `Node.js 18+`、`python-jose`、`Passlib` 和旧版本路线图标题。

重写 `README.md`。保留名称、在线地址、截图、演示、致谢、素材声明和许可证。第一屏用白话解释项目，并给四个身份入口。技术栈只保留简短真实版本。完整启动、部署和路线图全部改为链接。

验收：README 不再复制路线图；不再声称 Node 18、python-jose、Passlib 或前端测试尚未建设；所有入口链接存在。

提交：`docs(readme): replace the mixed guide with a clear entry`

### Step 15：统一验证文档质量

过时内容扫描只检查当前入口，不扫描历史归档：

```bash
rg -n 'Node(\.js)?[[:space:]]+18|python-jose|Passlib|ARM staging (可用|已开放)' \
  README.md docs/README.md docs/START_HERE.md docs/GAMEPLAY.md \
  docs/ARCHITECTURE.md docs/DEVELOPMENT.md docs/CONTRIBUTING.md \
  docs/DEPLOYMENT.md docs/OPERATIONS.md docs/GLOSSARY.md docs/ROADMAP.md
```

最终检查：

- `git diff --check` 通过。
- 递归检查根 `README.md` 与 `Path('docs').rglob('*.md')` 中的本地链接；代码块中的示例链接不计；允许链接到仓库中的 `archive/`、`deploy/`、`assets/` 等目录。
- README 前 30 行能找到在线体验和四类身份入口。
- `docs/README.md` 的六条固定路径都能走到终点。
- 普通段落句长检查排除标题、表格、代码块和链接 URL；保留链接文字与行内代码文字。按 Unicode 字符计数，每个汉字、英文字母、数字和标点各计 1，空格不计。按文件名和出现顺序抽取前 20 句，至少 18 句不超过 35 个字符。
- 扫描当前入口中的常见英文占位词、中文“待补内容”和未解析的星号文件名；结果必须为 0。高级资料索引必须使用真实文件链接。
- 危险操作表逐项含“风险、保护、成功标志、恢复入口”。
- 用 `docs/DEVELOPMENT.md` 的最短启动命令，实际打开后端、前端，完成注册、onboarding 和进入 `/play`。
- 读取浏览器控制台和网络日志；没有由文档命令造成的 4xx/5xx 或 CORS 错误。

本步骤只做验证，不修改任何文档。发现问题时，本步骤失败；为每个受影响文件追加独立修正步骤和提交，然后重新执行本步骤。

## 四、不做的事情

- 不删除历史报告、计划、测试证据和资产授权记录。
- 不把生产密钥、用户资料或内部凭证写进文档。
- 不为了让文档好看而声称关闭功能已经开放。
- 不在本次文档整理中修改游戏逻辑、数据库或线上开关。
- 不把高级安全手册强行改成儿童教程；高级手册只从总目录按需进入。
