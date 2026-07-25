# Simverse World 2026-07-25 Archive

This directory is a read-only snapshot of superseded project material. The only active planning document is [`docs/ROADMAP.md`](../../docs/ROADMAP.md).

## Contents

- `docs/`: the former `docs/` tree, preserving its original relative layout. It contains old plans, specifications, ADRs, research, reports, test notes, render evidence, and progress ledgers.
- `memory/README.md`: the cleanup ledger for expired agent/session memory. Raw ignored material is retained locally under `.omx/archive/2026-07-25/` rather than published.
- `memory/backend/docs/design/WORLD_CLOCK_DESIGN.md`: the superseded design that kept action quotas on real days; the implemented policy now lives in the active Roadmap.

## Deliberate relocations

- `docs/screenshots/` moved to `assets/screenshots/` because the root README and design guide still use those files.
- `docs/art/asset-provenance.json` moved to `frontend/config/asset-provenance.json` because it is an executable release gate input, not narrative documentation.
- `docs/ROADMAP_2026-07-24.md` remains unchanged in the snapshot; a rewritten `docs/ROADMAP.md` is the active replacement.

Archived files retain source-era wording and paths. They are evidence, not current instructions; do not update them in place to describe new work.

Current hook-owned runtime state, logs, and the unresolved Lab external-approval request remain under `.omx/`; they are operational state rather than project documentation. The active task baseline was compacted to the current `master` worktree.
