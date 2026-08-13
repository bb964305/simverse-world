import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { LandingPage } from './LandingPage'

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
    expect(screen.getAllByRole('link', { name: /进入世界/ })[0]).toHaveAttribute('href', '/login')
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
})
