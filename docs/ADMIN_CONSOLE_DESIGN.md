# Simverse World — 后台管理控制台设计方案

> **时效注**：As-Is 盘点基于 2026-07-25 master，采用前需刷新（此后已新增 seasons/resident_sprites 路由与 GovernanceInsights/SocietyInsights 面板）。

> 版本：v1.0（2026-07-25）
> 范围：`/admin` 管理后台的信息架构、视觉规范、页面设计、组件规范与实施路线图。
> 前置事实：项目**已有**一个可用的管理后台（11 个前端面板 + 16 个后端 admin 路由）。本方案不是从零设计，而是**盘点现状 → 补齐缺口 → 升级体验**的演进式设计。

---

## 1. 现状盘点（As-Is）

### 1.1 已有架构

- **入口**：`frontend/src/pages/AdminPage.tsx` — `useState` 驱动的 Tab 切换，`AdminSidebar` + 内容区两栏布局，路由守卫 `user.is_admin`（非管理员重定向 `/play`）。
- **后端**：`backend/app/routers/admin/` 统一挂载在 `/admin` 前缀下，每个端点通过 `require_admin` 依赖鉴权（JWT → is_banned 检查 → is_admin 检查）。
- **前端 API 层**：`services/api/admin.ts` + `services/api/adminWorld.ts`。

### 1.2 覆盖矩阵：后端能力 vs 前端 UI

| 后端路由模块 | 端点数 | 前端面板 | 覆盖状态 |
|---|---|---|---|
| `dashboard` | 4（stats / trends / top-residents / health） | DashboardPanel | ✅ 已覆盖 |
| `users` | 4（列表 / 详情 / 调币 / patch） | UsersPanel（含子组件目录） | ✅ 已覆盖 |
| `residents` | 7（CRUD + presets + batch/district + batch/status-reset） | ResidentsPanel | ⚠️ **部分**：presets 与两个 batch 端点无 UI |
| `forge_monitor` | 4（列表 / active / searxng-health / 详情） | ForgeMonitorPanel | ⚠️ **部分**：searxng-health 无 UI |
| `economy` | 6（stats / series / investments / transactions / config GET+PUT） | EconomyPanel | ✅ 已覆盖 |
| `llm_usage` | 1（summary） | LlmUsagePanel | ✅ 已覆盖 |
| `events` | 4（列表 / 创建 / patch / 删除） | EventsPanel | ⚠️ **部分**：PATCH 编辑无 UI |
| `gossip` | 2（recent / chains） | RumorChainPanel | ✅ 已覆盖 |
| `lab` | 5（status / kill-switch / runs / arbitrate / cancel） | LabRunsPanel | ⚠️ **部分**：arbitrate（人工仲裁）无 UI |
| `world`（提案） | 5（列表 / 详情 / approve / reject / revert） | ProposalsPanel | ✅ 已覆盖 |
| `system_config` | 8（groups / entries / entry / batch / llm / heat / user-llm-policy） | SystemConfigPanel | ⚠️ **部分**：heat / user-llm-policy 视图待确认 |
| `items`（商店物品） | 3（列表 / 创建 / patch） | — | ❌ **无 UI** |
| `policies`（政策分级） | 3（列表 / seed / amend） | — | ❌ **无 UI** |
| `offices`（官职） | 1（列表，只读） | — | ❌ **无 UI**（后端注释明确标注前端 out of scope） |
| `social_graph`（社交图谱） | 1（nodes/edges/circles，只读） | — | ❌ **无 UI**（后端注释明确标注可视化待做） |

### 1.3 体验与工程层面的缺口

