import { API_BASE, getToken } from '../api/core'

export interface AnchoredContent {
  content_id: string
  content_uri: string
  content_hash: `0x${string}`
  filename: string
  media_type: string
  size: number
}

export async function uploadWeb3Content(file: File | Blob, filename?: string): Promise<AnchoredContent> {
  const token = getToken()
  if (!token) throw new Error('Wallet session required')
  const form = new FormData()
  form.append('file', file, filename || (file instanceof File ? file.name : 'content.json'))
  const response = await fetch(`${API_BASE}/web3/content`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof body?.detail === 'string' ? body.detail : `Upload failed (${response.status})`)
  return body as AnchoredContent
}

export async function snapshotGameMemory(residentId: string): Promise<AnchoredContent> {
  const token = getToken()
  if (!token) throw new Error('Wallet session required')
  const response = await fetch(`${API_BASE}/web3/content/memory-snapshot/${encodeURIComponent(residentId)}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof body?.detail === 'string' ? body.detail : `Snapshot failed (${response.status})`)
  return body as AnchoredContent
}

function normalizedHash(value: string): string {
  return value.toLowerCase().replace(/^0x/, '')
}

export async function readPrivateAnchoredJson<T>(contentUri: string, expectedHash: `0x${string}`): Promise<T> {
  const token = getToken()
  if (!token) throw new Error('Wallet session required')

  const apiUrl = new URL(API_BASE, window.location.origin)
  const contentUrl = new URL(contentUri, apiUrl)
  if (contentUrl.origin !== apiUrl.origin || !contentUrl.pathname.startsWith('/web3/content/')) {
    throw new Error('Unsupported private content URI')
  }

  const response = await fetch(contentUrl, { headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body?.detail === 'string' ? body.detail : `Content download failed (${response.status})`)
  }
  const bytes = new Uint8Array(await response.arrayBuffer())
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
  const actualHash = Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('')
  if (actualHash !== normalizedHash(expectedHash)) throw new Error('Anchored content hash mismatch')

  try {
    return JSON.parse(new TextDecoder().decode(bytes)) as T
  } catch (reason) {
    throw new Error('Anchored save is not valid JSON', { cause: reason })
  }
}
