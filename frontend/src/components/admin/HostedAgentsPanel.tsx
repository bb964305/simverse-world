import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  formatActorLocation,
  formatLocationName,
  hasProjectedCoordinates,
} from '../spectator/formatting'
import { SpectatorLegend, SpectatorMap } from '../spectator/SpectatorMap'
import {
  HOSTED_AGENT_LOG_LIMIT,
  createHostedAgent,
  getHostedAgentState,
  listHostedAgents,
  startHostedAgent,
  stopHostedAgent,
  updateHostedAgent,
  type HostedAgentLog,
  type HostedAgentState,
  type HostedAgentSummary,
} from '../../services/api'
import {
  HostedAgentEditor,
  type HostedAgentEditorSubmission,
} from './HostedAgentEditor'
import '../../styles/admin-hosted-agents.css'

const STATE_POLL_INTERVAL_MS = 2_000
const LIST_REFRESH_INTERVAL_MS = 15_000
const CREATE_REQUEST_STORAGE_KEY = 'simverse.hosted-agent.pending-create.v1'
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

const DESIRED_LABELS: Record<string, string> = {
  running: '保持在线',
  paused: '已暂停',
  stopped: '已暂停',
  disabled: '已停用',
}

const RUNTIME_LABELS: Record<string, string> = {
  provisioning: '初始化中',
  starting: '启动中',
  idle: '在线待机',
  claimed: '在线',
  running: '在线',
  stopping: '暂停中',
  stopped: '已暂停',
  backoff: '退避重试',
  budget_paused: '额度暂停',
  auth_blocked: '凭据需更新',
  error: '异常',
  disabled: '已停用',
}

function displayedRuntimeStatus(
  desiredStatus: string,
  runtimeStatus: string,
): { label: string; status: string } {
  if (desiredStatus === 'disabled') return { label: '已停用', status: 'disabled' }
  if (desiredStatus === 'paused' || desiredStatus === 'stopped') {
    return { label: '已暂停', status: 'paused' }
  }
  return {
    label: RUNTIME_LABELS[runtimeStatus] ?? runtimeStatus,
    status: runtimeStatus,
  }
}

const LOG_KIND_LABELS: Record<HostedAgentLog['kind'], string> = {
  system: '系统',
  observe: '观察',
  decision_summary: '决定',
  action: '行动',
  result: '结果',
  error: '错误',
}

function isAbortError(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === 'AbortError'
}

function readableTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date)
}

function formatCompactNumber(value: number): string {
  return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

function mergeLogs(current: HostedAgentLog[], incoming: HostedAgentLog[]): HostedAgentLog[] {
  const bySequence = new Map<number, HostedAgentLog>()
  current.forEach((entry) => bySequence.set(entry.seq, entry))
  incoming.forEach((entry) => bySequence.set(entry.seq, entry))
  return [...bySequence.values()]
    .sort((a, b) => a.seq - b.seq)
    .slice(-HOSTED_AGENT_LOG_LIMIT)
}

function stateFromSummary(summary: HostedAgentSummary): HostedAgentState {
  return { ...summary, snapshot: null, logs: [], log_cursor: 0 }
}

function summaryFromState(state: HostedAgentState): HostedAgentSummary {
  return {
    id: state.id,
    request_id: state.request_id,
    version: state.version,
    display_name: state.display_name,
    resident_slug: state.resident_slug,
    desired_status: state.desired_status,
    runtime_status: state.runtime_status,
    goal: state.goal,
    provider: state.provider,
    policy: state.policy,
    health: state.health,
    usage_today: state.usage_today,
    last_error_code: state.last_error_code,
    agent: state.agent,
  }
}

function mergeState(
  current: HostedAgentState | null,
  next: HostedAgentState,
): HostedAgentState {
  if (!current || current.id !== next.id) return next
  const nextHasPolicy = Object.values(next.policy).some((value) => value > 0)
  const nextHasUsage = Object.values(next.usage_today).some((value) => typeof value === 'number' && value > 0)
  return {
    ...next,
    version: next.version || current.version,
    display_name: next.display_name === '未命名居民' ? current.display_name : next.display_name,
    resident_slug: next.resident_slug ?? current.resident_slug,
    goal: next.goal || current.goal,
    provider: next.provider.model || next.provider.host || next.provider.key_configured
      ? { ...current.provider, ...next.provider }
      : current.provider,
    policy: nextHasPolicy ? next.policy : current.policy,
    health: {
      last_heartbeat_at: next.health.last_heartbeat_at ?? current.health.last_heartbeat_at,
      next_retry_at: next.health.next_retry_at ?? current.health.next_retry_at,
      consecutive_failures: next.health.consecutive_failures,
    },
    usage_today: nextHasUsage || next.usage_today.resets_at ? next.usage_today : current.usage_today,
    // The Hosted state projection always includes this nullable field. `null`
    // is an explicit recovery signal, not a missing partial-response value.
    last_error_code: next.last_error_code,
    agent: next.agent ?? current.agent,
    snapshot: next.snapshot ?? current.snapshot,
    logs: mergeLogs(current.logs, next.logs),
    log_cursor: Math.max(current.log_cursor, next.log_cursor),
  }
}

function readPendingCreateId(): string | null {
  try {
    const value = sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY)
    if (!value) return null
    if (UUID_PATTERN.test(value)) return value
    sessionStorage.removeItem(CREATE_REQUEST_STORAGE_KEY)
  } catch {
    // Browser storage is optional. The in-memory ref still protects retries in
    // this mounted panel and never contains provider configuration.
  }
  return null
}

