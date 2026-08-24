const DEFAULT_RETURN_TO = '/play'
const RETURN_TO_ORIGIN = 'https://simverse.invalid'
const OAUTH_RETURN_TO_KEY = 'simverse.oauth.return-to'
const AUTH_ENTRY_PATHS = new Set(['/login', '/onboarding', '/auth/callback'])

export function safeAuthReturnTo(
  raw: string | null | undefined,
  fallback = DEFAULT_RETURN_TO,
): string {
  if (!raw || !raw.startsWith('/')) return fallback

  try {
    const parsed = new URL(raw, RETURN_TO_ORIGIN)
    if (parsed.origin !== RETURN_TO_ORIGIN) return fallback
    if (parsed.pathname.startsWith('//') || AUTH_ENTRY_PATHS.has(parsed.pathname.replace(/\/+$/, ''))) {
      return fallback
    }
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return fallback
  }
}

export function loginPath(returnTo: string): string {
  return `/login?next=${encodeURIComponent(safeAuthReturnTo(returnTo))}`
}

export function onboardingPath(returnTo: string): string {
  return `/onboarding?next=${encodeURIComponent(safeAuthReturnTo(returnTo))}`
}

export function rememberOAuthReturnTo(returnTo: string): void {
  try {
    sessionStorage.setItem(OAUTH_RETURN_TO_KEY, safeAuthReturnTo(returnTo))
  } catch {
    // Storage can be unavailable in privacy-restricted browsers. OAuth still
    // succeeds; it simply falls back to the normal /play destination.
  }
}

export function readOAuthReturnTo(): string {
  try {
    return safeAuthReturnTo(sessionStorage.getItem(OAUTH_RETURN_TO_KEY))
  } catch {
    return DEFAULT_RETURN_TO
  }
}

export function clearOAuthReturnTo(): void {
  try {
    sessionStorage.removeItem(OAUTH_RETURN_TO_KEY)
  } catch {
    // Best-effort cleanup only.
  }
}
