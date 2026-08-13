import { MAP_TILES_H, MAP_TILES_W } from '../../game/worldGeometry'
import type { SpectatorActor } from '../../services/spectator'
import {
  hasProjectedCoordinates,
  spectatorActorLabel,
} from './formatting'

interface SpectatorMapProps {
  actors: SpectatorActor[]
  focusSlug?: string | null
  label: string
}

function positionPercent(value: number, max: number): string {
  const safe = Number.isFinite(value) ? Math.min(max, Math.max(0, value)) : 0
  return `${(safe / max) * 100}%`
}

export function SpectatorMap({ actors, focusSlug, label }: SpectatorMapProps) {
  const projectedActors = actors.filter(hasProjectedCoordinates)

  return (
    <figure className="spectator-map" aria-label={label}>
      <img src="/marketing/world-map.jpg" alt="Simverse World 小镇地图" />
      <div className="spectator-map__markers" role="list" aria-label={`${label}地图点位`}>
        {projectedActors.map((actor) => (
          <span
            className="spectator-map__marker"
            data-kind={actor.kind}
            data-focus={actor.slug === focusSlug}
            key={`${actor.kind}:${actor.slug}`}
            role="img"
            tabIndex={0}
            aria-label={spectatorActorLabel(actor)}
            style={{
              left: positionPercent(actor.tile_x, MAP_TILES_W - 1),
              top: positionPercent(actor.tile_y, MAP_TILES_H - 1),
            }}
            title={spectatorActorLabel(actor)}
          >
            <span aria-hidden="true">{actor.name.slice(0, 1)}</span>
          </span>
        ))}
      </div>
      <figcaption className="spectator-map__caption">
        <span>只读地图</span>
        <span>位置经过公开投影，不提供游戏控制</span>
      </figcaption>
    </figure>
  )
}

export function SpectatorLegend() {
  return (
    <div className="spectator-legend" aria-label="角色图例">
      {([
        ['npc', '居民'],
        ['agent', 'Agent'],
        ['human', '真人'],
      ] as const).map(([kind, label]) => (
        <span key={kind}><i data-kind={kind} />{label}</span>
      ))}
    </div>
  )
}
