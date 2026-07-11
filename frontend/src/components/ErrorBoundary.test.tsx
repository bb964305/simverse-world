import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { ErrorBoundary } from './ErrorBoundary'

function Bomb({ blow }: { blow: boolean }) {
  if (blow) throw new Error('Phaser exploded')
  return <div>safe content</div>
}

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
