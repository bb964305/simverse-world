import { useEffect, useMemo, useState } from 'react'
import { TopNav } from '../components/TopNav'
import { useGameStore } from '../stores/gameStore'
import { useLocale } from '../services/locale'
import {
  getCapsules,
  createCapsule,
  getResidents,
  getMe,
  type CapsuleData,
  type ResidentListItem,
} from '../services/api'

const MAX_CONTENT = 500
const CAPSULE_FEE = 10

const DETAIL_COPY: [string, string, string][] = [
  ['content is required', 'Content is required', '信的内容不能为空'],
  ['content too long', `Content is too long (maximum ${MAX_CONTENT} characters)`, `信太长了（最多 ${MAX_CONTENT} 字）`],
  ['deliver_on must be 3 days to 1 year from now', 'Delivery must be between 3 days and 1 year from now', '送达日期需在 3 天到 1 年之间'],
  ['carrier resident not found', 'Carrier resident not found', '承运居民不存在'],
  ['Insufficient Soul Coins', 'Insufficient Soul Credits (SC)', '灵魂积分（SC）不足'],
]

function errorCopy(e: unknown, english: boolean): string {
  const msg = e instanceof Error ? e.message : ''
  for (const [match, en, zh] of DETAIL_COPY) if (msg.includes(match)) return english ? en : zh
  return english ? 'Unable to send. Please try again.' : '寄出失败，请稍后重试'
}

function isoDaysFromNow(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() + days)
  return d.toISOString().slice(0, 10)
}

