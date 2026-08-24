import '@testing-library/jest-dom/vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AdminPage } from './AdminPage'
import { useGameStore } from '../stores/gameStore'

vi.mock('../components/admin/DashboardPanel', () => ({
  DashboardPanel: () => (
    <div data-testid="long-admin-content" style={{ height: 1800 }}>
      <button type="button" data-testid="admin-bottom-marker">页面底部</button>
    </div>
  ),
}))

const adminCss = readFileSync(resolve(process.cwd(), 'src/styles/admin-console.css'), 'utf8')
const hostedCss = readFileSync(resolve(process.cwd(), 'src/styles/admin-hosted-agents.css'), 'utf8')

function setSyntheticScrollSize(element: HTMLElement, clientHeight: number, scrollHeight: number) {
  Object.defineProperties(element, {
    clientHeight: { configurable: true, value: clientHeight },
    scrollHeight: { configurable: true, value: scrollHeight },
  })
}

function renderAdmin(width: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  return render(
    <MemoryRouter initialEntries={['/admin']}>
      <AdminPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  useGameStore.setState({
    user: {
      id: 'admin-1',
      name: 'Admin',
      email: 'admin@example.com',
      avatar: null,
      soul_coin_balance: 0,
      is_admin: true,
    },
    token: 'admin-token',
  })
  const style = document.createElement('style')
  style.dataset.testid = 'admin-scroll-styles'
  style.textContent = `${adminCss}\n${hostedCss}`
  document.head.appendChild(style)
})

afterEach(() => {
  cleanup()
  document.querySelector('style[data-testid="admin-scroll-styles"]')?.remove()
})

describe.each([
  ['desktop', 1280, 720],
  ['narrow touch viewport', 390, 640],
] as const)('AdminPage scroll contract — %s', (_label, width, viewportHeight) => {
  it('keeps content beyond the viewport reachable by wheel and touch scrolling', () => {
    const view = renderAdmin(width)
    const scrollRoot = view.container.querySelector<HTMLElement>('.admin-console')
    const sidebar = view.container.querySelector<HTMLElement>('.admin-sidebar')
    expect(scrollRoot).not.toBeNull()
    expect(sidebar).not.toBeNull()
    expect(screen.getByTestId('admin-bottom-marker')).toBeInTheDocument()

    const root = scrollRoot!
    setSyntheticScrollSize(root, viewportHeight, 2200)
    expect(root.scrollHeight).toBeGreaterThan(root.clientHeight)
    expect(getComputedStyle(root).overflowY).toBe('auto')
    expect(getComputedStyle(root).overflowX).toBe('hidden')
    expect(getComputedStyle(sidebar!).position).toBe('sticky')
    expect(getComputedStyle(sidebar!).top).toBe('0px')

    expect(fireEvent.wheel(root, { deltaY: 480 })).toBe(true)
    expect(fireEvent.touchMove(root, { touches: [{ clientY: 80 }] })).toBe(true)

    root.scrollTop = root.scrollHeight - root.clientHeight
    fireEvent.scroll(root)
    expect(root.scrollTop).toBe(2200 - viewportHeight)
  })

  it('gives the sticky control menu its own bounded vertical scroll area', () => {
    const view = renderAdmin(width)
    fireEvent.click(screen.getByRole('button', { name: /控制中心/ }))
    const menu = view.container.querySelector<HTMLElement>('.admin-control-nav')
    expect(menu).not.toBeNull()
    expect(getComputedStyle(menu!).overflowY).toBe('auto')
    expect(getComputedStyle(menu!).maxHeight).toContain('100dvh')
  })
})

describe('Hosted Agent nested scrolling', () => {
  it('keeps the log independently scrollable without cancelling wheel/touch chaining', () => {
    const view = renderAdmin(390)
    const root = view.container.querySelector<HTMLElement>('.admin-console')!
    const content = view.container.querySelector<HTMLElement>('.admin-content')!
    const log = document.createElement('ol')
    log.className = 'hosted-agent-log'
    content.appendChild(log)
    setSyntheticScrollSize(log, 440, 1200)

    expect(getComputedStyle(log).overflow).toBe('auto')
    expect(log.scrollHeight).toBeGreaterThan(log.clientHeight)

    const bubbledWheel = vi.fn()
    root.addEventListener('wheel', bubbledWheel)
    expect(fireEvent.wheel(log, { deltaY: 320 })).toBe(true)
    expect(fireEvent.touchMove(log, { touches: [{ clientY: 40 }] })).toBe(true)
    log.scrollTop = log.scrollHeight - log.clientHeight
    fireEvent.scroll(log)
    fireEvent.wheel(log, { deltaY: 320 })
    expect(log.scrollTop).toBe(760)
    expect(bubbledWheel).toHaveBeenCalledTimes(2)
  })

  it('retains horizontal touch scrolling for the resident list on narrow screens', () => {
    expect(hostedCss).toMatch(
      /@media \(max-width: 820px\)[\s\S]*?\.hosted-agent-list \{[\s\S]*?overflow-x: auto;[\s\S]*?-webkit-overflow-scrolling: touch;/,
    )
  })
})
