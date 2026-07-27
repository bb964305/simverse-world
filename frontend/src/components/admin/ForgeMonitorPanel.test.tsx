import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../services/api', () => ({
  getAdminForgeActive: vi.fn(),
  getAdminForgeHistory: vi.fn(),
}))

import { getAdminForgeActive, getAdminForgeHistory } from '../../services/api'
import { ForgeMonitorPanel } from './ForgeMonitorPanel'

afterEach(cleanup)

// 后端 ForgeSessionListItem 的真实形状(backend/app/schemas/admin.py:145)。
// 面板曾经声明 forge_id / started_at / elapsed_seconds,后端一个都不返回。
const SESSION = {
  id: 'f1e2d3c4-aaaa-bbbb-cccc-000000000001',
  user_id: 'u-1',
  character_name: '测试角色',
  mode: 'deep',
  status: 'building',
  current_stage: 'building',
  created_at: '2026-07-27T03:00:00',
  updated_at: '2026-07-27T03:04:00',
}

describe('ForgeMonitorPanel', () => {
  it('renders an active session from the real backend payload', async () => {
    vi.mocked(getAdminForgeActive).mockResolvedValue([SESSION])
    vi.mocked(getAdminForgeHistory).mockResolvedValue({
      items: [], total: 0, offset: 0, limit: 20,
    })

    render(<ForgeMonitorPanel token="t" />)

    await waitFor(() => expect(screen.getByText('测试角色')).toBeTruthy())
    // id 前 8 位,而不是对 undefined 调 .slice 抛 TypeError
    expect(screen.getByText('f1e2d3c4')).toBeTruthy()
    expect(screen.getByText('构建中')).toBeTruthy()
  })

  it('asks the history endpoint for offset/limit, not page/per_page', async () => {
    vi.mocked(getAdminForgeActive).mockResolvedValue([])
    vi.mocked(getAdminForgeHistory).mockResolvedValue({
      items: [SESSION], total: 1, offset: 0, limit: 20,
    })

    render(<ForgeMonitorPanel token="t" />)

    await waitFor(() => expect(getAdminForgeHistory).toHaveBeenCalled())
    const params = vi.mocked(getAdminForgeHistory).mock.calls[0][1]
    expect(params).toMatchObject({ offset: 0, limit: 20 })
    expect(params).not.toHaveProperty('page')
    expect(params).not.toHaveProperty('per_page')
  })
})
