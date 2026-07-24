# 实验楼收尾报告（Lab Closeout）

- **分支**：`feat/lab-closeout` @ worktree `/Volumes/data/dev/sv-lab`
- **基线**：`af16bd4`（`fix(portrait)…`）→ HEAD `22ecbc1`
- **日期**：2026-07-25
- **改动范围**：14 文件，+406 / −51；仅 `frontend/**` 与 `backend/tests/**`（红线核验见文末）
- **验证**：`vitest` 全绿 94 passed / 23 files；`tsc -b` clean；`npm run build` REALEXIT=0（mount-safe sentinel）

## 结论先行

- **项 1–4：已在 master 完成**（本轮开工核查发现均非未决项，无需重做，见下表）。
- **项 5：Lab 前端收尾——本轮全部完成**：三视口响应式、OS reduced-motion 自动探测并接线、44px 触控目标，并补掉了未定义的 `sv-pulse` keyframe（原本是静默 no-op）。
- **预存测试失败：4 个已全修**，方式是把过时测试对齐当前 ADR 契约，**未改任何生产代码**（用户选定的"全修（推荐）"路线）。
- **对抗式复核**（4 维度 + 独立验证者）：6 条 finding 全部 CONFIRMED 但**全为 nit/low，零回归、零红线越界、零造假**，`mustFixBeforeReport: []`。其中唯一实质项（sv-pulse 缺 CSS reduced-motion 兜底）已顺手加固。
- **红线全部遵守**：未动 alembic、未翻 `lab_adapter`/`lab_oci_enabled`/canary 默认值、未碰小镇主线目录、未合并、未 push。

## 逐项状态

| 项 | 状态 | 证据 / 落点 |
|---|---|---|
| 1–4 | ✅ 开工核查=已在 master 完成 | 非本轮改动；核查见"项 1–4 复核"节 |
| 5 三视口响应式 | ✅ 完成 | `game-ui.css:462-469` 新增平板层 `@media (min-width:681px) and (max-width:1024px)`；配合既有 `<=680px` 移动层(383)+桌面默认 |
| 5 reduced-motion | ✅ 完成 | `useReducedMotion.ts`（matchMedia 探测）→ `LabTimelineLive.tsx` 接线 → `ExperimentPanel.tsx:334` |
| 5 触控 44px | ✅ 完成 | `labControls.ts`：每个交互控件 `minHeight:44`；关闭键 `labClose()` 44×44 |
| 5 sv-pulse keyframe | ✅ 完成（原为 no-op bug） | `game-ui.css:474-477` 定义；消费者 `LabTimeline.tsx:28` |
| 预存 4 失败 | ✅ 全修（仅测试） | 提交 `382194b`，仅动 3 个 `backend/tests/*.py` |
| reduced-motion CSS 兜底 | ✅ 复核后加固 | `game-ui.css:456-463` `[style*="sv-pulse"]{animation:none}` |

## 提交清单（8 个，一步一 commit，均带 `Verified-by:`）

```
382194b test(lab): align stale artifact/concurrency tests to current ADR contracts
88b2c7e fix(lab):  define missing sv-pulse keyframe so timeline pulse is not a no-op
0c121c2 feat(lab): add useReducedMotion hook + matchMedia test stub
97580ac feat(lab): wire OS reduced-motion into ExperimentPanel timeline
450a875 feat(lab): extract ExperimentPanel control styles with 44px min touch targets
fbeff90 feat(lab): add tablet-tier responsive rules for the Lab modal (three-viewport)
07aedea test(lab): assert timeline applies/drops sv-pulse end-to-end via reduced-motion
22ecbc1 fix(lab):  add CSS reduced-motion backstop for sv-pulse (defense-in-depth)
```

## 项 1–4 复核（为何判定"已在 master 完成"）

本轮开工时逐项核查，确认 1–4 并非未决项，故未重做：

- **artifact 元数据契约（ADR-lab-artifact-storage）**：`serialize_artifact`（`backend/app/services/lab_task_service.py:120`）仅返回元数据；正文 `text_md`/`uri` 只经 `/download` 出口，digest 篡改在 `/download`（`verify_and_get`）判 409，元数据 GET 是纯 ACL（`get_manifest_for_user`，404/200 无正文）。这一契约在 master 已落地——本轮 4 个"失败"实为**测试滞后于该契约**，非功能缺失。
- **`is_releasable` 语义**（`backend/app/services/lab_artifact_service.py:71`）：要求 `storage_status ∈ {legacy, released}` + 完整性；内存态未 flush 的 artifact `storage_status=None`，DB 默认 `"legacy"` 不生效——这正是 2 个 serialize 测试原本误判 `unlocked is False` 的根因，已在测试内补 `storage_status="legacy"` 对齐。

