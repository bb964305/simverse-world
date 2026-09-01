import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { EconomySection } from './EconomySection'
import type { AllSettings } from '../../../services/api'

// E2E-07: GET /settings only ever returns `economy.soul_coin_balance` (never
// a `balance` key) — assert the section renders the real number instead of
// the old always-'—' placeholder.
vi.mock('../../../services/api', () => ({
  updateEconomy: vi.fn().mockResolvedValue({ economy: { soul_coin_balance: 0, low_balance_alert: 0 } }),
}))

afterEach(() => {
  cleanup()
})

function makeSettings(economy: AllSettings['economy']): AllSettings {
  return {
    account: {
      display_name: 'Tester',
      email: 'tester@example.com',
      wallet_address: '0x1234567890123456789012345678901234567890',
      has_password: true,
      github_bound: false,
      linuxdo_bound: false,
      linuxdo_trust_level: null,
    },
    character: null,
    interaction: {},
    privacy: {},
    llm: {},
    economy,
  }
}

describe('EconomySection', () => {
  it('renders the real soul coin balance instead of a placeholder', () => {
    render(<EconomySection settings={makeSettings({ soul_coin_balance: 155, low_balance_alert: 10 })} />)
    expect(screen.getByText('155')).toBeInTheDocument()
    expect(screen.queryByText('—')).not.toBeInTheDocument()
  })

  it('formats large balances with locale separators', () => {
    render(<EconomySection settings={makeSettings({ soul_coin_balance: 12345, low_balance_alert: 10 })} />)
    expect(screen.getByText('12,345')).toBeInTheDocument()
  })

  it('pre-fills the alert threshold input from settings', () => {
    render(<EconomySection settings={makeSettings({ soul_coin_balance: 0, low_balance_alert: 42 })} />)
    expect(screen.getByPlaceholderText('例如 100')).toHaveValue(42)
  })
})
