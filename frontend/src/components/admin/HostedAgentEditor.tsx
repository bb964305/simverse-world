import { useEffect, useState, type FormEvent } from 'react'
import type { HostedAgentSummary } from '../../services/api'

export interface HostedAgentEditorSubmission {
  display_name: string
  base_url?: string
  api_key?: string
  model: string
  goal: string
  heartbeat_seconds: number
  action_interval_seconds: number
  daily_action_limit: number
  daily_token_limit: number
}

interface HostedAgentEditorProps {
  mode: 'create' | 'edit'
  initial: HostedAgentSummary | null
  pendingCreate: boolean
  onCancel: () => void
  onSubmit: (submission: HostedAgentEditorSubmission) => Promise<void>
}

function normalizeOpenAIBaseUrl(value: string): string {
  let parsed: URL
  try {
    parsed = new URL(value.trim())
  } catch {
    throw new Error('Base URL 不是有效网址')
  }
  if (parsed.protocol !== 'https:') throw new Error('Base URL 只支持 HTTPS')
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('Base URL 不能包含凭据、查询参数或片段')
  }
  parsed.pathname = parsed.pathname.replace(/\/+$/, '') || '/'
  if (!parsed.pathname.endsWith('/v1')) throw new Error('Base URL 需以 /v1 结尾')
  return parsed.toString().replace(/\/$/, '')
}

function positiveInteger(value: string, label: string): number {
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < 1) throw new Error(`${label}必须是正整数`)
  return parsed
}

