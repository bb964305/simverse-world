import { MAP_TILES_H, MAP_TILES_W } from '../../game/worldGeometry'
import type { SpectatorActor } from '../../services/spectator'
import type { Locale } from '../../services/locale'
import {
  hasProjectedCoordinates,
  spectatorActorLabel,
} from './formatting'

interface SpectatorMapProps {
  actors: SpectatorActor[]
  focusSlug?: string | null
  label: string
  locale?: Locale
}

function positionPercent(value: number, max: number): string {
  const safe = Number.isFinite(value) ? Math.min(max, Math.max(0, value)) : 0
  return `${(safe / max) * 100}%`
}

export function SpectatorMap({ actors, focusSlug, label, locale = 'zh-CN' }: SpectatorMapProps) {
  const projectedActors = actors.filter(hasProjectedCoordinates)
  const copy = locale === 'en'
    ? { image: 'Simverse World town map', points: ' map markers', readOnly: 'Read-only map', privacy: 'Public projection only; game controls are disabled' }
    : { image: 'Simverse World 小镇地图', points: '地图点位', readOnly: '只读地图', privacy: '位置经过公开投影，不提供游戏控制' }

  return (
    <figure className="spectator-map" aria-label={label}>
      <img src="/marketing/world-map.jpg" alt={copy.image} />
      <div className="spectator-map__markers" role="list" aria-label={`${label}${copy.points}`}>
        {projectedActors.map((actor) => (
          <span
            className="spectator-map__marker"
            data-kind={actor.kind}
            data-focus={actor.slug === focusSlug}
            key={`${actor.kind}:${actor.slug}`}
            role="img"
            tabIndex={0}
            aria-label={spectatorActorLabel(actor, locale)}
            style={{
              left: positionPercent(actor.tile_x, MAP_TILES_W - 1),
              top: positionPercent(actor.tile_y, MAP_TILES_H - 1),
            }}
            title={spectatorActorLabel(actor, locale)}
          >
            <span aria-hidden="true">{actor.name.slice(0, 1)}</span>
          </span>
        ))}
      </div>
      <figcaption className="spectator-map__caption">
        <span>{copy.readOnly}</span>
        <span>{copy.privacy}</span>
      </figcaption>
    </figure>
  )
}

export function SpectatorLegend({ locale = 'zh-CN' }: { locale?: Locale }) {
  const labels = locale === 'en' ? ['Resident', 'Agent', 'Human'] : ['居民', 'Agent', '真人']
  return (
    <div className="spectator-legend" aria-label={locale === 'en' ? 'Actor legend' : '角色图例'}>
      {([
        ['npc', labels[0]],
        ['agent', labels[1]],
        ['human', labels[2]],
      ] as const).map(([kind, actorLabel]) => (
        <span key={kind}><i data-kind={kind} />{actorLabel}</span>
      ))}
    </div>
  )
}
