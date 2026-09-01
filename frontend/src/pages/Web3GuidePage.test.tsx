import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Web3GuidePage } from './Web3GuidePage'
import { useLocale } from '../services/locale'

vi.mock('../services/web3/connectivity', () => ({
  checkProductionConnectivity: vi.fn().mockResolvedValue({
    api: { ok: true, detail: 'https://simverse.space/api/health' },
    chain: { ok: true, detail: 'Robinhood Chain · 4663' },
    contract: { ok: true, detail: 'Proxy online · next Agent #1' },
    checkedAt: '2026-09-01T13:00:00.000Z',
  }),
}))

beforeEach(() => useLocale.setState({ locale: 'zh-CN' }))
afterEach(() => { cleanup(); document.body.classList.remove('guide-page-open') })

describe('Web3GuidePage', () => {
  it('shows an actionable end-to-end guide and live connectivity', async () => {
    render(<MemoryRouter><Web3GuidePage /></MemoryRouter>)

    expect(screen.getByRole('heading', { level: 1, name: /从钱包到链上居民/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '真实使用流程' })).toBeInTheDocument()
    expect(screen.getByText('连接钱包并签名登录')).toBeInTheDocument()
    expect(screen.getByText('链上记忆、保存与恢复')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/Proxy online/)).toBeInTheDocument())
  })

  it('switches the complete guide to English', () => {
    useLocale.setState({ locale: 'en' })
    render(<MemoryRouter><Web3GuidePage /></MemoryRouter>)
    expect(screen.getByRole('heading', { level: 1, name: /From wallet to onchain resident/ })).toBeInTheDocument()
    expect(screen.getByText('Anchor and restore memory or saves')).toBeInTheDocument()
  })
})
