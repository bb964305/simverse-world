import { btnBase } from './helpers'

// ─── Pagination ───────────────────────────────────────────────────

interface UsersPaginationProps {
  page: number
  totalPages: number
  perPage: number
  setPage: React.Dispatch<React.SetStateAction<number>>
}

export function UsersPagination({ page, totalPages, perPage, setPage }: UsersPaginationProps) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
      <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
        第 {page} / {totalPages} 页，每页 {perPage} 条
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button
          onClick={() => setPage(1)}
          disabled={page <= 1}
          style={{ ...btnBase, opacity: page <= 1 ? 0.4 : 1, cursor: page <= 1 ? 'default' : 'pointer' }}
        >
          «
        </button>
        <button
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={page <= 1}
          style={{ ...btnBase, opacity: page <= 1 ? 0.4 : 1, cursor: page <= 1 ? 'default' : 'pointer' }}
        >
          ‹ 上一页
        </button>

        {/* Page numbers around current */}
        {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
          const start = Math.max(1, Math.min(page - 2, totalPages - 4))
          return start + i
        }).map((p) => (
          <button
            key={p}
            onClick={() => setPage(p)}
            style={{
              ...btnBase,
              minWidth: 32,
              padding: '6px 8px',
              background: p === page ? 'var(--accent-blue)' : 'var(--bg-input)',
              color: p === page ? 'white' : 'var(--text-secondary)',
              border: p === page ? 'none' : '1px solid var(--border)',
              fontWeight: p === page ? 700 : 400,
            }}
          >
            {p}
          </button>
        ))}

        <button
          onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          disabled={page >= totalPages}
          style={{ ...btnBase, opacity: page >= totalPages ? 0.4 : 1, cursor: page >= totalPages ? 'default' : 'pointer' }}
        >
          下一页 ›
        </button>
        <button
          onClick={() => setPage(totalPages)}
          disabled={page >= totalPages}
          style={{ ...btnBase, opacity: page >= totalPages ? 0.4 : 1, cursor: page >= totalPages ? 'default' : 'pointer' }}
        >
          »
        </button>
      </div>
    </div>
  )
}
