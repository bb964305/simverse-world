import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { OnboardingPage } from './OnboardingPage'
import { useLocale } from '../services/locale'
import { useGameStore } from '../stores/gameStore'
import { checkOnboarding, createPlayerResident, getResidents, getSpriteTemplates, skipOnboarding } from '../services/api'
import { loadOwnedAgents } from '../services/web3/agentRegistry'
import { registerResidentOnchain } from '../services/web3/passport'

vi.mock('../services/api', () => ({
  checkOnboarding: vi.fn(), createPlayerResident: vi.fn(), getResidents: vi.fn(), getSpriteTemplates: vi.fn(), skipOnboarding: vi.fn(),
}))
vi.mock('../services/web3/wallet', () => ({ configuredChainId: () => 4663, configuredChainName: () => 'Robinhood Chain' }))
vi.mock('../services/web3/agentRegistry', () => ({ registryConfigured: () => true, loadOwnedAgents: vi.fn() }))
vi.mock('../services/web3/passport', () => ({ registerResidentOnchain: vi.fn() }))

const wallet = '0x1234567890123456789012345678901234567890'

beforeEach(() => {
  useLocale.setState({ locale: 'en' })
  useGameStore.setState({ token: 'wallet-token', user: { id: 'u1', name: 'Wallet user', email: 'wallet@example.invalid', avatar: null, soul_coin_balance: 0, wallet_address: wallet } })
  vi.mocked(checkOnboarding).mockResolvedValue({ needs_onboarding: true, player_resident_id: null })
  vi.mocked(getResidents).mockResolvedValue([{ id: 'preset-1', slug: 'nova-origin', name: 'Nova Origin', district: 'free', status: 'idle', heat: 0, sprite_key: '埃迪', sprite_url: null, sprite_content_hash: null, sprite_generation_run_id: null, portrait_url: null, tile_x: 0, tile_y: 0, star_rating: 0, token_cost_per_turn: 0, meta_json: { origin: 'preset' } }])
  vi.mocked(getSpriteTemplates).mockResolvedValue([{ key: '埃迪', gender: 'neutral', age_group: 'adult', vibe: 'builder', tags: ['cyber'] }])
  vi.mocked(createPlayerResident).mockResolvedValue({ id: 'resident-1', slug: 'p-nova', name: 'Nova', sprite_key: '埃迪', tile_x: 75, tile_y: 56 })
  vi.mocked(skipOnboarding).mockResolvedValue({ id: 'resident-2', slug: 'p-starter', name: 'Starter', sprite_key: '埃迪', tile_x: 75, tile_y: 56 })
  vi.mocked(registerResidentOnchain).mockResolvedValue({ agentId: 7n, transaction: `0x${'ab'.repeat(32)}` })
  vi.mocked(loadOwnedAgents).mockReset().mockResolvedValueOnce([]).mockResolvedValueOnce([{ id: 7n, uri: 'https://example.invalid/metadata', state: {} as never, worldProofCount: 0n }])
})

afterEach(() => cleanup())

describe('OnboardingPage onchain city registration', () => {
  it('creates a playable resident and requires a real Agent Passport transaction before entry', async () => {
    render(<MemoryRouter initialEntries={['/onboarding?next=%2Fplay']}><OnboardingPage /></MemoryRouter>)

    expect(await screen.findByRole('heading', { name: 'Choose how you enter the world' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Resident name'), { target: { value: 'Nova' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create resident' }))

    expect(await screen.findByRole('heading', { name: 'Register your Agent Passport' })).toBeInTheDocument()
    expect(createPlayerResident).toHaveBeenCalledWith('wallet-token', { name: 'Nova', sprite_key: '埃迪' })
    fireEvent.click(screen.getByRole('button', { name: 'Register identity onchain' }))

    await waitFor(() => expect(registerResidentOnchain).toHaveBeenCalledWith('en', wallet, expect.objectContaining({ id: 'resident-1', name: 'Nova' })))
    expect(await screen.findByText('AGENT PASSPORT #7')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Enter Simverse World' })).toBeInTheDocument()
  })

  it('shows a concise message when the wallet rejects Passport registration', async () => {
    vi.mocked(registerResidentOnchain).mockRejectedValueOnce(new Error(`User rejected the request.
Request Arguments: from: 0x5E807ae9C82bA691Fca0CC1f56EB01eb58d6f04C data: 0xa3dcc39c...
Details: Request Signature: User denied request signature. Version: viem@2.56.1`))
    render(<MemoryRouter initialEntries={['/onboarding?next=%2Fplay']}><OnboardingPage /></MemoryRouter>)

    fireEvent.change(await screen.findByLabelText('Resident name'), { target: { value: 'Nova' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create resident' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Register identity onchain' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Request cancelled in your wallet. Nothing was submitted and no gas was spent.')
    expect(alert).not.toHaveTextContent('0x5E807')
    expect(alert).not.toHaveTextContent('viem')
  })
})
