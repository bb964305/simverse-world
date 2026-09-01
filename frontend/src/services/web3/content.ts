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
