export interface SpriteRunMutationResult {
  run_id: string
}

export interface SpriteRunRecoveryControl {
  action: 'resume' | 'retry'
  label: '重新排队' | '重新生成'
}

export function formatCostUpperBound(value: number | null): string {
  if (value === null) return '成本上界待配置'
  const amount = value.toFixed(6).replace(/0{1,2}$/, '')
  return `成本上界 ≤$${amount}`
}

export function recoveryControlForStatus(status: string): SpriteRunRecoveryControl | null {
  if (status === 'interrupted') return { action: 'resume', label: '重新排队' }
  if (status === 'failed') return { action: 'retry', label: '重新排队' }
  if (status === 'quarantined' || status === 'rejected') {
    return { action: 'retry', label: '重新生成' }
  }
  return null
}

export function selectedRunIdAfterMutation(
  currentRunId: string | null,
  result: SpriteRunMutationResult,
): string {
  return result.run_id || currentRunId || ''
}
