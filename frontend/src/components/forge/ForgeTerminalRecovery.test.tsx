import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

import {
  apiFetch,
  deepForgeStart,
  deepForgeStatus,
  forgeAnswer,
  forgeQuick,
  forgeStart,
  forgeStatus,
} from '../../services/api'
import type { DeepForgeStatusResponse, ForgeStatusResponse } from '../../services/api'
import { onWSMessage } from '../../services/ws'
import { DeepForge } from './DeepForge'
import { ForgeChat } from './ForgeChat'
import { QuickForge } from './QuickForge'
import {
  DEEP_FORGE_TERMINAL_RECOVERY_MESSAGE,
  FORGE_GENERATION_POLL_DEADLINE_MS,
  FORGE_TERMINAL_RECOVERY_MESSAGE,
} from './terminalRecovery'

vi.mock('../../services/api', () => ({
  apiFetch: vi.fn(),
  deepForgeStart: vi.fn(),
  deepForgeStatus: vi.fn(),
  forgeAnswer: vi.fn(),
  forgeQuick: vi.fn(),
  forgeStart: vi.fn(),
  forgeStatus: vi.fn(),
}))

vi.mock('../../services/ws', () => ({
  onWSMessage: vi.fn(),
}))

function status(
  state: ForgeStatusResponse['status'],
  overrides: Partial<ForgeStatusResponse> = {},
): ForgeStatusResponse {
  return {
    forge_id: 'forge-1',
    status: state,
    step: 5,
    name: '阿青',
    answers: {},
    ability_md: '',
    persona_md: '',
    soul_md: '',
    star_rating: 4,
    district: 'academy',
    resident_id: state === 'done' ? 'resident-1' : null,
    error: state === 'error' ? '后端生成失败' : null,
    ...overrides,
  }
}

function deepStatus(
  state: DeepForgeStatusResponse['status'],
  overrides: Partial<DeepForgeStatusResponse> = {},
): DeepForgeStatusResponse {
  return {
    forge_id: 'deep-1',
    status: state,
    stage: state,
    progress: state === 'done' || state === 'error' ? 100 : 20,
    name: '阿青',
    ability_md: state === 'done' ? '# Ability' : null,
    persona_md: state === 'done' ? '# Persona' : null,
    soul_md: state === 'done' ? '# Soul' : null,
    star_rating: 3,
    district: 'academy',
    resident_id: state === 'done' ? 'deep-resident-1' : null,
    error: state === 'error' ? '深度后端失败' : null,
    ...overrides,
  }
}

