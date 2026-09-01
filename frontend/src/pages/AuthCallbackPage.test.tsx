import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { StrictMode } from 'react'
import { AuthCallbackPage } from './AuthCallbackPage'
import { readOAuthReturnTo, rememberOAuthReturnTo } from '../services/authReturnTo'
import { useGameStore } from '../stores/gameStore'

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="route-location">{`${location.pathname}${location.search}`}</output>
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  useGameStore.setState({ user: null, token: null })
  window.history.pushState({}, '', '/auth/callback?token=oauth-token')
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  vi.unstubAllGlobals()
  window.history.pushState({}, '', '/')
})

describe('AuthCallbackPage return destination', () => {
  it('restores the pre-OAuth admin destination through onboarding', async () => {
    rememberOAuthReturnTo('/admin')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        id: 'admin-1',
        name: 'Admin',
        email: 'admin@example.com',
        avatar: null,
        soul_coin_balance: 0,
        is_admin: true,
      }),
    }))

    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/auth/callback?token=oauth-token']}>
          <AuthCallbackPage />
          <LocationProbe />
        </MemoryRouter>
      </StrictMode>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('route-location')).toHaveTextContent('/onboarding?next=%2Fadmin')
    })
    expect(useGameStore.getState().token).toBe('oauth-token')
    expect(sessionStorage.getItem('token')).toBe('oauth-token')
    expect(readOAuthReturnTo()).toBe('/play')
  })

  it('aborts and fences an in-flight identity request when the callback unmounts', async () => {
    rememberOAuthReturnTo('/admin')
    let requestSignal: AbortSignal | null | undefined
    let resolveFetch!: (response: Response) => void
    const pending = new Promise<Response>((resolve) => { resolveFetch = resolve })
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      requestSignal = init?.signal
      return pending
    }))

    const view = render(
      <MemoryRouter initialEntries={['/auth/callback?token=oauth-token']}>
        <AuthCallbackPage />
      </MemoryRouter>,
    )
    view.unmount()

    expect(requestSignal?.aborted).toBe(true)
    await act(async () => {
      resolveFetch({
        ok: true,
        json: () => Promise.resolve({ id: 'late-admin', is_admin: true }),
      } as Response)
      await pending
    })
    expect(useGameStore.getState().token).toBeNull()
    expect(readOAuthReturnTo()).toBe('/admin')
  })

  it('keeps the admin destination when OAuth identity verification fails', async () => {
    vi.useFakeTimers()
    rememberOAuthReturnTo('/admin')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))

    render(
      <MemoryRouter initialEntries={['/auth/callback?token=oauth-token']}>
        <AuthCallbackPage />
        <LocationProbe />
      </MemoryRouter>,
    )

    await act(async () => { await Promise.resolve() })
    expect(screen.getByText('登录失败：无法获取用户信息')).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(screen.getByTestId('route-location')).toHaveTextContent('/login?next=%2Fadmin')
    expect(readOAuthReturnTo()).toBe('/admin')
    vi.useRealTimers()
  })
})
