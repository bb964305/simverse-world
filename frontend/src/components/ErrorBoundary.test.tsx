import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { ErrorBoundary } from './ErrorBoundary'

function Bomb({ blow }: { blow: boolean }) {
  if (blow) throw new Error('Phaser exploded')
  return <div>safe content</div>
}

function ChunkBomb() {
  throw new Error('Failed to fetch dynamically imported module: https://simverse.world/assets/SeasonsPage-mBwfgq0G.js')
  // Unreachable, but keeps this a valid JSX component type (throw alone infers `void`).
  return null
}

beforeEach(() => {
  sessionStorage.clear()
})

afterEach(cleanup)

describe('ErrorBoundary', () => {
  it('renders children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <Bomb blow={false} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('safe content')).toBeInTheDocument()
  })

  it('shows fallback with error message instead of white-screening', () => {
    // React logs boundary-caught errors; keep test output clean.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Bomb blow={true} />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('页面出错了')).toBeInTheDocument()
    expect(screen.getByText('Phaser exploded')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('重试 clears the error state and re-renders children', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    let blow = true
    function Flaky() {
      if (blow) throw new Error('once')
      return <div>recovered</div>
    }
    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()
    blow = false
    fireEvent.click(screen.getByText('重试'))
    expect(screen.getByText('recovered')).toBeInTheDocument()
    spy.mockRestore()
  })
})

describe('ErrorBoundary — stale-chunk self-healing (E2E-05)', () => {
  function mockReload() {
    const reload = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload },
    })
    return reload
  }

  it('auto-reloads once when a dynamic import chunk fails to load', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = mockReload()
    render(
      <ErrorBoundary>
        <ChunkBomb />
      </ErrorBoundary>,
    )
    expect(reload).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem('sv:chunk-reload-attempted')).toBe('1')
    spy.mockRestore()
  })

  it('does not reload a second time once the guard flag is already set, and shows fallback UI', () => {
    sessionStorage.setItem('sv:chunk-reload-attempted', '1')
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = mockReload()
    render(
      <ErrorBoundary>
        <ChunkBomb />
      </ErrorBoundary>,
    )
    expect(reload).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/新版本已发布|手动刷新/)).toBeInTheDocument()
    spy.mockRestore()
  })

  it('does not reload for an ordinary render error (regression)', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = mockReload()
    render(
      <ErrorBoundary>
        <Bomb blow={true} />
      </ErrorBoundary>,
    )
    expect(reload).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('sv:chunk-reload-attempted')).toBeNull()
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('Phaser exploded')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('clears the guard flag after a healthy mount so future real failures can self-heal again', () => {
    sessionStorage.setItem('sv:chunk-reload-attempted', '1')
    render(
      <ErrorBoundary>
        <div>safe content</div>
      </ErrorBoundary>,
    )
    expect(sessionStorage.getItem('sv:chunk-reload-attempted')).toBeNull()
  })
})
