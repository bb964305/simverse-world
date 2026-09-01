import { useEffect, useRef, useState } from 'react'
import { API_BASE, getResidentCard, type ResidentCardData } from '../services/api'
import { useLocale } from '../services/locale'

// ── C1 灵魂卡片：canvas 分享卡 ───────────────────────────────────────
// GET /residents/{slug}/card 的公开摘要绘制成 900x1200 PNG，可下载分享。
// portrait_url 是后端 /static 相对路径 → 补 API_BASE 后可能跨域，Image 加
// crossOrigin='anonymous' 防 canvas taint；CORS 拒绝时 onerror 兜底为首字母
// 圆形头像（纯绘制，永不 taint），下载路径始终可用。

const W = 900
const H = 1200
const FONT = 'Inter, Geist, "Noto Sans", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif'

function resolvePortraitUrl(url: string): string {
  return /^https?:\/\//i.test(url) ? url : `${API_BASE}${url}`
}

function loadPortrait(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.crossOrigin = 'anonymous' // 跨域 /static 肖像必须带 CORS，否则 canvas 被 taint 无法导出
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('portrait load failed'))
    img.src = url
  })
}

/** CJK 无空格 → 逐字符测量换行；超出 maxLines 时末行加省略号。 */
function wrapText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number, maxLines: number): string[] {
  const clean = text.replace(/\s+/g, ' ').trim()
  const lines: string[] = []
  let line = ''
  for (const ch of clean) {
    if (line !== '' && ctx.measureText(line + ch).width > maxWidth) {
      lines.push(line)
      line = ch === ' ' ? '' : ch
    } else {
      line += ch
    }
  }
  if (line.trim()) lines.push(line)
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines)
    kept[maxLines - 1] = `${kept[maxLines - 1].slice(0, -1)}…`
    return kept
  }
  return lines
}

