// Hand-rolled SVG sparkline geometry (D4 creator dashboard).
// Pure functions producing path strings / rect boxes — no chart library,
// same zero-dependency stance as the C2 graphs.

export interface SparkBox {
  width: number
  height: number
  /** Inner padding in px, defaults to 2. */
  pad?: number
}

export interface SparkDomain {
  min?: number
  max?: number
}

export interface BarRect {
  x: number
  y: number
  width: number
  height: number
}

const round1 = (n: number) => Math.round(n * 10) / 10

/** Value domain: explicit bounds win; otherwise [min(0, data)..max(data)]. */
function resolveDomain(values: number[], domain?: SparkDomain): { lo: number; hi: number } {
  const finite = values.filter((v) => Number.isFinite(v))
  const lo = domain?.min ?? Math.min(0, ...(finite.length ? finite : [0]))
  let hi = domain?.max ?? Math.max(...(finite.length ? finite : [0]))
  if (hi <= lo) hi = lo + 1 // flat/empty series still renders a baseline
  return { lo, hi }
}

/** y pixel for a value (SVG y axis points down). */
function yAt(v: number, lo: number, hi: number, box: SparkBox): number {
  const pad = box.pad ?? 2
  const usable = box.height - pad * 2
  return round1(pad + (1 - (v - lo) / (hi - lo)) * usable)
}

/** x pixel for point i of n (points spread edge-to-edge inside the padding). */
function xAt(i: number, n: number, box: SparkBox): number {
  const pad = box.pad ?? 2
  const usable = box.width - pad * 2
  return round1(n <= 1 ? pad + usable / 2 : pad + (i / (n - 1)) * usable)
}

/**
 * Polyline path ("M x y L x y …") for a line chart. `null` values are gaps:
 * the line breaks and restarts at the next present point.
 */
export function linePath(values: Array<number | null>, box: SparkBox, domain?: SparkDomain): string {
  const present = values.filter((v): v is number => v !== null)
  if (present.length === 0) return ''
  const { lo, hi } = resolveDomain(present, domain)
  const parts: string[] = []
  let penDown = false
  values.forEach((v, i) => {
    if (v === null) { penDown = false; return }
    parts.push(`${penDown ? 'L' : 'M'} ${xAt(i, values.length, box)} ${yAt(v, lo, hi, box)}`)
    penDown = true
  })
  return parts.join(' ')
}

/** Dot positions for the non-null points of a line chart. */
export function lineDots(values: Array<number | null>, box: SparkBox, domain?: SparkDomain): Array<{ x: number; y: number }> {
  const present = values.filter((v): v is number => v !== null)
  if (present.length === 0) return []
  const { lo, hi } = resolveDomain(present, domain)
  return values.flatMap((v, i) =>
    v === null ? [] : [{ x: xAt(i, values.length, box), y: yAt(v, lo, hi, box) }])
}

/** Column-chart rects, baseline at value 0. Zero values get a 1px stub. */
export function barRects(values: number[], box: SparkBox, domain?: SparkDomain): BarRect[] {
  const pad = box.pad ?? 2
  const { lo, hi } = resolveDomain(values, domain)
  const usable = box.width - pad * 2
  const step = usable / Math.max(values.length, 1)
  const barW = Math.max(round1(step * 0.7), 1)
  const y0 = yAt(Math.max(lo, 0), lo, hi, box)
  return values.map((v, i) => {
    const yv = yAt(v, lo, hi, box)
    const h = Math.max(round1(Math.abs(y0 - yv)), 1)
    return { x: round1(pad + i * step + (step - barW) / 2), y: round1(Math.min(yv, y0 - 1)), width: barW, height: h }
  })
}

/** Zero-fill a sparse {date,value} series into `days` values starting at `since`. */
export function fillDailySeries(points: Array<{ date: string; value: number }>, since: string, days: number): number[] {
  const byDate = new Map(points.map((p) => [p.date, p.value]))
  const start = Date.parse(`${since}T00:00:00Z`)
  if (Number.isNaN(start) || days <= 0) return []
  return Array.from({ length: days }, (_, i) => {
    const day = new Date(start + i * 86_400_000).toISOString().slice(0, 10)
    return byDate.get(day) ?? 0
  })
}
