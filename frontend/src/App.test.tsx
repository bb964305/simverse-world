import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AppRoutes } from './App'
import { useGameStore } from './stores/gameStore'
import { checkOnboarding } from './services/api'

vi.mock('./pages/GamePage', () => ({
  GamePage: () => <main data-testid="game-page">Game World</main>,
}))

vi.mock('./pages/ForgePage', () => ({
  ForgePage: () => <main data-testid="forge-page">Forge</main>,
}))

vi.mock('./pages/OnboardingPage', () => ({
  OnboardingPage: () => <main data-testid="onboarding-page">Onboarding</main>,
}))

// HomeRoute (E2E-01) re-checks onboarding before rendering GamePage so a
// player landing on "/" directly (bookmark, closed tab, browser back) can't
// skip the resident picker. Stub the API call; individual tests override it.
vi.mock('./services/api', () => ({
  checkOnboarding: vi.fn(),
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
  // Default: onboarding already completed, so existing authenticated-route
  // tests that don't care about onboarding keep seeing GamePage.
  vi.mocked(checkOnboarding).mockReset().mockResolvedValue({
    needs_onboarding: false,
    player_resident_id: 'resident-1',
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

  it('shows the game at /play when authenticated', async () => {
    useGameStore.setState({ user, token: 'token' })
    renderRoute('/play')
    expect(await screen.findByTestId('game-page')).toBeInTheDocument()
  })

  it('redirects /play to login when logged out', async () => {
    renderRoute('/play')
    expect(await screen.findByRole('heading', { name: '进入 Simverse' })).toBeInTheDocument()
    expect(screen.queryByTestId('game-page')).not.toBeInTheDocument()
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

// E2E-01: landing directly on "/" (bookmark, closed tab, browser back) used
// to skip onboarding entirely because HomeRoute never asked. It now re-runs
// the same checkOnboarding call the /onboarding route makes.
describe('HomeRoute onboarding gate', () => {
  it('redirects to /onboarding when the backend says onboarding is needed', async () => {
    vi.mocked(checkOnboarding).mockResolvedValue({ needs_onboarding: true, player_resident_id: null })
    useGameStore.setState({ user, token: 'token' })

    renderRoute('/')

    expect(await screen.findByTestId('onboarding-page')).toBeInTheDocument()
    expect(screen.queryByTestId('game-page')).not.toBeInTheDocument()
    expect(checkOnboarding).toHaveBeenCalledWith('token')
  })

  it('renders the game when the backend says onboarding is already done', async () => {
    vi.mocked(checkOnboarding).mockResolvedValue({ needs_onboarding: false, player_resident_id: 'resident-1' })
    useGameStore.setState({ user, token: 'token' })

    renderRoute('/')

    expect(await screen.findByTestId('game-page')).toBeInTheDocument()
    expect(screen.queryByTestId('onboarding-page')).not.toBeInTheDocument()
  })

  it('fails open into the game when the onboarding check errors', async () => {
    vi.mocked(checkOnboarding).mockRejectedValue(new Error('network error'))
    useGameStore.setState({ user, token: 'token' })

    renderRoute('/')

    // Must not strand the player on the loading fallback just because the
    // network call failed — fail open rather than fail closed.
    expect(await screen.findByTestId('game-page')).toBeInTheDocument()
    expect(screen.queryByTestId('onboarding-page')).not.toBeInTheDocument()
  })

  it('does not flash the game before the onboarding check resolves', async () => {
    let resolveCheck!: (v: { needs_onboarding: boolean; player_resident_id: string | null }) => void
    vi.mocked(checkOnboarding).mockReturnValue(
      new Promise((resolve) => { resolveCheck = resolve })
    )
    useGameStore.setState({ user, token: 'token' })

    renderRoute('/')

    // While the check is in flight, neither the game nor onboarding should render.
    expect(screen.queryByTestId('game-page')).not.toBeInTheDocument()
    expect(screen.queryByTestId('onboarding-page')).not.toBeInTheDocument()

    resolveCheck({ needs_onboarding: true, player_resident_id: null })

    expect(await screen.findByTestId('onboarding-page')).toBeInTheDocument()
  })
})