async function flushPromises(): Promise<void> {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

async function submitQuick(): Promise<void> {
  fireEvent.change(screen.getByPlaceholderText(/例如：张三/), {
    target: { value: '阿青' },
  })
  fireEvent.change(screen.getByPlaceholderText(/在这里粘贴任何/), {
    target: { value: '这是一段足够长的人物材料' },
  })
  fireEvent.click(screen.getByRole('button', { name: /立即提取 Skill/ }))
  await flushPromises()
}

describe('guided/quick Forge terminal recovery', () => {
  let wsHandler: ((data: Record<string, unknown>) => void) | null
  let unsubscribe: Mock<() => void>

  beforeEach(() => {
    vi.useFakeTimers()
    wsHandler = null
    unsubscribe = vi.fn<() => void>()
    vi.mocked(onWSMessage).mockImplementation((handler) => {
      wsHandler = handler
      return unsubscribe
    })
    vi.mocked(forgeStart).mockReset()
    vi.mocked(forgeAnswer).mockReset()
    vi.mocked(forgeQuick).mockReset()
    vi.mocked(forgeStatus).mockReset()
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value: vi.fn(),
    })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('guided mode converges by bounded polling when the terminal WS frame is lost', async () => {
    vi.mocked(forgeStart).mockResolvedValue({
      forge_id: 'forge-1', step: 1, question: 'q2',
    })
    vi.mocked(forgeAnswer)
      .mockResolvedValueOnce({ forge_id: 'forge-1', step: 2, next_step: 3, question: 'q3', ability_md: null, persona_md: null, soul_md: null })
      .mockResolvedValueOnce({ forge_id: 'forge-1', step: 3, next_step: 4, question: 'q4', ability_md: null, persona_md: null, soul_md: null })
      .mockResolvedValueOnce({ forge_id: 'forge-1', step: 4, next_step: 5, question: 'q5', ability_md: null, persona_md: null, soul_md: null })
      .mockResolvedValueOnce({ forge_id: 'forge-1', step: 5, next_step: null, question: null, ability_md: null, persona_md: null, soul_md: null })
    vi.mocked(forgeStatus)
      .mockResolvedValueOnce(status('collecting'))
      .mockResolvedValueOnce(status('collecting'))
      .mockResolvedValueOnce(status('collecting'))
      .mockResolvedValueOnce(status('collecting'))
      .mockResolvedValueOnce(status('generating'))
      .mockResolvedValueOnce(status('done'))

    render(<ForgeChat onStateUpdate={vi.fn()} onComplete={vi.fn()} />)

    for (const answer of ['阿青', 'a2', 'a3', 'a4', 'a5']) {
      const input = screen.getByPlaceholderText('输入你的回答…')
      fireEvent.change(input, { target: { value: answer } })
      fireEvent.click(screen.getByRole('button', { name: '发送' }))
      await flushPromises()
    }

    expect(wsHandler).not.toBeNull()
    expect(screen.getByText('开始炼化…')).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })

    expect(screen.getByText(/炼化完成！阿青/)).toBeInTheDocument()
    expect(vi.mocked(forgeStatus)).toHaveBeenCalledTimes(6)
  })

  it('quick mode retries 404/network races after the only terminal WS frame', async () => {
    vi.mocked(forgeQuick).mockResolvedValue({ forge_id: 'forge-1', status: 'generating' })
    vi.mocked(forgeStatus)
      .mockResolvedValueOnce(status('generating'))
      .mockRejectedValueOnce(new Error('API 404'))
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(status('done'))

    render(<QuickForge onStateUpdate={vi.fn()} onComplete={vi.fn()} />)
    await submitQuick()
    expect(wsHandler).not.toBeNull()

    act(() => wsHandler?.({ type: 'forge_done', forge_id: 'forge-1' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })

    expect(screen.getByText('✅ 炼化完成！')).toBeInTheDocument()
    expect(vi.mocked(forgeStatus)).toHaveBeenCalledTimes(4)
  })

  it('quick mode exits the spinner with a visible error after bounded retries', async () => {
    vi.mocked(forgeQuick).mockResolvedValue({ forge_id: 'forge-1', status: 'generating' })
    vi.mocked(forgeStatus)
      .mockResolvedValueOnce(status('generating'))
      .mockRejectedValue(new Error('API 404'))

    render(<QuickForge onStateUpdate={vi.fn()} onComplete={vi.fn()} />)
    await submitQuick()
    act(() => wsHandler?.({ type: 'forge_done', forge_id: 'forge-1' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(7600) })

    expect(screen.getByText(FORGE_TERMINAL_RECOVERY_MESSAGE)).toBeInTheDocument()
    expect(screen.queryByText('正在炼化中…')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /立即提取 Skill/ })).toBeEnabled()
  })

  it('quick mode also converges by polling with no terminal WS frame', async () => {
    vi.mocked(forgeQuick).mockResolvedValue({ forge_id: 'forge-1', status: 'generating' })
    vi.mocked(forgeStatus)
      .mockResolvedValueOnce(status('generating'))
      .mockResolvedValueOnce(status('done'))

    render(<QuickForge onStateUpdate={vi.fn()} onComplete={vi.fn()} />)
    await submitQuick()
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })

    expect(screen.getByText('✅ 炼化完成！')).toBeInTheDocument()
    expect(vi.mocked(forgeStatus)).toHaveBeenCalledTimes(2)
  })

  it('keeps a valid 15-minute generation alive and errors only at the 20-minute deadline', async () => {
    vi.mocked(forgeQuick).mockResolvedValue({ forge_id: 'forge-1', status: 'generating' })
    vi.mocked(forgeStatus).mockResolvedValue(status('generating'))

    render(<QuickForge onStateUpdate={vi.fn()} onComplete={vi.fn()} />)
    await submitQuick()
    await act(async () => { await vi.advanceTimersByTimeAsync(15 * 60 * 1000) })

    expect(screen.getByText('正在炼化中…')).toBeInTheDocument()
    expect(screen.queryByText(FORGE_TERMINAL_RECOVERY_MESSAGE)).not.toBeInTheDocument()

    await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60 * 1000) })
    expect(screen.getByText(FORGE_TERMINAL_RECOVERY_MESSAGE)).toBeInTheDocument()
    expect(screen.queryByText('正在炼化中…')).not.toBeInTheDocument()
  })

  it('aborts polling, retry timers, and completion callbacks on unmount', async () => {
    vi.mocked(forgeQuick).mockResolvedValue({ forge_id: 'forge-1', status: 'generating' })
    vi.mocked(forgeStatus).mockResolvedValue(status('generating'))
    const onComplete = vi.fn()

    const view = render(<QuickForge onStateUpdate={vi.fn()} onComplete={onComplete} />)
    await submitQuick()
    expect(vi.mocked(forgeStatus)).toHaveBeenCalledTimes(1)

    view.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FORGE_GENERATION_POLL_DEADLINE_MS)
    })

    expect(unsubscribe).toHaveBeenCalledTimes(1)
    expect(vi.mocked(forgeStatus)).toHaveBeenCalledTimes(1)
    expect(onComplete).not.toHaveBeenCalled()
  })
})

