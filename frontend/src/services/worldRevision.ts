// Phase 9 (recovery plan) — revision-aware world convergence.
//
// The world_changed v1 envelope carries a monotonic `seq` (the durable
// source_cursor) and a `world_revision_id`. Map, minimap, and Exploration Codex
// must converge on the SAME cursor without duplicate success effects: a
// re-delivered or out-of-order world_changed with a seq we already applied is a
// no-op, and only a genuinely newer cursor triggers a refetch. On a gap/reconnect
// the caller refetches unconditionally (a lower/unknown cursor can hide a missed
// revision), then re-syncs from the fetched state. Pure + framework-free so it is
// unit-testable and shared by the WS layer and any world-consuming view.

export interface WorldConvergence {
  sourceCursor: number            // highest applied source_cursor (0 = nothing yet)
  worldRevisionId: string | null  // the revision id at that cursor
}

export interface WorldChangedLike {
  seq?: number
  world_revision_id?: string | null
}

export const INITIAL_CONVERGENCE: WorldConvergence = { sourceCursor: 0, worldRevisionId: null }

// True when this event advances the world past what we have applied — the only
// case that should trigger a refetch + a single convergence success effect.
export function isNewerRevision(state: WorldConvergence, event: WorldChangedLike): boolean {
  return Number(event.seq ?? 0) > state.sourceCursor
}

// Advance the converged cursor. Idempotent: an already-applied/older seq returns
// the SAME state object, so a duplicate delivery cannot double-count.
export function advanceConvergence(state: WorldConvergence, event: WorldChangedLike): WorldConvergence {
  const seq = Number(event.seq ?? 0)
  if (seq <= state.sourceCursor) return state
  return { sourceCursor: seq, worldRevisionId: event.world_revision_id ?? state.worldRevisionId }
}

// After a (re)connect or a refetch, converge to the server's reported cursor.
// Never moves the cursor backward — a stale snapshot cannot un-apply a revision.
export function reconcileToSnapshot(
  state: WorldConvergence,
  snapshot: { source_cursor?: number; world_revision_id?: string | null },
): WorldConvergence {
  const seq = Number(snapshot.source_cursor ?? 0)
  if (seq <= state.sourceCursor) return state
  return { sourceCursor: seq, worldRevisionId: snapshot.world_revision_id ?? state.worldRevisionId }
}
