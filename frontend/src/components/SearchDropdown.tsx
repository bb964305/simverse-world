import { useState, useRef, useEffect, useId } from 'react'
import { bridge } from '../game/phaserBridge'
import { useLocale } from '../services/locale'
import { localizeDynamicText } from '../services/worldLocalization'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'

interface SearchResult {
  id: string; slug: string; name: string; district: string; status: string;
  heat: number; tile_x: number; tile_y: number; meta_json: { role?: string } | null;
}

const DISTRICT_NAMES: Record<string, readonly [string, string]> = {
  engineering: ['Engineering District', '工程街区'], product: ['Product District', '产品街区'], academy: ['Academy District', '学院区'], free: ['Free District', '自由区'],
}

export function SearchDropdown() {
  const locale = useLocale((state) => state.locale)
  const isEn = locale === 'en'
  const listboxId = useId()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [loading, setLoading] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const search = async (q: string) => {
    if (!q.trim()) { setResults([]); return }
    setLoading(true)
    try {
      const resp = await fetch(`${API}/search?q=${encodeURIComponent(q)}&limit=8`)
      if (resp.ok) setResults(await resp.json())
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }

  const handleInput = (value: string) => {
    setQuery(value)
    setOpen(true)
    setActiveIndex(-1)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => void search(value), 300)
  }

  const handleSelect = (r: SearchResult) => {
    // Pan camera to resident position via bridge
    bridge.emit('camera:pan', { tile_x: r.tile_x, tile_y: r.tile_y, slug: r.slug })
    setOpen(false)
    setQuery('')
    setResults([])
    setActiveIndex(-1)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    e.stopPropagation()
    if (e.key === 'Escape') {
      setOpen(false)
      return
    }
    if (e.key === 'ArrowDown' && results.length > 0) {
      e.preventDefault()
      setActiveIndex((current) => Math.min(results.length - 1, current + 1))
      return
    }
    if (e.key === 'ArrowUp' && results.length > 0) {
      e.preventDefault()
      setActiveIndex((current) => Math.max(0, current - 1))
      return
    }
    if (e.key === 'Enter' && activeIndex >= 0 && results[activeIndex]) {
      e.preventDefault()
      handleSelect(results[activeIndex])
    }
  }

  return (
    <div ref={containerRef} style={{ position: 'relative' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '5px 10px' }}>
        <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>🔍</span>
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => handleInput(e.target.value)}
          onFocus={() => { setOpen(true); if (query) void search(query) }}
          onKeyDown={handleKeyDown}
          placeholder={isEn ? 'Search residents…' : '搜索居民…'}
          role="combobox"
          aria-label={isEn ? 'Search residents' : '搜索居民'}
          aria-expanded={open}
          aria-controls={listboxId}
          aria-activedescendant={activeIndex >= 0 ? `${listboxId}-${results[activeIndex]?.id}` : undefined}
          style={{ background: 'none', border: 'none', color: 'var(--text-primary)', fontSize: 13, outline: 'none', width: 140 }}
        />
      </div>

      {open && (query.trim() || results.length > 0) && (
        <div id={listboxId} role="listbox" style={{
          position: 'absolute', top: '100%', left: 0, right: 0, marginTop: 4,
          background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 8,
          boxShadow: '0 8px 24px rgba(0,0,0,0.3)', zIndex: 100, overflow: 'hidden', minWidth: 240,
        }}>
          {loading && <div style={{ padding: '10px 14px', color: 'var(--text-muted)', fontSize: 12 }}>{isEn ? 'Searching…' : '搜索中…'}</div>}
          {!loading && results.length === 0 && query.trim() && (
            <div style={{ padding: '10px 14px', color: 'var(--text-muted)', fontSize: 12 }}>{isEn ? 'No residents found' : '没有找到居民'}</div>
          )}
          {results.map((r, index) => (
            <button key={r.id} id={`${listboxId}-${r.id}`} role="option" aria-selected={activeIndex === index}
              onClick={() => handleSelect(r)} style={{
              width: '100%', padding: '10px 14px', cursor: 'pointer', display: 'flex', gap: 10, alignItems: 'center',
              border: 'none', borderBottom: '1px solid var(--border)', textAlign: 'left', color: 'var(--text-primary)',
              background: activeIndex === index ? 'var(--bg-input)' : 'transparent',
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-input)')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}>
              <div style={{ width: 32, height: 32, background: 'var(--bg-input)', borderRadius: 6, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 18, flexShrink: 0 }}>🧑‍💻</div>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{r.name}</div>
                <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                  {localizeDynamicText(r.meta_json?.role, locale)} · {DISTRICT_NAMES[r.district]?.[isEn ? 0 : 1] ?? r.district}
                </div>
              </div>
              <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>🔥{r.heat}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
