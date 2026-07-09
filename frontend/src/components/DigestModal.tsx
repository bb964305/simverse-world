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
      style={{
        position: 'fixed', inset: 0, zIndex: 200, background: 'rgba(0,0,0,0.6)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-card)', border: '1px solid var(--border)',
          borderRadius: 12, width: 'min(640px, 92vw)', maxHeight: '82vh',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}
      >
        <div style={{
          padding: '14px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <span style={{ fontWeight: 700, fontSize: 15 }}>📰 村落日报</span>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            fontSize: 18, cursor: 'pointer',
          }}>✕</button>
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
