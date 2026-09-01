import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AgentStudioPage } from './AgentStudioPage'
import { useLocale } from '../services/locale'
import { createAgentPassport, loadOwnedAgents } from '../services/web3/agentRegistry'
import { uploadWeb3Content } from '../services/web3/content'
import { useGameStore } from '../stores/gameStore'

vi.mock('../components/TopNav', () => ({ TopNav: () => <nav data-testid="top-nav" /> }))
vi.mock('../services/web3/wallet', () => ({
  configuredChainId: () => 31337,
  configuredChainName: () => 'Simverse Local',
}))
vi.mock('../services/web3/agentRegistry', () => ({
  registryConfigured: () => true,
  loadOwnedAgents: vi.fn().mockResolvedValue([]),
  createAgentPassport: vi.fn().mockResolvedValue(`0x${'ab'.repeat(32)}`),
  publishTrainingVersion: vi.fn(),
  anchorMemory: vi.fn(),
  anchorSave: vi.fn(),
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
    await waitFor(() => expect(uploadWeb3Content).toHaveBeenCalled())
    await waitFor(() => expect(createAgentPassport).toHaveBeenCalledWith(
      'zh-CN',
      '0x1234567890123456789012345678901234567890',
      'http://test/web3/content/content-1',
      `0x${'12'.repeat(32)}`,
    ))
    expect(await screen.findByRole('status')).toHaveTextContent('交易已确认')
  })

  it('switches the studio copy to English', async () => {
    useLocale.setState({ locale: 'en' })
    render(<MemoryRouter><AgentStudioPage /></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: 'Onchain Agent Studio' })).toBeInTheDocument()
    expect(screen.getByText(/Turn an existing resident/)).toBeInTheDocument()
  })
})
