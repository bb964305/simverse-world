import { useCallback, useEffect, useRef, useState } from 'react'
import { TopNav } from '../components/TopNav'
import { connectWS, onWSMessage } from '../services/ws'
import { useGameStore } from '../stores/gameStore'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLocale, type Locale } from '../services/locale'
import {
  getDebates,
  getDebate,
  getMe,
  stakeDebate,
  voteDebate,
  type DebateView,
  type DebateSide,
  type DebateTurn,
} from '../services/api'

// Backend lifecycle (debate_service.py): announced → live → voting → settled.
// Stakes (10-200 🪙) only while "announced"; free votes only while "voting".
const STAKE_MIN = 10
const STAKE_MAX = 200

const STATUS_META: Record<string, { en: string; zh: string; color: string; pulse?: boolean }> = {
  announced: { en: 'Staking open', zh: '下注中', color: 'var(--accent-green)' },
  live: { en: 'Live debate', zh: '辩论中', color: 'var(--accent-red)', pulse: true },
  voting: { en: 'Voting open', zh: '投票中', color: 'var(--accent-blue)' },
  settled: { en: 'Settled', zh: '已结束', color: 'var(--text-muted)' },
}

// Map the backend DebateError detail strings to Chinese; unknown ones show raw.
const DETAIL_COPY: [string, string, string][] = [
  ['already staked on this debate', 'You already staked on this debate', '你已在本场辩论下过注'],
  ['already voted on this debate', 'You already voted on this debate', '你已在本场辩论投过票'],
  ['debate is not open for staking', 'Staking is not open for this debate', '本场辩论当前不可下注'],
  ['debate is not open for voting', 'Voting is not open for this debate', '本场辩论当前不可投票'],
  ['Insufficient Soul Coins', 'Insufficient Soul Credits (SC)', '灵魂积分（SC）不足'],
  ['amount must be', `Amount must be between ${STAKE_MIN} and ${STAKE_MAX} SC`, `金额需在 ${STAKE_MIN}-${STAKE_MAX} SC 之间`],
]

// Pull the backend `detail` string out of an apiFetch error ("API 400: {json}").
function errDetail(e: unknown, locale: Locale): string {
  const msg = e instanceof Error ? e.message : String(e)
  const m = msg.match(/^API \d+: ([\s\S]*)$/)
  let detail = msg
  if (m) {
    try {
      const parsed = JSON.parse(m[1]) as { detail?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
    } catch { /* raw body wasn't JSON — fall through */ }
  }
  for (const [match, en, zh] of DETAIL_COPY) {
    if (detail.includes(match)) return locale === 'en' ? en : zh
  }
  return detail
}

function StatusBadge({ status }: { status: string }) {
  const locale = useLocale((state) => state.locale)
  const meta = STATUS_META[status] ?? { en: status, zh: status, color: 'var(--text-muted)' }
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      fontSize: 11, fontWeight: 600, padding: '2px 8px', borderRadius: 10,
      color: meta.color, border: `1px solid ${meta.color}`,
      whiteSpace: 'nowrap',
      animation: meta.pulse ? 'debateLivePulse 1.2s ease-in-out infinite' : undefined,
    }}>
      {meta.pulse && <span style={{ width: 6, height: 6, borderRadius: '50%', background: meta.color }} />}
      {locale === 'en' ? meta.en : meta.zh}
    </span>
  )
}

// Proportional A/B bar; a 50/50 split when both sides are still zero.
function SideBar({ a, b, unit }: { a: number; b: number; unit: string }) {
  const total = a + b
  const pctA = total > 0 ? (a / total) * 100 : 50
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ display: 'flex', height: 10, borderRadius: 5, overflow: 'hidden', background: 'var(--bg-input)' }}>
        <div style={{ width: `${pctA}%`, background: 'var(--accent-red)', transition: 'width 0.4s' }} />
        <div style={{ width: `${100 - pctA}%`, background: 'var(--accent-blue)', transition: 'width 0.4s' }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
        <span style={{ color: 'var(--accent-red)' }}>{`${a} ${unit}`}</span>
        <span style={{ color: 'var(--accent-blue)' }}>{`${b} ${unit}`}</span>
      </div>
    </div>
  )
}

