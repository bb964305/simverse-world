# 可并行开工清单(与 S0 上线批次互不干扰)

> 前提:`KICKOFF_PROMPT_S0_LAUNCH.md` 那条主线正在(或即将)执行,它独占:git 主线合并、生产部署、
> `config.py` / `.env.example` / `nightly_cron.py` / `preset_characters.py` / agent 作息与时间模块 / `PROGRESS.md`。
> 以下工作项都避开这些,可以立即另开 Claude Code 会话跑。

## 所有并行线共同纪律(每条提示词都已内嵌)

1. 独立 `git worktree` + 独立分支,基于指定 base;**做完只留分支,不合并、不 push、不部署**,统一等 S0 落地后按顺序收口。
2. 红区文件禁止修改:`backend/app/config.py`、`backend/.env.example`、`backend/app/tasks/nightly_cron.py`、`backend/seed/preset_characters.py`、`docs/PROGRESS.md`。确需新配置一律读环境变量并在报告中登记,留待收口时统一进 config。
3. 各线进展写到 `docs/reports/<分支名>-report.md`,不碰 PROGRESS.md。
4. 测试纪律不变:改动范围内 pytest/vitest 全绿;前端用 Node 22。

## 开工总览

| # | 工作线 | 分支(base) | 改哪里 | 为何不冲突 | 规模 |
|---|---|---|---|---|---|
| 1 | NPC 图片理解修复 | `fix/npc-vision`(port/prod-fixes-onto-044) | `backend/app/media/**` + 新测试 | S0 批次完全不碰 media/ | 中 |
| 2 | Lab 收尾包 | lab 分支系(origin/main) | `backend/app/lab/**` + lab 前端组件 | 与小镇主线不同分支血统,文件集不相交 | 大 |
| 3 | 社会扩展第一批接口规格 | `docs/society-specs`(HEAD) | 仅新增 `docs/kickoffs/*.md` | 纯文档,零代码 | 大 |
| 4 | 市政厅/实验楼终端只读面板 | `feat/townhall-readonly`(port/prod-fixes-onto-044) | 前端新组件 + 1 个新只读 router 文件 | 几乎全是新增文件 | 中 |
| 5 | 邮局美术落图 | `art/post-office`(port/prod-fixes-onto-044) | 仅 tilemap.json | Brief 已写好,S0 不碰地图资产 | 小 |

## 暂缓清单(想开也别开,会撞车)

| 工作项 | 撞谁 |
|---|---|
| 成本优化杠杆(skip-decide、收尾合并、滑窗) | 改 agent 决策/聊天链路,与拟真开闸线同文件 |
| 情绪写入点、向量检索接线等算法修复 | 同上,agent/memory 是 S0 校准中的活动区 |
| 任何 alembic 迁移类新功能 | master/lab 双头未合,先别再加头 |
| 生产测试数据清理 | 动生产库,等部署完一起做 |

---

## 提示词 1 · NPC 图片理解修复

```
任务:修复生产确认的高优先级缺陷——NPC 无法理解玩家发送的图片(带图对话一律回复"没有视觉能力")。

准备:
- git worktree add ../sv-vision port/prod-fixes-onto-044 && 新建分支 fix/npc-vision
- 先读 docs/testing/TEST_REPORT_2026-07-24.md 的 P1-1 节(含根因分析)、backend/app/media/model_router.py、backend/app/media/service.py、backend/app/config.py(只读,禁止修改)。

背景根因(高置信,需先复核):model_router.py 把图片包装成 image block 后,仍统一使用
settings.effective_model 发送,而生产模型不具备视觉能力,图片内容从未被消费。

要求:
1. 二选一并说明理由:(a) 视觉路由——图片消息路由到可视觉模型(模型名走新环境变量
   SV_VISION_MODEL,用 os.environ/pydantic env 读取,不改 config.py);(b) 理解前置——用视觉
   模型先把图转成文字描述,注入原对话链。倾向 (b),对现有链路侵入最小。
2. 失败要优雅:视觉调用失败时回落现有纯文本行为,不崩对话。
3. 视觉调用必须接入现有 LLM 计量(llm_usage)与预算熔断,禁止绕过 Meter。
4. 测试:单测覆盖路由/描述注入/失败回落;新增 E2E 断言(mock 视觉返回,断言 NPC 回复含
   图中颜色/物体),放新测试文件,不改既有测试。
5. 改动范围外的 pytest 必须保持全绿。禁止合并、push、部署。
6. 产出 docs/reports/fix-npc-vision-report.md:根因确认过程、方案、改动清单、
   需要在收口时写入 config.py/.env.example 的配置项清单、生产验证步骤。
```

