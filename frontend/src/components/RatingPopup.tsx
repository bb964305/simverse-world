import { useEffect, useRef, useState } from 'react'

interface RatingPopupProps {
  residentName: string
  conversationId: string
  onRate: (rating: number) => void
  onSkip: () => void
}

export function RatingPopup({ residentName, onRate, onSkip }: RatingPopupProps) {
  const [hovered, setHovered] = useState(0)
  const [selected, setSelected] = useState(0)
  const skipButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    skipButtonRef.current?.focus()
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onSkip()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onSkip])

  const submit = () => {
    if (selected > 0) onRate(selected)
  }

  return (
    <div className="game-modal-backdrop">
      <div
        className="game-modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rating-dialog-title"
        style={{ padding: 28, width: 'min(320px, calc(100vw - 24px))', textAlign: 'center' }}
      >
        <div style={{ fontSize: 32, marginBottom: 8 }}>💬</div>
        <div id="rating-dialog-title" style={{ fontWeight: 700, fontSize: 16, marginBottom: 4 }}>对话结束了</div>
        <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 20 }}>
          和 {residentName} 的对话怎么样？
        </div>

        {/* Star rating */}
        <div style={{ display: 'flex', justifyContent: 'center', gap: 8, marginBottom: 20 }}>
          {[1, 2, 3, 4, 5].map((star) => (
            <button
              key={star}
              onMouseEnter={() => setHovered(star)}
              onMouseLeave={() => setHovered(0)}
              onClick={() => setSelected(star)}
              style={{
                background: 'none', border: 'none', cursor: 'pointer',
                fontSize: 28, lineHeight: 1, padding: 4,
                transform: (hovered >= star || selected >= star) ? 'scale(1.2)' : 'scale(1)',
                transition: 'transform 0.1s ease',
                filter: (hovered >= star || selected >= star) ? 'none' : 'grayscale(1) opacity(0.4)',
              }}
            >
              ⭐
            </button>
          ))}
        </div>

        {selected > 0 && (
          <div style={{ color: 'var(--text-secondary)', fontSize: 12, marginBottom: 12 }}>
            {['', '很一般', '还不错', '挺好的', '非常好', '完美！'][selected]}
          </div>
        )}

        <div style={{ display: 'flex', gap: 8 }}>
          <button ref={skipButtonRef} onClick={onSkip} style={{
            flex: 1, background: 'var(--bg-input)', color: 'var(--text-muted)',
            border: '1px solid var(--border)', padding: '10px 16px',
            borderRadius: 'var(--radius)', fontSize: 13, cursor: 'pointer',
          }}>
            跳过
          </button>
          <button onClick={submit} disabled={selected === 0} style={{
            flex: 1, background: selected > 0 ? 'var(--accent-red)' : 'var(--bg-input)',
            color: selected > 0 ? 'white' : 'var(--text-muted)',
            border: 'none', padding: '10px 16px', borderRadius: 'var(--radius)',
            fontSize: 13, fontWeight: 700, cursor: selected > 0 ? 'pointer' : 'not-allowed',
          }}>
            提交评分
          </button>
        </div>
      </div>
    </div>
  )
}
