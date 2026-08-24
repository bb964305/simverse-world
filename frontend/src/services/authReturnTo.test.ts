import { beforeEach, describe, expect, it } from 'vitest'
import {
  clearOAuthReturnTo,
  loginPath,
  onboardingPath,
  readOAuthReturnTo,
  rememberOAuthReturnTo,
  safeAuthReturnTo,
} from './authReturnTo'

beforeEach(() => {
  sessionStorage.clear()
})

describe('auth return destinations', () => {
  it('preserves internal paths including query and hash', () => {
    expect(safeAuthReturnTo('/admin?tab=hosted#agent')).toBe('/admin?tab=hosted#agent')
    expect(loginPath('/admin')).toBe('/login?next=%2Fadmin')
    expect(onboardingPath('/admin')).toBe('/onboarding?next=%2Fadmin')
  })

  it('lets authenticated route guards choose a stricter safe fallback', () => {
    expect(safeAuthReturnTo('https://evil.example/admin', '/')).toBe('/')
  })

  it.each([
    'https://evil.example/admin',
    '//evil.example/admin',
    '/\\evil.example/admin',
    '/..//evil.example/admin',
    '/%2e%2e//evil.example/admin',
    'javascript:alert(1)',
    'admin',
  ])('rejects unsafe return destination %s', (destination) => {
    expect(safeAuthReturnTo(destination)).toBe('/play')
  })

  it.each(['/login', '/login?next=%2Fadmin', '/onboarding', '/auth/callback?token=secret'])(
    'rejects authentication entry point %s as a return destination',
    (destination) => {
      expect(safeAuthReturnTo(destination)).toBe('/play')
    },
  )

  it('keeps an OAuth return destination until authentication succeeds', () => {
    rememberOAuthReturnTo('/admin')
    expect(readOAuthReturnTo()).toBe('/admin')
    expect(readOAuthReturnTo()).toBe('/admin')
    clearOAuthReturnTo()
    expect(readOAuthReturnTo()).toBe('/play')
  })
})
