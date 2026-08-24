import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  start: vi.fn(),
  stop: vi.fn(),
  state: vi.fn(),
}))

vi.mock('../../services/api', async () => {
  const actual = await vi.importActual<typeof import('../../services/api')>('../../services/api')
  return {
    ...actual,
    listHostedAgents: (...args: unknown[]) => mocks.list(...args),
    createHostedAgent: (...args: unknown[]) => mocks.create(...args),
    updateHostedAgent: (...args: unknown[]) => mocks.update(...args),
    startHostedAgent: (...args: unknown[]) => mocks.start(...args),
    stopHostedAgent: (...args: unknown[]) => mocks.stop(...args),
    getHostedAgentState: (...args: unknown[]) => mocks.state(...args),
  }
})

import { HostedAgentsPanel } from './HostedAgentsPanel'
import type { HostedAgentState, HostedAgentSummary } from '../../services/api'

const CREATE_REQUEST_STORAGE_KEY = 'simverse.hosted-agent.pending-create.v1'

const SNAPSHOT = {
  generated_at: '2026-08-15T01:00:00Z',
  agent: {
    slug: 'lin-zhou',
    name: '林舟',
    status: 'active',
    model_label: 'model-a',
    current_goal: '认识邻居',
    is_online: true,
  },
  self: {
    slug: 'lin-zhou',
    name: '林舟',
    kind: 'agent' as const,
    status: 'active',
    district: 'central_plaza',
    tile_x: 70,
    tile_y: 50,
  },
  nearby: {
    residents: [{
      slug: 'mei',
      name: '梅',
      kind: 'npc' as const,
      status: 'idle',
      district: 'central_plaza',
      tile_x: 71,
      tile_y: 50,
    }],
    players: [],
  },
  location: { slug: 'central_plaza', name: '中心广场' },
  recent_events: [],
}

function summary(overrides: Partial<HostedAgentSummary> = {}): HostedAgentSummary {
  return {
    id: 'host-1',
    request_id: null,
    version: 3,
    display_name: '林舟',
    resident_slug: 'lin-zhou',
    desired_status: 'running',
    runtime_status: 'claimed',
    goal: '认识邻居',
    provider: {
      host: 'api.example.com',
      base_url: null,
      model: 'model-a',
      key_configured: true,
      key_updated_at: '2026-08-15T00:00:00Z',
    },
    policy: {
      heartbeat_seconds: 30,
      action_interval_seconds: 30,
      daily_action_limit: 200,
      daily_token_limit: 200_000,
    },
    health: {
      last_heartbeat_at: '2026-08-15T01:00:00Z',
      next_retry_at: null,
      consecutive_failures: 0,
    },
    usage_today: {
      actions: 5,
      calls: 6,
      input_tokens: 1_000,
      output_tokens: 200,
      total_tokens: 1_200,
      estimated_cost_usd: null,
      resets_at: '2026-08-16T00:00:00Z',
    },
    last_error_code: null,
    agent: SNAPSHOT.agent,
    ...overrides,
  }
}

function state(overrides: Partial<HostedAgentState> = {}): HostedAgentState {
  return {
    ...summary(),
    snapshot: SNAPSHOT,
    logs: [
      { seq: 1, at: '2026-08-15T01:00:00Z', kind: 'observe', summary: '看见中心广场。' },
      { seq: 2, at: '2026-08-15T01:00:02Z', kind: 'action', summary: '向梅走近。' },
    ],
    log_cursor: 2,
    ...overrides,
  }
}

async function openCreateEditor() {
  fireEvent.click(await screen.findByRole('button', { name: /\+ 新建居民/ }))
  expect(await screen.findByRole('heading', { name: '新建常驻 Agent 居民' })).toBeTruthy()
}

function fillCreateForm(secret = 'provider-secret') {
  fireEvent.change(screen.getByRole('textbox', { name: '居民名称' }), { target: { value: '新居民' } })
  fireEvent.change(screen.getByRole('textbox', { name: '模型' }), { target: { value: 'model-b' } })
  fireEvent.change(screen.getByRole('textbox', { name: /OpenAI-compatible Base URL/ }), { target: { value: 'https://api.example.com/v1' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: secret } })
}

