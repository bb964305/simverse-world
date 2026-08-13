import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { TownPage } from './TownPage'

const getPublicTownSnapshot = vi.fn()

vi.mock('../services/spectator', async () => {
  const actual = await vi.importActual<typeof import('../services/spectator')>('../services/spectator')
  return { ...actual, getPublicTownSnapshot: (...args: unknown[]) => getPublicTownSnapshot(...args) }
})

afterEach(() => {
  cleanup()
  getPublicTownSnapshot.mockReset()
  vi.useRealTimers()
})

describe('TownPage', () => {
  it('renders an anonymous, read-only town projection', async () => {
    getPublicTownSnapshot.mockResolvedValue({
      generated_at: '2026-08-13T12:00:00Z',
      world_time: '2026-08-13T20:00:00+08:00',
      counts: { residents: 2, agents: 1, humans: 0, online: 1 },
      residents: [
        { slug: 'mei', name: '梅', kind: 'npc', status: '散步', district: 'central_plaza', tile_x: 30, tile_y: 20, is_online: true },
        { slug: 'agent-a', name: '观测员 A', kind: 'agent', status: '观察中', district: 'central_plaza', tile_x: 33, tile_y: 20, is_online: true },
      ],
      activity: [{ at: '2026-08-13T12:00:00Z', summary: '一位 Agent 来到中央广场' }],
    })

    render(<MemoryRouter><TownPage /></MemoryRouter>)

    expect(await screen.findByRole('heading', { name: '小镇正在运行' })).toBeInTheDocument()
    expect(screen.getByText('观测员 A')).toBeInTheDocument()
    expect(screen.getByText('一位 Agent 来到中央广场')).toBeInTheDocument()
    expect(screen.getByText('Agent 玩家')).toBeInTheDocument()
    expect(screen.getAllByText(/中央广场 ·/)).not.toHaveLength(0)
    expect(screen.queryByRole('button', { name: /移动|交谈|发送/ })).not.toBeInTheDocument()
    expect(getPublicTownSnapshot).toHaveBeenCalledTimes(1)
  })

  it('waits for the current poll to finish before scheduling another one', async () => {
    vi.useFakeTimers()
    getPublicTownSnapshot.mockReturnValue(new Promise(() => undefined))

    render(<MemoryRouter><TownPage /></MemoryRouter>)

    expect(getPublicTownSnapshot).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(getPublicTownSnapshot).toHaveBeenCalledTimes(1)
  })

  it('shows a retryable error without falling back to the authenticated game', async () => {
    getPublicTownSnapshot.mockRejectedValue(new Error('公共投影暂不可用'))
    render(<MemoryRouter><TownPage /></MemoryRouter>)
    expect(await screen.findByRole('alert')).toHaveTextContent('公共投影暂不可用')
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })
})