function roundedRectPath(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

function drawCard(canvas: HTMLCanvasElement, card: ResidentCardData, portrait: HTMLImageElement | null, en: boolean) {
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  // ① 渐变底 + 暗角
  const bg = ctx.createLinearGradient(0, 0, 0, H)
  bg.addColorStop(0, '#232338')
  bg.addColorStop(0.55, '#191927')
  bg.addColorStop(1, '#101018')
  ctx.fillStyle = bg
  ctx.fillRect(0, 0, W, H)

  // 头像后方的品牌色光晕
  const glow = ctx.createRadialGradient(W / 2, 300, 40, W / 2, 300, 320)
  glow.addColorStop(0, 'rgba(233,69,96,0.22)')
  glow.addColorStop(1, 'rgba(233,69,96,0)')
  ctx.fillStyle = glow
  ctx.fillRect(0, 0, W, 640)

  // 内框
  roundedRectPath(ctx, 28, 28, W - 56, H - 56, 24)
  ctx.strokeStyle = 'rgba(255,255,255,0.10)'
  ctx.lineWidth = 2
  ctx.stroke()

  // 装饰星点
  ctx.fillStyle = 'rgba(255,255,255,0.28)'
  const dots: [number, number, number][] = [
    [120, 130, 2.2], [780, 110, 1.6], [690, 210, 2.6], [180, 250, 1.4],
    [760, 420, 1.8], [110, 480, 2.0], [820, 640, 1.4], [90, 860, 1.6],
  ]
  for (const [x, y, r] of dots) {
    ctx.beginPath()
    ctx.arc(x, y, r, 0, Math.PI * 2)
    ctx.fill()
  }

  // ② 头像：圆形裁切肖像，加载失败 → 首字母圆
  const cx = W / 2
  const cy = 300
  const R = 120
  ctx.save()
  ctx.beginPath()
  ctx.arc(cx, cy, R, 0, Math.PI * 2)
  ctx.clip()
  if (portrait) {
    // cover 裁切：短边贴合圆的直径
    const scale = Math.max((R * 2) / portrait.width, (R * 2) / portrait.height)
    const dw = portrait.width * scale
    const dh = portrait.height * scale
    ctx.drawImage(portrait, cx - dw / 2, cy - dh / 2, dw, dh)
  } else {
    const av = ctx.createLinearGradient(cx - R, cy - R, cx + R, cy + R)
    av.addColorStop(0, '#e94560')
    av.addColorStop(1, '#6c5ce7')
    ctx.fillStyle = av
    ctx.fillRect(cx - R, cy - R, R * 2, R * 2)
    const initial = Array.from(card.name.trim())[0]?.toUpperCase() ?? '?'
    ctx.fillStyle = 'rgba(255,255,255,0.92)'
    ctx.font = `700 110px ${FONT}`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(initial, cx, cy + 8)
  }
  ctx.restore()
  // 头像描边环
  ctx.beginPath()
  ctx.arc(cx, cy, R + 7, 0, Math.PI * 2)
  ctx.strokeStyle = 'rgba(233,69,96,0.85)'
  ctx.lineWidth = 5
  ctx.stroke()
  ctx.beginPath()
  ctx.arc(cx, cy, R + 16, 0, Math.PI * 2)
  ctx.strokeStyle = 'rgba(255,255,255,0.12)'
  ctx.lineWidth = 2
  ctx.stroke()

  ctx.textAlign = 'center'
  ctx.textBaseline = 'alphabetic'

  // ③ 名字
  ctx.fillStyle = '#f4f4f5'
  ctx.font = `700 58px ${FONT}`
  ctx.fillText(card.name, cx, 520, W - 160)

  // ④ SBTI 徽章（可能缺失）
  let y = 566
  if (card.sbti_type) {
    const label = card.sbti_name ? `${card.sbti_type} · ${card.sbti_name}` : card.sbti_type
    ctx.font = `600 26px ${FONT}`
    const tw = ctx.measureText(label).width
    const pw = tw + 48
    roundedRectPath(ctx, cx - pw / 2, y, pw, 46, 23)
    ctx.fillStyle = 'rgba(108,92,231,0.30)'
    ctx.fill()
    ctx.strokeStyle = 'rgba(108,92,231,0.85)'
    ctx.lineWidth = 2
    ctx.stroke()
    ctx.fillStyle = '#c7bfff'
    ctx.fillText(label, cx, y + 33)
    y += 76
  } else {
    y += 14
  }

  // ⑤ 星级（实心 + 空心补满 5 颗）
  const rating = Math.max(0, Math.min(5, card.star_rating))
  ctx.font = `36px ${FONT}`
  ctx.fillStyle = '#fbbf24'
  ctx.fillText('★'.repeat(rating), cx - (5 - rating) * 21, y + 30)
  if (rating < 5) {
    ctx.fillStyle = 'rgba(251,191,36,0.28)'
    ctx.fillText('★'.repeat(5 - rating), cx + rating * 21, y + 30)
  }
  y += 96

  // 分隔线
  ctx.strokeStyle = 'rgba(255,255,255,0.10)'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(180, y)
  ctx.lineTo(W - 180, y)
  ctx.stroke()
  y += 66

  // ⑥ soul 首段摘录
  ctx.fillStyle = 'rgba(233,69,96,0.55)'
  ctx.font = '700 64px Georgia, serif'
  ctx.fillText('“', cx - 300, y)
  ctx.font = `28px ${FONT}`
  ctx.fillStyle = '#d4d4d8'
  const excerpt = card.soul_excerpt || (en ? 'This resident\'s soul is still being written…' : '这位居民的灵魂还在书写中……')
  for (const line of wrapText(ctx, excerpt, 620, 5)) {
    ctx.fillText(line, cx, y)
    y += 46
  }
  y += 26

  // ⑦ 最深回响（最高 importance reflection，可能为 null）
  if (card.signature_reflection) {
    ctx.font = `24px ${FONT}`
    ctx.fillStyle = 'rgba(161,161,170,0.95)'
    for (const line of wrapText(ctx, `「${card.signature_reflection}」`, 600, 3)) {
      ctx.fillText(line, cx, y)
      y += 38
    }
    ctx.font = `22px ${FONT}`
    ctx.fillStyle = 'rgba(113,113,122,0.9)'
    ctx.fillText(en ? '— DEEPEST ECHO' : '—— 印象最深的回响', cx, y + 8)
  }

  // ⑧ 对话数徽章（固定在落款上方，避让正文）
  const convLabel = en
    ? `💬 ${card.total_conversations} conversations shared`
    : `💬 已陪伴对话 ${card.total_conversations} 次`
  ctx.font = `600 26px ${FONT}`
  const cw = ctx.measureText(convLabel).width + 56
  roundedRectPath(ctx, cx - cw / 2, 1010, cw, 52, 26)
  ctx.fillStyle = 'rgba(255,255,255,0.06)'
  ctx.fill()
  ctx.strokeStyle = 'rgba(255,255,255,0.14)'
  ctx.lineWidth = 1.5
  ctx.stroke()
  ctx.fillStyle = '#e4e4e7'
  ctx.fillText(convLabel, cx, 1046)

  // ⑨ 落款
  ctx.strokeStyle = 'rgba(255,255,255,0.10)'
  ctx.beginPath()
  ctx.moveTo(180, 1100)
  ctx.lineTo(W - 180, 1100)
  ctx.stroke()
  ctx.font = `700 30px ${FONT}`
  ctx.fillStyle = '#e94560'
  ctx.fillText('S I M V E R S E   W O R L D', cx, 1148)
  ctx.font = `20px ${FONT}`
  ctx.fillStyle = 'rgba(113,113,122,0.9)'
  ctx.fillText(en ? `@${card.slug} · RESIDENT SOUL PROFILE` : `@${card.slug} · 灵魂居民档案`, cx, 1180)
}

interface SoulCardProps {
  slug: string
  onClose: () => void
}

export function SoulCard({ slug, onClose }: SoulCardProps) {
  const en = useLocale((state) => state.locale === 'en')
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [card, setCard] = useState<ResidentCardData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)

  // 拉取公开卡片数据
  useEffect(() => {
    let cancelled = false
    getResidentCard(slug)
      .then((c) => { if (!cancelled) setCard(c) })
      .catch(() => { if (!cancelled) setError(en ? 'Could not load card data. Try again shortly.' : '卡片数据加载失败，请稍后重试') })
    return () => { cancelled = true }
  }, [slug, en])

  // 数据就绪后绘制；肖像加载失败静默降级为首字母头像
  useEffect(() => {
    if (!card) return
    let cancelled = false
    void (async () => {
      let portrait: HTMLImageElement | null = null
      if (card.portrait_url) {
        try {
          portrait = await loadPortrait(resolvePortraitUrl(card.portrait_url))
        } catch {
          portrait = null // taint/404 兜底：纯文字卡
        }
      }
      const canvas = canvasRef.current
      if (cancelled || !canvas) return
      drawCard(canvas, card, portrait, en)
      setReady(true)
    })()
    return () => { cancelled = true }
  }, [card, en])

  const download = () => {
    const canvas = canvasRef.current
    if (!canvas || !ready) return
    try {
      canvas.toBlob((blob) => {
        if (!blob) return
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `soul-card-${slug}.png`
        a.click()
        URL.revokeObjectURL(url)
      }, 'image/png')
    } catch {
      // SecurityError（canvas 被 taint）——crossOrigin 兜底后理论上不可达
      setError(en ? 'Image export failed because the portrait blocks cross-origin access.' : '图片导出失败：肖像跨域受限')
    }
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000,
        background: 'rgba(0,0,0,0.65)', display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: '#18181b', border: '1px solid #27272a', borderRadius: 12,
          padding: 20, width: 420, maxWidth: '92vw', maxHeight: '92vh', overflowY: 'auto',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
          <h3 style={{ fontSize: 16, fontWeight: 700, margin: 0 }}>{en ? 'Soul Card' : '灵魂卡片'}</h3>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: 18, cursor: 'pointer', padding: 4 }}
          >
            x
          </button>
        </div>

        {error ? (
          <div style={{
            background: 'rgba(233,69,96,0.12)', border: '1px solid rgba(233,69,96,0.3)',
            borderRadius: 6, padding: '10px 12px', fontSize: 13, color: '#e94560', marginBottom: 14,
          }}>
            {error}
          </div>
        ) : (
          <div style={{ position: 'relative', marginBottom: 14 }}>
            <canvas
              ref={canvasRef}
              width={W}
              height={H}
              style={{ display: 'block', width: '100%', borderRadius: 10, border: '1px solid #27272a' }}
            />
            {!ready && (
              <div style={{
                position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: 'var(--text-muted)', fontSize: 13,
              }}>
                {en ? 'Rendering card…' : '卡片绘制中…'}
              </div>
            )}
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <button
            onClick={onClose}
            style={{ background: '#27272a', color: 'white', border: 'none', padding: '8px 18px', borderRadius: 'var(--radius)', fontSize: 13, cursor: 'pointer' }}
          >
            {en ? 'Close' : '关闭'}
          </button>
          <button
            onClick={download}
            disabled={!ready}
            style={{
              background: ready ? '#e94560' : '#3f3f46', color: 'white', border: 'none',
              padding: '8px 18px', borderRadius: 'var(--radius)', fontSize: 13, fontWeight: 600,
              cursor: ready ? 'pointer' : 'default', opacity: ready ? 1 : 0.6,
            }}
          >
            {en ? 'Download image' : '下载图片'}
          </button>
        </div>
      </div>
    </div>
  )
}
