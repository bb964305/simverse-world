import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { getLatestDigest, type DigestData } from '../services/api'

interface Props {
  onClose: () => void
}

export function DigestModal({ onClose }: Props) {
  const [digest, setDigest] = useState<DigestData | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    getLatestDigest()
      .then((r) => { if (!cancelled) setDigest(r.digest) })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  return (
    <div
      onClick={onClose}
      className="game-modal-backdrop"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="game-modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="digest-dialog-title"
        style={{
          width: 'min(640px, calc(100vw - 32px))',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}
      >
        <div style={{
          padding: '14px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <span id="digest-dialog-title" style={{ fontWeight: 700, fontSize: 15 }}>📰 村落日报</span>
          <button autoFocus onClick={onClose} className="game-dialog-close" aria-label="关闭村落日报">✕</button>
        </div>
        <div style={{ padding: '20px 24px', overflowY: 'auto', lineHeight: 1.75, fontSize: 14 }}>
          {loading ? (
            <div style={{ color: 'var(--text-muted)' }}>加载中…</div>
          ) : digest ? (
            <div className="digest-md">
              <div style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 12 }}>{digest.date}</div>
              <ReactMarkdown>{digest.content_md}</ReactMarkdown>
            </div>
          ) : (
            <div style={{ color: 'var(--text-muted)' }}>还没有日报，明天早上再来看看吧。</div>
          )}
        </div>
      </div>
    </div>
  )
}
