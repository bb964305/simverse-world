import { useEffect, useState } from 'react'
import { bridge } from '../game/phaserBridge'
import { logPhoto } from '../services/api'
import { useLocale } from '../services/locale'

interface Shot {
  dataUrl: string
  residentSlug: string
  residentName: string
}

// E10 group photo: GameScene answers 'photo:take' with a 'photo:result'
// canvas snapshot; this modal frames it polaroid-style with the resident's
// mood quip from POST /photos/log.
export function PhotoBooth() {
  const locale = useLocale((state) => state.locale)
  const isEn = locale === 'en'
  const [shot, setShot] = useState<Shot | null>(null)
  const [quip, setQuip] = useState<string | null>(null)

  useEffect(() => {
    return bridge.on('photo:result', (data: unknown) => {
      const { dataUrl, residentSlug, residentName } = data as {
        dataUrl: string; residentSlug: string; residentName?: string
      }
      setShot({ dataUrl, residentSlug, residentName: residentName ?? residentSlug })
      setQuip(null)
      logPhoto(residentSlug, undefined)
        .then((r) => setQuip(r.quip))
        .catch(() => setQuip(isEn ? '(click)' : '（咔嚓）'))
    })
  }, [isEn])

  if (!shot) return null

  const dateStr = new Date().toLocaleDateString(isEn ? 'en-US' : 'zh-CN')

  return (
    <div onClick={() => setShot(null)} style={{
      position: 'fixed', inset: 0, zIndex: 250, background: 'rgba(0,0,0,0.7)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: '#f8f6f1', borderRadius: 4, padding: '14px 14px 18px',
        boxShadow: '0 16px 48px rgba(0,0,0,0.6)', transform: 'rotate(-1.5deg)',
        maxWidth: 'min(560px, 92vw)',
      }}>
        <img
          src={shot.dataUrl}
          alt={isEn ? `Photo with ${shot.residentName}` : `与${shot.residentName}的合影`}
          style={{ display: 'block', width: '100%', borderRadius: 2, imageRendering: 'pixelated' }}
        />
        <div style={{
          marginTop: 12, textAlign: 'center', color: '#4a4438',
          fontFamily: 'Georgia, "Songti SC", serif',
        }}>
          <div style={{ fontSize: 14, fontWeight: 700 }}>📸 {isEn ? `Photo with ${shot.residentName}` : `与 ${shot.residentName} 的合影`}</div>
          <div style={{ fontSize: 11, color: '#8a8272', marginTop: 2 }}>{dateStr} · Simverse World</div>
          <div style={{ fontSize: 12, marginTop: 8, minHeight: 18, fontStyle: 'italic' }}>
            {quip === null ? '……' : `「${quip}」`}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'center', marginTop: 12 }}>
          <a
            href={shot.dataUrl}
            download={`simverse-photo-${shot.residentSlug}.png`}
            style={{
              padding: '6px 16px', borderRadius: 4, fontSize: 12, fontWeight: 600,
              background: '#4a4438', color: '#f8f6f1', textDecoration: 'none',
            }}
          >{isEn ? 'Save photo' : '保存照片'}</a>
          <button onClick={() => setShot(null)} style={{
            padding: '6px 16px', borderRadius: 4, fontSize: 12, cursor: 'pointer',
            background: 'transparent', border: '1px solid #b8b0a0', color: '#4a4438',
          }}>{isEn ? 'Close' : '关闭'}</button>
        </div>
      </div>
    </div>
  )
}
