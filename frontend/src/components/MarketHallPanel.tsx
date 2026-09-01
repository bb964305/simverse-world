import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { bridge } from '../game/phaserBridge'
import {
  getCurrentMarket,
  getMe,
  purchaseMarketOffer,
  type CurrentMarket,
  type MarketOffer,
} from '../services/api'
import { onWSMessage } from '../services/ws'
import { useGameStore } from '../stores/gameStore'
import { useLocale, type Locale } from '../services/locale'

const ACCENT = '#d97706'

function requestKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `market:${crypto.randomUUID()}`
  }
  return `market:${Date.now()}:${Math.random().toString(36).slice(2)}`
}

function phaseLabel(phase: CurrentMarket['phase'], locale: Locale): string {
  if (!phase) return locale === 'en' ? 'CLOSED' : '闭市'
  const labels: Record<NonNullable<CurrentMarket['phase']>, readonly [string, string]> = {
    waiting: ['WAITING', '候场中'], inbound: ['ARRIVING', '进镇中'], trading: ['OPEN', '开市中'], outbound: ['DEPARTING', '离镇中'],
  }
  return labels[phase][locale === 'en' ? 0 : 1]
}

function errorText(error: unknown, locale: Locale): string {
  const message = error instanceof Error ? error.message : String(error)
  const en = locale === 'en'
  if (message.includes('402') || message.includes('余额不足')) return en ? 'Insufficient SC balance' : 'SC 余额不足'
  if (message.includes('售罄')) return en ? 'Just sold out. Refresh the catalog.' : '商品刚刚售罄，请刷新货架'
  if (message.includes('限购')) return en ? 'Already purchased during this visit' : '本次到访已经购买过该项目'
  if (message.includes('停止交易')) return en ? 'This market visit has ended' : '本次集市已经结束'
  if (message.includes('灰度关闭')) return en ? 'Player trading is not enabled yet' : '玩家交易仍在灰度关闭中'
  return en ? 'Trade not completed. Refresh and try again.' : '交易未完成，请刷新后重试'
}

function Countdown({ closesAt }: { closesAt: string | null }) {
  const locale = useLocale((state) => state.locale)
  const [seconds, setSeconds] = useState<number | null>(null)
  useEffect(() => {
    if (!closesAt) return
    const closesAtMs = new Date(closesAt).getTime()
    const timer = window.setInterval(
      () => setSeconds(Math.max(0, Math.floor((closesAtMs - Date.now()) / 1000))),
      1000,
    )
    return () => window.clearInterval(timer)
  }, [closesAt])
  if (!closesAt) return null
  if (seconds === null) return <span>{locale === 'en' ? 'Syncing close time' : '结算时间同步中'}</span>
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  return <span style={{ fontVariantNumeric: 'tabular-nums' }}>{locale === 'en' ? 'Closes in' : '剩余'} {hours > 0 ? `${hours}:` : ''}{String(minutes).padStart(2, '0')}:{String(rest).padStart(2, '0')}</span>
}

const OFFER_EN: Record<string, readonly [string, string]> = {
  import_tea: ['Caravan Tea', 'Tea carried in from distant trade routes; becomes a placeable home collectible.'],
  import_trinket: ['Travel Trinket', 'A curious item found deep in the caravan’s cargo; becomes a placeable home collectible.'],
  import_cloth: ['Patterned Cloth', 'Foreign patterned cloth; becomes a placeable home collectible.'],
  market_appraisal: ['Trade Route Appraisal', 'A caravan appraiser authenticates one of your resident works currently for sale.'],
  market_artisan_lantern: ['Artisan Lantern Commission', 'Commission a limited home lantern from a traveling artisan.'],
}

