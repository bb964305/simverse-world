import { useEffect, useState, useCallback } from 'react'
import { useGameStore } from '../stores/gameStore'
import {
  getShopCatalog,
  getShopInventory,
  purchaseShopItem,
  getResidents,
  getMe,
  type ShopItemData,
  type ShopInventoryRow,
  type ResidentListItem,
} from '../services/api'

// Backend detail strings → 中文 (shop_service/shop_effects ShopError messages).
const DETAIL_ZH: [string, string][] = [
  ['insufficient balance', '余额不足'],
  ['item not found or inactive', '商品不存在或已下架'],
  ['resident_slug and new_name are required', '请选择居民并填写新名字'],
  ['name too long', '名字太长了'],
  ['name contains disallowed words', '名字包含敏感词'],
  ['resident not found', '居民不存在'],
  ['you can only rename your own resident', '只能给自己的居民改名'],
  ['resident_slug is required', '请选择一位居民'],
]

function errZh(e: unknown): string {
  const msg = e instanceof Error ? e.message : ''
  for (const [en, zh] of DETAIL_ZH) if (msg.includes(en)) return zh
  return '购买失败，请稍后重试'
}

const KIND_LABEL: Record<string, string> = {
  gift: '礼物', consumable: '道具', decor: '装饰',
}

interface Props {
  onClose: () => void
}

