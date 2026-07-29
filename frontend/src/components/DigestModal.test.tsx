import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { DigestModal } from './DigestModal'
import type { DigestData } from '../services/api'

const getLatestDigest = vi.fn()

vi.mock('../services/api', () => ({
  getLatestDigest: (...a: unknown[]) => getLatestDigest(...a),
}))

const REAL_DIGEST: DigestData = {
  id: 'd1', scope: 'village', date: '2026-07-27', title: '2026-07-27 村落日报',
  content_md: '# 2026-07-27 村落日报\n小镇今天很热闹，大家都在广场上聊天。',
  stats: {}, created_at: null,
}

const EMPTY_DIGEST: DigestData = {
  id: 'd2', scope: 'village', date: '2026-07-28', title: '2026-07-28 村落日报',
  content_md: '',
  stats: {}, created_at: null,
}

afterEach(cleanup)

describe('DigestModal', () => {
  it('renders the digest body when the backend returns a real digest', async () => {
    getLatestDigest.mockReset().mockResolvedValue({ digest: REAL_DIGEST })
    render(<DigestModal onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText(/小镇今天很热闹/)).toBeInTheDocument())
    expect(screen.getByText('2026-07-27')).toBeInTheDocument()
    expect(screen.queryByText(/还没有日报/)).not.toBeInTheDocument()
    expect(screen.queryByText(/内容还没准备好/)).not.toBeInTheDocument()
  })

  // E2E-08: 后端理论上已经在读取侧过滤掉空正文行了，但前端不应该只信任
  // 后端这一层——它需要能扛住后端回滚 / 未来接入的其它数据源，直接判空
  // digest.content_md，而不是只判断 digest 是否为 null。
  it('shows the empty-content copy (not a blank body) when digest is non-null but content_md is empty', async () => {
    getLatestDigest.mockReset().mockResolvedValue({ digest: EMPTY_DIGEST })
    render(<DigestModal onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText(/内容还没准备好/)).toBeInTheDocument())
    expect(screen.queryByText(/还没有日报/)).not.toBeInTheDocument()
    // The stray date line + empty ReactMarkdown block must not render either.
    expect(screen.queryByText('2026-07-28')).not.toBeInTheDocument()
  })

  it('shows the no-digest-at-all copy when digest is null', async () => {
    getLatestDigest.mockReset().mockResolvedValue({ digest: null })
    render(<DigestModal onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText(/还没有日报/)).toBeInTheDocument())
    expect(screen.queryByText(/内容还没准备好/)).not.toBeInTheDocument()
  })
})
