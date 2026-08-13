import { describe, expect, it } from 'vitest'
import {
  CARAVAN_CONVOY_ATLAS_URL,
  CARAVAN_CONVOY_TEXTURE_URL,
  CARAVAN_MERCHANT_ATLAS_URL,
  CARAVAN_MERCHANT_TEXTURE_URL,
  CARAVAN_STALL_TEXTURE_URL,
} from './caravanAssetRuntime'

describe('caravan asset runtime URLs', () => {
  it('uses one build-derived cache token for every coupled atlas asset', () => {
    const urls = [
      CARAVAN_CONVOY_ATLAS_URL,
      CARAVAN_CONVOY_TEXTURE_URL,
      CARAVAN_MERCHANT_ATLAS_URL,
      CARAVAN_MERCHANT_TEXTURE_URL,
      CARAVAN_STALL_TEXTURE_URL,
    ]
    const versions = urls.map((url) => new URL(url, 'https://game.test').searchParams.get('v'))
    expect(versions[0]).toMatch(/^[a-z0-9-]+$/)
    expect(new Set(versions).size).toBe(1)
    expect(urls.every((url) => url.startsWith('/assets/village/caravan/'))).toBe(true)
  })
})
