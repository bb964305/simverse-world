import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { EconomyPage } from './EconomyPage'
import { useLocale } from '../services/locale'

beforeEach(() => useLocale.setState({ locale: 'en' }))
afterEach(() => cleanup())

describe('EconomyPage', () => {
  it('documents planned utility without presenting an undeployed SIM token as live', () => {
    render(<MemoryRouter><EconomyPage /></MemoryRouter>)
    expect(screen.getByText('DESIGN DRAFT / TOKEN NOT LAUNCHED')).toBeInTheDocument()
    expect(screen.getByText('No SIM contract or official token address exists yet.')).toBeInTheDocument()
    expect(screen.getByText('Not deployed')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Sinks and burn policy' })).toBeInTheDocument()
  })
})
