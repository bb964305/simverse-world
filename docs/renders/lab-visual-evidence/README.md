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
