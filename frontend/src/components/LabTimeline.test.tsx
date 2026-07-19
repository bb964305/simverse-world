import { describe, expect, it } from 'vitest'
import { render, within } from '@testing-library/react'
import { LabTimeline } from './LabTimeline'
import { resolveLabDisplay } from '../services/labState'

describe('LabTimeline — four separate tracks (Phase 9)', () => {
  it('renders Task, Run, phase, and connection as four distinct tracks', () => {
    const d = resolveLabDisplay({ taskStatus: 'running', runStatus: 'running' })
    const { container } = render(<LabTimeline display={d} />)
    const tracks = container.querySelectorAll('[role="listitem"]')
    expect(tracks.length).toBe(4)
    for (const t of ['task', 'run', 'phase', 'connection']) {
      expect(container.querySelector(`[data-track="${t}"]`)).toBeTruthy()
    }
  })

  it('keeps Task and Run labels separate (never merged)', () => {
    const d = resolveLabDisplay({ taskStatus: 'review', runStatus: 'succeeded' })
    const { container } = render(<LabTimeline display={d} />)
    const q = within(container)
    expect(q.getByTestId('track-task').textContent).toBe('待验收')
    expect(q.getByTestId('track-run').textContent).toBe('成功')
  })

  it('verifying phase overlays only a running run; no run shows an em dash', () => {
    const running = render(
      <LabTimeline display={resolveLabDisplay({ taskStatus: 'running', runStatus: 'running', eventPhase: 'verifying' })} />)
    expect(within(running.container).getByTestId('track-phase').textContent).toBe('验证中')

    const noRun = render(<LabTimeline display={resolveLabDisplay({ taskStatus: 'funded' })} />)
    const q = within(noRun.container)
    expect(q.getByTestId('track-run').textContent).toBe('—')
    expect(q.getByTestId('track-phase').textContent).toBe('—')
  })

  it('a disconnected/reduced-motion connection renders the tracks STATIC', () => {
    const dc = render(
      <LabTimeline display={resolveLabDisplay({ taskStatus: 'running', runStatus: 'running', connection: 'disconnected' })} />)
    expect(dc.container.querySelector('[aria-label="lab-timeline"]')!.getAttribute('data-frozen')).toBe('true')
    expect(within(dc.container).getByTestId('track-connection').textContent).toBe('已断开')

    // reduced-motion forces frozen even when connected
    const rm = render(
      <LabTimeline display={resolveLabDisplay({ taskStatus: 'running', runStatus: 'running' })} reducedMotion />)
    expect(rm.container.querySelector('[aria-label="lab-timeline"]')!.getAttribute('data-frozen')).toBe('true')
  })

  it('an unknown status shows the flagged 未知状态 chip, never a fabricated idle', () => {
    const d = resolveLabDisplay({ taskStatus: 'wat', runStatus: 'huh' })
    const { container } = render(<LabTimeline display={d} />)
    const q = within(container)
    expect(q.getByTestId('track-task').textContent).toBe('未知状态')
    expect(q.getByTestId('track-run').textContent).toBe('未知状态')
  })
})
