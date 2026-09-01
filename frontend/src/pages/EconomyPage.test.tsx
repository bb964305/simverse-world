import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { EconomyPage } from './EconomyPage'
import { useLocale } from '../services/locale'

beforeEach(() => useLocale.setState({ locale: 'en' }))
afterEach(() => cleanup())

describe('EconomyPage', () => {
  it('publishes the live SIM contract without claiming unverified source code', () => {
    render(<MemoryRouter><EconomyPage /></MemoryRouter>)
    expect(screen.getByText('LIVE TOKEN / ROBINHOOD CHAIN 4663')).toBeInTheDocument()
    expect(screen.getByText(/Blockscout source is not yet verified/)).toBeInTheDocument()
    expect(screen.getByText('0xff70581e7794079597a61d45a41cdea27846d780')).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /Buy SIM/ })[0]).toHaveAttribute('href', 'https://gmgn.ai/robinhood/token/0xff70581e7794079597a61d45a41cdea27846d780')
    expect(screen.getAllByRole('link', { name: /View contract/ })[0]).toHaveAttribute('href', 'https://robinhoodchain.blockscout.com/token/0xff70581e7794079597a61d45a41cdea27846d780')
    expect(screen.getByRole('heading', { name: 'Sinks and burn policy' })).toBeInTheDocument()
  })
})
