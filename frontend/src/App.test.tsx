import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AppRoutes } from './App'
import { useGameStore } from './stores/gameStore'

vi.mock('./pages/GamePage', () => ({
  GamePage: () => <main data-testid="game-page">Game World</main>,
}))

const user = {
  id: 'user-1',
  name: 'Resident',
  email: 'resident@example.com',
  avatar: null,
  soul_coin_balance: 0,
}

function renderRoute(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  useGameStore.setState({
    user: null,
    token: null,
    wsStatus: 'connected',
    achievementToast: null,
    pendingEncounter: null,
  })
})

afterEach(() => {
  cleanup()
  document.body.classList.remove('marketing-page-open', 'auth-page-open')
})

describe('public and authenticated routes', () => {
  it('keeps the marketing site at / when logged out', async () => {
    renderRoute('/')
    expect(await screen.findByRole('heading', { level: 1, name: 'Simverse World' })).toBeInTheDocument()
    expect(screen.queryByTestId('game-page')).not.toBeInTheDocument()
  })

  it('keeps the marketing site at / while giving authenticated users a /play entry', async () => {
    useGameStore.setState({ user, token: 'token' })
    renderRoute('/')
    await screen.findByRole('heading', { level: 1, name: 'Simverse World' })
    expect(screen.getAllByRole('link', { name: /进入世界/ })[0]).toHaveAttribute('href', '/play')
    expect(screen.queryByTestId('game-page')).not.toBeInTheDocument()
  })

  it('redirects authenticated /login visits to /play', async () => {
    useGameStore.setState({ user, token: 'token' })
    renderRoute('/login')
    expect(await screen.findByTestId('game-page')).toBeInTheDocument()
  })

  it('does not leak gameplay overlays onto the public homepage', async () => {
    useGameStore.setState({
      user,
      token: 'token',
      wsStatus: 'reconnecting',
      achievementToast: { code: 'first', title: 'First Visit', reward_sc: 5 },
      pendingEncounter: { resident_slug: 'mei', resident_name: '梅', location_id: 'square', opener: '你好' },
    })
    renderRoute('/')
    await screen.findByRole('heading', { level: 1, name: 'Simverse World' })
    expect(screen.queryByText('连接已断开，正在重连…')).not.toBeInTheDocument()
    expect(screen.queryByText('First Visit')).not.toBeInTheDocument()
    expect(screen.queryByText('你好')).not.toBeInTheDocument()
  })
})