function writePendingCreateId(value: string): void {
  try {
    sessionStorage.setItem(CREATE_REQUEST_STORAGE_KEY, value)
  } catch {
    // Do not fall back to storing any form or credential data.
  }
}

function removePendingCreateId(expected: string | null): void {
  try {
    if (!expected || sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY) === expected) {
      sessionStorage.removeItem(CREATE_REQUEST_STORAGE_KEY)
    }
  } catch {
    // Best effort only.
  }
}

function createRequestId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  const bytes = new Uint8Array(16)
  if (typeof globalThis.crypto?.getRandomValues === 'function') {
    globalThis.crypto.getRandomValues(bytes)
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function optimisticSummary(
  id: string,
  requestId: string,
  desiredStatus: string,
  runtimeStatus: string,
  submission: HostedAgentEditorSubmission,
): HostedAgentSummary {
  const host = submission.base_url ? new URL(submission.base_url).host : null
  return {
    id,
    request_id: requestId,
    version: 0,
    display_name: submission.display_name,
    resident_slug: null,
    desired_status: desiredStatus,
    runtime_status: runtimeStatus,
    goal: submission.goal,
    provider: {
      host,
      base_url: null,
      model: submission.model,
      key_configured: Boolean(submission.api_key),
      key_updated_at: null,
    },
    policy: {
      heartbeat_seconds: submission.heartbeat_seconds,
      action_interval_seconds: submission.action_interval_seconds,
      daily_action_limit: submission.daily_action_limit,
      daily_token_limit: submission.daily_token_limit,
    },
    health: { last_heartbeat_at: null, next_retry_at: null, consecutive_failures: 0 },
    usage_today: {
      actions: 0,
      calls: 0,
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      estimated_cost_usd: null,
      resets_at: null,
    },
    last_error_code: null,
    agent: null,
  }
}

export function HostedAgentsPanel({ token }: { token: string }) {
  const [agents, setAgents] = useState<HostedAgentSummary[]>([])
  const [total, setTotal] = useState(0)
  const [selectedId, setSelectedId] = useState('')
  const [observedState, setObservedState] = useState<HostedAgentState | null>(null)
  const [editor, setEditor] = useState<{
    mode: 'create' | 'edit'
    initial: HostedAgentSummary | null
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [listError, setListError] = useState('')
  const [stateError, setStateError] = useState('')
  const [lifecycleBusy, setLifecycleBusy] = useState<'start' | 'stop' | null>(null)
  const [pendingCreate, setPendingCreate] = useState(() => Boolean(readPendingCreateId()))

  const agentsRef = useRef(agents)
  const listControllerRef = useRef<AbortController | null>(null)
  const stateControllerRef = useRef<AbortController | null>(null)
  const mutationControllerRef = useRef<AbortController | null>(null)
  const pollTimerRef = useRef<number | null>(null)
  const cursorRef = useRef(0)
  const pendingCreateIdRef = useRef<string | null>(readPendingCreateId())
  const lifecycleInFlightRef = useRef(false)

  agentsRef.current = agents

  const refreshList = useCallback(async (quiet = false) => {
    listControllerRef.current?.abort()
    const controller = new AbortController()
    listControllerRef.current = controller
    if (!quiet) setLoading(true)
    try {
      const response = await listHostedAgents(token, controller.signal)
      if (controller.signal.aborted) return
      const pendingRequestId = pendingCreateIdRef.current
      const recovered = pendingRequestId
        ? response.items.find((agent) => agent.request_id === pendingRequestId)
        : undefined
      if (recovered) {
        removePendingCreateId(pendingRequestId)
        pendingCreateIdRef.current = null
        setPendingCreate(false)
        setEditor(null)
      }
      setAgents(response.items)
      setTotal(response.total)
      setSelectedId((current) => {
        if (recovered) return recovered.id
        if (current && response.items.some((agent) => agent.id === current)) return current
        return response.items.find((agent) => agent.desired_status === 'running')?.id
          ?? response.items[0]?.id
          ?? ''
      })
      setListError('')
    } catch (reason) {
      if (!controller.signal.aborted && !isAbortError(reason)) {
        setListError(reason instanceof Error ? reason.message : '无法读取托管居民')
      }
    } finally {
      if (listControllerRef.current === controller) listControllerRef.current = null
      if (!controller.signal.aborted && !quiet) setLoading(false)
    }
  }, [token])

  useEffect(() => {
    void refreshList(false)
    const timer = window.setInterval(() => void refreshList(true), LIST_REFRESH_INTERVAL_MS)
    return () => {
      window.clearInterval(timer)
      listControllerRef.current?.abort()
      listControllerRef.current = null
    }
  }, [refreshList])

  useEffect(() => {
    if (!selectedId) {
      setObservedState(null)
      return
    }
    let disposed = false
    const seed = agentsRef.current.find((agent) => agent.id === selectedId)
    cursorRef.current = 0
    setObservedState(seed ? stateFromSummary(seed) : null)
    setStateError('')

    const schedule = () => {
      if (disposed) return
      pollTimerRef.current = window.setTimeout(() => {
        pollTimerRef.current = null
        void poll()
      }, STATE_POLL_INTERVAL_MS)
    }

    const poll = async () => {
      if (disposed) return
      stateControllerRef.current?.abort()
      const controller = new AbortController()
      stateControllerRef.current = controller
      try {
        const next = await getHostedAgentState(token, selectedId, cursorRef.current, controller.signal)
        if (disposed || controller.signal.aborted) return
        cursorRef.current = Math.max(cursorRef.current, next.log_cursor)
        setObservedState((current) => mergeState(current, next))
        setAgents((current) => current.map((agent) => {
          if (agent.id !== next.id) return agent
          const merged = mergeState(stateFromSummary(agent), next)
          return summaryFromState(merged)
        }))
        setStateError('')
      } catch (reason) {
        if (!disposed && !controller.signal.aborted && !isAbortError(reason)) {
          setStateError(reason instanceof Error ? reason.message : '无法刷新托管状态')
        }
      } finally {
        if (stateControllerRef.current === controller) stateControllerRef.current = null
      }
      schedule()
    }

    void poll()
    return () => {
      disposed = true
      stateControllerRef.current?.abort()
      stateControllerRef.current = null
      if (pollTimerRef.current !== null) {
        window.clearTimeout(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [selectedId, token])

  useEffect(() => () => {
    // Closing the page only stops browser I/O. It deliberately never invokes
    // the hosted Agent stop endpoint; desired_status lives on the server.
    mutationControllerRef.current?.abort()
  }, [])

  const clearPendingCreate = () => {
    removePendingCreateId(pendingCreateIdRef.current)
    pendingCreateIdRef.current = null
    setPendingCreate(false)
  }

  const createAgent = async (submission: HostedAgentEditorSubmission) => {
    if (!submission.base_url || !submission.api_key) throw new Error('创建居民需要 Base URL 和 API Key')
    const requestId = pendingCreateIdRef.current ?? createRequestId()
    pendingCreateIdRef.current = requestId
    writePendingCreateId(requestId)
    setPendingCreate(true)
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    try {
      const created = await createHostedAgent(token, {
        request_id: requestId,
        display_name: submission.display_name,
        base_url: submission.base_url,
        api_key: submission.api_key,
        model: submission.model,
        goal: submission.goal,
        heartbeat_seconds: submission.heartbeat_seconds,
        action_interval_seconds: submission.action_interval_seconds,
        daily_action_limit: submission.daily_action_limit,
        daily_token_limit: submission.daily_token_limit,
      }, controller.signal)
      if (controller.signal.aborted) return
      clearPendingCreate()
      const optimistic = optimisticSummary(
        created.id,
        requestId,
        created.desired_status,
        created.runtime_status,
        submission,
      )
      optimistic.version = created.version ?? 0
      setAgents((current) => [optimistic, ...current.filter((agent) => agent.id !== created.id)])
      setTotal((current) => Math.max(current + (agentsRef.current.some((agent) => agent.id === created.id) ? 0 : 1), 1))
      setSelectedId(created.id)
      setEditor(null)
      void refreshList(true)
    } finally {
      if (mutationControllerRef.current === controller) mutationControllerRef.current = null
    }
  }

  const updateAgent = async (submission: HostedAgentEditorSubmission) => {
    if (!editor?.initial) throw new Error('没有可编辑的托管居民')
    const target = editor.initial
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    try {
      const updated = await updateHostedAgent(token, target.id, {
        version: target.version,
        ...(submission.base_url ? { base_url: submission.base_url } : {}),
        ...(submission.api_key ? { api_key: submission.api_key } : {}),
        model: submission.model,
        goal: submission.goal,
        heartbeat_seconds: submission.heartbeat_seconds,
        action_interval_seconds: submission.action_interval_seconds,
        daily_action_limit: submission.daily_action_limit,
        daily_token_limit: submission.daily_token_limit,
      }, controller.signal)
      if (controller.signal.aborted) return
      setAgents((current) => current.map((agent) => agent.id === target.id ? {
        ...agent,
        version: updated.version ?? agent.version,
        goal: submission.goal,
        desired_status: updated.desired_status,
        runtime_status: updated.runtime_status,
        provider: {
          ...agent.provider,
          base_url: submission.base_url ?? agent.provider.base_url,
          host: submission.base_url ? new URL(submission.base_url).host : agent.provider.host,
          model: submission.model,
          key_configured: agent.provider.key_configured || Boolean(submission.api_key),
        },
        policy: {
          heartbeat_seconds: submission.heartbeat_seconds,
          action_interval_seconds: submission.action_interval_seconds,
          daily_action_limit: submission.daily_action_limit,
          daily_token_limit: submission.daily_token_limit,
        },
      } : agent))
      setEditor(null)
      void refreshList(true)
    } finally {
      if (mutationControllerRef.current === controller) mutationControllerRef.current = null
    }
  }

  const changeLifecycle = async (operation: 'start' | 'stop') => {
    if (!selectedId || lifecycleInFlightRef.current) return
    lifecycleInFlightRef.current = true
    setLifecycleBusy(operation)
    setStateError('')
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    try {
      const changed = operation === 'start'
        ? await startHostedAgent(token, selectedId, controller.signal)
        : await stopHostedAgent(token, selectedId, controller.signal)
      if (controller.signal.aborted) return
      setAgents((current) => current.map((agent) => agent.id === selectedId ? {
        ...agent,
        version: changed.version ?? agent.version,
        desired_status: changed.desired_status,
        runtime_status: changed.runtime_status,
      } : agent))
      setObservedState((current) => current?.id === selectedId ? {
        ...current,
        version: changed.version ?? current.version,
        desired_status: changed.desired_status,
        runtime_status: changed.runtime_status,
      } : current)
      void refreshList(true)
    } catch (reason) {
      if (!controller.signal.aborted && !isAbortError(reason)) {
        setStateError(reason instanceof Error ? reason.message : '托管状态切换失败')
      }
    } finally {
      if (mutationControllerRef.current === controller) mutationControllerRef.current = null
      lifecycleInFlightRef.current = false
      if (!controller.signal.aborted) setLifecycleBusy(null)
    }
  }

  const selectedSummary = agents.find((agent) => agent.id === selectedId) ?? null
  const selected = observedState?.id === selectedId
    ? observedState
    : selectedSummary
      ? stateFromSummary(selectedSummary)
      : null
  const snapshot = selected?.snapshot ?? null
  const visibleActors = useMemo(() => {
    if (!snapshot) return []
    return [snapshot.self, ...snapshot.nearby.residents, ...snapshot.nearby.players]
      .filter(hasProjectedCoordinates)
  }, [snapshot])
  const location = snapshot
    ? formatLocationName(snapshot.location, formatActorLocation(snapshot.self))
    : '等待位置'
  const desiredRunning = selected?.desired_status === 'running'
  const disabled = selected?.desired_status === 'disabled'
  const selectedRuntimeDisplay = selected
    ? displayedRuntimeStatus(selected.desired_status, selected.runtime_status)
    : null
  const actionRatio = selected?.policy.daily_action_limit
    ? Math.min(100, (selected.usage_today.actions / selected.policy.daily_action_limit) * 100)
    : 0
  const tokenRatio = selected?.policy.daily_token_limit
    ? Math.min(100, (selected.usage_today.total_tokens / selected.policy.daily_token_limit) * 100)
    : 0

  if (editor) {
    return (
      <div className="hosted-agents-panel">
        <HostedAgentEditor
          mode={editor.mode}
          initial={editor.initial}
          pendingCreate={pendingCreate}
          onCancel={() => setEditor(null)}
          onSubmit={editor.mode === 'create' ? createAgent : updateAgent}
        />
      </div>
    )
  }

  return (
    <div className="hosted-agents-panel">
      <section className="hosted-agent-persistence" aria-label="托管说明">
        <span aria-hidden="true">●</span>
        <div>
          <strong>由 Simverse 后台持续托管</strong>
          <p>只要小镇服务运行且居民设为“保持在线”，关闭、刷新或切换此页面都不会停止它；服务重启后会按期望状态恢复。</p>
        </div>
      </section>

      <div className="hosted-agent-toolbar">
        <div>
          <p>HOSTED RESIDENTS</p>
          <h2>常驻 Agent 居民</h2>
          <span>共 {total} 位</span>
        </div>
        <div>
          <button type="button" className="hosted-agent-secondary" onClick={() => void refreshList(false)} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button type="button" className="hosted-agent-primary" onClick={() => setEditor({ mode: 'create', initial: null })}>
            + 新建居民
          </button>
        </div>
      </div>

      {listError && <div className="hosted-agent-error" role="alert">{listError}</div>}

      <div className="hosted-agent-workspace">
        <aside className="hosted-agent-list" aria-label="托管居民列表">
          {loading && agents.length === 0 ? (
            <div className="hosted-agent-empty">读取托管居民…</div>
          ) : agents.length === 0 ? (
            <div className="hosted-agent-empty">
              <strong>还没有常驻居民</strong>
              <span>创建后由后台持续保活。</span>
            </div>
          ) : agents.map((agent) => {
            const runtimeDisplay = displayedRuntimeStatus(agent.desired_status, agent.runtime_status)
            return (
              <button
                type="button"
                key={agent.id}
                className={`hosted-agent-list-item${agent.id === selectedId ? ' hosted-agent-list-item--selected' : ''}`}
                onClick={() => setSelectedId(agent.id)}
                aria-pressed={agent.id === selectedId}
              >
                <span className="hosted-agent-list-avatar" aria-hidden="true">{agent.display_name.slice(0, 1)}</span>
                <span className="hosted-agent-list-copy">
                  <strong>{agent.display_name}</strong>
                  <small>{agent.provider.model || '等待模型'} · {runtimeDisplay.label}</small>
                </span>
                <i data-status={runtimeDisplay.status} aria-hidden="true" />
              </button>
            )
          })}
        </aside>

        <section className="hosted-agent-detail">
          {!selected ? (
            <div className="hosted-agent-detail-empty">
              <strong>选择一位托管居民</strong>
              <span>可查看地图、行动日志、当日用量与运行健康。</span>
            </div>
          ) : (
            <>
              <header className="hosted-agent-detail-header">
                <span className="hosted-agent-detail-avatar" aria-hidden="true">{selected.display_name.slice(0, 1)}</span>
                <div className="hosted-agent-detail-identity">
                  <p>HOSTED AGENT · {selected.id.slice(0, 8)}</p>
                  <h2>{selected.agent?.name || selected.display_name}</h2>
                  <span>{selected.provider.model || '等待模型信息'} · {location}</span>
                </div>
                <div className="hosted-agent-statuses">
                  <span data-kind="desired" data-status={selected.desired_status}>{DESIRED_LABELS[selected.desired_status] ?? selected.desired_status}</span>
                  <span data-kind="runtime" data-status={selectedRuntimeDisplay?.status}>{selectedRuntimeDisplay?.label}</span>
                </div>
                <div className="hosted-agent-detail-actions">
                  <button type="button" className="hosted-agent-secondary" onClick={() => setEditor({ mode: 'edit', initial: selected })}>编辑配置</button>
                  {desiredRunning ? (
                    <button type="button" className="hosted-agent-stop" onClick={() => void changeLifecycle('stop')} disabled={lifecycleBusy !== null}>
                      {lifecycleBusy === 'stop' ? '暂停中…' : '暂停托管'}
                    </button>
                  ) : (
                    <button type="button" className="hosted-agent-primary" onClick={() => void changeLifecycle('start')} disabled={lifecycleBusy !== null || disabled}>
                      {lifecycleBusy === 'start' ? '启动中…' : disabled ? '已停用' : '恢复托管'}
                    </button>
                  )}
                </div>
              </header>

              <dl className="hosted-agent-metrics">
                <div><dt>今日动作</dt><dd>{selected.usage_today.actions.toLocaleString()} / {selected.policy.daily_action_limit.toLocaleString()}</dd></div>
                <div><dt>今日 Token</dt><dd>{formatCompactNumber(selected.usage_today.total_tokens)} / {formatCompactNumber(selected.policy.daily_token_limit)}</dd></div>
                <div><dt>模型调用</dt><dd>{selected.usage_today.calls.toLocaleString()}</dd></div>
                <div><dt>估算费用</dt><dd>{selected.usage_today.estimated_cost_usd === null ? '未定价' : `$${selected.usage_today.estimated_cost_usd.toFixed(4)}`}</dd></div>
              </dl>

              <div className="hosted-agent-budget-bars">
                <div>
                  <span><b>动作额度</b><i>{actionRatio.toFixed(0)}%</i></span>
                  <progress max={100} value={actionRatio} />
                </div>
                <div>
                  <span><b>Token 额度</b><i>{tokenRatio.toFixed(0)}%</i></span>
                  <progress max={100} value={tokenRatio} />
                </div>
                <p>余额重置：{readableTime(selected.usage_today.resets_at)} · 任意 OpenAI-compatible 模型无法可靠推断单价，未定价时仅 Token 与动作上限为硬限。</p>
              </div>

              {(stateError || selected.last_error_code) && (
                <div className="hosted-agent-notice" role={stateError ? 'alert' : 'status'}>
                  {stateError || `运行状态：${selected.last_error_code}`}
                  {selected.health.next_retry_at ? ` · 下次重试 ${readableTime(selected.health.next_retry_at)}` : ''}
                </div>
              )}

              <div className="hosted-agent-observer-grid">
                <section className="hosted-agent-map-card">
                  <div className="hosted-agent-card-heading">
                    <div><p>LIVE MAP</p><h3>居民附近视野</h3></div>
                    <SpectatorLegend />
                  </div>
                  {snapshot ? (
                    <SpectatorMap actors={visibleActors} focusSlug={snapshot.self.slug} label={`${selected.display_name} 的托管视野`} />
                  ) : (
                    <div className="hosted-agent-placeholder">等待后台返回首次安全观察…</div>
                  )}
                </section>

                <section className="hosted-agent-log-card">
                  <div className="hosted-agent-card-heading">
                    <div><p>ACTIVITY LOG</p><h3>结构化行动日志</h3></div>
                    <span>{selected.logs.length}</span>
                  </div>
                  {selected.logs.length ? (
                    <ol className="hosted-agent-log" aria-label="Agent 行动日志" aria-live="polite">
                      {selected.logs.map((entry) => (
                        <li key={entry.seq}>
                          <div><time>{readableTime(entry.at)}</time><span data-kind={entry.kind}>{LOG_KIND_LABELS[entry.kind]}</span></div>
                          <p>{entry.summary}</p>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <div className="hosted-agent-placeholder">托管 worker 开始行动后会在此显示脱敏日志。</div>
                  )}
                </section>
              </div>

              <footer className="hosted-agent-footnote">
                当前页面每 2 秒读取一次所选居民的脱敏状态；关闭页面只停止读取，不会停止后台托管。
              </footer>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
