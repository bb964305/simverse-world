import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SeasonsPage } from './SeasonsPage'

const getCurrentSeason = vi.fn()
const getSeasonLeaderboard = vi.fn()
const getOpenPolls = vi.fn()
const votePoll = vi.fn()

vi.mock('../services/api', () => ({
  getCurrentSeason: (...a: unknown[]) => getCurrentSeason(...a),
  getSeasonLeaderboard: (...a: unknown[]) => getSeasonLeaderboard(...a),
  getOpenPolls: (...a: unknown[]) => getOpenPolls(...a),
  votePoll: (...a: unknown[]) => votePoll(...a),
}))

vi.mock('../components/TopNav', () => ({ TopNav: () => <nav /> }))
vi.mock('../stores/gameStore', () => ({
  useGameStore: (sel: (s: unknown) => unknown) => sel({ user: { id: 'u1' } }),
}))

// 生产 /polls/open 的真实形状：option 是对象，不是字符串。
// 这里刻意保留旧后端才会出现的内部字段，钉住「前端自己也不能崩」。
const CIVIC_POLL = {
  id: 'poll-civic',
  season_id: null,
  question: '在南苑空地兴建一座邮局',
  options: [
    { label: '赞成兴建', npc_votes: 20 },
    { label: '暂缓,维持现状', npc_votes: 3 },
  ],
  closes_at: null,
  is_election: false,
}

const ELECTION_POLL = {
  id: 'poll-elec',
  season_id: null,
  question: '镇长选举:谁来当下一任镇长?',
  options: [{ label: '赵启文', npc_votes: 17 }, { label: '何巧云', npc_votes: 5 }],
  closes_at: null,
  is_election: true,
}

beforeEach(() => {
  getCurrentSeason.mockReset().mockResolvedValue({ season: null })
  getSeasonLeaderboard.mockReset().mockResolvedValue({ top: [], season: null })
  getOpenPolls.mockReset().mockResolvedValue({ polls: [CIVIC_POLL] })
  votePoll.mockReset().mockResolvedValue({ ok: true })
})

afterEach(cleanup)

function renderPage() {
  return render(<MemoryRouter><SeasonsPage /></MemoryRouter>)
}

describe('SeasonsPage', () => {
  it('renders object-shaped options as their label instead of crashing', async () => {
    renderPage()
    expect(await screen.findByRole('button', { name: /赞成兴建/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /暂缓/ })).toBeInTheDocument()
  })

  it('casts a vote and marks the chosen option', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /赞成兴建/ }))
    await waitFor(() => expect(votePoll).toHaveBeenCalledWith('poll-civic', 0))
    expect(await screen.findByText('✓已投')).toBeInTheDocument()
  })

  it('restores the voted marker from my_vote across reloads', async () => {
    getOpenPolls.mockResolvedValue({ polls: [{ ...CIVIC_POLL, my_vote: 1 }] })
    renderPage()
    expect(await screen.findByText('✓已投')).toBeInTheDocument()
    expect(votePoll).not.toHaveBeenCalled()
  })

  it('shows the already-voted branch when the backend rejects a repeat', async () => {
    votePoll.mockRejectedValue(new Error('API 400: {"detail":"already voted on this poll"}'))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /赞成兴建/ }))
    expect(await screen.findByText('已投过')).toBeInTheDocument()
  })

  it('splits mayor elections into their own labelled section', async () => {
    getOpenPolls.mockResolvedValue({ polls: [CIVIC_POLL, ELECTION_POLL] })
    renderPage()
    expect(await screen.findByText('🏛️ 镇长选举')).toBeInTheDocument()
    expect(screen.getByText('🗳️ 议案投票')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /赵启文/ })).toBeInTheDocument()
  })

  it('hides the election section when there is no election running', async () => {
    renderPage()
    expect(await screen.findByText('🗳️ 议案投票')).toBeInTheDocument()
    expect(screen.queryByText('🏛️ 镇长选举')).not.toBeInTheDocument()
  })
})
