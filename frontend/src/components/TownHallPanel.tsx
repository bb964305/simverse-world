import { useState, useEffect, useRef, useCallback, type CSSProperties } from 'react'
import { bridge } from '../game/phaserBridge'
import { getTownHallOverview, type TownHallOverview } from '../services/api'
import { useLocale } from '../services/locale'

// TownHallPanel — read-only 市政厅 (Society Expansion §10). Projects existing
// M1–M6 data (duty holders, open civic polls, recent election, config-derived
// finances); there are NO write actions here. Self-mounted in TopNav, opened
// via the `townhall:open` bridge event. Skeleton mirrors ExperimentPanel.

type TownTab = 'policy' | 'office' | 'poll' | 'result' | 'rep'
const ACCENT = '#b98cff' // 紫 — distinct from the lab's teal

const TABS: { key: TownTab; label: readonly [string, string] }[] = [
  { key: 'policy', label: ['Policy & finance', '政策 & 财政'] },
  { key: 'office', label: ['Office holders', '在任职位'] },
  { key: 'poll', label: ['Open polls', '议案投票'] },
  { key: 'result', label: ['Election results', '选举结果'] },
  { key: 'rep', label: ['Reputation', '声誉'] },
]

const FINANCE_LABELS: [keyof TownHallOverview['finances'], readonly [string, string], string][] = [
  ['npc_default_wage_sc', ['Resident daily wage', '居民日薪'], 'SC'],
  ['npc_meal_cost_sc', ['Meal cost', '每餐花费'], 'SC'],
  ['market_day_discount', ['Market Day discount', '集市日折扣'], '×'],
  ['market_day_weekday', ['Market Day weekday', '集市日(周)'], ''],
  ['civic_poll_days', ['Poll duration (days)', '议案投票时长(天)'], ''],
  ['election_interval_days', ['Election interval (days)', '选举周期(天)'], ''],
  ['election_mayor_wage_bonus', ['Mayor allowance', '镇长津贴'], 'SC'],
]

const muted: CSSProperties = { fontSize: 12, color: 'var(--text-muted)' }
const card: CSSProperties = {
  border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 8,
  background: 'var(--bg-input)',
}

export function TownHallPanel() {
  const locale = useLocale((state) => state.locale)
  const isEn = locale === 'en'
  const index = isEn ? 0 : 1
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<TownTab>('policy')
  const [data, setData] = useState<TownHallOverview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    getTownHallOverview()
      .then((d) => setData(d))
      .catch(() => setError(isEn ? 'Town Hall is temporarily unavailable' : '市政厅暂时无法访问'))
      .finally(() => setLoading(false))
  }, [isEn])

  useEffect(() => {
    const unsubOpen = bridge.on('townhall:open', () => {
      bridge.emit('bulletin:close')
      bridge.emit('experiment:close')
      setOpen(true)
      load()
    })
    const unsubClose = bridge.on('townhall:close', () => setOpen(false))
    return () => { unsubOpen(); unsubClose() }
  }, [load])

  useEffect(() => {
    if (!open) return
    closeButtonRef.current?.focus()
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  if (!open) return null

  return (
    <div
      className="game-modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) setOpen(false) }}
    >
      <section
        className="game-modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="townhall-dialog-title"
        style={{ borderColor: `${ACCENT}55` }}
      >
        <div className="game-dialog-header" style={{
          padding: '16px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          background: `${ACCENT}0f`,
        }}>
          <div>
            <div id="townhall-dialog-title" style={{ fontWeight: 800, fontSize: 15, color: ACCENT }}>🏛️ {isEn ? 'Town Hall' : '市政厅'}</div>
            <div style={{ ...muted, marginTop: 2 }}>
              {isEn ? 'Mayor' : '现任镇长'}: {data?.mayor ? data.mayor.name : (isEn ? 'Vacant' : '空缺')} · {isEn ? 'Public read-only record' : '只读公示'}
            </div>
          </div>
          <button ref={closeButtonRef} onClick={() => setOpen(false)} className="game-dialog-close" aria-label={isEn ? 'Close Town Hall' : '关闭市政厅'}>✕</button>
        </div>

        <div style={{ display: 'flex', gap: 4, padding: '8px 20px 0', borderBottom: '1px solid var(--border)' }}>
          {TABS.map((t) => (
            <button key={t.key} onClick={() => setTab(t.key)} style={{
              background: 'none', border: 'none', cursor: 'pointer', padding: '8px 10px',
              fontSize: 13, fontWeight: 600, color: tab === t.key ? ACCENT : 'var(--text-muted)',
              borderBottom: `2px solid ${tab === t.key ? ACCENT : 'transparent'}`,
            }}>{t.label[index]}</button>
          ))}
        </div>

        <div style={{ padding: 20, overflowY: 'auto' }}>
          {loading && <div style={muted}>{isEn ? 'Loading…' : '加载中…'}</div>}
          {error && <div style={{ fontSize: 12, color: '#ef4444' }}>{error}</div>}
          {!loading && !error && data && <TownTabBody tab={tab} data={data} />}
        </div>
      </section>
    </div>
  )
}

function TownTabBody({ tab, data }: { tab: TownTab; data: TownHallOverview }) {
  if (tab === 'policy') return <PolicyTab data={data} />
  if (tab === 'office') return <OfficeTab data={data} />
  if (tab === 'poll') return <PollTab data={data} />
  if (tab === 'rep') return <RepTab data={data} />
  return <ResultTab data={data} />
}

function PolicyTab({ data }: { data: TownHallOverview }) {
  const isEn = useLocale((state) => state.locale) === 'en'
  const index = isEn ? 0 : 1
  const rows = FINANCE_LABELS.filter(([k]) => data.finances[k] !== undefined)
  return (
    <div>
      <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8, color: ACCENT }}>📋 {isEn ? 'Current policy and finance' : '现行政策与财政'}</div>
      {rows.length === 0 && <div style={muted}>{isEn ? 'No public policy values.' : '暂无公开的政策数值。'}</div>}
      {rows.map(([k, label, unit]) => (
        <div key={k} style={{ display: 'flex', justifyContent: 'space-between', ...card, marginBottom: 6, padding: '8px 12px' }}>
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{label[index]}</span>
          <span style={{ fontSize: 12, fontWeight: 600 }}>{unit} {data.finances[k]}</span>
        </div>
      ))}
    </div>
  )
}

