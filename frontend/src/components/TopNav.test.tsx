import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { TopNav } from './TopNav'
import { useGameStore } from '../stores/gameStore'
import { bridge } from '../game/phaserBridge'
import { useLocale } from '../services/locale'

const caravanHarness = vi.hoisted(() => ({
  listener: null as ((projection: { snapshot: unknown }) => void) | null,
}))

vi.mock('../services/ws', () => ({
  disconnectWS: vi.fn(),
  onWSMessage: () => () => {},
}))

vi.mock('../services/api', () => ({
  getNotifications: vi.fn().mockResolvedValue({ notifications: [], unread_count: 0 }),
  getDailyQuest: vi.fn().mockResolvedValue({ login_streak: 1, quest: null }),
  getActiveEvents: vi.fn().mockResolvedValue({ events: [] }),
  getMe: vi.fn().mockResolvedValue({ soul_coin_balance: 110, lab_enabled: true }),
}))

vi.mock('../services/caravanProjection', () => ({
  refreshCaravanProjection: vi.fn().mockResolvedValue({ snapshot: null }),
  subscribeCaravanProjection: (listener: (projection: { snapshot: unknown }) => void) => {
    caravanHarness.listener = listener
    listener({ snapshot: null })
    return () => {
      if (caravanHarness.listener === listener) caravanHarness.listener = null
    }
  },
  caravanBannerText: (state: { visible?: boolean; phase?: string } | null) => {
    if (!state?.visible) return null
    return state.phase === 'trading' ? '商队已在集市大厅开摊' : null
  },
}))

vi.mock('./SearchDropdown', () => ({ SearchDropdown: () => <div data-testid="resident-search" /> }))
vi.mock('./NotificationDrawer', () => ({
  NotificationDrawer: () => <div role="region" aria-label="通知面板" />,
}))
vi.mock('./DigestModal', () => ({
  DigestModal: ({ onClose }: { onClose: () => void }) => (
    <div role="dialog" aria-label="村落日报"><button onClick={onClose}>关闭日报</button></div>
  ),
}))
vi.mock('./CommissionModal', () => ({
  CommissionModal: ({ onClose }: { onClose: () => void }) => (
    <div role="dialog" aria-label="委托板"><button onClick={onClose}>关闭委托</button></div>
  ),
}))
vi.mock('./ShopModal', () => ({
  ShopModal: ({ onClose }: { onClose: () => void }) => (
    <div role="dialog" aria-label="杂货铺"><button onClick={onClose}>关闭商店</button></div>
  ),
}))
vi.mock('./BulletinBoard', () => ({ BulletinBoard: () => null }))
vi.mock('./ExperimentPanel', () => ({ ExperimentPanel: () => null }))
vi.mock('./TownHallPanel', () => ({ TownHallPanel: () => null }))
vi.mock('./LabTerminalPanel', () => ({ LabTerminalPanel: () => null }))

const user = {
  id: 'user-1',
  name: 'UI QA',
  email: 'ui@example.com',
  avatar: null,
  soul_coin_balance: 110,
}

beforeEach(() => {
  useLocale.setState({ locale: 'zh-CN' })
  useGameStore.setState({ user, token: 'token', unreadCount: 2, digestUnread: true })
})

afterEach(() => {
  cleanup()
  caravanHarness.listener = null
})

function renderNav() {
  return render(<MemoryRouter><TopNav /></MemoryRouter>)
}

describe('TopNav overlay ownership', () => {
  it('shows the live caravan banner and removes it on a terminal snapshot', () => {
    renderNav()
    act(() => caravanHarness.listener?.({ snapshot: { visible: true, phase: 'trading' } }))
    expect(screen.getByRole('status')).toHaveTextContent('靛篷商队 · 商队已在集市大厅开摊')

    act(() => caravanHarness.listener?.({ snapshot: { visible: false, phase: 'departed' } }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('keeps account and notification popovers mutually exclusive', async () => {
    renderNav()
    fireEvent.click(screen.getByTitle('通知'))
    expect(screen.getByRole('region', { name: '通知面板' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '账号菜单' }))
    expect(screen.queryByRole('region', { name: '通知面板' })).not.toBeInTheDocument()
    expect(screen.getByText('UI QA')).toBeInTheDocument()

    await waitFor(() => expect(screen.getByText('🪙 110')).toBeInTheDocument())
  })

  it('mounts menu dialogs outside nav and closes them with Escape', () => {
    renderNav()
    fireEvent.click(screen.getByRole('button', { name: '打开世界菜单' }))
    fireEvent.click(screen.getByRole('menuitem', { name: /商店/ }))

    const dialog = screen.getByRole('dialog', { name: '杂货铺' })
    expect(dialog).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: '游戏主导航' })).not.toContainElement(dialog)

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: '杂货铺' })).not.toBeInTheDocument()
  })
})

describe('TopNav townhall + lab-terminal entries', () => {
  it('emits townhall:open when the 市政厅 entry is clicked', () => {
    const spy = vi.fn()
    const unsub = bridge.on('townhall:open', spy)
    renderNav()
    fireEvent.click(screen.getByRole('button', { name: /市政厅/ }))
    expect(spy).toHaveBeenCalledTimes(1)
    unsub()
  })

  it('emits labterminal:open when lab is enabled', async () => {
    const spy = vi.fn()
    const unsub = bridge.on('labterminal:open', spy)
    renderNav()
    // lab_enabled resolves from getMe(); wait for the gated entry to appear.
    const btn = await screen.findByRole('button', { name: /实验楼终端/ })
    fireEvent.click(btn)
    expect(spy).toHaveBeenCalledTimes(1)
    unsub()
  })
})
