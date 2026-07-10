import { useCallback, useEffect, useState } from 'react'
import {
  getAdminEvents,
  createAdminEvent,
  deleteAdminEvent,
  type AdminWorldEvent,
} from '../../services/api'

const TYPE_OPTIONS = [
  { value: 'festival', label: '🎆 节日' },
  { value: 'news', label: '📰 新闻' },
  { value: 'custom', label: '📣 自定义' },
]

function toLocalInput(offsetMinutes: number): string {
  const d = new Date(Date.now() + offsetMinutes * 60_000)
  d.setSeconds(0, 0)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// Backend serializes naive UTC datetimes (no Z suffix) — anchor them to UTC
// before parsing, or JS reads them as local time and the status flips wrong.
function parseUTC(iso: string | null): Date | null {
  if (!iso) return null
  return new Date(/[Zz]|[+-]\d{2}:\d{2}$/.test(iso) ? iso : iso + 'Z')
}

function fmtLocal(iso: string | null): string {
  const d = parseUTC(iso)
  return d ? d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'
}

export function EventsPanel({ token }: { token: string }) {
  const [events, setEvents] = useState<AdminWorldEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Create form
  const [type, setType] = useState('custom')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [startsAt, setStartsAt] = useState(() => toLocalInput(2))
  const [endsAt, setEndsAt] = useState(() => toLocalInput(2 + 120))
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)

  const load = useCallback(() => {
    getAdminEvents(token)
      .then((r) => { setEvents(r.events); setError(null) })
      .catch(() => setError('事件列表加载失败'))
      .finally(() => setLoading(false))
  }, [token])

  useEffect(() => { load() }, [load])

  const create = async () => {
    if (busy) return
    if (!title.trim()) { setNotice({ ok: false, text: '标题不能为空' }); return }
    if (!startsAt || !endsAt || new Date(endsAt) <= new Date(startsAt)) {
      setNotice({ ok: false, text: '结束时间需晚于开始时间' })
      return
    }
    setBusy(true)
    setNotice(null)
    try {
      await createAdminEvent(token, {
        type,
        title: title.trim(),
        description: description.trim(),
        starts_at: new Date(startsAt).toISOString(),
        ends_at: new Date(endsAt).toISOString(),
      })
      setNotice({ ok: true, text: '已投放 — 事件建为待激活，cron 会在开始时间翻转并广播横幅。' })
      setTitle('')
      setDescription('')
      load()
    } catch (e) {
      const msg = e instanceof Error ? e.message : ''
      setNotice({ ok: false, text: msg.includes('ends_at') ? '结束时间需晚于开始时间' : '投放失败' })
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string) => {
    try { await deleteAdminEvent(token, id); load() } catch { /* keep list */ }
  }

  const inputStyle = {
    background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6,
    color: 'var(--text-primary)', fontSize: 13, padding: '7px 10px', outline: 'none',
  } as const

  return (
    <div style={{ maxWidth: 900 }}>
      <h1 style={{ fontSize: 20, fontWeight: 700, marginBottom: 24, color: 'var(--text-primary)' }}>
        世界事件投放
      </h1>

      {/* Create form */}
      <div style={{
        background: 'var(--bg-card)', border: '1px solid var(--border)',
        borderRadius: 10, padding: '20px 24px', marginBottom: 16,
      }}>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <select value={type} onChange={(e) => setType(e.target.value)} style={{ ...inputStyle, cursor: 'pointer' }}>
            {TYPE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <input
            value={title} onChange={(e) => setTitle(e.target.value)}
            placeholder="事件标题（横幅展示）" maxLength={100}
            style={{ ...inputStyle, flex: '1 1 220px' }}
          />
        </div>
        <textarea
          value={description} onChange={(e) => setDescription(e.target.value)}
          placeholder="描述 — 会注入居民对话与决策 prompt，并显示在横幅里"
          rows={2} maxLength={300}
          style={{ ...inputStyle, width: '100%', boxSizing: 'border-box', marginTop: 10, resize: 'vertical' }}
        />
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginTop: 10 }}>
          <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
            开始
            <input type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} style={inputStyle} />
          </label>
          <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
            结束
            <input type="datetime-local" value={endsAt} onChange={(e) => setEndsAt(e.target.value)} style={inputStyle} />
          </label>
          <button onClick={() => void create()} disabled={busy} style={{
            marginLeft: 'auto', padding: '8px 22px', borderRadius: 6, fontSize: 13, fontWeight: 600,
            background: 'var(--accent)', color: 'white', border: 'none',
            cursor: 'pointer', opacity: busy ? 0.6 : 1,
          }}>
            {busy ? '投放中…' : '📣 投放事件'}
          </button>
        </div>
        {notice && (
          <div style={{ fontSize: 12, marginTop: 10, color: notice.ok ? '#53d769' : '#ff6b6b' }}>
            {notice.text}
          </div>
        )}
      </div>

      {/* Event list */}
      {loading ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>加载中…</div>
      ) : error ? (
        <div style={{ color: '#ff6b6b', fontSize: 13 }}>{error}</div>
      ) : events.length === 0 ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: 24 }}>
          还没有投放过事件。
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {events.map((e) => {
            const end = parseUTC(e.ends_at)
            const ended = end != null && end < new Date()
            const status = e.is_active ? { text: '🟢 进行中', color: '#53d769' }
              : ended ? { text: '⚪ 已结束', color: 'var(--text-muted)' }
              : { text: '🕐 待开始', color: '#eab308' }
            return (
              <div key={e.id} style={{
                display: 'flex', alignItems: 'center', gap: 12, padding: '10px 14px',
                background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 8,
              }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
                    {e.title}
                    <span style={{ fontSize: 10, color: 'var(--text-muted)', marginLeft: 8 }}>{e.type}</span>
                  </div>
                  <div style={{
                    fontSize: 11, color: 'var(--text-muted)', marginTop: 2,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {fmtLocal(e.starts_at)} → {fmtLocal(e.ends_at)}
                    {e.description && ` · ${e.description}`}
                  </div>
                </div>
                <span style={{ fontSize: 11, color: status.color, whiteSpace: 'nowrap' }}>{status.text}</span>
                <button onClick={() => void remove(e.id)} style={{
                  padding: '4px 10px', borderRadius: 6, fontSize: 11, cursor: 'pointer',
                  background: 'transparent', border: '1px solid var(--border)', color: 'var(--text-muted)',
                }}>删除</button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