1. **无 URL 路由**：Tab 状态在 `useState` 里，刷新丢失、无法深链（不能把"经济面板"的链接发给别人）。
2. **token 传递不一致**：部分面板接 `token` prop，部分面板自己从 store 取 —— 两套模式并存。
3. **无审计日志**：调币、封禁、政策修订、提案审批等敏感操作没有留痕（后端也没有 audit 表）。
4. **单一权限位**：只有 `is_admin` 布尔值，没有角色分级（如只读观察员 vs 运营 vs 超管）。
5. **危险操作确认不统一**：各面板自行处理（或不处理）二次确认。
6. **无全局刷新/轮询约定**：各面板自行拉数据，实时性和请求频率无规范。

---

## 2. 设计目标与原则

| 目标 | 说明 |
|---|---|
| **全量覆盖** | 后端每一个 admin 端点都有对应 UI，不留"只能 curl"的暗角 |
| **可深链** | 每个面板一个 URL（`/admin/economy`），刷新不丢状态，可分享 |
| **运营效率** | 高频操作（查用户、调币、看成本、杀实验）≤ 3 次点击可达 |
| **安全兜底** | 危险操作统一二次确认 + 展示影响面；政策分级矩阵在 UI 层就阻止越权直批 |
| **视觉一致** | 复用现有 zinc 暗色令牌体系（`global.css`），不引入新 UI 依赖库 |

**原则**：
- 演进不推翻 —— 现有 11 个面板保留，逐步迁移到新框架；新面板按新规范写。
- 零新依赖 —— 继续用 React 19 + React Router 7 + Zustand + 内联样式/CSS 变量，不引入 antd/mui（保持包体和风格统一）。
- 只读优先 —— 新面板先做只读视图（低风险快速见效），写操作随后补。

---

## 3. 信息架构（To-Be）

11 个平铺 Tab 已经到了认知负荷的临界点，再加 4 个新面板会到 15 个。重组为 **6 个分组**：

```
🏛 管理控制台 (/admin)
│
├─ 总览
│   └─ 仪表盘        /admin/dashboard      核心指标、趋势、服务健康、Top 居民
│
├─ 用户与居民
│   ├─ 用户          /admin/users          搜索/封禁/调币/详情
│   ├─ 居民          /admin/residents      CRUD、预设管理、批量迁区/状态重置 ★补
│   └─ 社交图谱      /admin/social-graph   关系网络可视化（力导向图 + 圈子列表）★新
│
├─ 世界运营
│   ├─ 世界事件      /admin/events         创建/编辑★补/删除广播事件
│   ├─ 世界提案      /admin/proposals      approve / reject / revert
│   ├─ 谣言链        /admin/gossip         传播链追溯
│   ├─ 政策          /admin/policies       分级矩阵、行政级直批、投票级导流 ★新
│   └─ 官职          /admin/offices        任期/持有人/机构 只读视图 ★新
│
├─ 经济
│   ├─ 经济总览      /admin/economy        指标、时序、投资、流水、参数配置
│   └─ 商店物品      /admin/items          物品 CRUD、上下架、定价 ★新
│
├─ AI 与成本
│   ├─ LLM 成本      /admin/llm            用量汇总、按模型/时段拆分
│   └─ 炼化监控      /admin/forge          会话列表、活跃会话、SearXNG 健康 ★补
│
├─ 实验楼
│   └─ 实验运行      /admin/lab            runs、kill-switch、cancel、人工仲裁 ★补
│
└─ 系统
    └─ 系统配置      /admin/system         分组配置、批量修改、热度/LLM 策略视图
```

★新 = 新面板（4 个）；★补 = 现有面板补功能（4 处）。

### 3.1 路由设计

```tsx
// App.tsx
<Route path="/admin" element={<AdminLayout />}>   {/* 布局路由：守卫 + 侧栏 + <Outlet /> */}
  <Route index element={<Navigate to="dashboard" replace />} />
  <Route path="dashboard"    element={<DashboardPanel />} />
  <Route path="users"        element={<UsersPanel />} />
  <Route path="residents"    element={<ResidentsPanel />} />
  <Route path="social-graph" element={<SocialGraphPanel />} />
  <Route path="events"       element={<EventsPanel />} />
  <Route path="proposals"    element={<ProposalsPanel />} />
  <Route path="gossip"       element={<RumorChainPanel />} />
  <Route path="policies"     element={<PoliciesPanel />} />
  <Route path="offices"      element={<OfficesPanel />} />
  <Route path="economy"      element={<EconomyPanel />} />
  <Route path="items"        element={<ItemsPanel />} />
  <Route path="llm"          element={<LlmUsagePanel />} />
  <Route path="forge"        element={<ForgeMonitorPanel />} />
  <Route path="lab"          element={<LabRunsPanel />} />
  <Route path="system"       element={<SystemConfigPanel />} />
</Route>
```

