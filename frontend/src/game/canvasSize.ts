/** Canvas sizing helpers — burn-in fix: first-load canvas rendered 150x90 at top-left
 * because Phaser read container size once, before layout settled (scale mode NONE). */

export function waitForNonZeroSize(
  el: HTMLElement,
  timeoutMs = 2000,
): Promise<{ width: number; height: number }> {
  return new Promise((resolve) => {
    const started = Date.now()
    const check = () => {
      const width = el.clientWidth
      const height = el.clientHeight
      if (width > 0 && height > 0) {
        resolve({ width, height })
        return
      }
      if (Date.now() - started >= timeoutMs) {
        resolve({ width: Math.max(1, width), height: Math.max(1, height) })
        return
      }
      requestAnimationFrame(check)
    }
    check()
  })
}

export function observeContainerResize(
  el: HTMLElement,
  cb: (width: number, height: number) => void,
): () => void {
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => cb(el.clientWidth, el.clientHeight))
    ro.observe(el)
    return () => ro.disconnect()
  }
  const onResize = () => cb(el.clientWidth, el.clientHeight)
  window.addEventListener('resize', onResize)
  return () => window.removeEventListener('resize', onResize)
}
