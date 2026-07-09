import { useEffect, useRef, useState } from 'react'
import { TopNav } from '../components/TopNav'
import { getRelationshipGraph } from '../services/api'

interface Sim { slug: string; name: string; x: number; y: number; vx: number; vy: number }

export function GraphPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [empty, setEmpty] = useState(false)

  useEffect(() => {
    let raf = 0
    let stop = false
    getRelationshipGraph(0.3).then((g) => {
      if (g.nodes.length === 0 || g.edges.length === 0) { setEmpty(true); return }
      const canvas = canvasRef.current
      if (!canvas) return
      const ctx = canvas.getContext('2d')!
      const W = canvas.width = canvas.clientWidth
      const H = canvas.height = canvas.clientHeight
      const sims: Record<string, Sim> = {}
      g.nodes.forEach((n, i) => {
        const ang = (i / g.nodes.length) * Math.PI * 2
        sims[n.slug] = { slug: n.slug, name: n.name, x: W / 2 + Math.cos(ang) * 160, y: H / 2 + Math.sin(ang) * 160, vx: 0, vy: 0 }
      })
      const tick = () => {
        if (stop) return
        const arr = Object.values(sims)
        for (const a of arr) {
          for (const b of arr) {
            if (a === b) continue
            const dx = a.x - b.x, dy = a.y - b.y
            const d2 = dx * dx + dy * dy || 0.01
            const rep = 2200 / d2
            a.vx += dx * rep * 0.0016; a.vy += dy * rep * 0.0016
          }
          a.vx += (W / 2 - a.x) * 0.002; a.vy += (H / 2 - a.y) * 0.002
        }
        for (const e of g.edges) {
          const a = sims[e.a], b = sims[e.b]
          if (!a || !b) continue
          const dx = b.x - a.x, dy = b.y - a.y
          const d = Math.hypot(dx, dy) || 0.01
          const f = (d - 120) * 0.01
          a.vx += dx / d * f; a.vy += dy / d * f
          b.vx -= dx / d * f; b.vy -= dy / d * f
        }
        for (const a of arr) { a.vx *= 0.85; a.vy *= 0.85; a.x += a.vx; a.y += a.vy }

        ctx.clearRect(0, 0, W, H)
        ctx.strokeStyle = 'rgba(148,163,184,0.35)'
        for (const e of g.edges) {
          const a = sims[e.a], b = sims[e.b]
          if (!a || !b) continue
          ctx.lineWidth = 1 + e.strength * 3
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke()
        }
        for (const a of arr) {
          ctx.fillStyle = a.slug === '__you__' ? '#0ea5e9' : '#e94560'
          ctx.beginPath(); ctx.arc(a.x, a.y, 8, 0, Math.PI * 2); ctx.fill()
          ctx.fillStyle = '#e2e8f0'; ctx.font = '11px sans-serif'; ctx.textAlign = 'center'
          ctx.fillText(a.name, a.x, a.y - 12)
        }
        raf = requestAnimationFrame(tick)
      }
      tick()
    }).catch(() => setEmpty(true))
    return () => { stop = true; cancelAnimationFrame(raf) }
  }, [])

  return (
    <>
      <TopNav />
      <div style={{ marginTop: 'var(--nav-height)', height: 'calc(100vh - var(--nav-height))', position: 'relative' }}>
        {empty ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)' }}>
            还没有足够的关系可以画成图谱，多和居民互动看看吧。
          </div>
        ) : (
          <canvas ref={canvasRef} style={{ width: '100%', height: '100%', display: 'block' }} />
        )}
      </div>
    </>
  )
}
