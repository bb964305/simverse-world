import { type AdminUserListItem } from '../../../services/api'
import { UserRow } from './UserRow'

// ─── Users Table ──────────────────────────────────────────────────

interface UsersTableProps {
  users: AdminUserListItem[]
  loading: boolean
  search: string
  actionLoading: string | null
  onAdjustCoin: (user: AdminUserListItem) => void
  onToggleBan: (user: AdminUserListItem) => void
  onToggleAdmin: (user: AdminUserListItem) => void
}

export function UsersTable({
  users,
  loading,
  search,
  actionLoading,
  onAdjustCoin,
  onToggleBan,
  onToggleAdmin,
}: UsersTableProps) {
  return (
    <div style={{ flex: 1, overflowX: 'auto', background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 12 }}>
      {loading ? (
        <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)', fontSize: 14 }}>
          加载中…
        </div>
      ) : users.length === 0 ? (
        <div style={{ padding: 64, textAlign: 'center', color: 'var(--text-muted)' }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>👤</div>
          <div style={{ fontSize: 14 }}>{search ? '没有找到匹配的用户' : '暂无用户数据'}</div>
        </div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed' }}>
          <colgroup>
            <col style={{ width: 160 }} />
            <col style={{ width: 180 }} />
            <col style={{ width: 90 }} />
            <col style={{ width: 110 }} />
            <col style={{ width: 80 }} />
            <col style={{ width: 110 }} />
            <col style={{ width: 80 }} />
            <col style={{ width: 260 }} />
          </colgroup>
          <thead>
            <tr style={{ background: 'var(--bg-input)' }}>
              {['用户名', '邮箱', '登录方式', 'Soul Coin', '居民数', '注册时间', '状态', '操作'].map((col) => (
                <th
                  key={col}
                  style={{
                    padding: '10px 14px',
                    fontSize: 12,
                    fontWeight: 600,
                    color: 'var(--text-muted)',
                    textAlign: col === 'Soul Coin' ? 'right' : col === '登录方式' || col === '居民数' || col === '状态' ? 'center' : 'left',
                    borderBottom: '1px solid var(--border)',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <UserRow
                key={user.id}
                user={user}
                onAdjustCoin={onAdjustCoin}
                onToggleBan={onToggleBan}
                onToggleAdmin={onToggleAdmin}
                actionLoading={actionLoading}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