export function ShopModal({ onClose }: Props) {
  const user = useGameStore((s) => s.user)
  const updateBalance = useGameStore((s) => s.updateBalance)
  const [tab, setTab] = useState<'catalog' | 'inventory'>('catalog')
  const [items, setItems] = useState<ShopItemData[]>([])
  const [inventory, setInventory] = useState<ShopInventoryRow[]>([])
  const [residents, setResidents] = useState<ResidentListItem[]>([])
  const [loading, setLoading] = useState(true)
  // Purchase flow state: the item being configured (gift target / rename form).
  const [active, setActive] = useState<ShopItemData | null>(null)
  const [targetSlug, setTargetSlug] = useState('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    Promise.all([getShopCatalog(), getResidents()])
      .then(([cat, res]) => {
        // tip 商品由公告板打赏消费，不进商店货架。
        setItems(cat.items.filter((i) => i.kind !== 'tip'))
        setResidents(res)
      })
      .catch(() => setItems([]))
      .finally(() => setLoading(false))
  }, [])

  const loadInventory = useCallback(() => {
    getShopInventory().then((r) => setInventory(r.inventory)).catch(() => setInventory([]))
  }, [])

  useEffect(() => { if (tab === 'inventory') loadInventory() }, [tab, loadInventory])

  // Gift targets are town NPCs (player residents use the p- slug prefix);
  // rename/portrait list everyone — ownership is enforced server-side.
  const pickerResidents = active?.kind === 'gift'
    ? residents.filter((r) => !r.slug.startsWith('p-'))
    : residents
  const needsTarget = (item: ShopItemData) => item.kind === 'gift' || item.code === 'rename_card' || item.code === 'portrait_redraw'

  const startPurchase = (item: ShopItemData) => {
    setNotice(null)
    if (!needsTarget(item)) { void doPurchase(item, undefined); return }
    setActive(item)
    setTargetSlug('')
    setNewName('')
  }

  const doPurchase = async (item: ShopItemData, context?: Record<string, unknown>) => {
    if (busy) return
    setBusy(true)
    setNotice(null)
    try {
      await purchaseShopItem(item.code, 1, context)
      setNotice({ ok: true, text: `已购买 ${item.name}（-${item.price_sc} 🪙）` })
      setActive(null)
      // Purchases charge without a coin_update WS frame — sync the balance.
      getMe().then((me) => updateBalance(me.soul_coin_balance)).catch(() => {})
      if (tab === 'inventory') loadInventory()
    } catch (e) {
      setNotice({ ok: false, text: errZh(e) })
    } finally {
      setBusy(false)
    }
  }

  const submitActive = () => {
    if (!active) return
    if (!targetSlug) { setNotice({ ok: false, text: '请选择一位居民' }); return }
    const ctx: Record<string, unknown> = { resident_slug: targetSlug }
    if (active.code === 'rename_card') {
      if (!newName.trim()) { setNotice({ ok: false, text: '请输入新名字' }); return }
      ctx.new_name = newName.trim()
    }
    void doPurchase(active, ctx)
  }

  return (
    <div onClick={onClose} className="game-modal-backdrop">
      <div
        onClick={(e) => e.stopPropagation()}
        className="game-modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="shop-dialog-title"
        style={{ width: 'min(640px, calc(100vw - 32px))', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
      >
        <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', gap: 12, alignItems: 'center' }}>
          <span id="shop-dialog-title" style={{ fontWeight: 700, fontSize: 15 }}>🛒 杂货铺</span>
          <div style={{ display: 'flex', gap: 4, marginLeft: 8 }}>
            {(['catalog', 'inventory'] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)} style={{
                background: tab === t ? 'var(--accent-red)' : 'transparent', border: 'none',
                color: tab === t ? 'white' : 'var(--text-muted)', padding: '4px 12px',
                borderRadius: 6, fontSize: 12, cursor: 'pointer',
              }}>{t === 'catalog' ? '货架' : '我的库存'}</button>
            ))}
          </div>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-secondary)' }}>
            🪙 {user?.soul_coin_balance ?? 0}
          </span>
          <button autoFocus onClick={onClose} className="game-dialog-close" aria-label="关闭杂货铺">×</button>
        </div>

        {notice && (
          <div style={{
            padding: '8px 20px', fontSize: 12,
            color: notice.ok ? 'var(--accent-green)' : 'var(--accent-red)',
            borderBottom: '1px solid var(--border)',
          }}>{notice.text}</div>
        )}

        <div style={{ overflowY: 'auto', padding: 16 }}>
          {loading ? (
            <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: 24 }}>加载中…</div>
          ) : tab === 'catalog' ? (
            items.length === 0 ? (
              <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: 24 }}>货架空空如也。</div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))', gap: 10 }}>
                {items.map((item) => (
                  <div key={item.code} style={{
                    background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 10,
                    padding: '12px 12px 10px', display: 'flex', flexDirection: 'column', gap: 6,
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ fontSize: 22 }}>{item.icon || '📦'}</span>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>{item.name}</div>
                        <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{KIND_LABEL[item.kind] ?? item.kind}</div>
                      </div>
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-secondary)', lineHeight: 1.5, flex: 1 }}>
                      {item.description}
                    </div>
                    <button onClick={() => startPurchase(item)} disabled={busy} style={{
                      padding: '5px 0', borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: 'pointer',
                      background: '#e9456018', border: '1px solid var(--accent-red)', color: 'var(--accent-red)',
                      opacity: busy ? 0.5 : 1,
                    }}>
                      🪙 {item.price_sc} 购买
                    </button>
                  </div>
                ))}
              </div>
            )
          ) : inventory.length === 0 ? (
            <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: 24 }}>
              还没有购买记录 — 去货架逛逛吧。
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {inventory.map((row) => (
                <div key={row.item_code} style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px',
                  background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 8,
                }}>
                  <span style={{ fontSize: 18 }}>{row.icon || '📦'}</span>
                  <span style={{ fontSize: 13, color: 'var(--text-primary)', flex: 1 }}>{row.name}</span>
                  <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>×{row.qty}</span>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>累计 🪙 {row.total_sc}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Target picker: gift → any resident; rename/portrait → own resident */}
        {active && (
          <div style={{ borderTop: '1px solid var(--border)', padding: '12px 20px', display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
              {active.icon} {active.name} — {active.kind === 'gift' ? '送给哪位居民？' : '选择你的居民'}
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <select value={targetSlug} onChange={(e) => setTargetSlug(e.target.value)} style={{
                flex: 1, minWidth: 140, background: 'var(--bg-input)', color: 'var(--text-primary)',
                border: '1px solid var(--border)', borderRadius: 6, padding: '6px 8px', fontSize: 12,
              }}>
                <option value="">选择居民…</option>
                {pickerResidents.map((r) => (
                  <option key={r.slug} value={r.slug}>{r.name}（{r.district}）</option>
                ))}
              </select>
              {active.code === 'rename_card' && (
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="新名字"
                  maxLength={24}
                  style={{
                    flex: 1, minWidth: 120, background: 'var(--bg-input)', color: 'var(--text-primary)',
                    border: '1px solid var(--border)', borderRadius: 6, padding: '6px 8px', fontSize: 12,
                  }}
                />
              )}
              <button onClick={submitActive} disabled={busy} style={{
                padding: '6px 16px', borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: 'pointer',
                background: 'var(--accent-red)', border: 'none', color: 'white', opacity: busy ? 0.5 : 1,
              }}>确认购买</button>
              <button onClick={() => setActive(null)} style={{
                padding: '6px 10px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
                background: 'transparent', border: '1px solid var(--border)', color: 'var(--text-muted)',
              }}>取消</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
