import { describe, expect, it } from 'vitest'
import {
  decideResidentTextureLoad,
  parseResidentSpriteUpdatedMessage,
  residentTextureKey,
  resolveResidentSpriteUrl,
  staticResidentSpriteUrl,
} from './residentSpriteRuntime'

describe('resident sprite runtime helpers', () => {
  it('resolves backend static paths against the configured API origin', () => {
    expect(resolveResidentSpriteUrl('/static/resident-sprites/a.png', 'https://api.example.test/base', 'abc123'))
      .toBe('https://api.example.test/static/resident-sprites/a.png?v=abc123')
  })

  it('keeps absolute CDN URLs and rejects non-http protocols', () => {
    expect(resolveResidentSpriteUrl('https://cdn.example.test/a.png', 'https://api.example.test'))
      .toBe('https://cdn.example.test/a.png')
    expect(resolveResidentSpriteUrl('javascript:alert(1)', 'https://api.example.test')).toBeNull()
  })

  it('uses content identity in dynamic texture keys and keeps static fallback keys', () => {
    expect(residentTextureKey({ id: 'Resident-01', sprite_key: '简' })).toBe('简')
    expect(residentTextureKey({
      id: 'Resident-01',
      sprite_key: '简',
      sprite_url: '/static/a.png',
      sprite_content_hash: 'ABCDEF9876543210',
    })).toBe('resident-sprite-resident-01-abcdef9876543210')
    expect(staticResidentSpriteUrl('A B/简')).toBe('/assets/village/agents/A%20B%2F%E7%AE%80/texture.png?v=legacy-blocked')
  })

  it('validates sprite update messages before Phaser consumes them', () => {
    expect(parseResidentSpriteUpdatedMessage({
      type: 'sprite_updated',
      resident_id: 'r1',
      slug: 'jane',
      sprite_key: '简',
      sprite_url: '/static/a.png',
      content_hash: 'abc',
      run_id: 'run1',
    })).toEqual({
      type: 'sprite_updated',
      resident_id: 'r1',
      slug: 'jane',
      sprite_key: '简',
      sprite_url: '/static/a.png',
      content_hash: 'abc',
      run_id: 'run1',
    })
    expect(parseResidentSpriteUpdatedMessage({ type: 'sprite_updated', resident_id: 1 })).toBeNull()
  })

  it('keeps the current texture on load failure', () => {
    expect(decideResidentTextureLoad(false, false, 'candidate', 'candidate')).toBe('keep-current')
  })

  it('discards out-of-order loads so the last event wins', () => {
    expect(decideResidentTextureLoad(true, false, 'newest', 'older')).toBe('discard-stale')
    expect(decideResidentTextureLoad(true, false, 'newest', 'newest')).toBe('apply')
  })

  it('discards completed loads after scene shutdown', () => {
    expect(decideResidentTextureLoad(true, true, 'candidate', 'candidate')).toBe('discard-stale')
  })
})
