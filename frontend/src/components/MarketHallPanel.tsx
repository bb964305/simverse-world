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

const ACCENT = '#d97706'

function requestKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `market:${crypto.randomUUID()}`
  }
  return `market:${Date.now()}:${Math.random().toString(36).slice(2)}`
}

function phaseLabel(phase: CurrentMarket['phase']): string {
  if (!phase) return '闭市'
  const labels: Record<NonNullable<CurrentMarket['phase']>, string> = {
    waiting: '候场中', inbound: '进镇中', trading: '开市中', outbound: '离镇中',
  }
  return labels[phase]
}

function errorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  if (message.includes('402') || message.includes('余额不足')) return 'Soul Coin 余额不足'
  if (message.includes('售罄')) return '商品刚刚售罄，请刷新货架'
  if (message.includes('限购')) return '本次到访已经购买过该项目'
  if (message.includes('停止交易')) return '本次集市已经结束'
  if (message.includes('灰度关闭')) return '玩家交易仍在灰度关闭中'
  return '交易未完成，请刷新后重试'
}

function Countdown({ closesAt }: { closesAt: string | null }) {
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
  if (seconds === null) return <span>结算时间同步中</span>
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  return <span style={{ fontVariantNumeric: 'tabular-nums' }}>剩余 {hours > 0 ? `${hours}:` : ''}{String(minutes).padStart(2, '0')}:{String(rest).padStart(2, '0')}</span>
}

function OfferCard({ offer, busy, onBuy }: {
  offer: MarketOffer
  busy: boolean
  onBuy: (offer: MarketOffer) => void
}) {
  const label = offer.type === 'good' ? '进口商品' : offer.type === 'service' ? '限时服务' : '商路合同'
  const reason = offer.purchased ? '本次已购'
    : offer.stock <= 0 ? '已售罄'
      : !offer.eligible ? offer.unavailable_reason || '暂不符合条件'
        : null
  return (
    <div style={{
      border: '1px solid var(--border)', borderRadius: 10, background: 'var(--bg-input)',
      padding: 12, display: 'flex', flexDirection: 'column', gap: 7, minHeight: 170,
    }}>
      <div style={{ display: 'flex', gap: 9, alignItems: 'center' }}>
        <span style={{ fontSize: 25 }}>{offer.icon}</span>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 700 }}>{offer.name}</div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{label}</div>
        </div>
      </div>
      <div style={{ color: 'var(--text-secondary)', fontSize: 11, lineHeight: 1.55, flex: 1 }}>{offer.description}</div>
      <div style={{ display: 'flex', color: 'var(--text-muted)', fontSize: 10 }}>
        <span>余量 {offer.stock}/{offer.stock_total}</span>
        <span style={{ marginLeft: 'auto' }}>每人限购 {offer.per_user_limit}</span>
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
        {busy ? '交易中…' : reason ?? `🪙 ${offer.price_sc} 购买`}
      </button>
    </div>
  )
}

export function MarketHallPanel() {
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
      setNotice({ ok: false, text: '集市状态暂时不可用' })
    } finally {
      setLoading(false)
    }
  }, [])

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
      setNotice({ ok: true, text: `已获得「${offer.name}」（-${result.total_sc} SC）` })
      const me = await getMe()
      updateBalance(me.soul_coin_balance)
      await load()
    } catch (error) {
      setNotice({ ok: false, text: errorText(error) })
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
            <div id="market-dialog-title" style={{ fontSize: 15, fontWeight: 800, color: ACCENT }}>🏬 商队集市</div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>到访限定商品、服务与实验楼成果候选</div>
          </div>
          <div style={{ marginLeft: 'auto', textAlign: 'right', fontSize: 11, color: 'var(--text-secondary)' }}>
            <div>{phaseLabel(market?.phase ?? null)} · 🪙 {balance}</div>
            <Countdown closesAt={market?.closes_at ?? null} />
          </div>
          <button ref={closeRef} onClick={() => setOpen(false)} className="game-dialog-close" aria-label="关闭商队集市">✕</button>
        </div>

        <div style={{ overflowY: 'auto', padding: 18 }}>
          {notice && <div style={{ color: notice.ok ? 'var(--accent-green)' : 'var(--accent-red)', fontSize: 12, marginBottom: 12 }}>{notice.text}</div>}
          {loading && !market ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 28 }}>正在查看商队货单…</div>
          ) : !market ? null : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
              <div style={{ border: `1px solid ${ACCENT}44`, background: '#d977060a', borderRadius: 9, padding: '10px 12px', fontSize: 12, lineHeight: 1.6 }}>
                <b style={{ color: ACCENT }}>{market.message}</b>
                {!market.enabled && <div style={{ color: 'var(--text-muted)' }}>货单保持可见；运维开闸后，交易阶段会自动开放购买。</div>}
              </div>

              {grouped.goods.length > 0 && <section>
                <h3 style={{ fontSize: 13, margin: '0 0 9px' }}>远方货物</h3>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 10 }}>
                  {grouped.goods.map((offer) => <OfferCard key={offer.code} offer={offer} busy={busyCode === offer.code} onBuy={buy} />)}
                </div>
              </section>}

              {grouped.services.length > 0 && <section>
                <h3 style={{ fontSize: 13, margin: '0 0 9px' }}>本次限定服务</h3>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10 }}>
                  {grouped.services.map((offer) => <OfferCard key={offer.code} offer={offer} busy={busyCode === offer.code} onBuy={buy} />)}
                </div>
              </section>}

              <section>
                <h3 style={{ fontSize: 13, margin: '0 0 9px' }}>🧪 实验楼市场候选</h3>
                {market.research_candidates.length === 0 ? (
                  <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>暂无通过审核的研究成果候选。</div>
                ) : market.research_candidates.map((candidate) => (
                  <div key={candidate.id} style={{ borderLeft: `2px solid ${ACCENT}`, padding: '5px 9px', marginBottom: 7 }}>
                    <div style={{ fontSize: 12, fontWeight: 700 }}>{candidate.title}</div>
                    <div style={{ color: 'var(--text-muted)', fontSize: 10 }}>{candidate.summary || '待产品化评审'} · 建议 {candidate.suggested_price_sc} SC</div>
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
