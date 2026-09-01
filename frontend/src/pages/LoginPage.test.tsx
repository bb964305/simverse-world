import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { LoginPage } from './LoginPage'
import { useLocale } from '../services/locale'
import { signInWithWallet } from '../services/web3/wallet'
import { useGameStore } from '../stores/gameStore'

vi.mock('../services/web3/wallet', () => ({
  configuredChainName: () => 'Robinhood Chain Testnet',
  signInWithWallet: vi.fn(),
}))

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="route-location">{`${location.pathname}${location.search}`}</output>
}

function renderLogin(path = '/login') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <LoginPage />
      <LocationProbe />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  useLocale.setState({ locale: 'zh-CN' })
  useGameStore.setState({ user: null, token: null })
  vi.mocked(signInWithWallet).mockReset()
})

afterEach(() => {
  cleanup()
  document.body.classList.remove('auth-page-open')
})

describe('LoginPage wallet identity', () => {
  it('exposes a wallet-only public identity flow', () => {
    renderLogin()
    expect(screen.getByRole('button', { name: /连接钱包并进入/ })).toBeInTheDocument()
    expect(screen.queryByText(/GitHub 登录|LinuxDo 登录/)).not.toBeInTheDocument()
    expect(screen.queryByPlaceholderText('邮箱')).not.toBeInTheDocument()
  })

  it('renders a wallet rejection as an accessible error', async () => {
    vi.mocked(signInWithWallet).mockRejectedValue(new Error('用户拒绝了签名'))
    renderLogin()
    fireEvent.click(screen.getByRole('button', { name: /连接钱包并进入/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('用户拒绝了签名')
  })

  it('stores the wallet session and preserves the requested destination', async () => {
    vi.mocked(signInWithWallet).mockResolvedValue({
      user: {
        id: 'wallet-user-1',
        name: 'Soul 0x1234…abcd',
        email: 'wallet@example.invalid',
        avatar: null,
        soul_coin_balance: 0,
        wallet_address: '0x123456789012345678901234567890123456abcd',
      },
      access_token: 'wallet-session-token',
    })
    renderLogin('/login?next=%2Fforge')
    fireEvent.click(screen.getByRole('button', { name: /连接钱包并进入/ }))

    await waitFor(() => expect(screen.getByTestId('route-location')).toHaveTextContent('/onboarding?next=%2Fforge'))
    expect(useGameStore.getState().token).toBe('wallet-session-token')
    expect(useGameStore.getState().user?.wallet_address).toMatch(/^0x1234/)
  })

  it('switches the complete access copy to English', () => {
    renderLogin()
    fireEvent.click(screen.getByRole('button', { name: 'EN' }))
    expect(screen.getByRole('heading', { name: 'Connect your onchain identity' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Connect wallet & enter/ })).toBeInTheDocument()
  })
})