function OfficeTab({ data }: { data: TownHallOverview }) {
  const isEn = useLocale((state) => state.locale) === 'en'
  return (
    <div>
      <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8, color: ACCENT }}>👥 {isEn ? 'Office holders' : '在任职位'}</div>
      {data.duties.length === 0 && <div style={muted}>{isEn ? 'No current office holders.' : '暂无在任公职。'}</div>}
      {data.duties.map((d) => (
        <div key={d.key} style={{ ...card, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>{d.title}</span>
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{d.holder_name}</span>
        </div>
      ))}
    </div>
  )
}

function PollTab({ data }: { data: TownHallOverview }) {
  const isEn = useLocale((state) => state.locale) === 'en'
  return (
    <div>
      <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8, color: ACCENT }}>🗳️ {isEn ? 'Open proposals' : '进行中的议案'}</div>
      {data.open_polls.length === 0 && <div style={muted}>{isEn ? 'No proposals are currently open.' : '目前没有进行中的议案。'}</div>}
      {data.open_polls.map((p) => (
        <div key={p.id} style={card}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>{p.question}</div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {p.options.map((o, i) => (
              <span key={i} style={{
                fontSize: 11, padding: '2px 8px', borderRadius: 999,
                border: '1px solid var(--border)', color: 'var(--text-muted)',
              }}>{String((o as { label?: unknown }).label ?? '')}</span>
            ))}
          </div>
          {p.closes_at && <div style={{ ...muted, marginTop: 6 }}>{isEn ? 'Closes' : '截止'} {p.closes_at}</div>}
        </div>
      ))}
    </div>
  )
}

function RepTab({ data }: { data: TownHallOverview }) {
  const isEn = useLocale((state) => state.locale) === 'en'
  const rep = data.reputation
  const rows = rep?.residents ?? []
  return (
    <div>
      <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8, color: ACCENT }}>⚖️ {isEn ? 'Public reputation' : '公共声誉'}</div>
      {rep && !rep.enabled && rows.length > 0 && (
        <div style={{ ...muted, marginBottom: 8 }}>
          {isEn ? 'Reputation is not enabled (REP_ENABLED=false); showing the latest stored nightly snapshot.' : '声誉系统未开闸（REP_ENABLED=false）——以下为最近一次夜间聚合的落库数据。'}
        </div>
      )}
      {rows.length === 0 && (
        <div style={muted}>
          {rep && !rep.enabled ? (isEn ? 'Reputation is disabled; no public data.' : '声誉系统未开闸（REP_ENABLED=false），暂无公示数据。') : (isEn ? 'No reputation data.' : '暂无声誉数据。')}
        </div>
      )}
      {rows.map((r) => (
        <div key={r.slug} style={{ ...card, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>{r.name}</span>
          <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {!r.credit_ok && (
              <span style={{
                fontSize: 11, padding: '2px 8px', borderRadius: 999,
                border: '1px solid #ef444455', color: '#ef4444',
              }}>{isEn ? 'Credit restricted' : '信用受限'}</span>
            )}
            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
              {(r.score >= 0 ? '+' : '') + r.score.toFixed(3)} · n={r.samples}
            </span>
          </span>
        </div>
      ))}
      {rep && rows.length > 0 && (
        <div style={{ ...muted, marginTop: 6 }}>
          {isEn ? 'Credit threshold' : '信用阈值'} {rep.credit_min_score} · {isEn ? 'nightly aggregate' : '低于则信用受限 · 数据来自夜间聚合'}
        </div>
      )}
    </div>
  )
}

function ResultTab({ data }: { data: TownHallOverview }) {
  const isEn = useLocale((state) => state.locale) === 'en'
  const e = data.recent_election
  if (!e) return <div style={muted}>{isEn ? 'No completed election yet.' : '还没有已完成的选举。'}</div>
  return (
    <div>
      <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8, color: ACCENT }}>🏆 {isEn ? 'Latest election result' : '最近选举结果'}</div>
      <div style={card}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>{e.question}</div>
        {e.winner_name && (
          <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
            {isEn ? 'Elected' : '当选'}: <b style={{ color: ACCENT }}>{e.winner_name}</b>
            {e.winner_votes != null ? ` · ${e.winner_votes} ${isEn ? 'votes' : '票'}` : ''}
          </div>
        )}
        {e.closed_at && <div style={{ ...muted, marginTop: 6 }}>{isEn ? 'Closed' : '结束于'} {e.closed_at}</div>}
      </div>
    </div>
  )
}