export function CapsulesPage() {
  const locale = useLocale((state) => state.locale)
  const en = locale === 'en'
  const updateBalance = useGameStore((s) => s.updateBalance)
  const [capsules, setCapsules] = useState<CapsuleData[] | null>(null)
  const [residents, setResidents] = useState<ResidentListItem[]>([])
  const [carrier, setCarrier] = useState('')
  const [deliverOn, setDeliverOn] = useState(isoDaysFromNow(30))
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    getCapsules().then((r) => setCapsules(r.capsules)).catch(() => setCapsules([]))
    getResidents()
      .then((rs) => setResidents(rs.filter((r) => !r.slug.startsWith('p-'))))
      .catch(() => setResidents([]))
  }, [])

  const isFirst = (capsules?.length ?? 0) === 0
  const carrierName = useMemo(
    () => residents.find((r) => r.slug === carrier)?.name ?? '',
    [residents, carrier],
  )

  const send = async () => {
    if (busy) return
    if (!carrier) { setNotice({ ok: false, text: en ? 'Choose a carrier resident' : '请选择一位承运居民' }); return }
    if (!content.trim()) { setNotice({ ok: false, text: en ? 'Content is required' : '信的内容不能为空' }); return }
    setBusy(true)
    setNotice(null)
    try {
      const c = await createCapsule(carrier, deliverOn, content.trim())
      setCapsules((prev) => [...(prev ?? []), c])
      setContent('')
      setNotice({ ok: true, text: en
        ? `Letter sealed. ${carrierName || 'The resident'} will keep it until ${c.deliver_on}.`
        : `信已封缄，${carrierName || '居民'}会替你保管到 ${c.deliver_on}。` })
      getMe().then((me) => updateBalance(me.soul_coin_balance)).catch(() => {})
    } catch (e) {
      setNotice({ ok: false, text: errorCopy(e, en) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <TopNav />
      <div style={{
        marginTop: 'var(--nav-height)', minHeight: 'calc(100vh - var(--nav-height))',
        background: 'var(--bg-primary)', padding: '32px 20px 60px',
        display: 'flex', flexDirection: 'column', alignItems: 'center',
      }}>
        {/* Letter paper composer — the one intentionally light surface in the app. */}
        <div style={{
          width: 'min(680px, 94vw)',
          background: 'linear-gradient(180deg, #f6efe0 0%, #f1e8d4 100%)',
          borderRadius: 6, padding: '36px 44px 28px',
          boxShadow: '0 12px 40px rgba(0,0,0,0.5)',
          color: '#4a3f2e', position: 'relative',
        }}>
          <div style={{ position: 'absolute', top: 14, right: 18, fontSize: 22 }}>💌</div>
          <div style={{ fontSize: 19, fontWeight: 700, letterSpacing: 2 }}>{en ? 'A letter to the future' : '写给未来的信'}</div>
          <div style={{ fontSize: 12, color: '#8a7a5e', marginTop: 4 }}>
            {en ? 'Entrust it to a resident, who will deliver it on the selected day.' : '交给一位居民保管，到期那天由 TA 替你送达。'}
            {isFirst
              ? <span style={{ color: '#3e7d3a', fontWeight: 600 }}> {en ? 'First letter is free' : '首封免费'}</span>
              : <span> {en ? `Fee: ${CAPSULE_FEE} SC` : `寄信费 ${CAPSULE_FEE} SC`}</span>}
          </div>

          <div style={{ display: 'flex', gap: 12, marginTop: 20, flexWrap: 'wrap' }}>
            <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
              {en ? 'Carrier' : '承运人'}
              <select value={carrier} onChange={(e) => setCarrier(e.target.value)} style={{
                background: '#fffdf6', color: '#4a3f2e', border: '1px solid #d8cbae',
                borderRadius: 4, padding: '5px 8px', fontSize: 12,
              }}>
                <option value="">{en ? 'Choose a resident…' : '选择居民…'}</option>
                {residents.map((r) => (
                  <option key={r.slug} value={r.slug}>{r.name}</option>
                ))}
              </select>
            </label>
            <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
              {en ? 'Delivery date' : '送达日'}
              <input
                type="date" value={deliverOn}
                min={isoDaysFromNow(3)} max={isoDaysFromNow(365)}
                onChange={(e) => setDeliverOn(e.target.value)}
                style={{
                  background: '#fffdf6', color: '#4a3f2e', border: '1px solid #d8cbae',
                  borderRadius: 4, padding: '4px 8px', fontSize: 12,
                }}
              />
            </label>
          </div>

          {/* Lined writing area */}
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value.slice(0, MAX_CONTENT))}
            placeholder={en ? 'Dear future me / you…' : '亲爱的未来的我／你：……'}
            rows={10}
            style={{
              width: '100%', boxSizing: 'border-box', marginTop: 16, padding: '4px 2px',
              background: 'repeating-linear-gradient(180deg, transparent, transparent 27px, #d8cbae 27px, #d8cbae 28px)',
              border: 'none', outline: 'none', resize: 'vertical',
              fontSize: 14, lineHeight: '28px', color: '#4a3f2e',
              fontFamily: 'Georgia, "Songti SC", serif',
            }}
          />
          <div style={{ display: 'flex', alignItems: 'center', marginTop: 10 }}>
            <span style={{ fontSize: 11, color: '#8a7a5e' }}>{content.length} / {MAX_CONTENT}</span>
            {notice && (
              <span style={{
                marginLeft: 12, fontSize: 12,
                color: notice.ok ? '#3e7d3a' : '#b03434',
              }}>{notice.text}</span>
            )}
            <button onClick={() => void send()} disabled={busy} style={{
              marginLeft: 'auto', padding: '7px 22px', borderRadius: 4, cursor: 'pointer',
              background: '#4a3f2e', color: '#f6efe0', border: 'none',
              fontSize: 13, fontWeight: 600, letterSpacing: 2, opacity: busy ? 0.6 : 1,
            }}>
              {busy ? (en ? 'Sealing…' : '封缄中…') : (en ? 'Seal and send' : '封缄寄出')}
            </button>
          </div>
        </div>

        {/* My capsules */}
        <div style={{ width: 'min(680px, 94vw)', marginTop: 28 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-primary)', marginBottom: 10 }}>
            📮 {en ? 'My capsules' : '我的胶囊'}
          </div>
          {capsules === null ? (
            <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{en ? 'Loading…' : '加载中…'}</div>
          ) : capsules.length === 0 ? (
            <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{en ? 'No letters yet — write your first one above for free.' : '还没有寄出过信 — 上面写第一封吧，首封免费。'}</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {capsules.map((c) => (
                <div key={c.id} style={{
                  background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10,
                  padding: '12px 16px',
                }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                    <span style={{ fontSize: 14 }}>{c.status === 'delivered' ? '📬' : '💌'}</span>
                    <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
                      {c.status === 'delivered' ? (en ? 'Delivered' : '已送达') : (en ? 'Sealed' : '封缄中')}
                    </span>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      {en ? 'Carrier' : '承运'}: {c.carrier_resident_slug} · {en ? 'Delivery' : '送达日'} {c.deliver_on}
                    </span>
                  </div>
                  {c.content && (
                    <div style={{
                      fontSize: 12, color: 'var(--text-secondary)', marginTop: 8, lineHeight: 1.7,
                      whiteSpace: 'pre-wrap', overflow: 'hidden', display: '-webkit-box',
                      WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
                    }}>{c.content}</div>
                  )}
                  {c.resident_note && (
                    <div style={{
                      fontSize: 12, color: 'var(--accent-green)', marginTop: 8,
                      borderTop: '1px dashed var(--border)', paddingTop: 8,
                    }}>
                      {en ? 'Carrier note' : '附言'}: {c.resident_note}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  )
}
