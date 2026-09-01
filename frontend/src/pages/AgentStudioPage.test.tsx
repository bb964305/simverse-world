import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AgentStudioPage } from './AgentStudioPage'
import { useLocale } from '../services/locale'
import { updateCharacter, updatePlayerPosition } from '../services/api'
import { loadLatestSaveAnchor, loadOwnedAgents } from '../services/web3/agentRegistry'
import { readPrivateAnchoredJson } from '../services/web3/content'
import { registerResidentOnchain } from '../services/web3/passport'
import { useGameStore } from '../stores/gameStore'

vi.mock('../components/TopNav', () => ({ TopNav: () => <nav data-testid="top-nav" /> }))
vi.mock('../services/api', () => ({
  updateCharacter: vi.fn().mockResolvedValue({}),
  updatePlayerPosition: vi.fn().mockResolvedValue({ tile_x: 88, tile_y: 41 }),
}))
vi.mock('../services/web3/wallet', () => ({
  configuredChainId: () => 31337,
  configuredChainName: () => 'Simverse Local',
}))
vi.mock('../services/web3/agentRegistry', () => ({
  AGENT_REGISTRY_ADDRESS: '0x1111111111111111111111111111111111111111',
  registryConfigured: () => true,
  residentKeyFor: () => `0x${'11'.repeat(32)}`,
  loadOwnedAgents: vi.fn().mockResolvedValue([]),
  loadLatestSaveAnchor: vi.fn(),
  publishTrainingVersion: vi.fn(),
  anchorMemory: vi.fn(),
  anchorSave: vi.fn(),
}))
vi.mock('../services/web3/passport', () => ({
  registerResidentOnchain: vi.fn().mockResolvedValue({ agentId: 7n, transaction: `0x${'ab'.repeat(32)}` }),
  syncResidentMetadataOnchain: vi.fn(),
}))
vi.mock('../services/web3/content', () => ({
  uploadWeb3Content: vi.fn().mockResolvedValue({
    content_id: 'content-1',
    content_uri: 'http://test/web3/content/content-1',
    content_hash: `0x${'12'.repeat(32)}`,
    filename: 'agent.json',
    media_type: 'application/json',
    size: 100,
  }),
  snapshotGameMemory: vi.fn(),
  readPrivateAnchoredJson: vi.fn(),
}))

beforeEach(() => {
  useLocale.setState({ locale: 'zh-CN' })
  useGameStore.setState({
    token: 'wallet-token',
    user: {
      id: 'user-1', name: 'Wallet User', email: 'wallet@example.invalid', avatar: null,
      soul_coin_balance: 0, wallet_address: '0x1234567890123456789012345678901234567890',
    },
  })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve([{
      id: 'resident-1', slug: 'cyber-soul', name: '赛博灵魂', district: 'free', status: 'active',
      star_rating: 4, sprite_key: '埃迪', meta_json: { role: 'builder' },
    }]),
  }))
  vi.mocked(loadOwnedAgents).mockResolvedValue([])
  vi.mocked(loadLatestSaveAnchor).mockReset()
  vi.mocked(readPrivateAnchoredJson).mockReset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('AgentStudioPage', () => {
  it('creates a passport from an existing game resident through content storage and the contract', async () => {
    render(<MemoryRouter><AgentStudioPage /></MemoryRouter>)
    expect(await screen.findByRole('option', { name: /赛博灵魂/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '创建链上身份' }))
    await waitFor(() => expect(registerResidentOnchain).toHaveBeenCalledWith(
      'zh-CN',
      '0x1234567890123456789012345678901234567890',
      expect.objectContaining({ id: 'resident-1' }),
    ))
    expect(await screen.findByRole('status')).toHaveTextContent('交易已确认')
  })

  it('switches the studio copy to English', async () => {
    useLocale.setState({ locale: 'en' })
    render(<MemoryRouter><AgentStudioPage /></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: 'Onchain Agent Studio' })).toBeInTheDocument()
    expect(screen.getByText(/Turn an existing resident/)).toBeInTheDocument()
  })

  it('verifies and restores the latest onchain save into the game profile', async () => {
    vi.mocked(loadOwnedAgents).mockResolvedValue([{
      id: 7n,
      worldProofCount: 0n,
      residentKey: `0x${'11'.repeat(32)}`,
      uri: 'http://test/web3/content/metadata',
      state: {
        metadataHash: `0x${'01'.repeat(32)}`, latestArtifactHash: `0x${'00'.repeat(32)}`,
        trainingRoot: `0x${'00'.repeat(32)}`, latestMemoryHash: `0x${'00'.repeat(32)}`,
        latestSaveHash: `0x${'34'.repeat(32)}`, version: 0n, memoryRevision: 0n,
        saveRevision: 1n, createdAt: 1n, updatedAt: 2n,
      },
    }])
    vi.mocked(loadLatestSaveAnchor).mockResolvedValue({
      contentHash: `0x${'34'.repeat(32)}`, parentHash: `0x${'00'.repeat(32)}`,
      contentURI: 'http://test/web3/content/save-1', revision: 1n, recordedAt: 2n,
    })
    vi.mocked(readPrivateAnchoredJson).mockResolvedValue({
      schema: 'simverse-save-v1',
      wallet: '0x1234567890123456789012345678901234567890',
      agent_id: '7', recorded_at: '2026-09-01T00:00:00.000Z',
      player: { sprite_key: '梅', tile_x: 88, tile_y: 41 },
    })

    render(<MemoryRouter><AgentStudioPage /></MemoryRouter>)
    const restore = await screen.findByRole('button', { name: '恢复最新链上存档' })
    await waitFor(() => expect(restore).toBeEnabled())
    fireEvent.click(restore)

    await waitFor(() => expect(readPrivateAnchoredJson).toHaveBeenCalledWith(
      'http://test/web3/content/save-1', `0x${'34'.repeat(32)}`,
    ))
    await waitFor(() => expect(updatePlayerPosition).toHaveBeenCalledWith(88, 41))
    expect(updateCharacter).toHaveBeenCalledWith({ sprite_key: '梅' })
    expect(useGameStore.getState().playerTileX).toBe(88)
    expect(useGameStore.getState().playerSpriteKey).toBe('梅')
    expect(await screen.findByRole('status')).toHaveTextContent('链上存档已恢复')
  })
})