// ── Debate list (left pane) ─────────────────────────────────────────
function DebateListItem({ d, selected, onClick }: { d: DebateView; selected: boolean; onClick: () => void }) {
  const locale = useLocale((state) => state.locale)
  const en = locale === 'en'
  return (
    <div
      onClick={onClick}
      style={{
        background: 'var(--bg-card)',
        border: selected ? '1px solid var(--accent-red)' : '1px solid var(--border)',
        borderRadius: 10, padding: 14, cursor: 'pointer',
      }}
      onMouseEnter={(e) => { if (!selected) e.currentTarget.style.borderColor = 'var(--text-muted)' }}
      onMouseLeave={(e) => { if (!selected) e.currentTarget.style.borderColor = 'var(--border)' }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)', lineHeight: 1.5 }}>
          {d.topic}
        </div>
        <StatusBadge status={d.status} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 8 }}>
        {d.resident_a_slug} vs {d.resident_b_slug}
      </div>
      <div style={{ display: 'flex', gap: 12, fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
        <span>{en ? 'Pool' : '奖池'} {d.pool_a + d.pool_b} SC</span>
        <span>{en ? 'Votes' : '票数'} {d.votes_a + d.votes_b}</span>
        {d.status === 'settled' && d.winner && (
          <span style={{ color: 'var(--text-secondary)' }}>
            {d.winner === 'draw' ? (en ? 'Draw' : '平局') : `${en ? 'Winner' : '胜者'}: ${d.winner === 'a' ? d.resident_a_slug : d.resident_b_slug}`}
          </span>
        )}
      </div>
    </div>
  )
}

// ── Transcript ──────────────────────────────────────────────────────
function TranscriptBubble({ turn }: { turn: DebateTurn }) {
  const locale = useLocale((state) => state.locale)
  const isA = turn.side === 'a'
  const accent = isA ? 'var(--accent-red)' : 'var(--accent-blue)'
  return (
    <div style={{ display: 'flex', justifyContent: isA ? 'flex-start' : 'flex-end' }}>
      <div style={{
        maxWidth: '78%', background: 'var(--bg-input)',
        border: `1px solid ${isA ? '#e9456044' : '#0ea5e944'}`,
        borderRadius: 10, padding: '8px 12px',
      }}>
        <div style={{ fontSize: 11, color: accent, fontWeight: 600, marginBottom: 4 }}>
          {locale === 'en' ? `Round ${turn.round}` : `第 ${turn.round} 轮`} · {turn.speaker}
        </div>
        <div style={{ fontSize: 13, color: 'var(--text-primary)', lineHeight: 1.6 }}>{turn.text}</div>
      </div>
    </div>
  )
}

