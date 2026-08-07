import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, waitFor } from '@testing-library/react'
import { GraphPage } from './GraphPage'
import { syncCanvasSize } from './syncCanvasSize'

// GraphPage talks to the network (getRelationshipGraph) and draws on a real
// canvas 2D context, neither of which jsdom provides. We mock/stub both so
// the component itself can mount in jsdom — see the "GraphPage canvas
// resize" describe block below. The resize *math* (E2E-09's actual fix) is
// additionally pinned by a direct unit test of the extracted pure function
// `syncCanvasSize`, per the task's fallback guidance, for a DOM-mock-free
// sanity check of the dpr arithmetic.

const getRelationshipGraph = vi.fn()

vi.mock('../services/api', () => ({
  getRelationshipGraph: (...args: unknown[]) => getRelationshipGraph(...args),
}))

vi.mock('../components/TopNav', () => ({ TopNav: () => <nav /> }))

const GRAPH = {
  nodes: [
    { slug: 'a', name: 'A', portrait_url: null, district: 'x' },
    { slug: 'b', name: 'B', portrait_url: null, district: 'x' },
  ],
  edges: [{ a: 'a', b: 'b', strength: 0.5, label: '朋友', mutual: true }],
}

function fakeCtx() {
  return {
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    fillText: vi.fn(),
    setTransform: vi.fn(),
    strokeStyle: '',
    fillStyle: '',
    lineWidth: 0,
    font: '',
    textAlign: 'center' as CanvasTextAlign,
  }
}

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  callback: ResizeObserverCallback
  observed: Element[] = []
  disconnected = false

  constructor(cb: ResizeObserverCallback) {
    this.callback = cb
    FakeResizeObserver.instances.push(this)
  }
  observe(el: Element) { this.observed.push(el) }
  unobserve() {}
  disconnect() { this.disconnected = true }
  trigger() {
    this.callback([] as unknown as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

let rectSize = { width: 390, height: 796 }

beforeEach(() => {
  FakeResizeObserver.instances = []
  rectSize = { width: 390, height: 796 }
  getRelationshipGraph.mockReset().mockResolvedValue(GRAPH)

  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  vi.stubGlobal('devicePixelRatio', 2)
  // jsdom has no rAF; the animation loop only needs to not crash the test —
  // resize behavior doesn't depend on frames actually ticking.
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 0))
  vi.stubGlobal('cancelAnimationFrame', vi.fn())

  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => fakeCtx() as unknown as CanvasRenderingContext2D,
  )
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(() => ({
    width: rectSize.width,
    height: rectSize.height,
    top: 0,
    left: 0,
    right: rectSize.width,
    bottom: rectSize.height,
    x: 0,
    y: 0,
    toJSON() { return {} },
  }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('GraphPage canvas resize (E2E-09)', () => {
  it('creates a ResizeObserver and observes the canvas container on mount', async () => {
    const { getByTestId } = render(<GraphPage />)
    await waitFor(() => expect(FakeResizeObserver.instances.length).toBe(1))
    expect(FakeResizeObserver.instances[0].observed).toContain(getByTestId('graph-container'))
  })

  it('syncs the canvas backing buffer (width/height attributes) to container size × dpr on resize', async () => {
    const { getByTestId } = render(<GraphPage />)
    await waitFor(() => expect(FakeResizeObserver.instances.length).toBe(1))
    const canvas = getByTestId('graph-canvas') as HTMLCanvasElement

    // Initial mount: 390×796 portrait viewport, dpr 2.
    expect(canvas.width).toBe(780)
    expect(canvas.height).toBe(1592)

    // Simulate rotation to landscape: container is now 844×342.
    rectSize = { width: 844, height: 342 }
    FakeResizeObserver.instances[0].trigger()

    expect(canvas.width).toBe(1688)
    expect(canvas.height).toBe(684)
    // The CSS box is driven by the 100%/100% inline style, not by the
    // resize handler — pin that we updated the backing-buffer *attribute*,
    // not this style.
    expect(canvas.style.width).toBe('100%')
  })

  it('disconnects the ResizeObserver on unmount', async () => {
    const { unmount } = render(<GraphPage />)
    await waitFor(() => expect(FakeResizeObserver.instances.length).toBe(1))
    const observer = FakeResizeObserver.instances[0]
    unmount()
    expect(observer.disconnected).toBe(true)
  })
})

describe('syncCanvasSize (pure resize math)', () => {
  it('sets the backing buffer to CSS size × dpr and returns the CSS size', () => {
    rectSize = { width: 300, height: 150 }
    const canvas = document.createElement('canvas')
    const container = document.createElement('div')

    const result = syncCanvasSize(canvas, container, 2)

    expect(canvas.width).toBe(600)
    expect(canvas.height).toBe(300)
    expect(result).toEqual({ width: 300, height: 150 })
  })

  it('defaults to a 1×1 buffer instead of 0×0 when the container has no measured size', () => {
    rectSize = { width: 0, height: 0 }
    const canvas = document.createElement('canvas')
    const container = document.createElement('div')

    const result = syncCanvasSize(canvas, container, 1)

    expect(canvas.width).toBe(1)
    expect(canvas.height).toBe(1)
    expect(result).toEqual({ width: 1, height: 1 })
  })
})
