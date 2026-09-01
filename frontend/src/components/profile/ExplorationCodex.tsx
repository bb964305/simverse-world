import { useEffect, useMemo, useState } from 'react'
import { getExplorationCodex, type CodexLocation } from '../../services/api'
import { useLocale } from '../../services/locale'
import { localizeLocationLore, localizeLocationName } from '../../services/worldLocalization'

// Minimap silhouette: location rects in tile space projected into a fixed
// SVG viewport. Unvisited = dark silhouette, visited = lit, secrets = ⭐.
const MAP_W = 640
const MAP_H = 300
const MAP_PAD = 10

export function ExplorationCodex() {
  const locale = useLocale((state) => state.locale)
  const en = locale === 'en'
  const [data, setData] = useState<{ total: number; visited: number; locations: CodexLocation[] } | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => {
    getExplorationCodex()
      .then(setData)
      .catch(() => setError(en ? 'Could not load the exploration codex. Try again shortly.' : '图鉴加载失败，请稍后重试'))
  }, [en])

  // Project tile-space bounds into the SVG viewport.
  const projected = useMemo(() => {
    if (!data) return []
    const xs = data.locations.flatMap((l) => [l.bounds[0], l.bounds[2]])
    const ys = data.locations.flatMap((l) => [l.bounds[1], l.bounds[3]])
    const minX = Math.min(...xs)
    const maxX = Math.max(...xs)
    const minY = Math.min(...ys)
    const maxY = Math.max(...ys)
    const sx = (MAP_W - MAP_PAD * 2) / Math.max(1, maxX - minX)
    const sy = (MAP_H - MAP_PAD * 2) / Math.max(1, maxY - minY)
    return data.locations.map((l) => ({
      loc: l,
      x: MAP_PAD + (l.bounds[0] - minX) * sx,
      y: MAP_PAD + (l.bounds[1] - minY) * sy,
      w: Math.max(6, (l.bounds[2] - l.bounds[0]) * sx),
      h: Math.max(6, (l.bounds[3] - l.bounds[1]) * sy),
    }))
  }, [data])

  if (error) return <div style={{ color: 'var(--accent-red)', fontSize: 13 }}>{error}</div>
  if (!data) return <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{en ? 'Loading…' : '加载中…'}</div>

  const secretsFound = data.locations.filter((l) => l.secret_found).length
  const secretsTotal = data.locations.filter((l) => l.has_secret).length
  const current = data.locations.find((l) => l.location_id === selected) ?? null

  return (
    <div style={{ maxWidth: 720 }}>
      <h1 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4, color: 'var(--text-primary)' }}>{en ? 'Exploration codex' : '探索图鉴'}</h1>
      <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 16 }}>
        {en ? 'Explored ' : '已探索 '}<span style={{ color: 'var(--accent-green)', fontWeight: 700 }}>{data.visited}</span> / {data.total}{en ? ' locations' : ' 个地点'}
        {secretsTotal > 0 && (
          <span style={{ marginLeft: 12 }}>⭐ {en ? 'Secrets' : '彩蛋'} {secretsFound} / {secretsTotal}</span>
        )}
      </div>

      {/* Silhouette minimap */}
      <div style={{
        background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10,
        padding: 12, marginBottom: 16,
      }}>
        <svg viewBox={`0 0 ${MAP_W} ${MAP_H}`} style={{ width: '100%', height: 'auto', display: 'block' }}>
          <rect x={0} y={0} width={MAP_W} height={MAP_H} fill="var(--bg-input)" rx={8} />
          {projected.map(({ loc, x, y, w, h }) => (
            <g key={loc.location_id} onClick={() => setSelected(loc.location_id)} style={{ cursor: 'pointer' }}>
              <rect
                x={x} y={y} width={w} height={h} rx={3}
                fill={loc.visited ? '#53d76933' : '#00000055'}
                stroke={selected === loc.location_id ? 'var(--accent-red)' : loc.visited ? '#53d769' : 'var(--border)'}
                strokeWidth={selected === loc.location_id ? 2 : 1}
              />
              {loc.visited && (
                <text x={x + w / 2} y={y + h / 2 + 3} textAnchor="middle" fontSize="9"
                  fill="var(--text-secondary)" style={{ pointerEvents: 'none' }}>
                  {localizeLocationName(loc.location_id, loc.name, locale)}
                </text>
              )}
              {loc.secret_found && (
                <text x={x + w - 6} y={y + 10} fontSize="9" style={{ pointerEvents: 'none' }}>⭐</text>
              )}
            </g>
          ))}
        </svg>
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
          {en ? 'Dark silhouettes are unexplored locations. Walk close to reveal them.' : '灰色剪影是尚未踏足的地点 — 在地图上走近即可点亮。'}
        </div>
      </div>

      {/* Selected lore card */}
      {current && (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10,
          padding: '14px 18px', marginBottom: 16,
        }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-primary)' }}>
              {current.visited ? '📖' : '🌫️'} {current.visited ? localizeLocationName(current.location_id, current.name, locale) : '???'}
            </span>
            {current.visited && (
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{en ? `Visited ${current.visit_count} times` : `到访 ${current.visit_count} 次`}</span>
            )}
            {current.secret_found && <span style={{ fontSize: 11 }}>⭐ {en ? 'Secret found' : '已发现彩蛋'}</span>}
            {current.has_secret && !current.secret_found && current.visited && (
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{en ? 'Something may be hidden here…' : '这里似乎藏着什么…'}</span>
            )}
          </div>
          <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.7, marginTop: 8 }}>
            {current.visited
              ? (localizeLocationLore(current.location_id, current.lore, locale) ?? (en ? 'No lore has been recorded for this place.' : '这个地方还没有记载。'))
              : (en ? 'Unexplored—go see it for yourself.' : '尚未探索 — 亲自去看看吧。')}
          </div>
        </div>
      )}

      {/* Location grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 8 }}>
        {data.locations.map((l) => (
          <button key={l.location_id} onClick={() => setSelected(l.location_id)} style={{
            textAlign: 'left', padding: '10px 12px', borderRadius: 8, cursor: 'pointer',
            background: selected === l.location_id ? '#e9456012' : 'var(--bg-card)',
            border: selected === l.location_id ? '1px solid var(--accent-red)' : '1px solid var(--border)',
          }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: l.visited ? 'var(--text-primary)' : 'var(--text-muted)' }}>
              {l.visited ? localizeLocationName(l.location_id, l.name, locale) : '???'}
              {l.secret_found && ' ⭐'}
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
              {l.visited ? (en ? `${l.visit_count} visits` : `到访 ${l.visit_count} 次`) : (en ? 'Unexplored' : '未探索')}
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}
