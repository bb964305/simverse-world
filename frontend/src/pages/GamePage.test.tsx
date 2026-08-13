import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../services/ws', () => ({
  connectWS: vi.fn(),
  disconnectWS: vi.fn(),
}))
vi.mock('../services/api', () => ({
  getSettings: vi.fn().mockResolvedValue({ character: { sprite_key: '乔治' } }),
}))
vi.mock('../game/GameScene', () => ({
  initGame: vi.fn(),
  destroyGame: vi.fn(),
}))
vi.mock('../game/phaserBridge', () => ({
  bridge: { on: vi.fn(() => vi.fn()) },
}))
vi.mock('../components/TopNav', () => ({ TopNav: () => null }))
vi.mock('../components/ChatDrawer', () => ({ ChatDrawer: () => null }))
vi.mock('../components/NpcTooltip', () => ({ NpcTooltip: () => null }))
vi.mock('../components/CoinNotification', () => ({ CoinNotification: () => null }))
vi.mock('../components/PhotoBooth', () => ({ PhotoBooth: () => null }))
vi.mock('../components/DecorEditor', () => ({ DecorEditor: () => null }))
vi.mock('../components/minimap/MinimapOverlay', () => ({ MinimapOverlay: () => null }))

import { destroyGame, initGame } from '../game/GameScene'
import { connectWS, disconnectWS } from '../services/ws'
import { GamePage } from './GamePage'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('GamePage socket ownership', () => {
  it('keeps the authenticated socket alive when navigating away', async () => {
    const view = render(<GamePage />)
    await waitFor(() => expect(initGame).toHaveBeenCalledTimes(1))
    expect(connectWS).toHaveBeenCalledTimes(1)

    view.unmount()
    await waitFor(() => expect(destroyGame).toHaveBeenCalledTimes(1))
    expect(disconnectWS).not.toHaveBeenCalled()
  })
})
