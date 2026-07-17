import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AppRoutes } from './App'
import { useGameStore } from './stores/gameStore'

vi.mock('./pages/GamePage', () => ({
  GamePage: () => <main data-testid="game-page">Game World</main>,
}))

vi.mock('./pages/ForgePage', () => ({
  ForgePage: () => <main data-testid="forge-page">Forge</main>,
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
  it('shows the public landing page at / when logged out', async () => {
    renderRoute('/')
    expect(await screen.findByRole('heading', { level: 1, name: 'Simverse World' })).toBeInTheDocument()
    expect(screen.queryByTestId('game-page')).not.toBeInTheDocument()
  })

  it('shows the game at / when authenticated', async () => {
    useGameStore.setState({ user, token: 'token' })
    renderRoute('/')
    expect(await screen.findByTestId('game-page')).toBeInTheDocument()
  })

  it('redirects authenticated /login visits back to the game', async () => {
    useGameStore.setState({ user, token: 'token' })
    renderRoute('/login')
    expect(await screen.findByTestId('game-page')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '进入 Simverse' })).not.toBeInTheDocument()
  })

  it('shows encounter cards only on the authenticated game route', async () => {
    useGameStore.setState({
      user,
      token: 'token',
      pendingEncounter: { resident_slug: 'mei', resident_name: '梅', location_id: 'square', opener: '只在游戏里显示' },
    })

    const game = renderRoute('/')
    expect(await screen.findByTestId('game-page')).toBeInTheDocument()
    expect(screen.getByText('只在游戏里显示')).toBeInTheDocument()
    game.unmount()

    renderRoute('/forge')
    expect(await screen.findByTestId('forge-page')).toBeInTheDocument()
    expect(screen.queryByText('只在游戏里显示')).not.toBeInTheDocument()
  })

  it('does not leak gameplay overlays onto public pages', async () => {
    useGameStore.setState({
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
