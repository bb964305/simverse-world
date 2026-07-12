import { type AdminUserListItem } from '../../../services/api'
import { loginMethodIcon, formatDate } from './helpers'

// ─── Table Row ────────────────────────────────────────────────────

interface UserRowProps {
  user: AdminUserListItem
  onAdjustCoin: (user: AdminUserListItem) => void
  onToggleBan: (user: AdminUserListItem) => void
  onToggleAdmin: (user: AdminUserListItem) => void
  actionLoading: string | null
}

export function UserRow({ user, onAdjustCoin, onToggleBan, onToggleAdmin, actionLoading }: UserRowProps) {
  const isLoading = actionLoading === user.id
  const cellStyle: React.CSSProperties = {
    padding: '12px 14px',
    fontSize: 13,
    borderBottom: '1px solid var(--border)',
    color: 'var(--text-secondary)',
    verticalAlign: 'middle',
  }

  return (
    <tr style={{ transition: 'background 0.12s' }}
        onMouseEnter={(e) => { (e.currentTarget as HTMLTableRowElement).style.background = 'rgba(255,255,255,0.035)' }}
        onMouseLeave={(e) => { (e.currentTarget as HTMLTableRowElement).style.background = '' }}
    >
      <td style={{ ...cellStyle, color: 'var(--text-primary)', fontWeight: 500 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{
            width: 28, height: 28, borderRadius: '50%',
            background: 'var(--bg-input)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 12, fontWeight: 700, color: 'var(--text-secondary)', flexShrink: 0,
          }}>
            {user.name?.[0]?.toUpperCase() ?? '?'}
          </div>
          <span style={{ maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {user.name}
          </span>
          {user.is_admin && (
            <span style={{ fontSize: 10, background: '#a78bfa22', color: '#a78bfa', padding: '1px 6px', borderRadius: 4, fontWeight: 600, flexShrink: 0 }}>管理员</span>
          )}
        </div>
      </td>
      <td style={{ ...cellStyle, maxWidth: 180 }}>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>
          {user.email}
        </span>
      </td>
      <td style={{ ...cellStyle, textAlign: 'center' }}>
        <div style={{ display: 'flex', gap: 4, justifyContent: 'center' }}>
          {(user.login_methods ?? []).map((m) => (
            <span key={m} title={m} style={{ fontSize: 16 }}>{loginMethodIcon(m)}</span>
          ))}
          {(!user.login_methods || user.login_methods.length === 0) && (
            <span style={{ color: 'var(--text-muted)' }}>—</span>
          )}
        </div>
      </td>
      <td style={{ ...cellStyle, textAlign: 'right', color: 'var(--accent-green)', fontWeight: 600 }}>
        🪙 {user.soul_coin_balance}
      </td>
      <td style={{ ...cellStyle, textAlign: 'center' }}>
        {user.resident_count}
      </td>
      <td style={cellStyle}>
        {formatDate(user.created_at)}
      </td>
      <td style={{ ...cellStyle, textAlign: 'center' }}>
        <span style={{
          fontSize: 11,
          fontWeight: 600,
          padding: '3px 8px',
          borderRadius: 4,
          background: user.is_banned ? '#ef444420' : '#22c55e20',
          color: user.is_banned ? '#ef4444' : '#22c55e',
        }}>
          {user.is_banned ? '已封禁' : '正常'}
        </span>
      </td>
      <td style={{ ...cellStyle }}>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'nowrap' }}>
          <button
            onClick={() => onAdjustCoin(user)}
            disabled={isLoading}
            style={{
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
              borderRadius: 5,
              padding: '4px 10px',
              fontSize: 11,
              cursor: isLoading ? 'default' : 'pointer',
              color: isLoading ? 'var(--text-muted)' : 'var(--accent-green)',
              whiteSpace: 'nowrap',
              fontWeight: 600,
            }}
          >
            调整余额
          </button>
          <button
            onClick={() => onToggleBan(user)}
            disabled={isLoading}
            style={{
              background: user.is_banned ? '#ef444418' : 'var(--bg-input)',
              border: `1px solid ${user.is_banned ? '#ef444440' : 'var(--border)'}`,
              borderRadius: 5,
              padding: '4px 10px',
              fontSize: 11,
              cursor: isLoading ? 'default' : 'pointer',
              color: isLoading ? 'var(--text-muted)' : (user.is_banned ? '#ef4444' : 'var(--text-secondary)'),
              whiteSpace: 'nowrap',
              fontWeight: 600,
            }}
          >
            {isLoading ? '…' : (user.is_banned ? '解封' : '封禁')}
          </button>
          <button
            onClick={() => onToggleAdmin(user)}
            disabled={isLoading}
            style={{
              background: user.is_admin ? '#a78bfa18' : 'var(--bg-input)',
              border: `1px solid ${user.is_admin ? '#a78bfa40' : 'var(--border)'}`,
              borderRadius: 5,
              padding: '4px 10px',
              fontSize: 11,
              cursor: isLoading ? 'default' : 'pointer',
              color: isLoading ? 'var(--text-muted)' : (user.is_admin ? '#a78bfa' : 'var(--text-secondary)'),
              whiteSpace: 'nowrap',
              fontWeight: 600,
            }}
          >
            {user.is_admin ? '取消管理员' : '设为管理员'}
          </button>
        </div>
      </td>
    </tr>
  )
}
