import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { LoginPage } from './LoginPage'
import { useGameStore } from '../stores/gameStore'

function mockFetchOnce(status: number, body: unknown) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  }))
}

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="route-location">{`${location.pathname}${location.search}`}</output>
}

function submitLogin(path = '/login') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <LoginPage />
      <LocationProbe />
    </MemoryRouter>,
  )
  fireEvent.change(screen.getByPlaceholderText('邮箱'), { target: { value: 'a@b.com' } })
  fireEvent.change(screen.getByPlaceholderText('密码'), { target: { value: 'pw' } })
  fireEvent.click(screen.getByText('进入城市'))
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  useGameStore.setState({ user: null, token: null })
})

afterEach(() => {
  cleanup()
  document.body.classList.remove('auth-page-open')
  vi.unstubAllGlobals()
})

describe('LoginPage error rendering', () => {
  it('renders string detail as-is', async () => {
    mockFetchOnce(401, { detail: '邮箱或密码错误' })
    submitLogin()
    expect(await screen.findByText('邮箱或密码错误')).toBeInTheDocument()
  })

  it('renders 422 pydantic detail array as readable text instead of crashing', async () => {
    // FastAPI validation errors return detail as a list of objects; rendering
    // them raw as a React child crashes the page into the ErrorBoundary.
    mockFetchOnce(422, {
      detail: [{
        type: 'value_error',
        loc: ['body', 'email'],
        msg: 'value is not a valid email address',
        input: 'x@test.local',
        ctx: { reason: 'special-use domain' },
      }],
    })
    submitLogin()
    expect(await screen.findByText(/value is not a valid email address/)).toBeInTheDocument()
  })

  it('falls back to generic message when body is unparseable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.reject(new Error('not json')),
    }))
    submitLogin()
    expect(await screen.findByText('操作失败')).toBeInTheDocument()
  })

  it('carries a safe return destination through successful login and onboarding', async () => {
    mockFetchOnce(200, {
      user: {
        id: 'admin-1',
        name: 'Admin',
        email: 'admin@example.com',
        avatar: null,
        soul_coin_balance: 0,
        is_admin: true,
      },
      access_token: 'admin-token',
    })

    submitLogin('/login?next=%2Fadmin')

    await waitFor(() => {
      expect(screen.getByTestId('route-location')).toHaveTextContent('/onboarding?next=%2Fadmin')
    })
    expect(useGameStore.getState().token).toBe('admin-token')
  })
})
