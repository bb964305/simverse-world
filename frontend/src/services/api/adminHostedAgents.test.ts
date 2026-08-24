import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createHostedAgent,
  getHostedAgentState,
  listHostedAgents,
  normalizeHostedAgentLogs,
  startHostedAgent,
  stopHostedAgent,
  updateHostedAgent,
} from './adminHostedAgents'
import { API_BASE } from './core'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('hosted Agent admin API', () => {
  it('normalizes list records without accepting provider credentials', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      items: [{
        id: 'host-1',
        request_id: '11111111-1111-4111-8111-111111111111',
        version: 3,
        display_name: '林舟',
        resident_slug: 'lin-zhou',
        desired_status: 'running',
        runtime_status: 'claimed',
        goal: '认识邻居',
        provider: {
          host: 'api.example.com',
          model: 'model-a',
          key_configured: true,
          api_key: 'must-not-cross-boundary',
          encrypted_key: 'ciphertext',
        },
        policy: {
          heartbeat_seconds: 30,
          action_interval_seconds: 30,
          daily_action_limit: 200,
          daily_token_limit: 200_000,
        },
        health: { last_heartbeat_at: '2026-08-15T01:00:00Z' },
        usage_today: { actions: 5, calls: 6, input_tokens: 100, output_tokens: 20 },
      }],
      total: 1,
    })))

    const response = await listHostedAgents('admin-token')

    expect(response.total).toBe(1)
    expect(response.items[0]).toMatchObject({
      id: 'host-1',
      request_id: '11111111-1111-4111-8111-111111111111',
      desired_status: 'running',
      runtime_status: 'claimed',
      provider: { host: 'api.example.com', model: 'model-a', key_configured: true },
      usage_today: { total_tokens: 120 },
    })
    expect(JSON.stringify(response)).not.toMatch(/must-not-cross-boundary|ciphertext|api_key|encrypted_key/)
  })

  it('submits a create UUID and provider key only in the POST body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      id: 'host-1',
      desired_status: 'running',
      runtime_status: 'provisioning',
    }, 202))
    vi.stubGlobal('fetch', fetchMock)

    await expect(createHostedAgent('admin-token', {
      request_id: '11111111-1111-4111-8111-111111111111',
      display_name: '林舟',
      base_url: 'https://api.example.com/v1',
      api_key: 'provider-secret',
      model: 'model-a',
      goal: '认识邻居',
      heartbeat_seconds: 30,
      action_interval_seconds: 30,
      daily_action_limit: 200,
      daily_token_limit: 200_000,
    })).resolves.toEqual({
      id: 'host-1',
      version: undefined,
      desired_status: 'running',
      runtime_status: 'provisioning',
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${API_BASE}/admin/hosted-agents`)
    expect(url).not.toContain('provider-secret')
    expect(init.method).toBe('POST')
    expect(init.headers).toEqual(expect.objectContaining({ Authorization: 'Bearer admin-token' }))
    expect(JSON.parse(String(init.body))).toMatchObject({
      request_id: '11111111-1111-4111-8111-111111111111',
      api_key: 'provider-secret',
      daily_action_limit: 200,
      daily_token_limit: 200_000,
    })
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('omits an unchanged write-only key from PATCH', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      id: 'host-1',
      version: 4,
      desired_status: 'running',
      runtime_status: 'claimed',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await updateHostedAgent('admin-token', 'host/1', {
      version: 3,
      model: 'model-b',
      goal: '继续生活',
      heartbeat_seconds: 30,
      action_interval_seconds: 60,
      daily_action_limit: 100,
      daily_token_limit: 100_000,
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${API_BASE}/admin/hosted-agents/host%2F1`)
    const body = JSON.parse(String(init.body)) as Record<string, unknown>
    expect(body).not.toHaveProperty('api_key')
    expect(body).not.toHaveProperty('display_name')
    expect(body).not.toHaveProperty('base_url')
  })

  it('uses explicit idempotent lifecycle endpoints', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ id: 'host-1', desired_status: 'running', runtime_status: 'starting' }))
      .mockResolvedValueOnce(jsonResponse({ id: 'host-1', desired_status: 'paused', runtime_status: 'stopping' }))
    vi.stubGlobal('fetch', fetchMock)

    await startHostedAgent('admin-token', 'host-1')
    await stopHostedAgent('admin-token', 'host-1')

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${API_BASE}/admin/hosted-agents/host-1/start`,
      expect.objectContaining({ method: 'POST' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${API_BASE}/admin/hosted-agents/host-1/stop`,
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('reads cursor state and retains only safe snapshot and log fields', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      id: 'host-1',
      desired_status: 'running',
      runtime_status: 'claimed',
      agent: { slug: 'lin-zhou', name: '林舟', status: 'active', model_label: 'model-a' },
      snapshot: {
        generated_at: '2026-08-15T01:00:00Z',
        agent: { slug: 'lin-zhou', name: '林舟', status: 'active' },
        self: { slug: 'lin-zhou', name: '林舟', kind: 'agent', status: 'active', district: 'central_plaza', tile_x: 70, tile_y: 50, secret: 'drop' },
        nearby: { residents: [], players: [] },
        location: { slug: 'central_plaza', name: '中心广场' },
        raw_observation: 'drop me',
      },
      logs: [
        { seq: 7, kind: 'observe', summary: '看见广场。', raw_prompt: 'drop' },
        { seq: 8, kind: 'chain_of_thought', summary: '隐藏推理' },
        { seq: 9, kind: 'action_result', message: '向东移动。', api_key: 'drop' },
      ],
      log_cursor: 9,
      usage_today: { actions: 2, input_tokens: 100, output_tokens: 20 },
    }))
    vi.stubGlobal('fetch', fetchMock)

    const state = await getHostedAgentState('admin-token', 'host/1', 6)

    expect(fetchMock.mock.calls[0][0]).toBe(
      `${API_BASE}/admin/hosted-agents/host%2F1/state?after_log_seq=6`,
    )
    expect(state.logs).toEqual([
      { seq: 7, at: null, kind: 'observe', summary: '看见广场。' },
      { seq: 9, at: null, kind: 'result', summary: '向东移动。' },
    ])
    expect(JSON.stringify(state)).not.toMatch(/raw_prompt|raw_observation|chain_of_thought|api_key|drop me/)
    expect(state.log_cursor).toBe(9)
    expect(state.snapshot?.self.tile_x).toBe(70)
  })
})

describe('hosted Agent log boundary', () => {
  it('caps logs at 500 and rejects malformed or unknown kinds', () => {
    const logs = Array.from({ length: 505 }, (_, seq) => ({ seq, kind: 'system', summary: `entry-${seq}` }))
    logs.push({ seq: 999, kind: 'unknown', summary: 'drop me' })

    const normalized = normalizeHostedAgentLogs(logs)

    expect(normalized).toHaveLength(500)
    expect(normalized[0].seq).toBe(5)
    expect(normalized.at(-1)?.seq).toBe(504)
  })

  it('keeps terminal rows with a safe local fallback when the server omits a summary', () => {
    expect(normalizeHostedAgentLogs([
      { seq: 7, kind: 'error', summary: null, private_message: '不得显示' },
      { seq: 8, kind: 'system' },
    ])).toEqual([
      { seq: 7, at: null, kind: 'error', summary: '运行步骤未完成' },
      { seq: 8, at: null, kind: 'system', summary: '运行状态已更新' },
    ])
  })
})