beforeEach(() => {
  mocks.list.mockResolvedValue({ items: [summary()], total: 1 })
  mocks.state.mockResolvedValue(state())
  mocks.create.mockResolvedValue({ id: 'host-2', desired_status: 'running', runtime_status: 'provisioning' })
  mocks.update.mockResolvedValue({ id: 'host-1', version: 4, desired_status: 'running', runtime_status: 'claimed' })
  mocks.start.mockResolvedValue({ id: 'host-1', desired_status: 'running', runtime_status: 'starting' })
  mocks.stop.mockResolvedValue({ id: 'host-1', desired_status: 'paused', runtime_status: 'stopping' })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  localStorage.clear()
  sessionStorage.clear()
  Object.values(mocks).forEach((mock) => mock.mockReset())
})

describe('HostedAgentsPanel', () => {
  it('renders durable status, focused map, safe logs and daily limits separately', async () => {
    render(<HostedAgentsPanel token="admin-token" />)

    expect(await screen.findByText('由 Simverse 后台持续托管')).toBeTruthy()
    expect(screen.getByText(/2026|Token 与动作上限为硬限/)).toBeTruthy()
    expect(screen.getAllByText('保持在线').length).toBeGreaterThan(0)
    expect(screen.getAllByText('在线').length).toBeGreaterThan(0)
    expect(await screen.findByText('看见中心广场。')).toBeTruthy()
    expect(screen.getByText('向梅走近。')).toBeTruthy()
    expect(screen.getByRole('list', { name: '林舟 的托管视野地图点位' })).toBeTruthy()
    expect(screen.getByText('未定价')).toBeTruthy()
  })

  it('aborts browser polling on unmount without stopping the hosted resident', async () => {
    let stateSignal: AbortSignal | undefined
    mocks.state.mockImplementation((...args: unknown[]) => {
      stateSignal = args[3] as AbortSignal
      return new Promise(() => {})
    })

    const view = render(<HostedAgentsPanel token="admin-token" />)
    await waitFor(() => expect(stateSignal).toBeDefined())
    view.unmount()

    expect(stateSignal?.aborted).toBe(true)
    expect(mocks.stop).not.toHaveBeenCalled()
  })

  it('creates a persistent resident and never stores or exposes the provider key', async () => {
    mocks.list.mockResolvedValue({ items: [], total: 0 })
    render(<HostedAgentsPanel token="admin-token" />)
    await openCreateEditor()
    fillCreateForm()
    fireEvent.click(screen.getByRole('button', { name: '创建并保持在线' }))

    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1))
    expect(mocks.create.mock.calls[0][1]).toMatchObject({
      request_id: expect.stringMatching(UUID_PATTERN_FOR_EXPECT),
      display_name: '新居民',
      base_url: 'https://api.example.com/v1',
      api_key: 'provider-secret',
      heartbeat_seconds: 30,
      action_interval_seconds: 30,
      daily_action_limit: 200,
      daily_token_limit: 200_000,
    })
    await waitFor(() => expect(screen.queryByLabelText('API Key')).toBeNull())
    expect(screen.queryByDisplayValue('provider-secret')).toBeNull()
    expect(sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY)).toBeNull()
    expect(JSON.stringify({
      local: Object.fromEntries(Object.entries(localStorage)),
      session: Object.fromEntries(Object.entries(sessionStorage)),
      href: window.location.href,
    })).not.toContain('provider-secret')
  })

  it.each([
    ['a lost response', new TypeError('Failed to fetch')],
    ['an HTTP 500 after commit', new Error('API 500: upstream response lost')],
  ])('reuses the same create UUID after %s', async (_label, failure) => {
    mocks.list.mockResolvedValue({ items: [], total: 0 })
    mocks.create
      .mockRejectedValueOnce(failure)
      .mockResolvedValueOnce({ id: 'host-2', desired_status: 'running', runtime_status: 'provisioning' })

    render(<HostedAgentsPanel token="admin-token" />)
    await openCreateEditor()
    fillCreateForm()
    fireEvent.click(screen.getByRole('button', { name: '创建并保持在线' }))
    expect(await screen.findByRole('alert')).toBeTruthy()

    const firstRequestId = mocks.create.mock.calls[0][1].request_id as string
    expect(sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY)).toBe(firstRequestId)
    expect(JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))).not.toContain('provider-secret')

    fireEvent.click(screen.getByRole('button', { name: '创建并保持在线' }))
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2))
    expect(mocks.create.mock.calls[1][1].request_id).toBe(firstRequestId)
    await waitFor(() => expect(sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY)).toBeNull())
  })

  it.each([409, 422])('retains the create UUID after an HTTP %s response', async (status) => {
    mocks.list.mockResolvedValue({ items: [], total: 0 })
    mocks.create
      .mockRejectedValueOnce(new Error(`API ${status}: create result is not confirmed`))
      .mockResolvedValueOnce({ id: 'host-2', desired_status: 'running', runtime_status: 'provisioning' })

    render(<HostedAgentsPanel token="admin-token" />)
    await openCreateEditor()
    fillCreateForm()
    fireEvent.click(screen.getByRole('button', { name: '创建并保持在线' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    const pendingId = mocks.create.mock.calls[0][1].request_id
    expect(sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY)).toBe(pendingId)

    fireEvent.click(screen.getByRole('button', { name: '创建并保持在线' }))
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2))
    expect(mocks.create.mock.calls[1][1].request_id).toBe(pendingId)
    await waitFor(() => expect(sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY)).toBeNull())
  })

  it('recovers a committed create from the owner list after a lost response and refresh', async () => {
    mocks.list.mockResolvedValue({ items: [], total: 0 })
    mocks.create.mockRejectedValueOnce(new TypeError('Failed to fetch'))

    const firstView = render(<HostedAgentsPanel token="admin-token" />)
    await openCreateEditor()
    fillCreateForm()
    fireEvent.click(screen.getByRole('button', { name: '创建并保持在线' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    const pendingId = mocks.create.mock.calls[0][1].request_id as string
    expect(sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY)).toBe(pendingId)
    firstView.unmount()

    const recoveredSummary = summary({
      id: 'host-recovered',
      request_id: pendingId,
      display_name: '已恢复居民',
      resident_slug: 'recovered-resident',
      agent: null,
    })
    mocks.list.mockResolvedValue({ items: [recoveredSummary], total: 1 })
    mocks.state.mockResolvedValue(state({
      ...recoveredSummary,
      snapshot: null,
      logs: [],
      log_cursor: 0,
    }))

    render(<HostedAgentsPanel token="admin-token" />)

    expect(await screen.findByRole('heading', { name: '已恢复居民' })).toBeTruthy()
    await waitFor(() => expect(sessionStorage.getItem(CREATE_REQUEST_STORAGE_KEY)).toBeNull())
    expect(mocks.create).toHaveBeenCalledTimes(1)
    expect(screen.queryByLabelText('API Key')).toBeNull()
  })

  it('treats identity and credentials as stable/write-only during edits', async () => {
    render(<HostedAgentsPanel token="admin-token" />)
    fireEvent.click(await screen.findByRole('button', { name: '编辑配置' }))

    const name = screen.getByRole('textbox', { name: '居民名称' }) as HTMLInputElement
    const key = screen.getByLabelText('API Key') as HTMLInputElement
    expect(name.disabled).toBe(true)
    expect(name.value).toBe('林舟')
    expect(key.value).toBe('')
    expect(key.placeholder).toContain('已加密保存')
    expect(screen.getByText(/API Key 由服务端主密钥加密保存/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(1))
    expect(mocks.update.mock.calls[0][2]).not.toHaveProperty('display_name')
    expect(mocks.update.mock.calls[0][2]).not.toHaveProperty('api_key')
    expect(mocks.update.mock.calls[0][2]).not.toHaveProperty('base_url')
    expect(mocks.update.mock.calls[0][2]).toMatchObject({ version: 3, action_interval_seconds: 30 })
  })

  it('keeps desired online status separate from runtime backoff', async () => {
    mocks.list.mockResolvedValue({ items: [summary({ runtime_status: 'backoff' })], total: 1 })
    mocks.state.mockResolvedValue(state({
      runtime_status: 'backoff',
      health: { last_heartbeat_at: null, next_retry_at: '2026-08-15T02:00:00Z', consecutive_failures: 2 },
      last_error_code: 'provider_unreachable',
    }))

    render(<HostedAgentsPanel token="admin-token" />)

    expect((await screen.findAllByText('保持在线')).length).toBeGreaterThan(0)
    expect((await screen.findAllByText('退避重试')).length).toBeGreaterThan(0)
    expect(await screen.findByText(/provider_unreachable/)).toBeTruthy()
  })

  it('clears a resolved backend error when the next state explicitly returns null', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const failed = state({
      runtime_status: 'backoff',
      health: { last_heartbeat_at: null, next_retry_at: '2026-08-15T02:00:00Z', consecutive_failures: 2 },
      last_error_code: 'provider_unreachable',
    })
    const recovered = state({
      runtime_status: 'claimed',
      health: { last_heartbeat_at: '2026-08-15T02:00:01Z', next_retry_at: null, consecutive_failures: 0 },
      last_error_code: null,
    })
    mocks.list.mockResolvedValue({ items: [summary({
      runtime_status: 'backoff',
      last_error_code: 'provider_unreachable',
    })], total: 1 })
    mocks.state.mockResolvedValueOnce(failed).mockResolvedValue(recovered)

    render(<HostedAgentsPanel token="admin-token" />)
    expect(await screen.findByText(/provider_unreachable/)).toBeTruthy()

    await act(async () => vi.advanceTimersByTimeAsync(2_100))

    expect(screen.queryByText(/provider_unreachable/)).toBeNull()
    expect(screen.getAllByText('在线').length).toBeGreaterThan(0)
  })

  it.each([
    ['paused', '已暂停', 'paused'],
    ['disabled', '已停用', 'disabled'],
  ])('lets desired %s override an idle runtime label and color', async (desiredStatus, label, status) => {
    const stopped = summary({ desired_status: desiredStatus, runtime_status: 'idle' })
    mocks.list.mockResolvedValue({ items: [stopped], total: 1 })
    mocks.state.mockResolvedValue(state({ ...stopped }))

    const view = render(<HostedAgentsPanel token="admin-token" />)

    await screen.findByRole('heading', { name: '林舟' })
    expect(screen.queryByText('在线待机')).toBeNull()
    expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    const listIndicator = view.container.querySelector('.hosted-agent-list-item > i')
    const runtimeBadge = view.container.querySelector('.hosted-agent-statuses span[data-kind="runtime"]')
    expect(listIndicator?.getAttribute('data-status')).toBe(status)
    expect(runtimeBadge?.getAttribute('data-status')).toBe(status)
  })

  it('guards explicit pause so repeated clicks issue only one request', async () => {
    let finishStop!: (value: { id: string; desired_status: string; runtime_status: string }) => void
    mocks.stop.mockImplementation(() => new Promise((resolve) => { finishStop = resolve }))

    render(<HostedAgentsPanel token="admin-token" />)
    const stopButton = await screen.findByRole('button', { name: '暂停托管' })
    fireEvent.click(stopButton)
    fireEvent.click(stopButton)
    expect(mocks.stop).toHaveBeenCalledTimes(1)

    await act(async () => finishStop({ id: 'host-1', desired_status: 'paused', runtime_status: 'stopping' }))
    await waitFor(() => expect(screen.getAllByText('已暂停').length).toBeGreaterThan(0))
  })
})

const UUID_PATTERN_FOR_EXPECT = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
