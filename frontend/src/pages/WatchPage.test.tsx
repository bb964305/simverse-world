import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { WatchPage } from './WatchPage'
import { SpectatorApiError } from '../services/spectator'

const createViewerSession = vi.fn()
const deleteViewerSession = vi.fn()
const getViewerSnapshot = vi.fn()

vi.mock('../services/spectator', async () => {
  const actual = await vi.importActual<typeof import('../services/spectator')>('../services/spectator')
  return {
    ...actual,
    createViewerSession: (...args: unknown[]) => createViewerSession(...args),
    deleteViewerSession: (...args: unknown[]) => deleteViewerSession(...args),
    getViewerSnapshot: (...args: unknown[]) => getViewerSnapshot(...args),
  }
})

const VIEW = {
  generated_at: '2026-08-13T12:00:00Z',
  agent: {
    slug: 'agent-a', name: '观测员 A', status: '探索中', model_label: 'Model A',
    current_goal: '认识梅', is_online: true,
  },
  self: {
    slug: 'agent-a', name: '观测员 A', kind: 'agent', status: '探索中', district: 'central_plaza', tile_x: 32, tile_y: 20,
  },
  nearby: {
    residents: [{ slug: 'mei', name: '梅', kind: 'npc', status: '空闲', district: 'central_plaza', tile_x: 34, tile_y: 20 }],
    players: [],
  },
  location: 'central_plaza',
  recent_events: [{ at: '2026-08-13T12:00:00Z', summary: '梅进入视野' }],
}

afterEach(() => {
  cleanup()
  createViewerSession.mockReset()
  deleteViewerSession.mockReset().mockResolvedValue({ ok: true })
  getViewerSnapshot.mockReset()
})

describe('WatchPage', () => {
  it('exchanges the view code, clears it from the DOM, and renders only the bound view', async () => {
    getViewerSnapshot
      .mockRejectedValueOnce(new SpectatorApiError(401, 'no session'))
      .mockResolvedValue(VIEW)
    createViewerSession.mockResolvedValue({ ok: true })

    render(<MemoryRouter><WatchPage /></MemoryRouter>)
    const input = await screen.findByLabelText('View token')
    fireEvent.change(input, { target: { value: 'sv_view_secret' } })
    fireEvent.click(screen.getByRole('button', { name: '开始跟随' }))

    await waitFor(() => expect(createViewerSession).toHaveBeenCalledWith('sv_view_secret'))
    expect(await screen.findByRole('heading', { name: '观测员 A' })).toBeInTheDocument()
    expect(screen.getByText('梅进入视野')).toBeInTheDocument()
    expect(screen.getByText('中央广场')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('sv_view_secret')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /移动|交谈|发送/ })).not.toBeInTheDocument()
  })

  it('resumes an existing HttpOnly viewer session without asking for the code again', async () => {
    getViewerSnapshot.mockResolvedValue(VIEW)
    render(<MemoryRouter><WatchPage /></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: '观测员 A' })).toBeInTheDocument()
    expect(screen.queryByLabelText('View token')).not.toBeInTheDocument()
    expect(createViewerSession).not.toHaveBeenCalled()
  })

  it('surfaces an invalid or revoked view code', async () => {
    getViewerSnapshot.mockRejectedValue(new SpectatorApiError(401, 'no session'))
    createViewerSession.mockRejectedValue(new SpectatorApiError(401, '查看码已撤销'))
    render(<MemoryRouter><WatchPage /></MemoryRouter>)
    const input = await screen.findByLabelText('View token')
    fireEvent.change(input, { target: { value: 'revoked' } })
    fireEvent.click(screen.getByRole('button', { name: '开始跟随' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('查看码已撤销')
  })

  it('ends the cookie session before accepting a different view code', async () => {
    getViewerSnapshot.mockResolvedValue(VIEW)
    render(<MemoryRouter><WatchPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '结束并更换查看码' }))
    await waitFor(() => expect(deleteViewerSession).toHaveBeenCalledTimes(1))
    expect(await screen.findByLabelText('View token')).toBeInTheDocument()
  })

  it('ignores a stale initial cookie probe after a successful token submission', async () => {
    let rejectInitial!: (reason?: unknown) => void
    getViewerSnapshot
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectInitial = reject }))
      .mockResolvedValue(VIEW)
    createViewerSession.mockResolvedValue({ ok: true })

    render(<MemoryRouter><WatchPage /></MemoryRouter>)
    fireEvent.change(await screen.findByLabelText('View token'), { target: { value: 'sv_view_secret' } })
    fireEvent.click(screen.getByRole('button', { name: '开始跟随' }))

    expect(await screen.findByRole('heading', { name: '观测员 A' })).toBeInTheDocument()
    rejectInitial(new SpectatorApiError(401, 'stale no session'))

    await waitFor(() => {
      expect(screen.queryByLabelText('View token')).not.toBeInTheDocument()
      expect(screen.getByText('梅进入视野')).toBeInTheDocument()
    })
  })
})
