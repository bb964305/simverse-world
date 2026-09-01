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

// 生产 /polls/open 的真实形状：option 是对象，不是字符串。当前后端
// （script_service.public_option）已经把内部字段清洗掉了，所以这个 fixture
// 只有 {label, npc_votes} —— 见下面 LEGACY_SHAPE_POLL，那个才是刻意保留
// 内部字段/旧形状，用来钉住 PollCard 的防御分支。
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

// PollCard 的防御分支（typeof opt === 'string' 兜字符串选项、
// opt?.label ?? `选项 ${idx+1}` 兜缺 label）此前没有任何测试覆盖。这里造一个
// 未过 public_option 清洗 / 旧后端才会吐出的形状：一个选项挂着 effect /
// _npc_voters / _proposer_slug 等内部 blob，一个是历史 string[] 数据
// （test_script_season.py 手工造的季就是 ["管家", "园丁"]），第三个是对象但
// 缺 label（`opt?.label ?? \`选项 ${idx + 1}\`` 分支此前只覆盖了
// typeof opt === 'string'，没有任何用例覆盖"是对象但没有 label"这一半）。
// 存在理由是「后端未部署新版本 / 回滚时页面不能崩」，值得钉住。
const LEGACY_SHAPE_POLL = {
  id: 'poll-legacy',
  season_id: null,
  question: '旧形状兼容性测试',
  options: [
    {
      label: '赞成兴建', npc_votes: 20,
      effect: { type: 'dynamic_location', data: { slug: 'post_office', bounds: [44, 100, 48, 106] } },
      _npc_voters: ['a', 'b'], _proposer_slug: 'prop', _eligible_at_open: ['a', 'b', 'c'],
    },
    '园丁',
    { npc_votes: 3 },
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
    expect(await screen.findByText('✓ 已投')).toBeInTheDocument()
  })

  it('restores the voted marker from my_vote across reloads', async () => {
    getOpenPolls.mockResolvedValue({ polls: [{ ...CIVIC_POLL, my_vote: 1 }] })
    renderPage()
    expect(await screen.findByText('✓ 已投')).toBeInTheDocument()
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

  it('tolerates legacy option shapes (internal fields, raw strings) without crashing', async () => {
    getOpenPolls.mockResolvedValue({ polls: [LEGACY_SHAPE_POLL] })
    renderPage()
    // Object option with internal blob still renders by its label only.
    expect(await screen.findByRole('button', { name: /赞成兴建/ })).toBeInTheDocument()
    // Raw string option (typeof opt === 'string' branch) renders as-is.
    expect(screen.getByRole('button', { name: '园丁' })).toBeInTheDocument()
    // Object option missing `label` (opt?.label ?? `选项 ${idx+1}` branch)
    // falls back to the positional placeholder — it's the 3rd option (idx 2).
    expect(screen.getByRole('button', { name: '选项 3' })).toBeInTheDocument()
    // None of the internal fields leak into the rendered DOM.
    expect(screen.queryByText(/_npc_voters/)).not.toBeInTheDocument()
    expect(screen.queryByText(/dynamic_location/)).not.toBeInTheDocument()
  })
})
