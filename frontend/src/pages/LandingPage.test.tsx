import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { LandingPage } from './LandingPage'
import { useLocale } from '../services/locale'

beforeEach(() => {
  useLocale.setState({ locale: 'zh-CN' })
})

afterEach(() => {
  cleanup()
  document.body.classList.remove('marketing-page-open')
})

describe('LandingPage', () => {
  it('presents the Simverse product and a working login entry', () => {
    render(
      <MemoryRouter>
        <LandingPage />
      </MemoryRouter>,
    )

    expect(screen.getByRole('heading', { level: 1, name: 'Simverse World' })).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /连接钱包/ })[0]).toHaveAttribute('href', '/login')
    expect(screen.getAllByRole('link', { name: /观察小镇|观看小镇实况/ })[0]).toHaveAttribute('href', '/town')
    expect(screen.getByRole('heading', { level: 2, name: /不是 NPC/ })).toBeInTheDocument()
  })

  it('exposes an accessible mobile navigation toggle', () => {
    render(
      <MemoryRouter>
        <LandingPage />
      </MemoryRouter>,
    )

    const menuButton = screen.getByRole('button', { name: '打开导航菜单' })
    expect(menuButton).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(menuButton)
    expect(screen.getByRole('button', { name: '关闭导航菜单' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('provides a complete English entry experience', () => {
    render(
      <MemoryRouter>
        <LandingPage />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'EN' }))
    expect(screen.getByText(/An onchain open world/)).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /Connect wallet/ })[0]).toHaveAttribute('href', '/login')
    expect(screen.getByRole('heading', { level: 2, name: /Not NPCs/ })).toBeInTheDocument()
  })
})
