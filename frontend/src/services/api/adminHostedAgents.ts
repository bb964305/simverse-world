import type {
  SpectatorActor,
  SpectatorActorKind,
  ViewerAgent,
  ViewerLocation,
  ViewerSnapshot,
} from '../spectator'
import { apiFetch } from './core'

export const HOSTED_AGENT_LOG_LIMIT = 500

export interface HostedAgentCreateRequest {
  request_id: string
  display_name: string
  base_url: string
  api_key: string
  model: string
  goal: string
  heartbeat_seconds: number
  action_interval_seconds: number
  daily_action_limit: number
  daily_token_limit: number
}

export interface HostedAgentPatchRequest {
  version: number
  base_url?: string
  api_key?: string
  model?: string
  goal?: string
  heartbeat_seconds?: number
  action_interval_seconds?: number
  daily_action_limit?: number
  daily_token_limit?: number
}

export interface HostedAgentProvider {
  host: string | null
  base_url: string | null
  model: string
  key_configured: boolean
  key_updated_at: string | null
}

export interface HostedAgentPolicy {
  heartbeat_seconds: number
  action_interval_seconds: number
  daily_action_limit: number
  daily_token_limit: number
}

export interface HostedAgentHealth {
  last_heartbeat_at: string | null
  next_retry_at: string | null
  consecutive_failures: number
}

export interface HostedAgentUsage {
  actions: number
  calls: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  estimated_cost_usd: number | null
  resets_at: string | null
}

export interface HostedAgentSummary {
  id: string
  request_id: string | null
  version: number
  display_name: string
  resident_slug: string | null
  desired_status: string
  runtime_status: string
  goal: string
  provider: HostedAgentProvider
  policy: HostedAgentPolicy
  health: HostedAgentHealth
  usage_today: HostedAgentUsage
  last_error_code: string | null
  agent: ViewerAgent | null
}

export type HostedAgentLogKind =
  | 'system'
  | 'observe'
  | 'decision_summary'
  | 'action'
  | 'result'
  | 'error'

export interface HostedAgentLog {
  seq: number
  at: string | null
  kind: HostedAgentLogKind
  summary: string
}

export interface HostedAgentState extends HostedAgentSummary {
  snapshot: ViewerSnapshot | null
  logs: HostedAgentLog[]
  log_cursor: number
}

export interface HostedAgentListResponse {
  items: HostedAgentSummary[]
  total: number
}

export interface HostedAgentMutationResponse {
  id: string
  version?: number
  desired_status: string
  runtime_status: string
}

const LOG_KIND_ALIASES: Record<string, HostedAgentLogKind> = {
  system: 'system',
  observe: 'observe',
  observation: 'observe',
  decision_summary: 'decision_summary',
  action: 'action',
  result: 'result',
  action_result: 'result',
  error: 'error',
}

const LOG_SUMMARY_FALLBACKS: Record<HostedAgentLogKind, string> = {
  system: '运行状态已更新',
  observe: '已完成一次观察',
  decision_summary: '已生成一次行动决定',
  action: '已提交一次行动',
  result: '已完成一次行动',
  error: '运行步骤未完成',
}

function recordOf(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function safeString(value: unknown, maxLength: number): string | null {
  if (typeof value !== 'string') return null
  const cleaned = value.trim()
  return cleaned ? cleaned.slice(0, maxLength) : null
}

function safeNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function safeInteger(value: unknown, fallback = 0): number {
  const number = safeNumber(value, fallback)
  return Number.isInteger(number) ? number : fallback
}

function safeBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback
}

function actorKind(value: unknown, fallback: SpectatorActorKind): SpectatorActorKind {
  return value === 'npc' || value === 'agent' || value === 'human' ? value : fallback
}

function normalizeActor(value: unknown, fallbackKind: SpectatorActorKind): SpectatorActor | null {
  const raw = recordOf(value)
  if (!raw) return null
  const slug = safeString(raw.slug, 100)
  const name = safeString(raw.name, 100)
  if (!slug || !name) return null
  return {
    slug,
    name,
    kind: actorKind(raw.kind, fallbackKind),
    status: safeString(raw.status, 100) ?? 'unknown',
    district: safeString(raw.district, 100),
    tile_x: typeof raw.tile_x === 'number' && Number.isFinite(raw.tile_x) ? raw.tile_x : null,
    tile_y: typeof raw.tile_y === 'number' && Number.isFinite(raw.tile_y) ? raw.tile_y : null,
    sprite_key: safeString(raw.sprite_key, 100),
    portrait_url: safeString(raw.portrait_url, 1_000),
    is_online: typeof raw.is_online === 'boolean' ? raw.is_online : undefined,
  }
}

