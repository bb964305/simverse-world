import { forgeStatus } from '../../services/api'
import type { ForgeStatusResponse } from '../../services/api'

// One immediate read plus four bounded retries. The backend session is durable,
// but a terminal WS frame can race a different API worker or a brief network
// outage; 7.5 seconds is enough to converge without restoring infinite polling.
export const FORGE_TERMINAL_RETRY_DELAYS_MS = [0, 500, 1000, 2000, 4000] as const
export const FORGE_GENERATION_POLL_DELAYS_MS = [2000, 3000, 5000] as const
// Legacy backend generation can legitimately run for 15 minutes. Give its
// durable stale/error transition headroom; this deadline is only the final UI
// safety net when both WS and status terminalization disappear.
export const FORGE_GENERATION_POLL_DEADLINE_MS = 20 * 60 * 1000

export const FORGE_TERMINAL_RECOVERY_MESSAGE =
  '暂时无法确认炼化最终结果。请刷新页面后在居民列表确认结果。'
export const DEEP_FORGE_TERMINAL_RECOVERY_MESSAGE =
  '暂时无法确认深度蒸馏最终结果。请刷新页面后在居民列表确认结果。'

export class ForgeTerminalRecoveryError extends Error {
  constructor(cause?: unknown, message = FORGE_TERMINAL_RECOVERY_MESSAGE) {
    super(message, { cause })
    this.name = 'ForgeTerminalRecoveryError'
  }
}

function abortError(): Error {
  const error = new Error('Forge terminal recovery aborted')
  error.name = 'AbortError'
  return error
}

export function isForgeRecoveryAbort(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

function waitForRetry(delayMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError())
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortError())
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, delayMs)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

interface TerminalStatus {
  status: string
}

type TerminalStatusFetcher<T extends TerminalStatus> = (
  signal: AbortSignal,
) => Promise<T>

export async function recoverTerminalStatus<T extends TerminalStatus>(
  fetchStatus: TerminalStatusFetcher<T>,
  signal: AbortSignal,
  recoveryMessage = FORGE_TERMINAL_RECOVERY_MESSAGE,
): Promise<T> {
  let lastError: unknown
  for (const delayMs of FORGE_TERMINAL_RETRY_DELAYS_MS) {
    if (delayMs > 0) await waitForRetry(delayMs, signal)
    if (signal.aborted) throw abortError()
    try {
      const status = await fetchStatus(signal)
      if (signal.aborted) throw abortError()
      if (status.status === 'done' || status.status === 'error') return status
      lastError = new Error(`Forge status remained ${status.status}`)
    } catch (error) {
      if (signal.aborted || isForgeRecoveryAbort(error)) throw abortError()
      lastError = error
    }
  }
  throw new ForgeTerminalRecoveryError(lastError, recoveryMessage)
}

export function recoverForgeTerminalStatus(
  forgeId: string,
  signal: AbortSignal,
): Promise<ForgeStatusResponse> {
  return recoverTerminalStatus(
    (requestSignal) => forgeStatus(forgeId, requestSignal),
    signal,
  )
}

/**
 * Bounded fallback for a completely missed terminal WebSocket frame. Polls
 * sequentially (never more than one request in flight) and caps at five-second
 * spacing until the total generation deadline expires.
 */
export async function pollTerminalStatus<T extends TerminalStatus>(
  fetchStatus: TerminalStatusFetcher<T>,
  signal: AbortSignal,
  onPending?: (status: T) => void,
  recoveryMessage = FORGE_TERMINAL_RECOVERY_MESSAGE,
): Promise<T> {
  const deadlineController = new AbortController()
  const deadlineTimer = setTimeout(
    () => deadlineController.abort(),
    FORGE_GENERATION_POLL_DEADLINE_MS,
  )
  const combinedSignal = AbortSignal.any([signal, deadlineController.signal])
  let delayIndex = 0
  let lastError: unknown

  try {
    while (!combinedSignal.aborted) {
      try {
        const status = await fetchStatus(combinedSignal)
        if (status.status === 'done' || status.status === 'error') return status
        onPending?.(status)
        lastError = new Error(`Forge status remained ${status.status}`)
      } catch (error) {
        if (combinedSignal.aborted) throw error
        lastError = error
      }

      const delayMs = FORGE_GENERATION_POLL_DELAYS_MS[
        Math.min(delayIndex, FORGE_GENERATION_POLL_DELAYS_MS.length - 1)
      ]
      delayIndex += 1
      await waitForRetry(delayMs, combinedSignal)
    }
  } catch (error) {
    if (signal.aborted) throw abortError()
    if (!deadlineController.signal.aborted) throw error
    lastError = error
  } finally {
    clearTimeout(deadlineTimer)
  }

  throw new ForgeTerminalRecoveryError(lastError, recoveryMessage)
}

export function pollForgeTerminalStatus(
  forgeId: string,
  signal: AbortSignal,
  onPending?: (status: ForgeStatusResponse) => void,
): Promise<ForgeStatusResponse> {
  return pollTerminalStatus(
    (requestSignal) => forgeStatus(forgeId, requestSignal),
    signal,
    onPending,
  )
}