## 预存 4 失败——修复方式（未改生产代码）

| 测试 | 根因 | 对齐方式 |
|---|---|---|
| `test_serialize_releases_clean_verified_content` | 内存态 artifact `storage_status=None`→不可 release | 构造加 `storage_status="legacy"`；断言 `unlocked is True` 且视图无 `text_md`/`uri` |
| `test_serialize_artifact_locked_hides_content_but_shows_integrity_metadata` | 同上 | 加 `storage_status="legacy"`；断言解锁视图无正文、`sha256` 在 |
| `…cross_tenant_404_tamper_409_flag_off_200`（重命名为 `…metadata_acl_and_download_digest_boundary`） | 旧测试误期望元数据 GET 篡改返 409 | 改为契约真值：跨租户元数据 404、属主元数据 200 无正文、篡改正文经 `/download` 返 409 |
| `test_lab_concurrency`（v2 拒绝） | match 串过时 | 对齐 `lab_runtime_v2_canary_enabled=true` |

> 后端验证（提交 `382194b` 的 `Verified-by`）：`uv run pytest tests/test_lab_concurrency.py tests/test_lab_artifact_download.py tests/test_lab_artifact_safety.py tests/test_lab_artifacts.py -q -> 20 passed`。

## 对抗式复核结论（4 维度 + 独立验证者）

6 条 finding 全部 CONFIRMED，但严重度全为 nit/low，`mustFixBeforeReport: []`：

- **[low] 已加固** — sv-pulse 无 CSS reduced-motion 兜底（此前仅 JS 层）→ 已加 `game-ui.css:456-463` 的 `[style*="sv-pulse"]{animation:none!important}`。
- **[low] 冗余非造假** — `LabTimelineLive.test.tsx` test #2 非判别性（删接线仍绿），但 #1/#3 能杀死该 mutation（验证者亲测 "2 failed | 1 passed"），故属冗余覆盖，保留。
- **[nit] 符合意图** — 关闭键 32→44px（`min-*` 覆盖全局固定尺寸，`max(44,32)=44`），仅 Lab 面板生效，兄弟弹窗仍 32；task-row `block→flex-column`，标题仍走 ellipsis（cross-axis stretch 绑定 rail 宽）。运行时 pass 眼校。
- **[nit] 本代码不可达** — `useReducedMotion` 未 guard `window`（Vite SPA 无 SSR，已核实无 entry-server）；`addListener` 理论抛错（真实浏览器不可达，stub 两法都给）。

## 红线核验（命令证据）

- `git diff --name-only af16bd4..HEAD | grep -i alembic` → **NONE**
- `… | grep -E "^backend/app/"` → **NONE**（未碰小镇主线 `backend/app/**`）
- `git diff af16bd4..HEAD | grep -iE "lab_adapter|lab_oci_enabled"` → **NONE**；`lab_runtime_v2_canary` 唯一命中是测试 `match=` 断言串，非默认值改动
- `… | grep -Ev "^frontend/|^backend/tests/"` → **NONE**（改动只在前端 + 后端测试）
- **未合并、未 push**（分支停在 `feat/lab-closeout`）

## 遗留 / 外部依赖 / TODO

- **需人工浏览器眼校（headless 无法覆盖的最后一环）**：本环境无 Playwright/Puppeteer，未擅自安装（供应链 + 红线最小化）。已做到的最强 headless 证据是 jsdom 渲染断言（`LabTimelineLive.test.tsx`：motion 允许时轨道带 inline `animation: sv-pulse`，reduced-motion 时清空）+ 构建产物含全部 CSS 改动。**仍需**在真实浏览器：① 切 OS reduced-motion 看脉冲停/走；② 375 / 768 / 1280 三宽度看布局；③ 量控件高 ≥44px。
- **无 alembic TODO**：本轮未触及任何需要迁移的 schema，红线"需迁移记 TODO"无触发项。
- **合并 / push / 部署**：均属对外不可逆动作，按红线**未执行**，待授权。
