import { getCurrentCaravan, type CaravanPhase, type CaravanState } from './api/caravan'
import { parseUTC } from '../utils/time'

const PHASES = new Set<CaravanPhase>([
  'scheduled', 'waiting', 'inbound', 'trading', 'outbound', 'departed', 'cancelled',
])

export interface CaravanProjection {
  snapshot: CaravanState | null
  /** server_time - local receipt time; used to animate against the server clock. */
  serverOffsetMs: number
  receivedAtMs: number
}

export interface CaravanPose {
  tileX: number
  tileY: number
  direction: 'down' | 'left' | 'right' | 'up'
  moving: boolean
}

export type CaravanRenderMode = 'hidden' | 'convoy' | 'stall'

export const EMPTY_CARAVAN_PROJECTION: CaravanProjection = Object.freeze({
  snapshot: null,
  serverOffsetMs: 0,
  receivedAtMs: 0,
})

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function finiteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function validTimestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(parseUTC(value).getTime())
}

function timestampMs(value: string): number {
  return parseUTC(value).getTime()
}

/** Parse an untrusted REST/WS value without allowing NaN or partial snapshots. */
export function parseCaravanState(value: unknown): CaravanState | null {
  if (!isRecord(value) || value.type !== 'caravan_state') return null
  if (!nullableString(value.visit_id) || !nullableString(value.world_event_id)) return null
  if (!Number.isInteger(value.version) || (value.version as number) < 0) return null
  if (!validTimestamp(value.server_time) || typeof value.visible !== 'boolean') return null
  if (value.phase !== null && (typeof value.phase !== 'string' || !PHASES.has(value.phase as CaravanPhase))) return null

  let position: CaravanState['position'] = null
  if (value.position !== null) {
    if (!isRecord(value.position)
      || !finiteNumber(value.position.tile_x)
      || !finiteNumber(value.position.tile_y)
      || !Number.isInteger(value.position.tile_x)
      || !Number.isInteger(value.position.tile_y)) return null
    position = { tile_x: value.position.tile_x, tile_y: value.position.tile_y }
  }

  let motion: CaravanState['motion'] = null
  if (value.motion !== null) {
    if (!isRecord(value.motion)
      || !Array.isArray(value.motion.path)
      || !validTimestamp(value.motion.started_at)
      || !validTimestamp(value.motion.ends_at)) return null
    const path: [number, number][] = []
    for (const point of value.motion.path) {
      if (!Array.isArray(point) || point.length !== 2
        || !finiteNumber(point[0]) || !finiteNumber(point[1])
        || !Number.isInteger(point[0]) || !Number.isInteger(point[1])) return null
      path.push([point[0], point[1]])
    }
    if (path.length === 0) return null
    if (timestampMs(value.motion.ends_at) < timestampMs(value.motion.started_at)) return null
    motion = { path, started_at: value.motion.started_at, ends_at: value.motion.ends_at }
  }

  if (!isRecord(value.summary)) return null
  const summaryRecord = value.summary
  const summaryKeys = ['fee_sc', 'bought', 'spent_sc', 'tax_sc', 'imports_stocked'] as const
  if (summaryKeys.some((key) => !finiteNumber(summaryRecord[key]))) return null
  const summary = {
    fee_sc: summaryRecord.fee_sc as number,
    bought: summaryRecord.bought as number,
    spent_sc: summaryRecord.spent_sc as number,
    tax_sc: summaryRecord.tax_sc as number,
    imports_stocked: summaryRecord.imports_stocked as number,
  }

  // A visible projection must identify a visit and have somewhere to render.
  if (value.visible && (value.visit_id === null || value.phase === null || position === null)) return null

  return {
    type: 'caravan_state',
    visit_id: value.visit_id,
    world_event_id: value.world_event_id,
    version: value.version as number,
    phase: value.phase as CaravanPhase | null,
    server_time: value.server_time,
    position,
    motion,
    summary,
    visible: value.visible,
  }
}

function visitSortKey(state: CaravanState): string {
  return state.visit_id ?? ''
}

/**
 * Apply one snapshot. Same-visit duplicates/out-of-order versions are ignored;
 * across visits, server_time orders snapshots and visit_id is only a stable tie.
 */
export function reduceCaravanProjection(
  current: CaravanProjection,
  incoming: CaravanState,
  receivedAtMs = Date.now(),
): CaravanProjection {
  const previous = current.snapshot
  if (previous) {
    if (previous.visit_id === incoming.visit_id) {
      if (incoming.version <= previous.version) return current
    } else {
      const previousTime = timestampMs(previous.server_time)
      const incomingTime = timestampMs(incoming.server_time)
      if (incomingTime < previousTime) return current
      // A REST empty snapshot is the authoritative statement that no visit is
      // renderable. Allow it to clear an active visit even when the database
      // serialized both observations in the same millisecond.
      const canonicalEmpty = incoming.visit_id === null && incoming.visible === false
      if (incomingTime === previousTime
        && !canonicalEmpty
        && visitSortKey(incoming) <= visitSortKey(previous)) return current
    }
  }
  return {
    snapshot: incoming,
    serverOffsetMs: timestampMs(incoming.server_time) - receivedAtMs,
    receivedAtMs,
  }
}

