# Project Memory Cleanup

The active project truth is `docs/ROADMAP.md`. This cleanup removed session memory that described completed work, deleted branches, and superseded implementation decisions.

## Archived locally

The following raw agent material was moved to the ignored local directory `.omx/archive/2026-07-25/` so it remains recoverable without publishing machine paths, internal transcripts, or full review diffs:

- root and backend `.superpowers/sdd/` task briefs, reports, review diffs, constraints, and progress ledgers;
- old `.omx/context/` files for the Hermes and cyber Lab sessions;
- old `.omx/plans/` Lab PRD, test specification, art specification, recovery plan, and blocker-resolution plan.

## Kept in the repository

- `backend/docs/design/WORLD_CLOCK_DESIGN.md` is preserved below `memory/backend/docs/design/` because it was version controlled. It is superseded: its real-day action quota recommendation does not match the implemented world-day policy.
- `.omx/approvals/lab-agent-services-d0.json` remains active because its external production prerequisites are still unresolved.
- Current hook state and logs remain under `.omx/`; they are runtime data, not project documentation.

The stale task baseline was reduced from seven completed/deleted branches to the current `master` worktree only.
