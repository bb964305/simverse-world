import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api/caravan', () => ({ getCurrentCaravan: vi.fn() }))

import { getCurrentCaravan } from './api/caravan'
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

  message(value: unknown): void {
    this.onmessage?.({ data: JSON.stringify(value) } as MessageEvent<string>)
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
  vi.mocked(getCurrentCaravan).mockReturnValue(new Promise(() => {}))
  useGameStore.setState({
    token: 'test-token',
    wsStatus: 'connected',
    onlinePlayers: new Map(),
  })
})

afterEach(() => {
  disconnectWS()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('online player websocket metadata', () => {
  it('preserves agent_controlled through partial updates and accepts missing field', () => {
    connectWS()
    const ws = FakeWebSocket.instances[0]
    ws.open()
    ws.message({ type: 'auth_ok' })

    ws.message({
      type: 'player_joined',
      player_id: 'agent-1',
      name: 'Village Guide',
      x: 10,
      y: 20,
      direction: 'left',
      agent_controlled: true,
    })

    expect(useGameStore.getState().onlinePlayers.get('agent-1')).toMatchObject({
      player_id: 'agent-1',
      name: 'Village Guide',
      x: 10,
      y: 20,
      direction: 'left',
      agent_controlled: true,
    })

    ws.message({
      type: 'player_moved',
      player_id: 'agent-1',
      name: 'Village Guide',
      x: 14,
      y: 28,
      direction: 'up',
    })

    expect(useGameStore.getState().onlinePlayers.get('agent-1')).toMatchObject({
      player_id: 'agent-1',
      name: 'Village Guide',
      x: 14,
      y: 28,
      direction: 'up',
      agent_controlled: true,
    })

    ws.message({
      type: 'online_players',
      players: [
        {
          player_id: 'human-1',
          name: 'Visitor',
          x: 0,
          y: 0,
          direction: 'down',
        },
      ],
    })

    expect(useGameStore.getState().onlinePlayers.get('human-1')).toMatchObject({
      player_id: 'human-1',
      name: 'Visitor',
      x: 0,
      y: 0,
      direction: 'down',
    })
    expect(useGameStore.getState().onlinePlayers.get('human-1')?.agent_controlled).toBeUndefined()
  })
})
