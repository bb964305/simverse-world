import { describe, expect, it } from 'vitest'
import { STATUS_CONFIG } from './statusConfig'

describe('StatusVisuals config', () => {
  it('includes researching as a read-only Resident activity (teal, no chat)', () => {
    const r = STATUS_CONFIG.researching
    expect(r).toBeDefined()
    expect(r.canChat).toBe(false)
    expect(r.tint).toBe(0x14b8a6) // teal lab research ring
    expect(r.label).toContain('研究')
  })

  it('keeps the existing resident statuses intact', () => {
    for (const key of ['idle', 'sleeping', 'chatting', 'popular', 'walking', 'socializing']) {
      expect(STATUS_CONFIG[key]).toBeDefined()
    }
  })
})