function OfferCard({ offer, busy, onBuy }: {
  offer: MarketOffer
  busy: boolean
  onBuy: (offer: MarketOffer) => void
}) {
  const locale = useLocale((state) => state.locale)
  const isEn = locale === 'en'
  const label = offer.type === 'good' ? (isEn ? 'Imported good' : '进口商品') : offer.type === 'service' ? (isEn ? 'Limited service' : '限时服务') : (isEn ? 'Trade contract' : '商路合同')
  const localized = OFFER_EN[offer.code]
  const reason = offer.purchased ? (isEn ? 'Purchased this visit' : '本次已购')
    : offer.stock <= 0 ? (isEn ? 'Sold out' : '已售罄')
      : !offer.eligible ? (isEn ? 'Requirements not met' : offer.unavailable_reason || '暂不符合条件')
        : null
  return (
    <div style={{
      border: '1px solid var(--border)', borderRadius: 10, background: 'var(--bg-input)',
      padding: 12, display: 'flex', flexDirection: 'column', gap: 7, minHeight: 170,
    }}>
      <div style={{ display: 'flex', gap: 9, alignItems: 'center' }}>
        <span style={{ fontSize: 25 }}>{offer.icon}</span>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 700 }}>{isEn ? (localized?.[0] ?? offer.name) : offer.name}</div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{label}</div>
        </div>
      </div>
      <div style={{ color: 'var(--text-secondary)', fontSize: 11, lineHeight: 1.55, flex: 1 }}>{isEn ? (localized?.[1] ?? offer.description) : offer.description}</div>
      <div style={{ display: 'flex', color: 'var(--text-muted)', fontSize: 10 }}>
        <span>{isEn ? 'Stock' : '余量'} {offer.stock}/{offer.stock_total}</span>
        <span style={{ marginLeft: 'auto' }}>{isEn ? 'Limit' : '每人限购'} {offer.per_user_limit}</span>
      </div>
      <button
        onClick={() => onBuy(offer)}
        disabled={busy || !offer.available}
        style={{
          minHeight: 36, borderRadius: 7, fontSize: 12, fontWeight: 700,
          border: `1px solid ${offer.available ? ACCENT : 'var(--border)'}`,
          color: offer.available ? ACCENT : 'var(--text-muted)',
          background: offer.available ? '#d9770614' : 'transparent',
          cursor: offer.available && !busy ? 'pointer' : 'default',
        }}
      >
        {busy ? (isEn ? 'Trading…' : '交易中…') : reason ?? `🪙 ${offer.price_sc} ${isEn ? 'Buy' : '购买'}`}
      </button>
    </div>
  )
}

