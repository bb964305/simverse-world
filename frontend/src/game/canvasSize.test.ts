// Task 5（burn-in 修复批次 1）：Phaser 画布首载渲染——尺寸等待 helpers
import { describe, expect, it, vi } from 'vitest'
import { waitForNonZeroSize } from './canvasSize'

function fakeEl(sizes: Array<[number, number]>): HTMLElement {
  const el = document.createElement('div')
  let i = 0
  Object.defineProperty(el, 'clientWidth', { get: () => sizes[Math.min(i, sizes.length - 1)][0] })
  Object.defineProperty(el, 'clientHeight', {
    get: () => {
      const h = sizes[Math.min(i, sizes.length - 1)][1]
      i += 1 // 每轮读取后推进一帧
      return h
    },
  })
  return el
}

describe('waitForNonZeroSize', () => {
  it('waits until the container has a real size', async () => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      setTimeout(() => cb(0), 0)
      return 1
    })
    const el = fakeEl([[0, 0], [0, 0], [1280, 640]])
    const size = await waitForNonZeroSize(el)
    expect(size).toEqual({ width: 1280, height: 640 })
    vi.unstubAllGlobals()
  })

  it('falls back to 1x1 floor on timeout', async () => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      setTimeout(() => cb(0), 0)
      return 1
    })
    const el = fakeEl([[0, 0]])
    const size = await waitForNonZeroSize(el, 50)
    expect(size.width).toBeGreaterThanOrEqual(1)
    expect(size.height).toBeGreaterThanOrEqual(1)
    vi.unstubAllGlobals()
  })
})