describe('deep Forge terminal recovery', () => {
  let wsHandler: ((data: Record<string, unknown>) => void) | null
  let unsubscribe: Mock<() => void>

  beforeEach(() => {
    vi.useFakeTimers()
    sessionStorage.setItem('token', 'deep-token')
    wsHandler = null
    unsubscribe = vi.fn<() => void>()
    vi.mocked(onWSMessage).mockImplementation((handler) => {
      wsHandler = handler
      return unsubscribe
    })
    vi.mocked(deepForgeStart).mockReset()
    vi.mocked(deepForgeStatus).mockReset()
    vi.mocked(apiFetch).mockReset()
  })

  afterEach(() => {
    cleanup()
    localStorage.clear()
    vi.useRealTimers()
  })

  async function startDeepForge(): Promise<void> {
    fireEvent.change(screen.getByPlaceholderText(/例如：埃隆·马斯克/), {
      target: { value: '阿青' },
    })
    fireEvent.click(screen.getByRole('button', { name: /开始深度蒸馏/ }))
    await flushPromises()
  }

  it('converges by bounded polling when the terminal WS frame is lost', async () => {
    vi.mocked(deepForgeStart).mockResolvedValue({
      forge_id: 'deep-1', status: 'routed',
    })
    vi.mocked(deepForgeStatus)
      .mockResolvedValueOnce(deepStatus('building'))
      .mockResolvedValueOnce(deepStatus('done'))

    render(<DeepForge onStateUpdate={vi.fn()} onComplete={vi.fn()} />)
    await startDeepForge()
    expect(wsHandler).not.toBeNull()

    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })

    expect(screen.getByText('蒸馏完成！')).toBeInTheDocument()
    expect(vi.mocked(deepForgeStatus)).toHaveBeenCalledTimes(2)
  })

  it('retries 404/network races after the only terminal WS frame', async () => {
    vi.mocked(deepForgeStart).mockResolvedValue({
      forge_id: 'deep-1', status: 'routed',
    })
    vi.mocked(deepForgeStatus)
      .mockResolvedValueOnce(deepStatus('building'))
      .mockRejectedValueOnce(new Error('API 404'))
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(deepStatus('done'))

    render(<DeepForge onStateUpdate={vi.fn()} onComplete={vi.fn()} />)
    await startDeepForge()

    act(() => wsHandler?.({ type: 'forge_done', forge_id: 'deep-1' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })

    expect(screen.getByText('蒸馏完成！')).toBeInTheDocument()
    expect(vi.mocked(deepForgeStatus)).toHaveBeenCalledTimes(4)
  })

  it('keeps valid work alive and shows a visible error only at the 20-minute deadline', async () => {
    vi.mocked(deepForgeStart).mockResolvedValue({
      forge_id: 'deep-1', status: 'routed',
    })
    vi.mocked(deepForgeStatus).mockResolvedValue(deepStatus('refining'))

    render(<DeepForge onStateUpdate={vi.fn()} onComplete={vi.fn()} />)
    await startDeepForge()

    await act(async () => { await vi.advanceTimersByTimeAsync(15 * 60 * 1000) })
    expect(screen.getByText(/深度蒸馏进行中/)).toBeInTheDocument()
    expect(screen.queryByText(DEEP_FORGE_TERMINAL_RECOVERY_MESSAGE)).not.toBeInTheDocument()

    await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60 * 1000) })
    expect(screen.getByText(DEEP_FORGE_TERMINAL_RECOVERY_MESSAGE)).toBeInTheDocument()
    expect(screen.queryByText(/深度蒸馏进行中/)).not.toBeInTheDocument()
  })

  it('aborts polling, retry timers, and completion callbacks on unmount', async () => {
    vi.mocked(deepForgeStart).mockResolvedValue({
      forge_id: 'deep-1', status: 'routed',
    })
    vi.mocked(deepForgeStatus).mockResolvedValue(deepStatus('building'))
    const onComplete = vi.fn()

    const view = render(<DeepForge onStateUpdate={vi.fn()} onComplete={onComplete} />)
    await startDeepForge()
    expect(vi.mocked(deepForgeStatus)).toHaveBeenCalledTimes(1)

    view.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FORGE_GENERATION_POLL_DEADLINE_MS)
    })

    expect(unsubscribe).toHaveBeenCalledTimes(1)
    expect(vi.mocked(deepForgeStatus)).toHaveBeenCalledTimes(1)
    expect(onComplete).not.toHaveBeenCalled()
  })
})
