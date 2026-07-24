import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import { bridge } from '../game/phaserBridge'
import { BulletinBoard } from './BulletinBoard'

const getBulletinPosts = vi.fn()

vi.mock('../services/api', () => ({
  getBulletinPosts: (...args: unknown[]) => getBulletinPosts(...args),
}))

function makePosts(n: number, prefix = 'p') {
  return Array.from({ length: n }, (_, i) => ({
    id: `${prefix}-${i}`,
    kind: 'notice',
    title: `帖子标题 ${prefix}-${i}`,
    content_md: '',
    likes: 0,
    tips_sc: 0,
    pinned: false,
    author_resident_id: null,
    author_name: '系统',
    author_portrait: null,
    created_at: null,
  }))
}

beforeEach(() => {
  getBulletinPosts.mockReset()
  // fetchBulletin() hits global fetch for the plaza tab; return a valid shape
  // so the (default) plaza view renders without crashing before we switch tabs.
  const plaza = JSON.stringify({ hot_residents: [], new_residents: [], recent_conversations_24h: 0 })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(plaza, { status: 200 })))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function openPostsTab() {
  render(<BulletinBoard />)
  act(() => { bridge.emit('bulletin:open') })
  fireEvent.click(await screen.findByRole('button', { name: '帖子' }))
}

describe('BulletinBoard pagination', () => {
  it('shows only the first page (pageSize) of loaded posts', async () => {
    getBulletinPosts.mockResolvedValue({ posts: makePosts(25), next_cursor: null })
    await openPostsTab()

    await waitFor(() => expect(screen.getByText('帖子标题 p-0')).toBeInTheDocument())
    // page size is 10 → the 11th post must not be on the first page
    expect(screen.getByText('帖子标题 p-9')).toBeInTheDocument()
    expect(screen.queryByText('帖子标题 p-10')).not.toBeInTheDocument()
  })

  it('renders the next slice after 下一页', async () => {
    getBulletinPosts.mockResolvedValue({ posts: makePosts(25), next_cursor: null })
    await openPostsTab()

    await waitFor(() => expect(screen.getByText('帖子标题 p-0')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /下一页/ }))
    expect(screen.getByText('帖子标题 p-10')).toBeInTheDocument()
    expect(screen.queryByText('帖子标题 p-0')).not.toBeInTheDocument()
  })

  it('resets to page 1 when the kind filter changes', async () => {
    getBulletinPosts.mockResolvedValue({ posts: makePosts(25), next_cursor: null })
    await openPostsTab()

    await waitFor(() => expect(screen.getByText('帖子标题 p-0')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /下一页/ }))
    expect(screen.getByText('帖子标题 p-10')).toBeInTheDocument()

    // switch filter → new fetch from the top, page resets to 1
    getBulletinPosts.mockResolvedValue({ posts: makePosts(25, 'q'), next_cursor: null })
    fireEvent.click(screen.getByRole('button', { name: '公告' }))
    await waitFor(() => expect(screen.getByText('帖子标题 q-0')).toBeInTheDocument())
    expect(screen.getByText('帖子标题 q-9')).toBeInTheDocument()
    expect(screen.queryByText('帖子标题 q-10')).not.toBeInTheDocument()
  })
})
