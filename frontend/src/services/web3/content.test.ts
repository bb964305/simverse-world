import { afterEach, describe, expect, it, vi } from 'vitest'
import { readPrivateAnchoredJson } from './content'

vi.mock('../api/core', () => ({
  API_BASE: 'http://localhost:8000',
  getToken: () => 'wallet-session-token',
}))

async function sha256Hex(bytes: Uint8Array): Promise<`0x${string}`> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes.buffer as ArrayBuffer))
  return `0x${Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('')}`
}

afterEach(() => vi.unstubAllGlobals())

describe('readPrivateAnchoredJson', () => {
  it('downloads through the wallet session and verifies the onchain hash', async () => {
    const bytes = new TextEncoder().encode(JSON.stringify({ schema: 'simverse-save-v1', value: 7 }))
    const expectedHash = await sha256Hex(bytes)
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: async () => bytes.buffer,
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(readPrivateAnchoredJson<{ value: number }>(
      'http://localhost:8000/web3/content/00000000-0000-0000-0000-000000000001',
      expectedHash,
    )).resolves.toMatchObject({ value: 7 })
    expect(fetchMock).toHaveBeenCalledWith(
      expect.any(URL),
      { headers: { Authorization: 'Bearer wallet-session-token' } },
    )
  })

  it('rejects content whose bytes no longer match the onchain anchor', async () => {
    const bytes = new TextEncoder().encode('{"value":8}')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: async () => bytes.buffer,
    }))
    await expect(readPrivateAnchoredJson(
      'http://localhost:8000/web3/content/00000000-0000-0000-0000-000000000001',
      `0x${'00'.repeat(32)}`,
    )).rejects.toThrow('Anchored content hash mismatch')
  })

  it('does not send the wallet session to a foreign URI', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await expect(readPrivateAnchoredJson(
      'https://attacker.example/web3/content/save',
      `0x${'00'.repeat(32)}`,
    )).rejects.toThrow('Unsupported private content URI')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
