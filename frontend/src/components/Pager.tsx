import type { CSSProperties, Dispatch, SetStateAction } from 'react'
import { useLocale } from '../services/locale'

// Phaser-free, dependency-free pager for game overlays (BulletinBoard etc.).
// Mirrors admin/users/UsersPagination's look without pulling in the admin
// helpers module. Purely presentational — the owner holds the page state.

const btnBase: CSSProperties = {
  background: 'var(--bg-input)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  padding: '6px 14px',
  fontSize: 12,
  cursor: 'pointer',
  color: 'var(--text-secondary)',
}

interface PagerProps {
  page: number
  totalPages: number
  setPage: Dispatch<SetStateAction<number>>
}

export function Pager({ page, totalPages, setPage }: PagerProps) {
  const en = useLocale((state) => state.locale === 'en')
  const atStart = page <= 1
  const atEnd = page >= totalPages
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
      <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
        {en ? `Page ${page} / ${totalPages}` : `第 ${page} / ${totalPages} 页`}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={atStart}
          style={{ ...btnBase, opacity: atStart ? 0.4 : 1, cursor: atStart ? 'default' : 'pointer' }}
        >
          ‹ {en ? 'Previous' : '上一页'}
        </button>
        <button
          onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          disabled={atEnd}
          style={{ ...btnBase, opacity: atEnd ? 0.4 : 1, cursor: atEnd ? 'default' : 'pointer' }}
        >
          {en ? 'Next' : '下一页'} ›
        </button>
      </div>
    </div>
  )
}
