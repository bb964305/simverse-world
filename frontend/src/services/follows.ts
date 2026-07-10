// E11 follow state: the backend has no "list my follows" endpoint, so the set
// of followed slugs lives client-side in localStorage ('followed_slugs') and is
// updated whenever POST/DELETE /follows/{slug} succeeds.
import { followResident, unfollowResident } from './api'

const STORAGE_KEY = 'followed_slugs'

function load(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') as unknown
    return new Set(Array.isArray(raw) ? raw.filter((s): s is string => typeof s === 'string') : [])
  } catch {
    return new Set()
  }
}

const followed: Set<string> = load()

function persist(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...followed]))
  } catch {
    // storage full / unavailable — in-memory set still works for this session
  }
}

export function isFollowed(slug: string): boolean {
  return followed.has(slug)
}

/** Update local state only (use after an API call made elsewhere succeeded). */
export function setFollowed(slug: string, value: boolean): void {
  if (value) followed.add(slug)
  else followed.delete(slug)
  persist()
}

/**
 * Toggle follow state via the API; resolves to the new state.
 * Throws on API failure (e.g. 400 "follow limit reached (50)") without
 * mutating local state.
 */
export async function toggleFollow(slug: string): Promise<boolean> {
  if (followed.has(slug)) {
    await unfollowResident(slug)
    setFollowed(slug, false)
    return false
  }
  await followResident(slug)
  setFollowed(slug, true)
  return true
}
