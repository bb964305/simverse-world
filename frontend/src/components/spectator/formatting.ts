import type {
  SpectatorActor,
  SpectatorActorKind,
  ViewerLocation,
} from '../../services/spectator'
import { LOCATIONS } from '../minimap/districtZonesData'
import type { Locale } from '../../services/locale'

export type ProjectedSpectatorActor = SpectatorActor & { tile_x: number; tile_y: number }

const LOCATION_LABELS: Map<string, string> = new Map(
  LOCATIONS.map((location) => [location.key, location.label]),
)

const ENGLISH_LOCATION_LABELS: Record<string, string> = {
  academy: 'Academy', tavern: 'Tavern', cafe: 'Cafe', workshop: 'Workshop',
  library: 'Library', shop: 'General Store', town_hall: 'Town Hall',
  experiment_building: 'Experiment Building', market_hall: 'Market Hall',
  north_path: 'North Promenade', central_plaza: 'Central Plaza', south_lawn: 'South Lawn',
  town_entrance: 'Town Entrance', east_gardens: 'East Gardens', south_quarter: 'South Quarter',
}

export const SPECTATOR_KIND_LABELS: Record<SpectatorActorKind, string> = {
  npc: '居民',
  agent: 'Agent 玩家',
  human: '真人玩家',
}

export function formatLocationName(
  location: string | ViewerLocation | null | undefined,
  fallback = '小镇中',
  locale: Locale = 'zh-CN',
): string {
  if (!location) return fallback
  if (typeof location !== 'string') {
    if (locale === 'en') return ENGLISH_LOCATION_LABELS[location.slug] || location.name || location.slug
    return location.name || LOCATION_LABELS.get(location.slug) || location.slug
  }
  if (locale === 'en') return ENGLISH_LOCATION_LABELS[location] || location.replaceAll('_', ' ')
  return LOCATION_LABELS.get(location) || location
}

export function formatActorLocation(
  actor: Pick<SpectatorActor, 'district'>,
  fallback = '小镇中',
  locale: Locale = 'zh-CN',
): string {
  return formatLocationName(actor.district, fallback, locale)
}

export function hasProjectedCoordinates(
  actor: SpectatorActor,
): actor is ProjectedSpectatorActor {
  return typeof actor.tile_x === 'number'
    && Number.isFinite(actor.tile_x)
    && typeof actor.tile_y === 'number'
    && Number.isFinite(actor.tile_y)
}

export function spectatorActorLabel(actor: SpectatorActor, locale: Locale = 'zh-CN'): string {
  const kind = locale === 'en'
    ? ({ npc: 'Resident', agent: 'Agent player', human: 'Human player' } as const)[actor.kind]
    : SPECTATOR_KIND_LABELS[actor.kind]
  return locale === 'en'
    ? `${actor.name}, ${kind}, ${formatActorLocation(actor, 'in town', locale)}, ${actor.status}`
    : `${actor.name}，${kind}，${formatActorLocation(actor)}，${actor.status}`
}