要点：
- `AdminLayout` 承担路由守卫（替代现在 AdminPage 里的 `Navigate`）、TopNav、侧栏和滚动容器；面板通过 `<Outlet />` 渲染。
- 列表页的筛选/分页状态放 `useSearchParams`（如 `/admin/users?q=alice&page=2`），实现完整深链。
- token 统一由面板内部 `useGameStore((s) => s.token)` 获取，**废除 token prop 传递**（消灭现状的两套模式）。

---

## 4. 视觉设计

沿用 `global.css` 现有令牌，不新建色板（管理后台是工具界面，用产品 App 侧的 zinc 体系，**不用** DESIGN.md 里营销站的 neon 体系）：

| 用途 | 令牌 | 值 |
|---|---|---|
| 页面底色 | `--bg-base` | 现值 |
| 卡片/侧栏 | `--bg-card` | `#18181b` |
| 输入/激活态 | `--bg-input` | `#27272a` |
| 边框 | `--border` | `#27272a` |
| 主文本 | `--text-primary` | `#fafafa` |
| 次文本 | `--text-secondary` | `#a1a1aa` |
| 弱文本 | `--text-muted` | `#71717a` |
| 危险/警示 | `--accent-red` | `#e94560` |
| 成功/健康 | `--accent-green` | `#53d769` |
| 信息/链接 | `--accent-blue` | `#0ea5e9` |

新增管理台专属令牌（加在 `global.css`）：

```css
:root {
  --admin-sidebar-w: 232px;
  --admin-warn: #f59e0b;        /* 警告（介于健康与危险之间） */
  --admin-danger-bg: #ef444414; /* 危险操作区底色 */
}
```

**布局骨架**（延续现状，明确规格）：

```
┌──────────────────────────────────────────────────┐
│ TopNav (48px, 全站共用)                            │
├──────────┬───────────────────────────────────────┤
│ 侧栏      │  面板标题 + 操作区（刷新/新建）            │
│ 232px    │  ───────────────────────────────       │
│ 6 个分组  │  内容区  padding:32  overflow-y:auto    │
│ 15 个项  │  max-width: 1280px（宽表面板可放开）      │
│          │                                       │
│ 🔐 提示   │                                       │
└──────────┴───────────────────────────────────────┘
```

- 侧栏分组：分组标题用 11px 大写弱文本（同现有 "管理面板" 标题样式），组间距 16px，项高约 38px；激活态沿用现有 `--bg-input` + 边框样式。
- 响应式：< 900px 时侧栏收窄为纯图标（48px），分组标题隐藏；管理台不做移动端专门优化（桌面工具定位）。
- 状态色语义：绿=健康/活跃/已通过，黄=等待/降级，红=失败/封禁/危险，蓝=进行中/链接。

---

## 5. 新增面板设计（4 个）

### 5.1 政策面板 `/admin/policies` （优先级最高——S2-5 刚落地，治理是当前迭代主线）

数据源：`GET /admin/policies`（返回 `enabled` 开关、`TIER_MATRIX`、政策行）；`POST /admin/policies/seed`；`POST /admin/policies/{key}/amend`。

