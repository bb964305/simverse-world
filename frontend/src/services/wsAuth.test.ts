import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api/caravan', () => ({ getCurrentCaravan: vi.fn() }))

import { getCurrentCaravan } from './api/caravan'
import { apiFetch } from './api/core'
import { connectWS, disconnectWS } from './ws'
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

  close(): void {
    if (this.readyState === FakeWebSocket.CLOSED) return
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.()
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeWebSocket)
  FakeWebSocket.instances = []
  localStorage.clear()
  vi.mocked(getCurrentCaravan).mockReturnValue(new Promise(() => {}))
  useGameStore.setState({ user: null, token: null, wsStatus: 'connected' })
})

afterEach(() => {
  disconnectWS()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('401 disconnects the WebSocket (F9 cross-account socket reuse)', () => {
  it('an apiFetch 401 closes the authenticated socket without scheduling a reconnect', async () => {
    sessionStorage.setItem('token', 'token-a')
    useGameStore.setState({ token: 'token-a' })
    connectWS()
    const ws = FakeWebSocket.instances[0]
    ws.open()
    expect(ws.readyState).toBe(FakeWebSocket.OPEN)

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })))
    await expect(apiFetch('/me')).rejects.toThrow('登录会话已过期')

    // Deliberate disconnect semantics: socket closed, no reconnect timer, no
    // 'reconnecting' banner, token cleared by the store logout.
    expect(ws.readyState).toBe(FakeWebSocket.CLOSED)
    vi.runOnlyPendingTimers()
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(useGameStore.getState().wsStatus).toBe('connected')
    expect(useGameStore.getState().token).toBeNull()
  })
})

describe('connectWS token change rebuilds the socket (F9 setAuth account switch)', () => {
  it('a different store token closes the old socket and re-auths with the new one', () => {
    useGameStore.setState({ token: 'token-a' })
    connectWS()
    const first = FakeWebSocket.instances[0]
    first.open()
    expect(first.send).toHaveBeenCalledWith(JSON.stringify({ type: 'auth', token: 'token-a' }))

    useGameStore.setState({ token: 'token-b' })
    connectWS()
    expect(first.readyState).toBe(FakeWebSocket.CLOSED)
    expect(FakeWebSocket.instances).toHaveLength(2)
    const second = FakeWebSocket.instances[1]
    second.open()
    expect(second.send).toHaveBeenCalledWith(JSON.stringify({ type: 'auth', token: 'token-b' }))
    // Tearing the stale socket down must not flip the reconnecting banner nor
    // schedule a reconnect for it.
    expect(useGameStore.getState().wsStatus).toBe('connected')
    vi.runOnlyPendingTimers()
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('the same token keeps the existing socket (no churn on re-mount)', () => {
    useGameStore.setState({ token: 'token-a' })
    connectWS()
    FakeWebSocket.instances[0].open()
    connectWS()
    expect(FakeWebSocket.instances).toHaveLength(1)
  })
})
