import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { AccountSection } from './AccountSection'
import { useLocale } from '../../../services/locale'
import type { AllSettings } from '../../../services/api'

const settings: AllSettings = {
  account: {
    display_name: 'Wallet Resident',
    email: 'legacy@example.com',
    wallet_address: '0x1234567890123456789012345678901234567890',
    has_password: true,
    github_bound: true,
    linuxdo_bound: true,
    linuxdo_trust_level: 3,
  },
  character: null,
  interaction: {},
  privacy: {},
  llm: {},
  economy: { soul_coin_balance: 0, low_balance_alert: 0 },
}

beforeEach(() => useLocale.setState({ locale: 'zh-CN' }))
afterEach(cleanup)

describe('AccountSection', () => {
  it('presents the wallet as the only sign-in identity', () => {
    render(<AccountSection settings={settings} />)

    expect(screen.getByTestId('wallet-identity-address')).toHaveTextContent(settings.account.wallet_address!)
    expect(screen.getByText(/Robinhood Chain 钱包/)).toBeInTheDocument()
    expect(screen.queryByText('GitHub')).not.toBeInTheDocument()
    expect(screen.queryByText('LinuxDo')).not.toBeInTheDocument()
    expect(screen.queryByText(settings.account.email)).not.toBeInTheDocument()
  })

  it('renders the wallet identity copy in English', () => {
    useLocale.setState({ locale: 'en' })
    render(<AccountSection settings={settings} />)

    expect(screen.getByText('Wallet identity')).toBeInTheDocument()
    expect(screen.getByText(/Robinhood Chain wallet/)).toBeInTheDocument()
  })
})
