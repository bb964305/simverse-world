import { describe, expect, it } from 'vitest'
import { resolveLabDisplay, selectLabTask, canDecideApproval, approvalId } from './labState'
import { artifactKindBadge, artifactStatusBadges } from './labArtifactBadges'

describe('canDecideApproval — server-authoritative controls (Phase 5)', () => {
  it('v1 projection: only an owner with allowed approve + can_decide may decide', () => {
    expect(canDecideApproval({ allowed_actions: ['approve', 'deny'], can_decide: true, status: 'pending' })).toBe(true)
    // observer / non-owner: server returns no actions and can_decide false
    expect(canDecideApproval({ allowed_actions: [], can_decide: false, status: 'pending' })).toBe(false)
    // can_decide true but server did not grant approve (e.g. already decided scope)
    expect(canDecideApproval({ allowed_actions: [], can_decide: true, status: 'pending' })).toBe(false)
  })

  it('legacy flag-off path (no projection) falls back to pending status', () => {
    expect(canDecideApproval({ status: 'pending' })).toBe(true)
    expect(canDecideApproval({ status: 'approved' })).toBe(false)
  })

  it('approvalId prefers the v1 projection id, falls back to the legacy id', () => {
    expect(approvalId({ approval_id: 'a1', id: 'legacy' })).toBe('a1')
    expect(approvalId({ id: 'legacy' })).toBe('legacy')
  })
})

describe('selectLabTask — single derivation point (Phase 1 build contract)', () => {
  const tasks = [
    { id: 't1', status: 'running' },
    { id: 't2', status: 'review' },
  ]

  it('returns the selected task so status display + actions share one source', () => {
    expect(selectLabTask(tasks, 't2')).toEqual({ id: 't2', status: 'review' })
  })

  it('returns undefined when nothing is selected or the id is missing', () => {
    expect(selectLabTask(tasks, null)).toBeUndefined()
    expect(selectLabTask(tasks, '')).toBeUndefined()
    expect(selectLabTask(tasks, 'gone')).toBeUndefined()
  })

  it('feeds resolveLabDisplay: selected task + run render as separate badges', () => {
    const t = selectLabTask(tasks, 't1')
    const d = resolveLabDisplay({ taskStatus: t?.status, runStatus: 'succeeded' })
    expect(d.task).toMatchObject({ value: 'running', known: true })
    expect(d.run).toMatchObject({ value: 'succeeded', known: true })
  })
})

describe('resolveLabDisplay — 6 rules', () => {
  it('rule 1: renders Task and Run canonical badges separately', () => {
    const d = resolveLabDisplay({ taskStatus: 'review', runStatus: 'succeeded' })
    expect(d.task).toMatchObject({ value: 'review', label: '待验收', known: true })
    expect(d.run).toMatchObject({ value: 'succeeded', label: '成功', known: true })
  })

  it('rule 2: a terminal run clears verifying phase and approval', () => {
    const d = resolveLabDisplay({
      taskStatus: 'review', runStatus: 'succeeded', eventPhase: 'verifying',
    })
    expect(d.phase).toBeNull()
    expect(d.approvalActive).toBe(false)
  })

  it('rule 3: needs_approval outranks running; verifying only overlays running', () => {
    const approval = resolveLabDisplay({ taskStatus: 'running', runStatus: 'needs_approval', eventPhase: 'verifying' })
    expect(approval.approvalActive).toBe(true)
    expect(approval.phase).toBeNull() // verifying does not overlay needs_approval

    const running = resolveLabDisplay({ taskStatus: 'running', runStatus: 'running', eventPhase: 'verifying' })
    expect(running.phase).toBe('verifying')

    const queued = resolveLabDisplay({ taskStatus: 'running', runStatus: 'queued', eventPhase: 'verifying' })
    expect(queued.phase).toBeNull() // verifying never overlays a non-running run
  })

  it('rule 4: connection overlay freezes but never rewrites Task/Run', () => {
    const d = resolveLabDisplay({ taskStatus: 'running', runStatus: 'running', connection: 'disconnected' })
    expect(d.frozen).toBe(true)
    expect(d.connection).toBe('disconnected')
    expect(d.task.value).toBe('running') // business badges unchanged
    expect(d.run?.value).toBe('running')
    expect(resolveLabDisplay({ taskStatus: 'running', connection: 'resyncing' }).frozen).toBe(true)
    expect(resolveLabDisplay({ taskStatus: 'running' }).frozen).toBe(false)
  })

  it('rule 5: researching is a read-only resident activity only', () => {
    const d = resolveLabDisplay({ taskStatus: 'running', runStatus: 'running', residentActivity: 'researching' })
    expect(d.activity).toBe('researching')
    expect(d.task.value).toBe('running') // not substituted by researching
    expect(resolveLabDisplay({ taskStatus: 'running', residentActivity: 'sleeping' }).activity).toBeNull()
  })

  it('rule 6: unknown values render 未知状态 and are flagged, never idle/running', () => {
    const d = resolveLabDisplay({ taskStatus: 'wat', runStatus: 'huh' })
    expect(d.task).toMatchObject({ label: '未知状态', known: false })
    expect(d.run).toMatchObject({ label: '未知状态', known: false })
  })

  it('has no run badge when there is no run', () => {
    expect(resolveLabDisplay({ taskStatus: 'funded' }).run).toBeNull()
  })
})

describe('artifact badges', () => {
  it('maps the 6 kinds and flags unknown', () => {
    expect(artifactKindBadge('world_draft')).toMatchObject({ label: '世界草案', known: true })
    for (const k of ['text', 'file', 'link', 'image', 'dataset']) {
      expect(artifactKindBadge(k).known).toBe(true)
    }
    expect(artifactKindBadge('weird')).toMatchObject({ label: '未知类型', known: false })
  })

  it('derives status badges from fields (verified + retained + locked)', () => {
    const badges = artifactStatusBadges({
      kind: 'file', unlocked: false, scan_status: 'clean',
      verification_status: 'verified', provenance: 'verifier', retention_hold: true,
    }).map((b) => b.key)
    expect(badges).toContain('verified')
    expect(badges).toContain('retained')
    expect(badges).toContain('locked')
  })

  it('flags quarantined / rejected / provenance_missing', () => {
    expect(artifactStatusBadges({ kind: 'file', scan_status: 'flagged' }).map((b) => b.key)).toContain('quarantined')
    expect(artifactStatusBadges({ kind: 'file', verification_status: 'rejected' }).map((b) => b.key)).toContain('rejected')
    expect(artifactStatusBadges({ kind: 'link', provenance: null }).map((b) => b.key)).toContain('provenance_missing')
  })
})