export function MarketHallPanel() {
  const locale = useLocale((state) => state.locale)
  const isEn = locale === 'en'
  const [open, setOpen] = useState(false)
  const [market, setMarket] = useState<CurrentMarket | null>(null)
  const [loading, setLoading] = useState(false)
  const [busyCode, setBusyCode] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const updateBalance = useGameStore((state) => state.updateBalance)
  const balance = useGameStore((state) => state.user?.soul_coin_balance ?? 0)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setMarket(await getCurrentMarket())
    } catch {
      setMarket(null)
      setNotice({ ok: false, text: isEn ? 'Market status is temporarily unavailable' : '集市状态暂时不可用' })
    } finally {
      setLoading(false)
    }
  }, [isEn])

  useEffect(() => {
    const unsubOpen = bridge.on('market:open', () => {
      bridge.emit('bulletin:close')
      bridge.emit('experiment:close')
      setOpen(true)
    })
    const unsubClose = bridge.on('market:close', () => setOpen(false))
    return () => { unsubOpen(); unsubClose() }
  }, [])

  useEffect(() => {
    if (!open) return
    setNotice(null)
    void load()
    closeRef.current?.focus()
    const poll = window.setInterval(() => void load(), 5000)
    const keydown = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', keydown)
    return () => {
      window.clearInterval(poll)
      document.removeEventListener('keydown', keydown)
    }
  }, [load, open])

  useEffect(() => onWSMessage((message) => {
    if (!open) return
    if (['caravan_state', 'market_purchase', 'market_player_purchase'].includes(String(message.type))) {
      void load()
    }
  }), [load, open])

  const grouped = useMemo(() => ({
    goods: market?.offers.filter((offer) => offer.type === 'good') ?? [],
    services: market?.offers.filter((offer) => offer.type !== 'good') ?? [],
  }), [market])

  const buy = async (offer: MarketOffer) => {
    if (!market?.visit_id || busyCode) return
    setBusyCode(offer.code)
    setNotice(null)
    try {
      const result = await purchaseMarketOffer(market.visit_id, offer.code, requestKey())
      const name = isEn ? (OFFER_EN[offer.code]?.[0] ?? offer.name) : offer.name
      setNotice({ ok: true, text: isEn ? `Received “${name}” (-${result.total_sc} SC)` : `已获得「${name}」（-${result.total_sc} SC）` })
      const me = await getMe()
      updateBalance(me.soul_coin_balance)
      await load()
    } catch (error) {
      setNotice({ ok: false, text: errorText(error, locale) })
      await load()
    } finally {
      setBusyCode(null)
    }
  }

  if (!open) return null
  return (
    <div className="game-modal-backdrop" onClick={(event) => { if (event.target === event.currentTarget) setOpen(false) }}>
      <section
        className="game-modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="market-dialog-title"
        style={{ width: 'min(760px, calc(100vw - 28px))', maxHeight: 'min(760px, calc(100vh - 40px))', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
      >
        <div style={{ padding: '14px 18px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 12, background: '#d977060b' }}>
          <div>
            <div id="market-dialog-title" style={{ fontSize: 15, fontWeight: 800, color: ACCENT }}>🏬 {isEn ? 'Caravan Market' : '商队集市'}</div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{isEn ? 'Visit-only goods, services, and Lab candidates' : '到访限定商品、服务与实验楼成果候选'}</div>
          </div>
          <div style={{ marginLeft: 'auto', textAlign: 'right', fontSize: 11, color: 'var(--text-secondary)' }}>
            <div>{phaseLabel(market?.phase ?? null, locale)} · 🪙 {balance} SC</div>
            <Countdown closesAt={market?.closes_at ?? null} />
          </div>
          <button ref={closeRef} onClick={() => setOpen(false)} className="game-dialog-close" aria-label={isEn ? 'Close caravan market' : '关闭商队集市'}>✕</button>
        </div>

        <div style={{ overflowY: 'auto', padding: 18 }}>
          {notice && <div style={{ color: notice.ok ? 'var(--accent-green)' : 'var(--accent-red)', fontSize: 12, marginBottom: 12 }}>{notice.text}</div>}
          {loading && !market ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 28 }}>{isEn ? 'Loading caravan catalog…' : '正在查看商队货单…'}</div>
          ) : !market ? null : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
              <div style={{ border: `1px solid ${ACCENT}44`, background: '#d977060a', borderRadius: 9, padding: '10px 12px', fontSize: 12, lineHeight: 1.6 }}>
                <b style={{ color: ACCENT }}>{isEn ? phaseLabel(market.phase, locale) : market.message}</b>
                {!market.enabled && <div style={{ color: 'var(--text-muted)' }}>{isEn ? 'The catalog remains visible. Purchases open automatically during trading once rollout is enabled.' : '货单保持可见；运维开闸后，交易阶段会自动开放购买。'}</div>}
              </div>

              {grouped.goods.length > 0 && <section>
                <h3 style={{ fontSize: 13, margin: '0 0 9px' }}>{isEn ? 'Goods from afar' : '远方货物'}</h3>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 10 }}>
                  {grouped.goods.map((offer) => <OfferCard key={offer.code} offer={offer} busy={busyCode === offer.code} onBuy={buy} />)}
                </div>
              </section>}

              {grouped.services.length > 0 && <section>
                <h3 style={{ fontSize: 13, margin: '0 0 9px' }}>{isEn ? 'Visit-only services' : '本次限定服务'}</h3>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10 }}>
                  {grouped.services.map((offer) => <OfferCard key={offer.code} offer={offer} busy={busyCode === offer.code} onBuy={buy} />)}
                </div>
              </section>}

              <section>
                <h3 style={{ fontSize: 13, margin: '0 0 9px' }}>🧪 {isEn ? 'Lab market candidates' : '实验楼市场候选'}</h3>
                {market.research_candidates.length === 0 ? (
                  <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>{isEn ? 'No approved research candidates yet.' : '暂无通过审核的研究成果候选。'}</div>
                ) : market.research_candidates.map((candidate) => (
                  <div key={candidate.id} style={{ borderLeft: `2px solid ${ACCENT}`, padding: '5px 9px', marginBottom: 7 }}>
                    <div style={{ fontSize: 12, fontWeight: 700 }}>{candidate.title}</div>
                    <div style={{ color: 'var(--text-muted)', fontSize: 10 }}>{candidate.summary || (isEn ? 'Awaiting product review' : '待产品化评审')} · {isEn ? 'Suggested' : '建议'} {candidate.suggested_price_sc} SC</div>
                  </div>
                ))}
              </section>
            </div>
          )}
        </div>
      </section>
    </div>
  )
}