function normalizeViewerAgent(value: unknown): ViewerAgent | null {
  const raw = recordOf(value)
  if (!raw) return null
  const resident = recordOf(raw.resident)
  const slug = safeString(raw.slug ?? resident?.slug, 100)
  const name = safeString(raw.name ?? resident?.name, 100)
  if (!slug || !name) return null
  return {
    slug,
    name,
    status: safeString(raw.status, 100) ?? 'unknown',
    model_label: safeString(raw.model_label, 100),
    current_goal: safeString(raw.current_goal, 400),
    is_online: typeof raw.is_online === 'boolean' ? raw.is_online : undefined,
  }
}

function normalizeLocation(value: unknown): ViewerLocation | string | null {
  const text = safeString(value, 100)
  if (text) return text
  const raw = recordOf(value)
  if (!raw) return null
  const slug = safeString(raw.slug, 100)
  const name = safeString(raw.name, 100)
  return slug && name ? { slug, name } : null
}

function normalizeSnapshot(value: unknown, fallbackAgent: ViewerAgent | null): ViewerSnapshot | null {
  const raw = recordOf(value)
  if (!raw) return null
  const agent = normalizeViewerAgent(raw.agent) ?? fallbackAgent
  const self = normalizeActor(raw.self, 'agent')
  const nearby = recordOf(raw.nearby)
  if (!agent || !self || !nearby) return null
  const residents = Array.isArray(nearby.residents)
    ? nearby.residents.map((actor) => normalizeActor(actor, 'npc')).filter((actor): actor is SpectatorActor => actor !== null)
    : []
  const players = Array.isArray(nearby.players)
    ? nearby.players.map((actor) => normalizeActor(actor, 'human')).filter((actor): actor is SpectatorActor => actor !== null)
    : []
  const events = Array.isArray(raw.recent_events)
    ? raw.recent_events.flatMap((event) => {
      const item = recordOf(event)
      const at = safeString(item?.at, 100)
      const summary = safeString(item?.summary, 2_000)
      return at && summary ? [{ at, summary }] : []
    })
    : undefined
  return {
    generated_at: safeString(raw.generated_at, 100) ?? '',
    agent,
    self,
    nearby: { residents, players },
    location: normalizeLocation(raw.location),
    recent_events: events,
  }
}

export function normalizeHostedAgentLogs(value: unknown): HostedAgentLog[] {
  if (!Array.isArray(value)) return []
  const normalized: HostedAgentLog[] = []
  for (const valueItem of value) {
    const item = recordOf(valueItem)
    if (!item) continue
    const seq = safeNumber(item.seq ?? item.sequence, Number.NaN)
    const rawKind = safeString(item.kind ?? item.type, 50)
    const kind = rawKind ? LOG_KIND_ALIASES[rawKind] : undefined
    if (!Number.isInteger(seq) || seq < 0 || !kind) continue
    const summary = safeString(item.summary ?? item.message, 2_000)
      ?? LOG_SUMMARY_FALLBACKS[kind]
    normalized.push({
      seq,
      at: safeString(item.at ?? item.created_at, 100),
      kind,
      summary,
    })
  }
  return normalized.slice(-HOSTED_AGENT_LOG_LIMIT)
}

function normalizeUsage(value: unknown): HostedAgentUsage {
  const raw = recordOf(value) ?? {}
  const inputTokens = safeInteger(raw.input_tokens)
  const outputTokens = safeInteger(raw.output_tokens)
  const reportedTotal = safeInteger(raw.total_tokens, inputTokens + outputTokens)
  const cost = raw.estimated_cost_usd ?? raw.est_cost_usd ?? raw.cost_usd
  return {
    actions: safeInteger(raw.actions ?? raw.action_count),
    calls: safeInteger(raw.calls ?? raw.llm_calls),
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    total_tokens: reportedTotal,
    estimated_cost_usd: typeof cost === 'number' && Number.isFinite(cost) ? cost : null,
    resets_at: safeString(raw.resets_at ?? raw.reset_at, 100),
  }
}

