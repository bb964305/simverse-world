import { describe, expect, it } from 'vitest'
import {
  INITIAL_CONVERGENCE, isNewerRevision, advanceConvergence, reconcileToSnapshot,
} from './worldRevision'

describe('world revision convergence (Phase 9)', () => {
  it('only a newer source_cursor triggers a refetch; duplicates/older are no-ops', () => {
    const s0 = INITIAL_CONVERGENCE
    expect(isNewerRevision(s0, { seq: 5 })).toBe(true)
    const s1 = advanceConvergence(s0, { seq: 5, world_revision_id: 'rev-5' })
    expect(s1).toEqual({ sourceCursor: 5, worldRevisionId: 'rev-5' })

    // A re-delivered event at the same cursor is idempotent (no duplicate effect).
    expect(isNewerRevision(s1, { seq: 5 })).toBe(false)
    expect(advanceConvergence(s1, { seq: 5, world_revision_id: 'rev-5' })).toBe(s1)  // same object
    // An out-of-order older event is also a no-op.
    expect(isNewerRevision(s1, { seq: 3 })).toBe(false)
    expect(advanceConvergence(s1, { seq: 3 })).toBe(s1)

    // A genuinely newer cursor advances + updates the revision id.
    const s2 = advanceConvergence(s1, { seq: 8, world_revision_id: 'rev-8' })
    expect(s2).toEqual({ sourceCursor: 8, worldRevisionId: 'rev-8' })
  })

  it('reconcileToSnapshot converges forward but never backward', () => {
    const s = advanceConvergence(INITIAL_CONVERGENCE, { seq: 8, world_revision_id: 'rev-8' })
    // A fresher snapshot advances.
    expect(reconcileToSnapshot(s, { source_cursor: 12, world_revision_id: 'rev-12' }))
      .toEqual({ sourceCursor: 12, worldRevisionId: 'rev-12' })
    // A stale snapshot cannot un-apply a revision.
    expect(reconcileToSnapshot(s, { source_cursor: 4, world_revision_id: 'rev-4' })).toBe(s)
  })
})