export function HostedAgentEditor({
  mode,
  initial,
  pendingCreate,
  onCancel,
  onSubmit,
}: HostedAgentEditorProps) {
  const [displayName, setDisplayName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [goal, setGoal] = useState('在小镇中自然生活，观察环境并主动认识居民')
  const [heartbeatSeconds, setHeartbeatSeconds] = useState('30')
  const [actionIntervalSeconds, setActionIntervalSeconds] = useState('30')
  const [dailyActionLimit, setDailyActionLimit] = useState('200')
  const [dailyTokenLimit, setDailyTokenLimit] = useState('200000')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    setDisplayName(initial?.display_name ?? '')
    // Some deployments intentionally return only the provider host. In that
    // case the edit field remains empty and omission means "keep existing".
    setBaseUrl(initial?.provider.base_url ?? '')
    setApiKey('')
    setModel(initial?.provider.model ?? '')
    setGoal(initial?.goal || '在小镇中自然生活，观察环境并主动认识居民')
    setHeartbeatSeconds(String(initial?.policy.heartbeat_seconds || 30))
    setActionIntervalSeconds(String(initial?.policy.action_interval_seconds || 30))
    setDailyActionLimit(String(initial?.policy.daily_action_limit || 200))
    setDailyTokenLimit(String(initial?.policy.daily_token_limit || 200_000))
    setSaving(false)
    setError('')
  }, [initial, mode])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (saving) return
    setError('')
    try {
      const name = displayName.trim()
      const key = apiKey.trim()
      const chosenModel = model.trim()
      const chosenGoal = goal.trim()
      const rawBaseUrl = baseUrl.trim()
      if (!name) throw new Error('请填写居民名称')
      if (mode === 'create' && !rawBaseUrl) throw new Error('请填写 Base URL')
      if (mode === 'create' && !key) throw new Error('请填写 API Key')
      if (!chosenModel) throw new Error('请填写模型名称')
      if (!chosenGoal) throw new Error('请填写居民长期目标')
      if (chosenGoal.length > 400) throw new Error('居民长期目标不能超过 400 个字符')

      const submission: HostedAgentEditorSubmission = {
        display_name: name,
        model: chosenModel,
        goal: chosenGoal,
        heartbeat_seconds: positiveInteger(heartbeatSeconds, '心跳间隔'),
        action_interval_seconds: positiveInteger(actionIntervalSeconds, '动作间隔'),
        daily_action_limit: positiveInteger(dailyActionLimit, '每日动作上限'),
        daily_token_limit: positiveInteger(dailyTokenLimit, '每日 Token 上限'),
      }
      if (submission.heartbeat_seconds < 15 || submission.heartbeat_seconds > 60) {
        throw new Error('心跳间隔必须在 15–60 秒之间')
      }
      if (submission.action_interval_seconds < 5 || submission.action_interval_seconds > 3_600) {
        throw new Error('动作间隔必须在 5–3600 秒之间')
      }
      if (submission.daily_action_limit > 1_000) {
        throw new Error('每日动作上限必须在 1–1000 之间')
      }
      if (submission.daily_token_limit < 1_000 || submission.daily_token_limit > 10_000_000) {
        throw new Error('每日 Token 上限必须在 1000–10000000 之间')
      }
      if (rawBaseUrl) submission.base_url = normalizeOpenAIBaseUrl(rawBaseUrl)
      if (key) submission.api_key = key

      setSaving(true)
      await onSubmit(submission)
      // Credentials are write-only and leave the live DOM immediately after a
      // successful create or rotation.
      setApiKey('')
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '保存失败'
      setError(apiKey ? message.replaceAll(apiKey, '[REDACTED]') : message)
    } finally {
      setSaving(false)
    }
  }

  const providerHint = initial?.provider.host
    ? `已配置 ${initial.provider.host}；留空则不修改`
    : '例如 https://api.openai.com/v1'

  return (
    <section className="hosted-agent-editor" aria-labelledby="hosted-agent-editor-title">
      <header className="hosted-agent-section-heading">
        <div>
          <p>HOSTED RESIDENT CONFIG</p>
          <h2 id="hosted-agent-editor-title">
            {mode === 'create' ? '新建常驻 Agent 居民' : `编辑 ${initial?.display_name ?? 'Agent'}`}
          </h2>
        </div>
        <button type="button" onClick={onCancel} disabled={saving}>取消</button>
      </header>

      <form className="hosted-agent-form" onSubmit={submit}>
        {mode === 'create' && pendingCreate && (
          <div className="hosted-agent-info hosted-agent-form__wide" role="status">
            上次创建结果尚未确认。系统会先通过列表自动恢复；如仍未找到，请使用与上次完全相同的配置和 API Key 重试。
          </div>
        )}

        <label>
          <span>居民名称</span>
          <input
            aria-label="居民名称"
            name="display_name"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            maxLength={100}
            disabled={saving || mode === 'edit'}
            required
          />
          {mode === 'edit' && <small>居民身份已注册，名称与社会关系保持稳定。</small>}
        </label>
        <label>
          <span>模型</span>
          <input
            aria-label="模型"
            name="model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="gpt-4.1-mini"
            autoCapitalize="none"
            autoComplete="off"
            spellCheck={false}
            maxLength={100}
            disabled={saving}
            required
          />
        </label>
        <label className="hosted-agent-form__wide">
          <span>OpenAI-compatible Base URL</span>
          <input
            aria-label="OpenAI-compatible Base URL"
            name="base_url"
            type="url"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
            placeholder={providerHint}
            autoCapitalize="none"
            autoComplete="off"
            spellCheck={false}
            maxLength={1_000}
            disabled={saving}
            required={mode === 'create'}
          />
          <small>仅支持 HTTPS 且需填写到 <code>/v1</code>；域名还需由管理员加入服务端允许列表。编辑时留空表示保留已配置地址。</small>
        </label>
        <label className="hosted-agent-form__wide">
          <span>API Key</span>
          <input
            aria-label="API Key"
            name="api_key"
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={mode === 'edit' && initial?.provider.key_configured ? '已加密保存 · 留空不修改' : 'sk-••••••••'}
            autoCapitalize="none"
            autoComplete="new-password"
            spellCheck={false}
            maxLength={2_000}
            disabled={saving}
            required={mode === 'create'}
          />
          <small className="hosted-agent-secret-note">
            API Key 由服务端主密钥加密保存，仅托管 worker 运行时解密；不会回传到页面、写入浏览器存储或日志。编辑时留空不修改现有密钥。
          </small>
          <small>为了替居民决策，所选模型服务会收到该居民的当前观察、公开身份与发给它的镇内消息；不会收到其他人的非相关私信。</small>
        </label>
        <label className="hosted-agent-form__wide">
          <span>居民长期目标</span>
          <textarea
            aria-label="居民长期目标"
            name="goal"
            value={goal}
            onChange={(event) => setGoal(event.target.value)}
            maxLength={400}
            rows={3}
            disabled={saving}
            required
          />
          <small>这是居民的公开角色目标，不要填写密钥或私密信息。</small>
        </label>

        <fieldset className="hosted-agent-policy hosted-agent-form__wide">
          <legend>保活与每日硬限</legend>
          <label>
            <span>心跳（秒）</span>
            <input type="number" min={15} max={60} step={1} value={heartbeatSeconds} onChange={(event) => setHeartbeatSeconds(event.target.value)} disabled={saving} required />
          </label>
          <label>
            <span>动作间隔（秒）</span>
            <input type="number" min={5} max={3600} step={1} value={actionIntervalSeconds} onChange={(event) => setActionIntervalSeconds(event.target.value)} disabled={saving} required />
          </label>
          <label>
            <span>每日动作上限</span>
            <input type="number" min={1} max={1000} step={1} value={dailyActionLimit} onChange={(event) => setDailyActionLimit(event.target.value)} disabled={saving} required />
          </label>
          <label>
            <span>每日 Token 上限</span>
            <input type="number" min={1000} max={10000000} step={1} value={dailyTokenLimit} onChange={(event) => setDailyTokenLimit(event.target.value)} disabled={saving} required />
          </label>
        </fieldset>

        {error && <div className="hosted-agent-error hosted-agent-form__wide" role="alert">{error}</div>}
        <div className="hosted-agent-form__actions hosted-agent-form__wide">
          <button type="submit" disabled={saving}>
            {saving ? '保存中…' : mode === 'create' ? '创建并保持在线' : '保存配置'}
          </button>
          <span>{mode === 'edit' ? '新配置由后台安全重载，不会创建新居民。' : '创建响应返回后，后台会继续完成身份初始化和连接。'}</span>
        </div>
      </form>
    </section>
  )
}
