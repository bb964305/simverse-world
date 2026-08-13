import type {
  SpectatorActor,
  SpectatorActorKind,
  ViewerLocation,
} from '../../services/spectator'
import { LOCATIONS } from '../minimap/districtZonesData'

export type ProjectedSpectatorActor = SpectatorActor & { tile_x: number; tile_y: number }

const LOCATION_LABELS: Map<string, string> = new Map(
  LOCATIONS.map((location) => [location.key, location.label]),
)

export const SPECTATOR_KIND_LABELS: Record<SpectatorActorKind, string> = {
  npc: '居民',
  agent: 'Agent 玩家',
  human: '真人玩家',
}

export function formatLocationName(
  location: string | ViewerLocation | null | undefined,
  fallback = '小镇中',
): string {
  if (!location) return fallback
  if (typeof location !== 'string') {
    return location.name || LOCATION_LABELS.get(location.slug) || location.slug
  }
  return LOCATION_LABELS.get(location) || location
}

export function formatActorLocation(
  actor: Pick<SpectatorActor, 'district'>,
  fallback = '小镇中',
): string {
  return formatLocationName(actor.district, fallback)
}

export function hasProjectedCoordinates(
  actor: SpectatorActor,
): actor is ProjectedSpectatorActor {
  return typeof actor.tile_x === 'number'
    && Number.isFinite(actor.tile_x)
    && typeof actor.tile_y === 'number'
    && Number.isFinite(actor.tile_y)
}

export function spectatorActorLabel(actor: SpectatorActor): string {
  return `${actor.name}，${SPECTATOR_KIND_LABELS[actor.kind]}，${formatActorLocation(actor)}，${actor.status}`
}