## 提示词 2 · Lab 收尾包

```
任务:推进实验楼(Lab)子系统的纯代码收尾项。全部工作在 lab 分支血统上,与小镇主线零交集。

准备:
- git worktree add ../sv-lab origin/main && 新建分支 feat/lab-closeout
- 先读 docs/adr/LAB_REMAINING-status.md(全文,特别是 Repository-state warning 与 OPEN 项)、
  docs/adr/ADR-lab-artifact-storage.md、docs/adr/T8-hardening.md、
  .omx/plans/lab-agent-recovery-completion-plan.md(如存在)。

按序执行:
1. 【最优先】处理文档警告的陈旧 git index:38 个 staged 路径含 20 个 staged 删除,直接提交会删掉
   Worker/遥测/测试文件。按 recovery plan 把未改条目对 HEAD 规整,再用显式路径重新 stage。
   完成前禁止任何 commit。
2. lab_max_concurrent_runs 全局并发上限:实现运行时消费者(DB 或 Redis 原子信号量,
   CAS reserve + 幂等 release),补并发测试。
3. run 创建→Redis 入队的崩溃窗口:把入队改经 outbox 路由 lab.run.enqueue + 对账逻辑,
   补崩溃注入测试(引擎已在 outbox_dispatcher.py,做接线不做重写)。
4. 产物对象存储:按 ADR-lab-artifact-storage 契约实现 S3 兼容后端,放在配置开关后面,
   默认关闭、缺配置 fail-closed;现有下载安全边界(sha256、不代理远端 URI)不得放松。
5. Lab 前端尾巴:三视口响应式、reduced-motion、触控尺寸(vitest + 截图证据)。

红线:不动 alembic(需要迁移的记 TODO 进报告);不翻 lab_adapter / lab_oci_enabled 默认值;
不动 backend/app/agent、services 等小镇主线目录;不合并不 push。
产出 docs/reports/lab-closeout-report.md:每项状态、剩余 blocked 项及其外部依赖。
```

## 提示词 3 · 社会扩展第一批接口规格(纯文档)

```
任务:为社会扩展第一、二批模块撰写可开工的 KICKOFF 接口规格。只产出文档,不写任何代码。

准备:
- git worktree add ../sv-specs HEAD && 新建分支 docs/society-specs
- 先读 docs/SOCIETY_EXPANSION_PLAN.md(全文)、docs/KICKOFF_PROMPT_REALISM_P2.md(格式范本)、
  以及涉及模块的现有代码(services/civic_service.py、election_service.py、coin_service.py、
  gossip_service.py、models/ 相关表)。

为以下 5 个模块各写一份 docs/kickoffs/KICKOFF_<模块>.md,严格照 REALISM P2 的格式:
任务切分(每项含改哪些文件、新表结构、接口签名)、门控开关与默认值、原子性要求、
测试口径(单测+集成各列出用例名)、探针出数定义、边界与"不碰区域":
1. S2-1 offices 职位实体化(镇长/文书/邮差/医生;与现有 duty_service、选举的衔接)
2. S2-5 policies 表 + 四级分级审批(与现有 world_change_proposal、admin 审批台的关系)
3. S1-1 公共声誉轴(nightly 从八卦情绪基调+目击聚合;消费端:赊账/选人权重/投票信任)
4. S1-3 议题立场与舆论动力学(有界信任模型;与辩论、日报的衔接)
5. S1-5 镇财政闭环(税/薪/公共支出;与 coin_service、resident_treasuries 的关系)

硬要求:
- 每份规格必须先列"现状锚点"(逐文件核实现有代码,给出行级引用),严禁凭想象写接口。
- 迁移号只写占位符 NNN,注明落地时按当时链头定。
- 全局纪律沿用总纲:规则做骨架、LLM 做血肉,新机制零 LLM 边际成本,独立门控默认 False。
- 每份末尾附"依赖与冲突声明":依赖哪些前置模块、会碰哪些文件、与其他 4 份的文件交集。
不改任何代码文件,不碰 docs/PROGRESS.md。
```