export function normalizeHostedAgentSummary(value: unknown): HostedAgentSummary | null {
  const root = recordOf(value)
  if (!root) return null
  const item = recordOf(root.hosted_agent ?? root.item) ?? root
  const provider = recordOf(item.provider) ?? {}
  const policy = recordOf(item.policy ?? item.limits) ?? {}
  const health = recordOf(item.health) ?? {}
  const usage = item.usage_today ?? item.usage
  const agent = normalizeViewerAgent(item.agent)
  const id = safeString(item.id ?? item.hosted_agent_id, 200)
  if (!id) return null
  return {
    id,
    request_id: safeString(item.request_id, 100),
    version: Math.max(0, safeInteger(item.version)),
    display_name: safeString(item.display_name ?? item.name, 100) ?? agent?.name ?? '未命名居民',
    resident_slug: safeString(item.resident_slug, 100) ?? agent?.slug ?? null,
    desired_status: safeString(item.desired_status ?? item.desired_state, 50) ?? 'paused',
    runtime_status: safeString(item.runtime_status ?? item.runtime_state ?? item.status, 50) ?? 'idle',
    goal: safeString(item.goal ?? agent?.current_goal, 400) ?? '',
    provider: {
      host: safeString(provider.host ?? item.provider_host, 255),
      base_url: safeString(provider.base_url ?? item.base_url, 1_000),
      model: safeString(provider.model ?? item.model, 100) ?? agent?.model_label ?? '',
      key_configured: safeBoolean(
        provider.key_configured ?? provider.credential_configured ?? item.key_configured,
      ),
      key_updated_at: safeString(provider.key_updated_at ?? item.key_updated_at, 100),
    },
    policy: {
      heartbeat_seconds: safeNumber(policy.heartbeat_seconds ?? item.heartbeat_seconds),
      action_interval_seconds: safeNumber(policy.action_interval_seconds ?? item.action_interval_seconds),
      daily_action_limit: safeInteger(policy.daily_action_limit ?? item.daily_action_limit),
      daily_token_limit: safeInteger(policy.daily_token_limit ?? item.daily_token_limit),
    },
    health: {
      last_heartbeat_at: safeString(health.last_heartbeat_at ?? item.last_heartbeat_at, 100),
      next_retry_at: safeString(health.next_retry_at ?? item.next_retry_at, 100),
      consecutive_failures: safeInteger(health.consecutive_failures ?? item.consecutive_failures),
    },
    usage_today: normalizeUsage(usage),
    last_error_code: safeString(item.last_error_code ?? item.stop_reason ?? item.error_code, 200),
    agent,
  }
}

export function normalizeHostedAgentState(value: unknown, fallbackId = ''): HostedAgentState {
  const root = recordOf(value) ?? {}
  const state = recordOf(root.state) ?? {}
  const hosted = recordOf(root.hosted_agent ?? root.item) ?? {}
  const combined = { ...hosted, ...root, ...state }
  // Remove wrapper keys after merging so a nested stale summary cannot hide
  // fresher desired/runtime values from the state envelope.
  const summaryFields: Record<string, unknown> = { ...combined }
  delete summaryFields.hosted_agent
  delete summaryFields.item
  delete summaryFields.state
  const summary = normalizeHostedAgentSummary({ ...summaryFields, id: summaryFields.id ?? fallbackId })
    ?? normalizeHostedAgentSummary({ id: fallbackId || 'unknown' })!
  const logs = normalizeHostedAgentLogs(root.logs ?? state.logs)
  const reportedCursor = safeInteger(root.log_cursor ?? root.turn_cursor ?? state.log_cursor)
  const highestSeq = logs.reduce((highest, entry) => Math.max(highest, entry.seq), 0)
  return {
    ...summary,
    snapshot: normalizeSnapshot(root.snapshot ?? state.snapshot, summary.agent),
    logs,
    log_cursor: Math.max(reportedCursor, highestSeq),
  }
}

function adminHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` }
}

function normalizeMutation(value: unknown, fallbackId = ''): HostedAgentMutationResponse {
  const raw = recordOf(value) ?? {}
  const item = recordOf(raw.hosted_agent ?? raw.item) ?? raw
  const id = safeString(item.id ?? item.hosted_agent_id, 200) ?? fallbackId
  if (!id) throw new Error('Hosted Agent 响应缺少 id')
  const version = safeInteger(item.version, -1)
  return {
    id,
    version: version >= 0 ? version : undefined,
    desired_status: safeString(item.desired_status ?? item.desired_state, 50) ?? 'running',
    runtime_status: safeString(item.runtime_status ?? item.runtime_state ?? item.status, 50) ?? 'provisioning',
  }
}

export async function listHostedAgents(
  token: string,
  signal?: AbortSignal,
): Promise<HostedAgentListResponse> {
  const raw = await apiFetch<unknown>('/admin/hosted-agents', {
    headers: adminHeaders(token),
    cache: 'no-store',
    signal,
  })
  const root = recordOf(raw)
  const rows = Array.isArray(raw)
    ? raw
    : Array.isArray(root?.items)
      ? root.items
      : Array.isArray(root?.agents)
        ? root.agents
        : []
  const items = rows
    .map(normalizeHostedAgentSummary)
    .filter((item): item is HostedAgentSummary => item !== null)
  return {
    items,
    total: Math.max(items.length, safeInteger(root?.total, items.length)),
  }
}

export async function createHostedAgent(
  token: string,
  request: HostedAgentCreateRequest,
  signal?: AbortSignal,
): Promise<HostedAgentMutationResponse> {
  const raw = await apiFetch<unknown>('/admin/hosted-agents', {
    method: 'POST',
    headers: adminHeaders(token),
    body: JSON.stringify(request),
    signal,
  })
  return normalizeMutation(raw)
}

export async function getHostedAgent(
  token: string,
  hostedAgentId: string,
  signal?: AbortSignal,
): Promise<HostedAgentSummary> {
  const raw = await apiFetch<unknown>(
    `/admin/hosted-agents/${encodeURIComponent(hostedAgentId)}`,
    { headers: adminHeaders(token), cache: 'no-store', signal },
  )
  const item = normalizeHostedAgentSummary(raw)
  if (!item) throw new Error('Hosted Agent 详情响应无效')
  return item
}

export async function updateHostedAgent(
  token: string,
  hostedAgentId: string,
  request: HostedAgentPatchRequest,
  signal?: AbortSignal,
): Promise<HostedAgentMutationResponse> {
  const raw = await apiFetch<unknown>(
    `/admin/hosted-agents/${encodeURIComponent(hostedAgentId)}`,
    {
      method: 'PATCH',
      headers: adminHeaders(token),
      body: JSON.stringify(request),
      signal,
    },
  )
  return normalizeMutation(raw, hostedAgentId)
}

async function changeHostedAgentLifecycle(
  token: string,
  hostedAgentId: string,
  operation: 'start' | 'stop',
  signal?: AbortSignal,
): Promise<HostedAgentMutationResponse> {
  const raw = await apiFetch<unknown>(
    `/admin/hosted-agents/${encodeURIComponent(hostedAgentId)}/${operation}`,
    { method: 'POST', headers: adminHeaders(token), signal },
  )
  return normalizeMutation(raw, hostedAgentId)
}

export function startHostedAgent(
  token: string,
  hostedAgentId: string,
  signal?: AbortSignal,
): Promise<HostedAgentMutationResponse> {
  return changeHostedAgentLifecycle(token, hostedAgentId, 'start', signal)
}

export function stopHostedAgent(
  token: string,
  hostedAgentId: string,
  signal?: AbortSignal,
): Promise<HostedAgentMutationResponse> {
  return changeHostedAgentLifecycle(token, hostedAgentId, 'stop', signal)
}

export async function getHostedAgentState(
  token: string,
  hostedAgentId: string,
  afterLogSeq: number,
  signal?: AbortSignal,
): Promise<HostedAgentState> {
  const query = new URLSearchParams({ after_log_seq: String(Math.max(0, afterLogSeq)) })
  const raw = await apiFetch<unknown>(
    `/admin/hosted-agents/${encodeURIComponent(hostedAgentId)}/state?${query.toString()}`,
    { headers: adminHeaders(token), cache: 'no-store', signal },
  )
  return normalizeHostedAgentState(raw, hostedAgentId)
}
