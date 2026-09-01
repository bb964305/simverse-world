import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  createHostedAgent,
  listHostedAgents,
  startHostedAgent,
  stopHostedAgent,
  type HostedAgentSummary,
} from '../services/api/adminHostedAgents'
import { useLocale } from '../services/locale'

interface Props {
  token: string | null
  passportReady: boolean
}

const COPY = {
  en: {
    index: '04 / PERSONAL AGENT RUNTIME', title: 'Run your Agent in the living world',
    lead: 'Connect an OpenAI-compatible model provider. The key is encrypted server-side and never returned by the API. Your Agent can keep observing and acting while you are away.',
    load: 'Loading runtime…', unavailable: 'Create and confirm your Agent Passport before enabling autonomous runtime.',
    name: 'Agent name', goal: 'Public goal', base: 'Provider base URL', key: 'API key', model: 'Model', create: 'Create runtime', creating: 'Validating provider…',
    running: 'RUNNING', paused: 'PAUSED', provisioning: 'PROVISIONING', start: 'Start Agent', stop: 'Pause Agent', refresh: 'Refresh',
    actions: 'Actions today', tokens: 'Tokens today', status: 'Runtime status', keyHint: 'Write-only secret · stored encrypted',
  },
  'zh-CN': {
    index: '04 / 个人 AGENT 挂机', title: '让你的 Agent 在世界里持续运行',
    lead: '连接 OpenAI 兼容的模型服务。密钥只在服务端加密保存，API 永远不会返回；离线后 Agent 也能继续观察和行动。',
    load: '正在加载运行状态…', unavailable: '请先创建并确认 Agent Passport，再启用自主挂机。',
    name: 'Agent 名称', goal: '公开目标', base: '模型服务地址', key: 'API 密钥', model: '模型', create: '创建挂机运行器', creating: '正在验证服务…',
    running: '运行中', paused: '已暂停', provisioning: '初始化中', start: '启动 Agent', stop: '暂停 Agent', refresh: '刷新',
    actions: '今日行动', tokens: '今日 Token', status: '运行状态', keyHint: '只写密钥 · 服务端加密保存',
  },
} as const

function errorText(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason)
}

export function PersonalAgentRuntime({ token, passportReady }: Props) {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const [items, setItems] = useState<HostedAgentSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [form, setForm] = useState({
    display_name: '',
    goal: locale === 'en' ? 'Meet residents and build a stable, useful daily life in Simverse.' : '认识小镇居民，逐渐建立稳定而有益的日常生活。',
    base_url: 'https://api.openai.com/v1',
    api_key: '',
    model: 'gpt-4.1-mini',
  })

  const refresh = useCallback(async () => {
    if (!token || !passportReady) return
    setLoading(true); setError('')
    try { setItems((await listHostedAgents(token)).items) }
    catch (reason) { setError(errorText(reason)) }
    finally { setLoading(false) }
  }, [passportReady, token])

  useEffect(() => { void refresh() }, [refresh])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!token) return
    setBusy(true); setError('')
    try {
      await createHostedAgent(token, {
        request_id: crypto.randomUUID(),
        ...form,
        heartbeat_seconds: 30,
        action_interval_seconds: 60,
        daily_action_limit: 200,
        daily_token_limit: 200_000,
      })
      setForm((current) => ({ ...current, api_key: '' }))
      await refresh()
    } catch (reason) { setError(errorText(reason)) }
    finally { setBusy(false) }
  }

  const changeStatus = async (item: HostedAgentSummary, start: boolean) => {
    if (!token) return
    setBusy(true); setError('')
    try {
      if (start) await startHostedAgent(token, item.id)
      else await stopHostedAgent(token, item.id)
      await refresh()
    } catch (reason) { setError(errorText(reason)) }
    finally { setBusy(false) }
  }

  return (
    <section className="agent-runtime agent-studio-card">
      <div className="agent-runtime__heading"><div><p className="agent-studio-card__index">{copy.index}</p><h2>{copy.title}</h2><span>{copy.lead}</span></div><button className="secondary" type="button" onClick={() => void refresh()} disabled={!passportReady || loading || busy}>{copy.refresh}</button></div>
      {!passportReady ? <p className="agent-runtime__empty">{copy.unavailable}</p> : loading ? <p className="agent-runtime__empty">{copy.load}</p> : (
        <>
          {items.map((item) => {
            const running = item.desired_status === 'running'
            const status = item.runtime_status === 'provisioning' ? copy.provisioning : running ? copy.running : copy.paused
            return <article className="agent-runtime__item" key={item.id}>
              <div><strong>{item.display_name}</strong><span>{item.provider.model} · {item.provider.host ?? '—'}</span></div>
              <dl><div><dt>{copy.status}</dt><dd data-running={running}>{status}</dd></div><div><dt>{copy.actions}</dt><dd>{item.usage_today.actions}</dd></div><div><dt>{copy.tokens}</dt><dd>{item.usage_today.total_tokens.toLocaleString()}</dd></div></dl>
              <button type="button" onClick={() => void changeStatus(item, !running)} disabled={busy}>{running ? copy.stop : copy.start}</button>
            </article>
          })}
          {items.length === 0 && <form className="agent-runtime__form" onSubmit={(event) => void submit(event)}>
            <label><span>{copy.name}</span><input required minLength={1} maxLength={100} value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></label>
            <label><span>{copy.goal}</span><input required minLength={1} maxLength={400} value={form.goal} onChange={(event) => setForm({ ...form, goal: event.target.value })} /></label>
            <label><span>{copy.base}</span><input required type="url" value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} /></label>
            <label><span>{copy.model}</span><input required value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} /></label>
            <label><span>{copy.key}</span><input required type="password" autoComplete="off" minLength={8} value={form.api_key} onChange={(event) => setForm({ ...form, api_key: event.target.value })} /><small>{copy.keyHint}</small></label>
            <button type="submit" disabled={busy}>{busy ? copy.creating : copy.create}</button>
          </form>}
        </>
      )}
      {error && <p className="agent-runtime__error" role="alert">{error}</p>}
    </section>
  )
}
