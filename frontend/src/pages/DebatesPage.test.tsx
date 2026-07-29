import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { DebatesPage } from './DebatesPage'
import type { DebateView } from '../services/api'

const getDebates = vi.fn()
const getDebate = vi.fn()
const getMe = vi.fn()
const stakeDebate = vi.fn()
const voteDebate = vi.fn()

vi.mock('../services/api', () => ({
  getDebates: (...a: unknown[]) => getDebates(...a),
  getDebate: (...a: unknown[]) => getDebate(...a),
  getMe: (...a: unknown[]) => getMe(...a),
  stakeDebate: (...a: unknown[]) => stakeDebate(...a),
  voteDebate: (...a: unknown[]) => voteDebate(...a),
}))

vi.mock('../services/ws', () => ({
  connectWS: vi.fn(),
  onWSMessage: vi.fn(() => () => {}),
}))

vi.mock('../components/TopNav', () => ({ TopNav: () => <nav /> }))

const DEBATE: DebateView = {
  id: 'd1',
  topic: '猫和狗谁更好',
  status: 'voting',
  resident_a_slug: 'cat-fan',
  resident_b_slug: 'dog-fan',
  pool_a: 30,
  pool_b: 20,
  votes_a: 3,
  votes_b: 2,
  winner: null,
  transcript: [],
}

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

beforeEach(() => {
  getDebates.mockReset().mockResolvedValue({ debates: [DEBATE] })
  getDebate.mockReset().mockResolvedValue(DEBATE)
  getMe.mockReset().mockResolvedValue({ soul_coin_balance: 0 })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('DebatesPage mobile layout', () => {
  it('shows both list and detail panes side by side on desktop', async () => {
    stubMatchMedia(false)
    render(<DebatesPage />)
    expect(await screen.findByText('猫和狗谁更好')).toBeInTheDocument()
    expect(screen.getByTestId('debate-list-pane')).toBeInTheDocument()
    expect(screen.getByTestId('debate-detail-pane')).toBeInTheDocument()
    expect(screen.getByText('从左侧选择一场辩论查看详情')).toBeInTheDocument()
    // No back button on desktop.
    expect(screen.queryByRole('button', { name: /返回列表/ })).not.toBeInTheDocument()
  })

  it('shows only the full-width list on mobile before anything is selected', async () => {
    stubMatchMedia(true)
    render(<DebatesPage />)
    expect(await screen.findByText('猫和狗谁更好')).toBeInTheDocument()
    expect(screen.getByTestId('debate-list-pane')).toBeInTheDocument()
    // Master/detail: the detail pane isn't rendered at all yet.
    expect(screen.queryByTestId('debate-detail-pane')).not.toBeInTheDocument()
  })

  it('switches to a full-width detail pane with a back button after selecting, hiding the list', async () => {
    stubMatchMedia(true)
    render(<DebatesPage />)
    fireEvent.click(await screen.findByText('猫和狗谁更好'))

    await waitFor(() => expect(screen.getByTestId('debate-detail-pane')).toBeInTheDocument())
    // Master/detail: list pane is gone while detail is shown.
    expect(screen.queryByTestId('debate-list-pane')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /返回列表/ })).toBeInTheDocument()
  })

  it('returns to the list when the back button is clicked', async () => {
    stubMatchMedia(true)
    render(<DebatesPage />)
    fireEvent.click(await screen.findByText('猫和狗谁更好'))
    fireEvent.click(await screen.findByRole('button', { name: /返回列表/ }))

    expect(screen.getByTestId('debate-list-pane')).toBeInTheDocument()
    expect(screen.queryByTestId('debate-detail-pane')).not.toBeInTheDocument()
  })
})
