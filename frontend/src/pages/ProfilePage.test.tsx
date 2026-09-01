import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { ProfilePage } from './ProfilePage'

// ProfilePage fans out to nine tab panels plus the sidebar, each with its
// own data fetching — out of scope for a layout test. Stub them so this
// test only exercises ProfilePage's own row/column split.
vi.mock('../components/TopNav', () => ({ TopNav: () => <nav /> }))
vi.mock('../components/profile/ProfileSidebar', () => ({
  ProfileSidebar: () => <div>sidebar-stub</div>,
}))
vi.mock('../components/profile/ResidentList', () => ({ ResidentList: () => <div>residents-stub</div> }))
vi.mock('../components/profile/ConversationHistory', () => ({ ConversationHistory: () => <div /> }))
vi.mock('../components/profile/TransactionHistory', () => ({ TransactionHistory: () => <div /> }))
vi.mock('../components/profile/ResidentEditor', () => ({ ResidentEditor: () => <div /> }))
vi.mock('../components/profile/SettingsPanel', () => ({ SettingsPanel: () => <div /> }))
vi.mock('../components/profile/AchievementsPanel', () => ({ AchievementsPanel: () => <div /> }))
vi.mock('../components/profile/FeedList', () => ({ FeedList: () => <div /> }))
vi.mock('../components/profile/WeeklyRecap', () => ({ WeeklyRecap: () => <div /> }))
vi.mock('../components/profile/ExplorationCodex', () => ({ ExplorationCodex: () => <div /> }))
vi.mock('../components/profile/CreatorDashboard', () => ({ CreatorDashboard: () => <div /> }))

vi.mock('../stores/gameStore', () => ({
  useGameStore: (sel: (s: unknown) => unknown) => sel({ profileTab: 'residents' }),
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
})

describe('ProfilePage mobile layout', () => {
  it('stacks the sidebar above content on narrow viewports', async () => {
    stubMatchMedia(true)
    render(<ProfilePage />)
    expect(screen.getByTestId('profile-layout').style.flexDirection).toBe('column')
    expect(await screen.findByText('residents-stub')).toBeInTheDocument()
  })

  it('keeps the desktop row layout at normal widths, unchanged', async () => {
    stubMatchMedia(false)
    render(<ProfilePage />)
    expect(screen.getByTestId('profile-layout').style.flexDirection).toBe('row')
    expect(await screen.findByText('residents-stub')).toBeInTheDocument()
  })
})