## 提示词 4 · 市政厅面板 + 实验楼终端(只读版)

```
任务:按社会扩展计划 §10 的建议,先行交付两个只读前端面板,让政治层尽早可见:
市政厅面板(TownHallPanel)与实验楼终端(LabTerminalPanel,仅玩家可见)。

准备:
- git worktree add ../sv-townhall port/prod-fixes-onto-044 && 新建分支 feat/townhall-readonly
- 先读 docs/SOCIETY_EXPANSION_PLAN.md 的 §7(前端专项)与 §10、
  frontend/src/components/minimap/ 与 admin/ 现有面板的实现模式、
  backend/app/routers/world.py 与 polls/debates 相关只读端点。

要求:
1. 后端:仅新增一个只读 router 文件 backend/app/routers/townhall.py(汇聚现有数据:在任 duty、
   进行中议案/投票、最近选举结果、镇财政现值——policies 表未建前投影 config 现值),
   在 main.py 注册(只允许 append 一行,这是本线唯一碰的既有文件)。无新表、无迁移、无写接口。
2. 前端:新增 TownHallPanel、LabTerminalPanel 组件与入口(参照现有 minimap/admin 面板模式);
   实验楼终端只读展示 lab 任务/运行状态(复用 services/api/lab.ts)。
3. 顺手打包两个既有前端小债:公告栏分页、集市日折扣在商店目录页的展示
   (ShopModal 显示折后价标签,folder 结算逻辑不动)。
4. 测试:每个新组件配 vitest;tsc + eslint 全绿;Node 22。
5. 不碰 gameStore 既有字段语义、不碰 GameScene 主渲染逻辑、不动红区文件;不合并不 push。
产出 docs/reports/townhall-readonly-report.md 含截图。
```

## 提示词 5 · 邮局美术落图

```
任务:把邮局(post_office)建筑画进小镇地图。后端几何已生效,地图上仍是纯草地。

准备:
- git worktree add ../sv-art port/prod-fixes-onto-044 && 新建分支 art/post-office
- 先读 docs/art/POST_OFFICE_ART_BRIEF.md(全文,含用地范围、图层、要素清单、验收 5 项)、
  docs/art/render_map_crop.py、frontend/scripts/verify-asset-provenance.mjs。

要求:
1. 严格按 brief:用地 x44-48 / y100-106(5×7),入口 tile (46,100) 必须保持可通行;
   只用现有已授权 tileset 拼装,不引入任何新素材文件。
2. 编辑 tilemap.json 指定图层;完成后跑 brief 的 5 项验收清单,并用 render_map_crop.py
   渲染前后对比图存入 docs/art/。
3. 跑 tilemap 前后端一致性校验与资产溯源校验脚本,必须全绿。
4. 若既有 tileset 拼不出关键要素(红色邮筒等),用最接近的替代并在报告中记录,不造新素材。
产出 docs/reports/art-post-office-report.md + 对比截图。不合并不 push。
```

---

## 收口顺序(S0 主线落地后,由主会话统一执行)

1. 先合 5(美术,零风险)→ 4(前端,main.py 一行冲突)→ 1(vision,补 config 项)→ 全量测试
2. Lab 线(2)不并入小镇主线,继续留在 lab 分支血统,等未来 lab-v2 大合并一起处理
3. 规格线(3)纯文档,随时可合
4. 每合一条跑全量 pytest,合完统一补一条 PROGRESS.md 记录
```