```
┌─ 政策治理 ────────────────────────────── [播种默认政策] ─┐
│ ⚠ POLIS_POLICY_ENABLED=false 时显示灰色禁用横幅          │
│                                                        │
│ 分级矩阵说明卡（4 行）：                                   │
│  行政级 administrative   → 管理员可直批 ✏️                 │
│  投票级 referendum/...   → 🔒 需走市政投票（按钮禁用+提示）   │
│  宪法核心 constitutional  → 🔒 不可修改                    │
│                                                        │
│ 政策表：key | 名称 | 当前值 | 层级徽章 | 版本 | 操作         │
│  - 行政级行：[修订] 按钮 → 弹窗（新值 + expected_version    │
│    乐观锁自动带上；409 冲突时提示重新加载）                   │
│  - 投票级行：按钮置灰，tooltip "需通过市政投票流程修改"       │
└────────────────────────────────────────────────────────┘
```

设计要点：**后端的分级 409 拒绝在 UI 层前置**——投票级政策直接不给修订入口，避免管理员误操作后看到裸 409。这是后端 docstring 里"夺权手法"防御的 UI 对应物。

### 5.2 商店物品面板 `/admin/items`

数据源：`GET/POST /admin/items`、`PATCH /admin/items/{code}`。

- 表格列：icon | code | 名称 | kind | 价格(SC) | active 开关 | 操作[编辑]。
- 新建/编辑弹窗：code（新建后不可改）、name、kind、description、icon（emoji 输入）、price_sc、payload_json（JSON 文本域 + 解析校验）、active。
- 下架 = `active` 开关 PATCH，无需删除端点（后端也没提供删除，UI 不造假按钮）。

### 5.3 官职面板 `/admin/offices`（只读）

数据源：`GET /admin/offices`。

- 卡片网格：每个官职一张卡 —— 职位 key、持有人（居民名+头像）、所属机构、策略、任期窗口（起止 + 剩余天数进度条）。
- 空缺职位显示 "🪑 空缺" 态。只读，无操作按钮（与后端能力一致）。

### 5.4 社交图谱面板 `/admin/social-graph`（只读）

数据源：`GET /admin/social-graph`（nodes / edges / circles）。

- 主视图：SVG 力导向图（**自实现简版**，几十个居民节点规模不需要 d3——避免新依赖；若后续节点数上百再评估引入 d3-force）。
  - 节点：居民，按 circle_id 着色；边：粗细映射 familiarity，颜色映射 affinity 正负。
  - 点击节点 → 右侧抽屉显示该居民的关系明细表。
- 副视图：圈子列表（连通分量），每个圈子显示成员头像组 + 规模。
- 刷新按钮手动拉取，不轮询（图计算在后端，成本可控但无需实时）。

---

## 6. 现有面板补齐（4 处）

| 面板 | 补什么 | 对应端点 |
|---|---|---|
| ResidentsPanel | ① 预设管理区（创建预设/删除预设）② 批量操作工具条：选中多个居民 → 批量迁区 / 批量状态重置 | `POST /presets`、`DELETE /presets/{id}`、`POST /batch/district`、`POST /batch/status-reset` |
| EventsPanel | 事件行加 [编辑] 按钮（现在只能删了重建） | `PATCH /events/{id}` |
| LabRunsPanel | 任务仲裁：待仲裁任务列表 + [通过/否决] 操作（带理由输入） | `POST /tasks/{task_id}/arbitrate` |
| ForgeMonitorPanel | 顶部加 SearXNG 健康徽章（绿/红 + 延迟） | `GET /forge/searxng-health` |

---

## 7. 共享组件规范（`components/admin/shared/`）

现有面板各自造轮子（表格、分页、弹窗样式略有差异）。新面板必须用共享件，老面板逐步迁移：