// ── Detail pane (right) ─────────────────────────────────────────────
function DebateDetail({ debate, onChanged }: { debate: DebateView; onChanged: () => void }) {
  const locale = useLocale((state) => state.locale)
  const en = locale === 'en'
  const [stakeSide, setStakeSide] = useState<DebateSide>('a')
  const [stakeAmount, setStakeAmount] = useState(String(STAKE_MIN))
  const [busy, setBusy] = useState(false)
  const [actionErr, setActionErr] = useState('')
  const [actionOk, setActionOk] = useState('')
  const transcriptEndRef = useRef<HTMLDivElement>(null)

  // New live turns should stay in view.
  useEffect(() => {
    if (debate.status === 'live') {
      transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [debate.transcript.length, debate.status])

  // Reset transient form state when switching debates.
  useEffect(() => {
    setActionErr('')
    setActionOk('')
    setStakeSide('a')
    setStakeAmount(String(STAKE_MIN))
  }, [debate.id])

  const doStake = async () => {
    const amount = Number(stakeAmount)
    if (!Number.isInteger(amount) || amount < STAKE_MIN || amount > STAKE_MAX) {
      setActionErr(en ? `Amount must be a whole number from ${STAKE_MIN} to ${STAKE_MAX}` : `金额需为 ${STAKE_MIN}-${STAKE_MAX} 之间的整数`)
      return
    }
    setBusy(true)
    setActionErr('')
    setActionOk('')
    try {
      await stakeDebate(debate.id, stakeSide, amount)
      setActionOk(en
        ? `Stake confirmed: ${stakeSide === 'a' ? 'Side A' : 'Side B'} · ${amount} SC`
        : `下注成功：${stakeSide === 'a' ? '正方' : '反方'} · ${amount} SC`)
      // Stakes charge coins server-side without a coin_update WS frame —
      // refresh so the TopNav balance doesn't go stale.
      getMe().then((me) => useGameStore.getState().updateBalance(me.soul_coin_balance)).catch(() => {})
      onChanged()
    } catch (e) {
      setActionErr(errDetail(e, locale))
    } finally {
      setBusy(false)
    }
  }

  const doVote = async (side: DebateSide) => {
    setBusy(true)
    setActionErr('')
    setActionOk('')
    try {
      await voteDebate(debate.id, side)
      setActionOk(en ? `Vote cast for Side ${side.toUpperCase()}` : `已投给${side === 'a' ? '正方' : '反方'}`)
      onChanged()
    } catch (e) {
      setActionErr(errDetail(e, locale))
    } finally {
      setBusy(false)
    }
  }

  const nameA = debate.resident_a_slug
  const nameB = debate.resident_b_slug
  const winnerName = debate.winner === 'a' ? nameA : debate.winner === 'b' ? nameB : null

  const sideBtn = (side: DebateSide) => {
    const active = stakeSide === side
    const color = side === 'a' ? 'var(--accent-red)' : 'var(--accent-blue)'
    return (
      <button
        key={side}
        onClick={() => setStakeSide(side)}
        disabled={busy}
        style={{
          padding: '6px 14px', fontSize: 12, fontWeight: 600, cursor: 'pointer',
          background: active ? color : 'var(--bg-input)',
          color: active ? 'white' : 'var(--text-secondary)',
          border: `1px solid ${active ? color : 'var(--border)'}`,
          borderRadius: 'var(--radius)',
        }}
      >
        {side === 'a' ? `${en ? 'Side A' : '正方'} ${nameA}` : `${en ? 'Side B' : '反方'} ${nameB}`}
      </button>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {/* Header card */}
      <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10, padding: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, alignItems: 'flex-start' }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-primary)', lineHeight: 1.5 }}>
            ⚔️ {debate.topic}
          </div>
          <StatusBadge status={debate.status} />
        </div>
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12,
          marginTop: 14, fontSize: 14, fontWeight: 700,
        }}>
          <span style={{ color: 'var(--accent-red)' }}>{nameA}</span>
          <span style={{ color: 'var(--text-muted)', fontSize: 12, fontWeight: 400 }}>VS</span>
          <span style={{ color: 'var(--accent-blue)' }}>{nameB}</span>
        </div>
        <div style={{ marginTop: 10 }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{en ? 'Stake pool (offchain SC)' : '奖池（链下 SC）'}</div>
          <SideBar a={debate.pool_a} b={debate.pool_b} unit="SC" />
        </div>
        <div style={{ marginTop: 10 }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{en ? 'Votes' : '票数'}</div>
          <SideBar a={debate.votes_a} b={debate.votes_b} unit={en ? 'votes' : '票'} />
        </div>

        {debate.status === 'settled' && (
          <div style={{
            marginTop: 14, padding: '10px 14px', borderRadius: 'var(--radius)',
            textAlign: 'center', fontSize: 13, fontWeight: 600,
            background: debate.winner === 'draw' ? 'var(--bg-input)' : '#e9456018',
            border: `1px solid ${debate.winner === 'draw' ? 'var(--border)' : 'var(--accent-red)'}`,
            color: debate.winner === 'draw' ? 'var(--text-secondary)' : 'var(--accent-red)',
          }}>
            {debate.winner === 'draw'
              ? (en ? '⚖️ Draw — every stake was refunded in full' : '⚖️ 平局 — 所有赌注已全额退还')
              : (en ? `🏆 ${winnerName ?? '?'} wins. The losing pool is distributed proportionally to winning stakers.` : `🏆 ${winnerName ?? '?'} 获胜！败方奖池按比例分给胜方下注者`)}
          </div>
        )}

        {/* Stake form: backend only accepts stakes while status === "announced" */}
        {debate.status === 'announced' && (
          <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 8 }}>
              {en
                ? `Back one side with ${STAKE_MIN}-${STAKE_MAX} offchain SC. One stake per player; a stake also counts as one vote.`
                : `押注支持一方（${STAKE_MIN}-${STAKE_MAX} 链下 SC，每人限一注，下注即计一票）`}
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              {sideBtn('a')}
              {sideBtn('b')}
              <input
                type="number"
                min={STAKE_MIN}
                max={STAKE_MAX}
                value={stakeAmount}
                disabled={busy}
                onChange={(e) => setStakeAmount(e.target.value)}
                style={{
                  width: 80, padding: '6px 8px', fontSize: 12,
                  background: 'var(--bg-input)', color: 'var(--text-primary)',
                  border: '1px solid var(--border)', borderRadius: 'var(--radius)',
                }}
              />
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>SC</span>
              <button
                onClick={doStake}
                disabled={busy}
                style={{
                  background: 'var(--accent-red)', color: 'white', border: 'none',
                  padding: '6px 16px', borderRadius: 'var(--radius)', fontSize: 12,
                  fontWeight: 600, cursor: busy ? 'default' : 'pointer',
                  opacity: busy ? 0.6 : 1,
                }}
              >
                {en ? 'Stake' : '下注'}
              </button>
            </div>
          </div>
        )}

        {/* Free vote: backend only accepts votes while status === "voting" */}
        {debate.status === 'voting' && (
          <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 8 }}>
              {en ? 'The debate is over. Cast one vote; an existing stake already counts.' : '辩论结束，投出你的一票（每人一票，已下注则自动计入）'}
            </div>
            <div style={{ display: 'flex', gap: 10 }}>
              <button
                onClick={() => doVote('a')}
                disabled={busy}
                style={{
                  flex: 1, padding: '8px 0', fontSize: 13, fontWeight: 600,
                  background: '#e9456022', color: 'var(--accent-red)',
                  border: '1px solid var(--accent-red)', borderRadius: 'var(--radius)',
                  cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.6 : 1,
                }}
              >
                {en ? `Vote A (${nameA})` : `投A票（${nameA}）`}
              </button>
              <button
                onClick={() => doVote('b')}
                disabled={busy}
                style={{
                  flex: 1, padding: '8px 0', fontSize: 13, fontWeight: 600,
                  background: '#0ea5e922', color: 'var(--accent-blue)',
                  border: '1px solid var(--accent-blue)', borderRadius: 'var(--radius)',
                  cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.6 : 1,
                }}
              >
                {en ? `Vote B (${nameB})` : `投B票（${nameB}）`}
              </button>
            </div>
          </div>
        )}

        {debate.status === 'live' && (
          <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-muted)' }}>
            {en ? 'The debate is live. Staking is closed; voting opens after the final turn.' : '辩论进行中，下注已截止；结束后将开放投票。'}
          </div>
        )}

        {actionErr && <div style={{ color: 'var(--accent-red)', fontSize: 12, marginTop: 10 }}>{actionErr}</div>}
        {actionOk && <div style={{ color: 'var(--accent-green)', fontSize: 12, marginTop: 10 }}>{actionOk}</div>}
      </div>

      {/* Transcript */}
      <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10, padding: 20 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-primary)', marginBottom: 12 }}>
          💬 {en ? 'Debate transcript' : '辩论实录'}
        </div>
        {debate.transcript.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
            {debate.status === 'announced'
              ? (en ? 'The debate has not started. Back the side you expect to win.' : '辩论尚未开始，先押注支持你看好的一方吧。')
              : (en ? 'No transcript yet.' : '暂无发言记录。')}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {debate.transcript.map((t, i) => <TranscriptBubble key={`${t.round}-${t.side}-${i}`} turn={t} />)}
            {debate.status === 'live' && (
              <div style={{ fontSize: 12, color: 'var(--text-muted)', textAlign: 'center' }}>
                ⏳ {en ? 'Waiting for the next speaker…' : '等待下一位发言…'}
              </div>
            )}
            <div ref={transcriptEndRef} />
          </div>
        )}
      </div>
    </div>
  )
}

