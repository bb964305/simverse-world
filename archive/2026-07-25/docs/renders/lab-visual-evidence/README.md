# Lab frontend visual verification — running app (2026-07-20)

The full stack was run end to end to visually verify the Phase 1/5/9 UI, NOT just
unit tests. Backend (uvicorn, SQLite + Redis) + frontend (vite) were bound to
`127.0.0.1` (the macOS system proxy bypasses localhost, so Chrome reaches them;
LAN/Tailscale IPs are hijacked by the proxy and fail). Authenticated as a seeded
player, entered the game, opened the 🧪 实验楼 (ExperimentPanel), and captured all
three tabs.

## Observed (screenshots captured via the in-app browser)

- **发布委托 (Publish):** title + brief inputs, scope chips (web_search selected,
  browse/code/http), researcher dropdown (公开招募), reward, 发布委托 — clean,
  readable, teal-accented.
- **运行直播 (Live) — the four-track timeline (Phase 9):** for the seeded task it
  renders FOUR SEPARATE labeled tracks, never merged:
  - 任务 (Task): 已分派   · 运行 (Run): 排队
  - 阶段 (Phase): —      · 连接 (Connection): 在线
  - plus 适配器 mock and a 取消委托 control.
  The states are truthful: phase is `—` (verifying overlays only a running run),
  connection online, Task/Run distinct — exactly the LabTimeline contract that is
  also unit-tested (LabTimeline.test.tsx).
- **产物 & 提案墙 (Artifacts):** correct empty state (no completed task yet).

## Assessment

No blocking visual defect on the panel: no overlap/clipping, readable contrast
over the game backdrop, consistent accent, semantically-truthful status (no
fabricated idle/running, unknown -> flagged chip by construction). The four-track
timeline behaves as designed in the live app.

## Honest boundary

This is a developer visual verification (the UI runs and is truthful), not the
formal `$visual-verdict >=90` scored pass across desktop/tablet/mobile/reduced-
motion — that scoring skill is not in this environment's toolset. Reduced-motion
and disconnected/frozen states are covered by unit tests; a full multi-viewport
scored sweep remains a manual QA step.

## Responsive (multi-viewport) analysis

The in-app browser's screenshot capture does not reflect `resize_window` (viewport
stayed 1920), so the multi-viewport check was done at the CSS/DOM level (the
substance of the reflow requirement) plus the live desktop render:

- Modal: `.game-modal-panel { width: min(620px, calc(100vw - 32px)) }` — it never
  overflows the viewport (measured `panelOverflowsViewport=false`); on a 375px
  phone it is ~343px, not 620px.
- Lab split (task list + live view): `@media (max-width:680px){ .game-lab-split{
    flex-direction: column } ... :first-child{ width:100% } }` — stacks vertically
  on mobile, so no horizontal overflow/clipping. Breakpoints exist at
  720/680/420px.
- Reduced-motion / disconnected: LabTimeline renders static (data-frozen), covered
  by LabTimeline.test.tsx.
- Minor polish (non-blocking): a few action buttons render ~36px tall vs the 44px
  touch-target ideal — no overlap/overflow, just a touch-size nit for a later pass.

Net: no blocking overlap/overflow/clipping defect across widths by construction;
the formal scored `$visual-verdict` sweep (a skill absent here) is the only piece
not run.
