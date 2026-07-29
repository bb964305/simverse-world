import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { ProfileSidebar } from './ProfileSidebar'

const setProfileTab = vi.fn()

vi.mock('../../stores/gameStore', () => ({
  useGameStore: (sel: (s: unknown) => unknown) => sel({
    user: { name: '测试用户', soul_coin_balance: 42 },
    profileTab: 'residents',
    setProfileTab: (...a: unknown[]) => setProfileTab(...a),
  }),
}))

function stubMatchMedia(matches: boolean) {
  vi.stubGlobal('matchMedia', vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
  })))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  setProfileTab.mockClear()
})

describe('ProfileSidebar mobile layout', () => {
  it('renders a full-width horizontal tab strip on narrow viewports', () => {
    stubMatchMedia(true)
    render(<ProfileSidebar residentCount={3} />)
    expect(screen.getByTestId('profile-sidebar-mobile')).toBeInTheDocument()
    // Balance is folded into the condensed header, not its own labelled block.
    expect(screen.queryByText('Soul Coin')).not.toBeInTheDocument()
    expect(screen.getByText(/🪙 42/)).toBeInTheDocument()
  })

  it('still switches tabs from the mobile strip', () => {
    stubMatchMedia(true)
    render(<ProfileSidebar residentCount={0} />)
    fireEvent.click(screen.getByRole('button', { name: /创作者/ }))
    expect(setProfileTab).toHaveBeenCalledWith('creator')
  })

  it('renders the full vertical sidebar on desktop, unchanged', () => {
    stubMatchMedia(false)
    render(<ProfileSidebar residentCount={3} />)
    expect(screen.queryByTestId('profile-sidebar-mobile')).not.toBeInTheDocument()
    expect(screen.getByText('Soul Coin')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /我的居民/ })).toBeInTheDocument()
  })
})
