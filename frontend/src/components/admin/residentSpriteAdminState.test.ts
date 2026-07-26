import { describe, expect, it } from 'vitest'
import {
  formatCostUpperBound,
  recoveryControlForStatus,
  selectedRunIdAfterMutation,
} from './residentSpriteAdminState'

describe('resident sprite admin state', () => {
  it('selects a replacement run returned by retry', () => {
    expect(selectedRunIdAfterMutation('quarantined-run', { run_id: 'replacement-run' }))
      .toBe('replacement-run')
  })

  it('shows quarantined runs as retryable generation work', () => {
    expect(recoveryControlForStatus('quarantined')).toEqual({
      action: 'retry',
      label: '重新生成',
    })
  })

  it('does not expose recovery controls for queued or approved runs', () => {
    expect(recoveryControlForStatus('requested')).toBeNull()
    expect(recoveryControlForStatus('approved')).toBeNull()
  })

  it('distinguishes unknown cost from a configured conservative upper bound', () => {
    expect(formatCostUpperBound(null)).toBe('成本上界待配置')
    expect(formatCostUpperBound(0.875)).toBe('成本上界 ≤$0.8750')
    expect(formatCostUpperBound(0.000001)).toBe('成本上界 ≤$0.000001')
  })
})
