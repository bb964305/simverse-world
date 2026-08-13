// @vitest-environment node
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

interface AtlasFrame {
  filename: string
  frame: { x: number; y: number; w: number; h: number }
  anchor: { x: number; y: number }
}

const CARAVAN_ASSET_DIR = fileURLToPath(
  new URL('../../public/assets/village/caravan/', import.meta.url),
)

function assetPath(...parts: string[]): string {
  return join(CARAVAN_ASSET_DIR, ...parts)
}

function readAtlas(name: 'merchant' | 'convoy'): AtlasFrame[] {
  const json = JSON.parse(readFileSync(assetPath(name, 'atlas.json'), 'utf8')) as { frames?: AtlasFrame[] }
  return json.frames ?? []
}

function pngDimensions(path: string): { width: number; height: number } {
  const png = readFileSync(path)
  expect(png.subarray(1, 4).toString('ascii')).toBe('PNG')
  return { width: png.readUInt32BE(16), height: png.readUInt32BE(20) }
}

function expectedFrames(action: 'walk' | 'roll'): string[] {
  return ['down', 'left', 'right', 'up'].flatMap((direction) => [
    ...Array.from({ length: 4 }, (_, frame) =>
      `${direction}-${action}.${String(frame).padStart(3, '0')}`),
    direction,
  ])
}

describe('caravan Phaser asset contract', () => {
  it.each([
    ['merchant', 'walk', 32, 96, 128],
    ['convoy', 'roll', 64, 192, 256],
  ] as const)('%s exposes the canonical four-direction atlas', (name, action, frameSize, width, height) => {
    const png = assetPath(name, 'texture.png')
    const atlas = assetPath(name, 'atlas.json')
    expect(existsSync(png)).toBe(true)
    expect(existsSync(atlas)).toBe(true)
    expect(pngDimensions(png)).toEqual({ width, height })

    const frames = readAtlas(name)
    expect(frames.map((frame) => frame.filename)).toEqual(expectedFrames(action))
    expect(frames).toHaveLength(20)
    expect(frames.every(({ frame }) => frame.w === frameSize && frame.h === frameSize)).toBe(true)
    const expectedAnchorY = name === 'convoy' ? 0.75 : 0.5
    expect(frames.every(({ anchor }) => anchor.x === 0.5 && anchor.y === expectedAnchorY)).toBe(true)
  })

  it('loads the trading stall as a native 64px image', () => {
    const png = assetPath('stall', 'texture.png')
    expect(existsSync(png)).toBe(true)
    expect(pngDimensions(png)).toEqual({ width: 64, height: 64 })
  })
})
