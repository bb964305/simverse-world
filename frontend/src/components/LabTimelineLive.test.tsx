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

  it('applies the sv-pulse animation to the verifying phase when motion is allowed, and drops it under reduced-motion', () => {
    const verifying = resolveLabDisplay({ taskStatus: 'running', runStatus: 'running', eventPhase: 'verifying' })

    stubReducedMotion(false)
    const live = render(<LabTimelineLive display={verifying} />)
    const phaseLive = live.container.querySelector('[data-testid="track-phase"]') as HTMLElement
    expect(phaseLive.style.animation).toContain('sv-pulse')

    stubReducedMotion(true)
    const reduced = render(<LabTimelineLive display={verifying} />)
    const phaseReduced = reduced.container.querySelector('[data-testid="track-phase"]') as HTMLElement
    expect(phaseReduced.style.animation).toBe('')
  })
})