function directionFor(dx: number, dy: number): CaravanPose['direction'] {
  if (Math.abs(dx) >= Math.abs(dy)) return dx < 0 ? 'left' : 'right'
  return dy < 0 ? 'up' : 'down'
}

/** Sample the server-timed polyline segment-by-segment (constant path speed). */
export function projectCaravanPose(
  projection: CaravanProjection,
  clientNowMs = Date.now(),
): CaravanPose | null {
  const state = projection.snapshot
  if (!state?.visible || !state.position) return null
  const fallback: CaravanPose = {
    tileX: state.position.tile_x,
    tileY: state.position.tile_y,
    // The south-gate staging point faces into town while waiting; the parked
    // merchant faces the player once the wagon has unfolded in the market hall.
    direction: state.phase === 'waiting' ? 'up' : 'down',
    moving: false,
  }
  if (!state.motion || !['inbound', 'outbound'].includes(state.phase ?? '')) return fallback

  const { path } = state.motion
  if (path.length < 2) return { ...fallback, tileX: path[0][0], tileY: path[0][1] }
  const startedAt = timestampMs(state.motion.started_at)
  const endsAt = timestampMs(state.motion.ends_at)
  if (!Number.isFinite(startedAt) || !Number.isFinite(endsAt)) return fallback

  const segments: Array<{
    x0: number
    y0: number
    x1: number
    y1: number
    length: number
  }> = []
  let totalLength = 0
  for (let i = 1; i < path.length; i += 1) {
    const [x0, y0] = path[i - 1]
    const [x1, y1] = path[i]
    const length = Math.hypot(x1 - x0, y1 - y0)
    if (length <= 0) continue
    segments.push({ x0, y0, x1, y1, length })
    totalLength += length
  }
  if (totalLength <= 0) return { ...fallback, tileX: path[0][0], tileY: path[0][1] }

  const serverNow = clientNowMs + projection.serverOffsetMs
  const duration = endsAt - startedAt
  const progress = duration <= 0 ? 1 : Math.max(0, Math.min(1, (serverNow - startedAt) / duration))
  let remaining = progress * totalLength
  for (let i = 0; i < segments.length; i += 1) {
    const { x0, y0, x1, y1, length } = segments[i]
    if (remaining <= length || i === segments.length - 1) {
      const local = Math.max(0, Math.min(1, remaining / length))
      return {
        tileX: x0 + (x1 - x0) * local,
        tileY: y0 + (y1 - y0) * local,
        direction: directionFor(x1 - x0, y1 - y0),
        moving: progress > 0 && progress < 1,
      }
    }
    remaining -= length
  }
  return fallback
}

export function caravanRenderMode(state: CaravanState | null): CaravanRenderMode {
  if (!state?.visible || state.phase === 'departed' || state.phase === 'cancelled') return 'hidden'
  return state.phase === 'trading' ? 'stall' : 'convoy'
}

export function caravanBannerText(state: CaravanState | null): string | null {
  if (!state?.visible) return null
  if (state.phase === 'inbound') return '远方商队正从南门进镇'
  if (state.phase === 'trading') return '商队已在集市大厅开摊'
  if (state.phase === 'outbound') return '集市散场，商队正在离镇'
  if (state.phase === 'waiting') return '远方商队正在南门外等候'
  return null
}

type ProjectionListener = (projection: CaravanProjection) => void
const listeners = new Set<ProjectionListener>()
let currentProjection = EMPTY_CARAVAN_PROJECTION
let refreshPromise: Promise<CaravanProjection> | null = null
let refreshGeneration = 0

export function getCaravanProjection(): CaravanProjection {
  return currentProjection
}

export function subscribeCaravanProjection(listener: ProjectionListener): () => void {
  listeners.add(listener)
  listener(currentProjection)
  return () => listeners.delete(listener)
}

export function convergeCaravanState(value: unknown, receivedAtMs = Date.now()): boolean {
  const parsed = parseCaravanState(value)
  if (!parsed) return false
  const next = reduceCaravanProjection(currentProjection, parsed, receivedAtMs)
  if (next === currentProjection) return false
  currentProjection = next
  listeners.forEach((listener) => listener(currentProjection))
  return true
}

/** Initial-load/reconnect convergence. Concurrent consumers share one GET. */
export function refreshCaravanProjection(): Promise<CaravanProjection> {
  if (refreshPromise) return refreshPromise
  const generation = refreshGeneration
  const request = getCurrentCaravan()
    .then((snapshot) => {
      // disconnect/account switch invalidates any response already in flight.
      if (generation === refreshGeneration) convergeCaravanState(snapshot)
      return currentProjection
    })
    .finally(() => {
      if (refreshPromise === request) refreshPromise = null
    })
  refreshPromise = request
  return refreshPromise
}

/** Deliberate disconnect/account switch: remove any prior account's projection. */
export function resetCaravanProjection(): void {
  refreshGeneration += 1
  currentProjection = EMPTY_CARAVAN_PROJECTION
  refreshPromise = null
  listeners.forEach((listener) => listener(currentProjection))
}
