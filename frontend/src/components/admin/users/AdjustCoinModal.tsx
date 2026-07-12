import { useEffect, useState, useRef } from 'react'
import { useGameStore } from '../../../stores/gameStore'
import { adminAdjustCoin, type AdminUserListItem } from '../../../services/api'

// ─── Types ────────────────────────────────────────────────────────

interface AdjustCoinModalProps {
  user: AdminUserListItem
  onClose: () => void
  onSuccess: (updatedBalance: number) => void
}

// ─── Balance Adjust Modal ─────────────────────────────────────────

export function AdjustCoinModal({ user, onClose, onSuccess }: AdjustCoinModalProps) {
  const token = useGameStore((s) => s.token)
  const [amount, setAmount] = useState<string>('')
  const [reason, setReason] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const handleSubmit = async () => {
    const parsed = parseInt(amount, 10)
    if (isNaN(parsed) || parsed === 0) {
      setError('请输入有效的金额（非零整数）')
      return
    }
    if (!reason.trim()) {
      setError('请填写调整原因')
      return
    }
    if (!token) return
    setLoading(true)
    setError(null)
    try {
      const result = await adminAdjustCoin(token, user.id, { amount: parsed, reason: reason.trim() })
      onSuccess(result.new_balance)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setLoading(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') onClose()
  }

  return (
    <div
      onKeyDown={handleKeyDown}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.6)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div style={{
        background: 'var(--bg-card)',
        border: '1px solid var(--border)',
        borderRadius: 12,
        padding: 28,
        width: 380,
        display: 'flex',
        flexDirection: 'column',
        gap: 18,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>调整 Soul Coin 余额</h3>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: 18, lineHeight: 1 }}
          >
            ×
          </button>
        </div>

        <div style={{ fontSize: 13, color: 'var(--text-secondary)', background: 'var(--bg-input)', borderRadius: 8, padding: '10px 14px' }}>
          <div>{user.name} <span style={{ color: 'var(--text-muted)' }}>({user.email})</span></div>
          <div style={{ marginTop: 4, color: 'var(--accent-green)' }}>当前余额：🪙 {user.soul_coin_balance}</div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <label style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 600 }}>调整金额（正数增加，负数扣减）</label>
          <input
            ref={inputRef}
            type="number"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="例如：100 或 -50"
            style={{
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              padding: '8px 12px',
              color: 'var(--text-primary)',
              fontSize: 14,
              outline: 'none',
            }}
          />
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <label style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 600 }}>调整原因</label>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="例如：活动奖励、补偿"
            style={{
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              padding: '8px 12px',
              color: 'var(--text-primary)',
              fontSize: 14,
              outline: 'none',
            }}
          />
        </div>

        {error && (
          <div style={{ fontSize: 13, color: '#ef4444', background: '#ef444415', padding: '8px 12px', borderRadius: 6 }}>
            {error}
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <button
            onClick={onClose}
            disabled={loading}
            style={{
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              padding: '8px 18px',
              fontSize: 13,
              cursor: 'pointer',
              color: 'var(--text-secondary)',
            }}
          >
            取消
          </button>
          <button
            onClick={() => { void handleSubmit() }}
            disabled={loading}
            style={{
              background: loading ? 'var(--bg-input)' : 'var(--accent-blue)',
              border: 'none',
              borderRadius: 6,
              padding: '8px 18px',
              fontSize: 13,
              cursor: loading ? 'default' : 'pointer',
              color: loading ? 'var(--text-muted)' : 'white',
              fontWeight: 600,
            }}
          >
            {loading ? '处理中…' : '确认调整'}
          </button>
        </div>
      </div>
    </div>
  )
}
