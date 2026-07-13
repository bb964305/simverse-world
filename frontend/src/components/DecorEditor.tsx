import { useEffect, useState } from 'react'
import { useGameStore } from '../stores/gameStore'
import { bridge } from '../game/phaserBridge'
import {
  checkOnboarding, getResidents, getHomeDecor, putHomeDecor, getShopInventory,
  decorEmoji, DECOR_MAX_ITEMS, HOUSING_BOUNDS, HOUSING_NAMES,
} from '../services/api'
import type { DecorItem, ShopInventoryRow } from '../services/api'

interface MyResident {
  slug: string
  home: string | null
}

/**
 * B3 home decor editor. A "装修" button appears while the player stands inside
 * their own home bbox (players without a home get a claim button instead —
 * the first empty save lazily assigns one server-side). Editing is a tile
 * grid: pick an owned decor item, click a cell to place, click a placed item
 * to remove; save does a full-replace PUT. Drag-drop/undo intentionally
 * simplified to click-place (recorded deviation).
 */
export function DecorEditor() {
  const token = useGameStore((s) => s.token)
  const tileX = useGameStore((s) => s.playerTileX)
  const tileY = useGameStore((s) => s.playerTileY)

  const [mine, setMine] = useState<MyResident | null>(null)
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<DecorItem[]>([])
  const [bounds, setBounds] = useState<[number, number, number, number] | null>(null)
  const [inventory, setInventory] = useState<ShopInventoryRow[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!token) return
    let cancelled = false
    Promise.all([checkOnboarding(token), getResidents()])
      .then(([check, residents]) => {
        if (cancelled || !check.player_resident_id) return
        const r = residents.find((x) => x.id === check.player_resident_id)
        if (r) setMine({ slug: r.slug, home: r.home_location_id ?? null })
      })
      .catch(() => { /* no player resident yet — button just stays hidden */ })
    return () => { cancelled = true }
  }, [token])

  const homeBounds = mine?.home ? HOUSING_BOUNDS[mine.home] : undefined
  const insideHome = !!homeBounds
    && tileX >= homeBounds[0] && tileX <= homeBounds[2]
    && tileY >= homeBounds[1] && tileY <= homeBounds[3]

  const openEditor = async () => {
    if (!mine || busy) return
    setBusy(true)
    setError(null)
    try {
      let resp = await getHomeDecor(mine.slug)
      if (!resp.home_location_id) {
        // No home yet: an empty full-replace claims a housing slot.
        resp = await putHomeDecor(mine.slug, [])
        setMine({ slug: mine.slug, home: resp.home_location_id })
        const b = resp.home_location_id ? HOUSING_BOUNDS[resp.home_location_id] : undefined
        if (b) {
          // Take the player to their new home (camera:pan also moves the player).
          bridge.emit('camera:pan', {
            tile_x: Math.round((b[0] + b[2]) / 2),
            tile_y: Math.round((b[1] + b[3]) / 2),
          })
        }
      }
      const inv = await getShopInventory()
      setInventory(inv.inventory.filter((row) => row.kind === 'decor'))
      setItems(resp.items)
      setBounds(resp.bounds)
      setOpen(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : '装修数据加载失败')
    } finally {
      setBusy(false)
    }
  }

  const ownedOf = (code: string) => inventory.find((r) => r.item_code === code)?.qty ?? 0
  const placedOf = (code: string) => items.filter((it) => it.item_code === code).length

  const cellClick = (x: number, y: number) => {
    const idx = items.findIndex((it) => it.x === x && it.y === y)
    if (idx >= 0) {
      setItems(items.filter((_, i) => i !== idx))
      return
    }
    if (!selected) {
      setError('先在右侧选择一件家具')
      return
    }
    if (items.length >= DECOR_MAX_ITEMS) {
      setError(`最多摆放 ${DECOR_MAX_ITEMS} 件`)
      return
    }
    if (ownedOf(selected) - placedOf(selected) <= 0) {
      setError('该物品数量不足，先去杂货铺购买')
      return
    }
    setError(null)
    setItems([...items, { item_code: selected, x, y, rot: 0 }])
  }

  const save = async () => {
    if (!mine || busy) return
    setBusy(true)
    setError(null)
    try {
      await putHomeDecor(mine.slug, items)
      setOpen(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  if (!mine) return null

  const showButton = !open && (insideHome || !mine.home)
  const buttonLabel = mine.home ? '🛋️ 装修' : '🏠 认领住房并装修'

  return (
    <>
      {showButton && (
        <button
          onClick={openEditor}
          disabled={busy}
          style={{
            position: 'fixed', left: 20, bottom: 20, zIndex: 290,
            background: 'var(--bg-card)', color: 'var(--text-primary)',
            border: '1px solid #f59e0b88', borderRadius: 10,
            padding: '10px 16px', fontSize: 14, fontWeight: 600,
            cursor: busy ? 'wait' : 'pointer', boxShadow: '0 4px 16px rgba(0,0,0,0.35)',
          }}
        >
          {busy ? '加载中…' : buttonLabel}
        </button>
      )}
      {!open && error && (
        <div style={{
          position: 'fixed', left: 20, bottom: 70, zIndex: 290, maxWidth: 280,
          background: '#7f1d1dcc', color: '#fecaca', borderRadius: 8,
          padding: '8px 12px', fontSize: 12,
        }}>
          {error}
        </div>
      )}
      {open && bounds && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 320,
          background: 'rgba(0,0,0,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <div style={{
            background: 'var(--bg-card)', border: '1px solid #f59e0b55',
            borderRadius: 14, padding: 18, maxHeight: '90vh', overflow: 'auto',
            boxShadow: '0 12px 48px rgba(0,0,0,0.5)',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
              <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-primary)' }}>
                🛋️ 装修{mine.home ? ` · ${HOUSING_NAMES[mine.home] ?? mine.home}` : ''}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                {items.length}/{DECOR_MAX_ITEMS} 件
              </div>
            </div>
            <div style={{ display: 'flex', gap: 14 }}>
              <div>
                <div style={{
                  display: 'grid',
                  gridTemplateColumns: `repeat(${bounds[2] - bounds[0] + 1}, 26px)`,
                  gap: 2,
                }}>
                  {Array.from({ length: (bounds[3] - bounds[1] + 1) * (bounds[2] - bounds[0] + 1) }, (_, i) => {
                    const w = bounds[2] - bounds[0] + 1
                    const x = i % w
                    const y = Math.floor(i / w)
                    const placed = items.find((it) => it.x === x && it.y === y)
                    return (
                      <button
                        key={`${x}-${y}`}
                        onClick={() => cellClick(x, y)}
                        title={placed ? `移除 ${placed.item_code}` : `(${x},${y})`}
                        style={{
                          width: 26, height: 26, padding: 0, fontSize: 15, lineHeight: '26px',
                          background: placed ? '#f59e0b22' : 'var(--bg-input)',
                          border: placed ? '1px solid #f59e0b88' : '1px solid #ffffff14',
                          borderRadius: 4, cursor: 'pointer',
                        }}
                      >
                        {placed ? decorEmoji(placed.item_code) : ''}
                      </button>
                    )
                  })}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
                  点击空格摆放所选家具，点击已摆放的家具移除
                </div>
              </div>
              <div style={{ width: 170, display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)' }}>我的家具</div>
                {inventory.length === 0 && (
                  <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                    还没有装饰品，去杂货铺（🛒）购买 decor 类商品吧
                  </div>
                )}
                {inventory.map((row) => {
                  const remaining = row.qty - placedOf(row.item_code)
                  return (
                    <button
                      key={row.item_code}
                      onClick={() => setSelected(row.item_code)}
                      style={{
                        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                        padding: '6px 10px', borderRadius: 8, fontSize: 13, cursor: 'pointer',
                        background: selected === row.item_code ? '#f59e0b22' : 'var(--bg-input)',
                        border: selected === row.item_code ? '1px solid #f59e0b' : '1px solid #ffffff14',
                        color: 'var(--text-primary)',
                      }}
                    >
                      <span>{decorEmoji(row.item_code)} {row.name}</span>
                      <span style={{ fontSize: 11, color: remaining > 0 ? 'var(--text-muted)' : '#f87171' }}>
                        剩 {remaining}
                      </span>
                    </button>
                  )
                })}
                <div style={{ flex: 1 }} />
                {error && (
                  <div style={{ fontSize: 12, color: '#f87171' }}>{error}</div>
                )}
                <button
                  onClick={save}
                  disabled={busy}
                  style={{
                    background: 'var(--accent-blue)', color: 'white', border: 'none',
                    padding: '9px', borderRadius: 8, fontSize: 13, fontWeight: 600,
                    cursor: busy ? 'wait' : 'pointer',
                  }}
                >
                  {busy ? '保存中…' : '保存'}
                </button>
                <button
                  onClick={() => { setOpen(false); setError(null) }}
                  style={{
                    background: 'var(--bg-input)', color: 'var(--text-muted)', border: 'none',
                    padding: '8px', borderRadius: 8, fontSize: 13, cursor: 'pointer',
                  }}
                >
                  取消
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