// ── Page ────────────────────────────────────────────────────────────
export function DebatesPage() {
  const locale = useLocale((state) => state.locale)
  const en = locale === 'en'
  const [debates, setDebates] = useState<DebateView[] | null>(null)
  const [listErr, setListErr] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<DebateView | null>(null)
  const [detailErr, setDetailErr] = useState('')
  const isMobile = useIsMobile()

  // Live debate_turn/voting_open frames arrive over WS. GamePage owns the
  // socket but tears it down on unmount — re-establish here (idempotent, and
  // intentionally not disconnected on unmount; same pattern as ForgePage).
  useEffect(() => {
    connectWS()
  }, [])

  const loadList = useCallback(() => {
    getDebates()
      .then((r) => { setDebates(r.debates); setListErr('') })
      .catch(() => setListErr(en ? 'Unable to load debates. Please try again.' : '辩论列表加载失败，请稍后重试'))
  }, [en])

  const loadDetail = useCallback((id: string) => {
    getDebate(id)
      .then((d) => { setDetail(d); setDetailErr('') })
      .catch(() => setDetailErr(en ? 'Unable to load debate details. Please try again.' : '辩论详情加载失败，请稍后重试'))
  }, [en])

  useEffect(() => { loadList() }, [loadList])

  useEffect(() => {
    if (selectedId) loadDetail(selectedId)
  }, [selectedId, loadDetail])

  // Only show a detail that matches the current selection; while the fetch for
  // a newly selected debate is in flight this stays null → loading text.
  const currentDetail = detail && detail.id === selectedId ? detail : null

  // Live updates: append debate_turn frames to the open transcript; when
  // voting opens or a debate aborts, re-fetch so pools/status stay honest.
  useEffect(() => {
    return onWSMessage((data) => {
      const type = data.type as string
      const debateId = data.debate_id as string | undefined
      if (type === 'debate_turn' && debateId) {
        if (debateId === selectedId) {
          const turn: DebateTurn = {
            round: (data.round as number) ?? 0,
            side: (data.side as DebateSide) ?? 'a',
            speaker: (data.speaker as string) ?? '?',
            text: (data.text as string) ?? '',
          }
          setDetail((d) => (d && d.id === debateId
            ? { ...d, status: 'live', transcript: [...d.transcript, turn] }
            : d))
        }
        // A turn also means the list's status may have flipped announced → live.
        setDebates((list) => list?.map((d) => (d.id === debateId && d.status === 'announced'
          ? { ...d, status: 'live' }
          : d)) ?? list)
      }
      if (type === 'debate_voting_open' || type === 'debate_aborted') {
        loadList()
        if (debateId && debateId === selectedId) loadDetail(debateId)
      }
    })
  }, [selectedId, loadList, loadDetail])

  const afterAction = useCallback(() => {
    loadList()
    if (selectedId) loadDetail(selectedId)
  }, [selectedId, loadList, loadDetail])

  // Mobile: a fixed 330px list + flex:1 detail leaves the detail pane ~60px
  // wide (see E2E-04 repro — its content collapses into a single vertical
  // character column). Switch to a master/detail flow instead — full-width
  // list until something's selected, then full-width detail with a way back.
  const showList = !isMobile || !selectedId
  const showDetail = !isMobile || !!selectedId

  return (
    <>
      <TopNav />
      <style>{'@keyframes debateLivePulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }'}</style>
      <div style={{
        marginTop: 'var(--nav-height)', height: 'calc(100vh - var(--nav-height))',
        display: 'flex', flexDirection: isMobile ? 'column' : 'row', background: 'var(--bg-page)',
      }}>
        {/* Left: list (mobile: full-width, hidden once a debate is selected) */}
        {showList && (
          <div data-testid="debate-list-pane" style={{
            width: isMobile ? '100%' : 330, flexShrink: 0, overflowY: 'auto',
            borderRight: isMobile ? 'none' : '1px solid var(--border)', padding: 16,
            display: 'flex', flexDirection: 'column', gap: 10,
          }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-primary)', marginBottom: 4 }}>
              ⚔️ {en ? 'Debate arena' : '辩论擂台'}
            </div>
            {debates === null && !listErr && <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{en ? 'Loading…' : '加载中…'}</div>}
            {listErr && <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{listErr}</div>}
            {debates !== null && debates.length === 0 && (
              <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{en ? 'No debates yet. Return when residents take opposing positions.' : '暂无辩论，等居民们吵起来再来看吧。'}</div>
            )}
            {debates?.map((d) => (
              <DebateListItem
                key={d.id}
                d={d}
                selected={d.id === selectedId}
                onClick={() => { setDetailErr(''); setSelectedId(d.id) }}
              />
            ))}
          </div>
        )}

        {/* Right: detail (mobile: only rendered once something's selected) */}
        {showDetail && (
          <div data-testid="debate-detail-pane" style={{ flex: 1, overflowY: 'auto', padding: isMobile ? 16 : 20 }}>
            {isMobile && selectedId && (
              <button
                onClick={() => setSelectedId(null)}
                style={{
                  background: 'transparent', border: 'none', color: 'var(--accent-blue)',
                  fontSize: 13, cursor: 'pointer', padding: 0, marginBottom: 14,
                  display: 'flex', alignItems: 'center', gap: 4,
                }}
              >
                ← {en ? 'Back to list' : '返回列表'}
              </button>
            )}
            {!selectedId && (
              <div style={{
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                height: '100%', color: 'var(--text-muted)', fontSize: 13,
              }}>
                {en ? 'Choose a debate from the list to view details' : '从左侧选择一场辩论查看详情'}
              </div>
            )}
            {selectedId && !currentDetail && !detailErr && (
              <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{en ? 'Loading…' : '加载中…'}</div>
            )}
            {selectedId && detailErr && !currentDetail && (
              <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{detailErr}</div>
            )}
            {selectedId && currentDetail && (
              <div style={{ maxWidth: isMobile ? undefined : 720, margin: isMobile ? undefined : '0 auto' }}>
                <DebateDetail debate={currentDetail} onChanged={afterAction} />
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}
