import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { ShopModal } from './ShopModal'

const getShopCatalog = vi.fn()
const getResidents = vi.fn()
const getMarketDay = vi.fn()

vi.mock('../services/api', () => ({
  getShopCatalog: (...a: unknown[]) => getShopCatalog(...a),
  getShopInventory: vi.fn().mockResolvedValue({ inventory: [] }),
  purchaseShopItem: vi.fn(),
  getResidents: (...a: unknown[]) => getResidents(...a),
  getMe: vi.fn().mockResolvedValue({ soul_coin_balance: 100 }),
  getMarketDay: (...a: unknown[]) => getMarketDay(...a),
}))

vi.mock('../stores/gameStore', () => ({
  useGameStore: (sel: (s: unknown) => unknown) =>
    sel({ user: { soul_coin_balance: 100 }, updateBalance: vi.fn() }),
}))

const CATALOG = {
  items: [
    { code: 'flower', name: '鲜花', kind: 'gift', price_sc: 20, icon: '🌸', description: '一束花' },
  ],
}

beforeEach(() => {
  getShopCatalog.mockReset().mockResolvedValue(CATALOG)
  getResidents.mockReset().mockResolvedValue([])
  getMarketDay.mockReset()
})

afterEach(cleanup)

describe('ShopModal market-day discount label', () => {
  it('shows struck-through original + discounted price on market day', async () => {
    getMarketDay.mockResolvedValue({ active: true, discount: 0.9, weekday: 5 })
    render(<ShopModal onClose={vi.fn()} />)

    // 20 * 0.9 = 18 discounted; original 20 shown struck through, 集市日 badge
    await waitFor(() => expect(screen.getByText(/18/)).toBeInTheDocument())
    expect(screen.getByText('集市日')).toBeInTheDocument()
    const original = screen.getByText(/^🪙 20$/)
    expect(original).toHaveStyle({ textDecoration: 'line-through' })
  })

  it('shows only the original price when it is not market day', async () => {
    getMarketDay.mockResolvedValue({ active: false, discount: 1.0, weekday: 5 })
    render(<ShopModal onClose={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/🪙 20 购买/)).toBeInTheDocument())
    expect(screen.queryByText('集市日')).not.toBeInTheDocument()
  })

  it('treats a failed market-day fetch as a normal day', async () => {
    getMarketDay.mockRejectedValue(new Error('boom'))
    render(<ShopModal onClose={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/🪙 20 购买/)).toBeInTheDocument())
    expect(screen.queryByText('集市日')).not.toBeInTheDocument()
  })
})
