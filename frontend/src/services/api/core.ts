import { useGameStore } from '../../stores/gameStore'

export const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'

// Requests that hang longer than this are aborted so the UI never spins forever.
const DEFAULT_TIMEOUT_MS = 15000

export function getToken(): string | null {
  return localStorage.getItem('token')
}

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token
    ? { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
    : { 'Content-Type': 'application/json' }
}

/**
 * Merge the caller's abort signal (e.g. from a component unmount) with a 15s
 * timeout so both a manual cancel and a stalled request tear the fetch down.
 */
function withTimeout(signal: AbortSignal | null | undefined): AbortSignal {
  const timeout = AbortSignal.timeout(DEFAULT_TIMEOUT_MS)
  return signal ? AbortSignal.any([signal, timeout]) : timeout
}

export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const { signal: callerSignal, ...rest } = options
  let resp: Response
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      ...rest,
      signal: withTimeout(callerSignal),
      headers: { ...authHeaders(), ...(options.headers as Record<string, string> || {}) },
    })
  } catch (err) {
    // Timeout fires as a TimeoutError; a caller unmount fires as AbortError.
    if (err instanceof DOMException && err.name === 'TimeoutError') {
      throw new Error(`请求超时（${DEFAULT_TIMEOUT_MS / 1000}s）：${path}`)
    }
    throw err
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      // Centralize logout through the store: it clears token + user (state and
      // localStorage) once. ProtectedRoute then redirects to /login, avoiding
      // the multiple hard `window.location` jumps concurrent 401s used to cause.
      useGameStore.getState().logout()
      throw new Error('Session expired')
    }
    const body = await resp.text()
    throw new Error(`API ${resp.status}: ${body}`)
  }
  return resp.json() as Promise<T>
}
