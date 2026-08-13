import { describe, expect, it } from 'vitest'
import {
  MarketAmbienceController,
  marketAmbienceDistanceGain,
  marketHallState,
} from './marketAmbience'

describe('market ambience state', () => {
  it('maps the caravan lifecycle to closed/opening/open/closing hall states', () => {
    expect(marketHallState(null)).toBe('closed')
    expect(marketHallState('waiting')).toBe('closed')
    expect(marketHallState('inbound')).toBe('opening')
    expect(marketHallState('trading')).toBe('open')
    expect(marketHallState('outbound')).toBe('closing')
    expect(marketHallState('departed')).toBe('closed')
  })

  it('keeps ambience local to the market hall and fades by distance', () => {
    expect(marketAmbienceDistanceGain(112, 94)).toBe(1)
    expect(marketAmbienceDistanceGain(116, 94)).toBe(1)
    expect(marketAmbienceDistanceGain(122, 94)).toBeGreaterThan(0)
    expect(marketAmbienceDistanceGain(122, 94)).toBeLessThan(1)
    expect(marketAmbienceDistanceGain(134, 94)).toBe(0)
  })

  it('remains a clean no-op when browser audio is unavailable', () => {
    const controller = new MarketAmbienceController()
    expect(() => {
      controller.arm()
      controller.setPhase('trading')
      controller.setListenerTile(112, 94)
      window.dispatchEvent(new Event('pointerdown'))
      controller.setPhase('outbound')
      controller.destroy()
      controller.destroy()
    }).not.toThrow()
  })
})