| 组件 | 职责 | 备注 |
|---|---|---|
| `AdminPanelHeader` | 标题 + 描述 + 右侧操作槽（刷新/新建按钮） | 每个面板顶部统一 |
| `StatCard` | 指标卡（数值 + 标签 + 可选环比） | Dashboard/Economy 已有类似实现，抽出 |
| `DataTable<T>` | 泛型表格：列定义、空态、加载骨架、行操作槽 | 替代各面板手写 table |
| `FilterBar` | 搜索框 + 下拉筛选组，状态同步到 `useSearchParams` | 深链的关键 |
| `Pagination` | 现有 `users/UsersPagination` 泛化后上提 | |
| `ConfirmDialog` | 危险操作统一确认：标题、影响面描述、需输入确认词的强确认模式（如封禁、revert 提案、kill-switch） | **所有**红色操作必须过它 |
| `JsonField` | JSON 编辑文本域 + 实时解析校验 + 错误提示 | items payload、system config 用 |
| `StatusBadge` | 状态徽章（绿/黄/红/蓝语义色） | |
| `EmptyState` | 空数据占位（图标 + 文案 + 可选动作） | |

数据获取约定：
- 统一 `useAdminQuery(fetcher, deps)` 轻量 hook（loading / error / refetch 三态），**不引入** react-query —— 管理台请求模式简单，自写 30 行 hook 足够。
- 轮询仅限两处：Dashboard 服务健康（30s）、Forge 活跃会话（10s）。其余手动刷新。

---

## 8. 权限与安全

### 8.1 现阶段（本期）
- 保持 `is_admin` 单权限位；前端 `AdminLayout` 守卫 + 后端 `require_admin` 双保险（已有，不动）。
- **UI 层危险操作分级**：
  - 一级（可逆）：直接执行 —— 编辑居民、改配置。
  - 二级（难逆）：`ConfirmDialog` 普通确认 —— 调币、删事件、cancel run。
  - 三级（不可逆/高危）：强确认（输入对象名） —— 封禁用户、revert 提案、lab kill-switch、政策 amend。

### 8.2 下一期（本方案预留，不在本期实现）
- **审计日志**：后端加 `admin_audit_log` 表（admin_id、action、target、payload、ip、时间），`require_admin` 依赖后挂写入；前端在"系统"分组下加 `/admin/audit` 只读面板。
- **角色分级**：`is_admin` → `admin_role`（viewer / operator / super），viewer 隐藏所有写操作按钮。

---

## 9. 实施路线图

| 阶段 | 内容 | 交付物 |
|---|---|---|
| **P0 框架迁移** | AdminLayout + 嵌套路由 + 分组侧栏；15 个面板挂到 URL；废除 token prop；shared 组件骨架（Header/ConfirmDialog/useAdminQuery） | 深链可用，现有功能零回归 |
| **P1 新面板** | 政策 → 物品 → 官职 → 社交图谱（按此序：治理主线优先，只读的靠后） | 后端 4 个无 UI 模块清零 |
| **P2 补齐旧面板** | 居民批量/预设、事件编辑、lab 仲裁、SearXNG 徽章 | 覆盖矩阵全绿 |
| **P3 硬化** | 审计日志（含后端表+迁移）、角色分级、老面板迁移到 DataTable/FilterBar | 安全与一致性收口 |

每阶段独立可合并（P0 是纯重构不改行为，适合先行独立 PR；P1 起每个面板一个小 PR）。

### 测试要求（沿用仓库 TDD 惯例）
- P0：AdminLayout 路由守卫测试（非 admin 重定向）、深链渲染对应面板测试。
- P1 每面板：API service 层单测 + 面板渲染/交互测试（参照现有 `*.test.tsx` 风格，vitest + testing-library）。
- 政策面板必测：投票级政策修订按钮禁用（防越权 UI 回归）。

---

## 附录 A：端点 → 面板映射速查

（略去已在 §1.2 矩阵中列出的内容；实施时以 `backend/app/routers/admin/*.py` 的 `@router.<method>` 装饰器为准源。）

## 附录 B：非目标（Out of Scope）

- 移动端适配（桌面工具定位，仅做侧栏图标化降级）。
- 引入组件库/图表库/状态库等新依赖。
- 营销站 neon 视觉体系（管理台走产品 zinc 体系）。
- 多语言（管理台固定中文）。
