import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { getLatestDigest, type DigestData } from '../services/api'

interface Props {
  onClose: () => void
}

/**
 * E2E-08 前端防线：后端读取侧已经过滤掉空正文行（见
 * backend/app/routers/digest.py），但前端不应该只信任后端这一层——后端可能
 * 回滚，未来也可能接入别的数据源。判据与后端 `has_real_digest_body` 同一把
 * 尺子：去掉首行「# 标题」之后剩余文本是否非空。
 *
 * 拿到非 null 但正文为空的 digest 对象时，如果只判断 `digest ?`，会走进
 * 渲染分支，`<ReactMarkdown>{''}</ReactMarkdown>` 渲染出一片空白——「还没
 * 有日报」的空态文案永远不会出现。
 */
function hasRealDigestBody(contentMd: string): boolean {
  let body = contentMd.trim()
  if (body.startsWith('#')) {
    body = body.split('\n').slice(1).join('\n')
  }
  return body.trim().length > 0
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
          ) : digest && hasRealDigestBody(digest.content_md) ? (
            <div className="digest-md">
              <div style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 12 }}>{digest.date}</div>
              <ReactMarkdown>{digest.content_md}</ReactMarkdown>
            </div>
          ) : digest ? (
            <div style={{ color: 'var(--text-muted)' }}>今天的日报内容还没准备好，晚点再来看看吧。</div>
          ) : (
            <div style={{ color: 'var(--text-muted)' }}>还没有日报，明天早上再来看看吧。</div>
          )}
        </div>
      </div>
    </div>
  )
}
