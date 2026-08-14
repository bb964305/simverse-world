import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CaravanState } from './api/caravan'

vi.mock('./api/caravan', () => ({ getCurrentCaravan: vi.fn() }))

import { getCurrentCaravan } from './api/caravan'
import {
  getCaravanProjection,
  refreshCaravanProjection,
  resetCaravanProjection,
} from './caravanProjection'
import {
  CARAVAN_DISCONNECT_STALE_MS,
  CARAVAN_RESYNC_INTERVAL_MS,
  connectWS,
  disconnectWS,
} from './ws'
import { useGameStore } from '../stores/gameStore'

class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []

  readyState = FakeWebSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  readonly send = vi.fn()
  readonly url: string

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.()
  }

  message(value: unknown): void {
    this.onmessage?.({ data: JSON.stringify(value) } as MessageEvent<string>)
  }

  close(): void {
    if (this.readyState === FakeWebSocket.CLOSED) return
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.()
  }
}

const SERVER_TIME = '2026-08-12T02:00:00.000Z'

function state(overrides: Partial<CaravanState> = {}): CaravanState {
  return {
    type: 'caravan_state',
    visit_id: 'visit-a',
    world_event_id: 'event-a',
    version: 1,
    phase: 'inbound',
    server_time: SERVER_TIME,
    position: { tile_x: 70, tile_y: 92 },
    motion: null,
    summary: { fee_sc: 0, bought: 0, spent_sc: 0, tax_sc: 0, imports_stocked: 0 },
    visible: true,
    ...overrides,
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeWebSocket)
  FakeWebSocket.instances = []
  resetCaravanProjection()
  vi.mocked(getCurrentCaravan).mockReset()
  useGameStore.setState({ token: 'test-token', wsStatus: 'connected' })
})

afterEach(() => {
  disconnectWS()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('caravan WebSocket convergence', () => {
  it('routes frames, rejects stale versions, refreshes after reconnect, and clears terminal state', async () => {
    let resolveInitial!: (snapshot: CaravanState) => void
    vi.mocked(getCurrentCaravan).mockReturnValueOnce(new Promise((resolve) => {
      resolveInitial = resolve
    }))

    connectWS()
    const first = FakeWebSocket.instances[0]
    first.open()
    expect(first.send).toHaveBeenCalledWith(JSON.stringify({ type: 'auth', token: 'test-token' }))
    first.message({ type: 'auth_ok' })

    first.message(state({ version: 2 }))
    first.message(state({ version: 1 }))
    expect(getCaravanProjection().snapshot?.version).toBe(2)

    // The slow initial GET is stale by the time it resolves and cannot rewind WS.
    resolveInitial(state({ version: 1 }))
    await refreshCaravanProjection()
    expect(getCaravanProjection().snapshot?.version).toBe(2)

    // A passive close exercises the real reconnect timer/auth path. Its newer
    // durable snapshot advances the same reducer used by WebSocket frames.
    vi.mocked(getCurrentCaravan).mockResolvedValueOnce(state({ version: 3, phase: 'trading' }))
    first.close()
    expect(useGameStore.getState().wsStatus).toBe('reconnecting')
    vi.runOnlyPendingTimers()
    const second = FakeWebSocket.instances[1]
    second.open()
    second.message({ type: 'auth_ok' })
    await refreshCaravanProjection()
    expect(getCaravanProjection().snapshot).toMatchObject({ version: 3, phase: 'trading' })

    second.message(state({ version: 4, phase: 'departed', visible: false }))
    expect(getCaravanProjection().snapshot).toMatchObject({ version: 4, visible: false })
  })

  it('expires a stale visible projection after a prolonged passive disconnect', () => {
    vi.mocked(getCurrentCaravan).mockReturnValue(new Promise(() => {}))
    connectWS()
    const ws = FakeWebSocket.instances[0]
    ws.open()
    ws.message({ type: 'auth_ok' })
    ws.message(state({ version: 2 }))
    expect(getCaravanProjection().snapshot?.visible).toBe(true)

    ws.close()
    vi.advanceTimersByTime(CARAVAN_DISCONNECT_STALE_MS - 1)
    expect(getCaravanProjection().snapshot?.visible).toBe(true)
    vi.advanceTimersByTime(1)
    expect(getCaravanProjection().snapshot).toBeNull()
  })

  it('periodically clears a missed terminal frame while the socket stays open', async () => {
    const empty = state({
      visit_id: null,
      world_event_id: null,
      version: 0,
      phase: null,
      position: null,
      motion: null,
      visible: false,
    })
    vi.mocked(getCurrentCaravan)
      .mockResolvedValueOnce(state({ version: 3, phase: 'trading' }))
      .mockResolvedValueOnce(empty)

    connectWS()
    const ws = FakeWebSocket.instances[0]
    ws.open()
    ws.message({ type: 'auth_ok' })
    await refreshCaravanProjection()
    expect(getCaravanProjection().snapshot).toMatchObject({
      phase: 'trading', visible: true,
    })

    // No close/reconnect and no departed WebSocket frame. The bounded REST
    // heartbeat must still converge to the server's canonical empty snapshot.
    await vi.advanceTimersByTimeAsync(CARAVAN_RESYNC_INTERVAL_MS)

    expect(ws.readyState).toBe(FakeWebSocket.OPEN)
    expect(getCurrentCaravan).toHaveBeenCalledTimes(2)
    expect(getCaravanProjection().snapshot).toMatchObject({
      visit_id: null, phase: null, visible: false,
    })
  })
})
