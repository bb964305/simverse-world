import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import { bridge } from '../game/phaserBridge'
import { TownHallPanel } from './TownHallPanel'
import type { TownHallOverview } from '../services/api'

const getTownHallOverview = vi.fn()

vi.mock('../services/api', () => ({
  getTownHallOverview: (...a: unknown[]) => getTownHallOverview(...a),
}))

const OVERVIEW: TownHallOverview = {
  mayor: { slug: 'zhao', name: '赵四' },
  duties: [
    { key: 'town_clerk', title: '公告与登记处', holder_slug: 'zhao', holder_name: '赵四' },
    { key: 'market_warden', title: '集市管理员', holder_slug: 'qian', holder_name: '钱五' },
  ],
  open_polls: [
    {
      id: 'poll-1', season_id: null, question: '要不要修喷泉？',
      options: [{ label: '修' }, { label: '不修' }], closes_at: null,
    },
  ],
  recent_election: {
    question: '镇长选举:2026春',
    closed_at: '2026-03-01T00:00:00Z',
    winner_slug: 'zhao', winner_name: '赵四', winner_votes: 12,
    options: [{ label: '赵四', won: true, final_votes: 12 }],
  },
  finances: {
    npc_default_wage_sc: 10,
    npc_meal_cost_sc: 3,
    market_day_weekday: 5,
    market_day_discount: 0.9,
  },
  // 开闸前 vm212 的真实形态:enabled=false 但 recompute 落库数据仍在
  reputation: {
    enabled: false,
    credit_min_score: 0.0058,
    residents: [
      {
        slug: 'gao', name: '高分居', score: 0.17, samples: 140,
        updated_at: '2026-08-05T00:00:00+00:00', credit_ok: true,
      },
      {
        slug: 'di', name: '低分居', score: -0.07, samples: 425,
        updated_at: '2026-08-05T00:00:00+00:00', credit_ok: false,
      },
    ],
  },
}

beforeEach(() => {
  getTownHallOverview.mockReset().mockResolvedValue(OVERVIEW)
})

afterEach(cleanup)

async function openPanel() {
  render(<TownHallPanel />)
  act(() => { bridge.emit('townhall:open') })
  return waitFor(() => expect(getTownHallOverview).toHaveBeenCalled())
}

describe('TownHallPanel', () => {
  it('is closed until the bridge opens it', () => {
    render(<TownHallPanel />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders the mayor on open', async () => {
    await openPanel()
    await waitFor(() => expect(screen.getByText(/赵四/)).toBeInTheDocument())
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('switches to the office tab and lists duty holders', async () => {
    await openPanel()
    await waitFor(() => expect(screen.getByRole('button', { name: /职位/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /职位/ }))
    expect(screen.getByText('公告与登记处')).toBeInTheDocument()
    expect(screen.getByText('集市管理员')).toBeInTheDocument()
  })

  it('shows open polls under the proposal tab', async () => {
    await openPanel()
    fireEvent.click(await screen.findByRole('button', { name: /议案/ }))
    await waitFor(() => expect(screen.getByText('要不要修喷泉？')).toBeInTheDocument())
  })

  it('shows the recent election result', async () => {
    await openPanel()
    fireEvent.click(await screen.findByRole('button', { name: /选举结果/ }))
    await waitFor(() => expect(screen.getByText(/镇长选举/)).toBeInTheDocument())
    expect(screen.getByText(/12/)).toBeInTheDocument()
  })

  it('lists reputation rows sorted with a credit badge under the rep tab', async () => {
    await openPanel()
    fireEvent.click(await screen.findByRole('button', { name: /声誉/ }))
    await waitFor(() => expect(screen.getByText('高分居')).toBeInTheDocument())
    const names = screen.getAllByText(/分居$/).map((e) => e.textContent)
    expect(names).toEqual(['高分居', '低分居'])
    expect(screen.getAllByText('信用受限')).toHaveLength(1)
    // enabled=false 且有数据 → 未开闸横幅与名单并存,而不是整页空态
    expect(screen.getByText(/未开闸/)).toBeInTheDocument()
    expect(screen.getByText(/信用阈值/)).toBeInTheDocument()
  })

  it('shows the gated empty state when reputation has no rows', async () => {
    getTownHallOverview.mockResolvedValue({
      ...OVERVIEW,
      reputation: { enabled: false, credit_min_score: 0.0058, residents: [] },
    })
    await openPanel()
    fireEvent.click(await screen.findByRole('button', { name: /声誉/ }))
    await waitFor(() =>
      expect(screen.getByText(/未开闸.*暂无公示数据/)).toBeInTheDocument())
    expect(screen.queryByText('信用受限')).not.toBeInTheDocument()
  })

  it('closes on the close button', async () => {
    await openPanel()
    fireEvent.click(await screen.findByLabelText('关闭市政厅'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})
