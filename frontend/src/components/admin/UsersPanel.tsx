import { AdjustCoinModal } from './users/AdjustCoinModal'
import { UsersTable } from './users/UsersTable'
import { UsersPagination } from './users/UsersPagination'
import { useUsersActions } from './users/useUsersActions'
import { btnBase } from './users/helpers'

// ─── Main Panel ───────────────────────────────────────────────────

export function UsersPanel() {
  const {
    users,
    total,
    page,
    setPage,
    perPage,
    totalPages,
    search,
    searchInput,
    setSearchInput,
    loading,
    error,
    adjustTarget,
    setAdjustTarget,
    actionLoading,
    fetchUsers,
    handleSearch,
    handleClearSearch,
    handleAdjustCoinSuccess,
    handleToggleBan,
    handleToggleAdmin,
  } = useUsersActions()

  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleSearch()
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20, height: '100%' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700 }}>👥 用户管理</h2>

        {/* Search bar */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
            <span style={{ position: 'absolute', left: 10, fontSize: 14, color: 'var(--text-muted)', pointerEvents: 'none' }}>🔍</span>
            <input
              type="text"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              onKeyDown={handleSearchKeyDown}
              placeholder="搜索用户名 / 邮箱…"
              style={{
                background: 'var(--bg-input)',
                border: '1px solid var(--border)',
                borderRadius: 8,
                padding: '7px 12px 7px 34px',
                color: 'var(--text-primary)',
                fontSize: 13,
                outline: 'none',
                width: 240,
              }}
            />
          </div>
          <button
            onClick={handleSearch}
            style={{
              ...btnBase,
              background: 'var(--accent-blue)',
              color: 'white',
              border: 'none',
              fontWeight: 600,
            }}
          >
            搜索
          </button>
          {search && (
            <button
              onClick={handleClearSearch}
              style={btnBase}
            >
              清除
            </button>
          )}
          <button onClick={() => { void fetchUsers() }} style={btnBase}>↻ 刷新</button>
        </div>
      </div>

      {/* Total info */}
      <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
        共 <strong style={{ color: 'var(--text-secondary)' }}>{total}</strong> 位用户
        {search && <span>，搜索：&ldquo;{search}&rdquo;</span>}
      </div>

      {/* Error */}
      {error && (
        <div style={{ fontSize: 13, color: '#ef4444', background: '#ef444415', padding: '8px 12px', borderRadius: 6 }}>
          ⚠️ {error}
        </div>
      )}

      {/* Table container */}
      <UsersTable
        users={users}
        loading={loading}
        search={search}
        actionLoading={actionLoading}
        onAdjustCoin={setAdjustTarget}
        onToggleBan={(u) => { void handleToggleBan(u) }}
        onToggleAdmin={(u) => { void handleToggleAdmin(u) }}
      />

      {/* Pagination */}
      {!loading && users.length > 0 && (
        <UsersPagination page={page} totalPages={totalPages} perPage={perPage} setPage={setPage} />
      )}

      {/* Adjust coin modal */}
      {adjustTarget && (
        <AdjustCoinModal
          user={adjustTarget}
          onClose={() => setAdjustTarget(null)}
          onSuccess={(balance) => handleAdjustCoinSuccess(adjustTarget.id, balance)}
        />
      )}
    </div>
  )
}
