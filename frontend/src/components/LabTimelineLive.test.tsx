import { describe, expect, it, vi, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { LabTimelineLive } from './LabTimelineLive'
import { resolveLabDisplay } from '../services/labState'

function stubReducedMotion(matches: boolean) {
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({
    matches,
    media: '(prefers-reduced-motion: reduce)',
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
  }))
}

afterEach(() => vi.unstubAllGlobals())

describe('LabTimelineLive — OS reduced-motion wired into the timeline', () => {
  const running = resolveLabDisplay({ taskStatus: 'running', runStatus: 'running' })

  it('freezes the timeline when the OS prefers reduced motion', () => {
    stubReducedMotion(true)
    const { container } = render(<LabTimelineLive display={running} />)
    expect(container.querySelector('[aria-label="lab-timeline"]')!.getAttribute('data-frozen')).toBe('true')
  })

  it('animates (not frozen) when no motion preference and connection is live', () => {
    stubReducedMotion(false)
    const { container } = render(<LabTimelineLive display={running} />)
    expect(container.querySelector('[aria-label="lab-timeline"]')!.getAttribute('data-frozen')).toBe('false')
  })
})
