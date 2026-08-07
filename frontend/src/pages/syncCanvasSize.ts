export interface CanvasCssSize { width: number; height: number }

/**
 * Resizes a canvas's backing pixel buffer to match its container's current
 * CSS size (times devicePixelRatio) and resets the drawing transform so
 * callers can keep drawing in CSS-pixel coordinates.
 *
 * Lives in its own file (rather than inlined in GraphPage's effect) so the
 * width/height/dpr math can be exercised directly in tests without needing a
 * real canvas 2D context (jsdom has none) — see GraphPage.test.tsx — and so
 * GraphPage.tsx only exports a component (react-refresh/only-export-components).
 */
export function syncCanvasSize(canvas: HTMLCanvasElement, container: Element, dpr: number): CanvasCssSize {
  const rect = container.getBoundingClientRect()
  const width = Math.max(1, Math.round(rect.width))
  const height = Math.max(1, Math.round(rect.height))
  const bufferWidth = Math.max(1, Math.round(width * dpr))
  const bufferHeight = Math.max(1, Math.round(height * dpr))
  if (canvas.width !== bufferWidth) canvas.width = bufferWidth
  if (canvas.height !== bufferHeight) canvas.height = bufferHeight
  // Reset (not multiply) the transform: ResizeObserver can fire repeatedly,
  // and setTransform is idempotent where ctx.scale would compound.
  const ctx = canvas.getContext('2d')
  ctx?.setTransform(dpr, 0, 0, dpr, 0, 0)
  return { width, height }
}
