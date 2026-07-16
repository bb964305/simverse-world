// Location data + types live in districtZonesData.ts so this file only
// exports a component (react-refresh/only-export-components).
import { useState, useEffect } from 'react'
import { LOCATIONS, MAP_TILES_W, MAP_TILES_H, type LocationKey } from './districtZonesData'
import { bridge } from '../../game/phaserBridge'
import { getWorldLocations, type WorldLocation } from '../../services/api'

function tileToMinimap(tileX: number, tileY: number, tileW: number, tileH: number, mapW: number, mapH: number) {
  return {
    left: (tileX / MAP_TILES_W) * mapW,
    top: (tileY / MAP_TILES_H) * mapH,
    width: (tileW / MAP_TILES_W) * mapW,
    height: (tileH / MAP_TILES_H) * mapH,
  }
}

interface Props {
  selected: LocationKey | null
  onSelect: (key: LocationKey) => void
  mapWidth?: number
  mapHeight?: number
}

// Static keys the minimap knows at compile time. Dynamic (overlay) locations
// are NOT part of LocationKey — they arrive at runtime from /world/locations and
// render as a separate, non-selectable layer (spec §7/§9, P3).
const STATIC_SLUGS = new Set<string>(LOCATIONS.map((l) => l.key))

export function DistrictZones({ selected, onSelect, mapWidth = 180, mapHeight = 130 }: Props) {
  const expanded = mapWidth > 200
  const [dynamic, setDynamic] = useState<WorldLocation[]>([])

  useEffect(() => {
    let cancelled = false
    const refresh = () => {
      getWorldLocations()
        .then((r) => {
          if (cancelled) return
          setDynamic(r.locations.filter((l) => l.dynamic && !STATIC_SLUGS.has(l.slug) && l.bounds))
        })
        .catch(() => {})
    }
    refresh()
    const unsub = bridge.on('world:changed', refresh)  // re-pull when a proposal applies/reverts
    return () => { cancelled = true; unsub() }
  }, [])

  return (
    <>
      {LOCATIONS.map((d) => {
        const pos = tileToMinimap(d.tileRect.x, d.tileRect.y, d.tileRect.w, d.tileRect.h, mapWidth, mapHeight)
        const isSelected = selected === d.key
        const isDimmed = selected !== null && !isSelected

        return (
          <div
            key={d.key}
            onClick={(e) => { e.stopPropagation(); onSelect(d.key) }}
            title={d.label}
            style={{
              position: 'absolute',
              left: pos.left,
              top: pos.top,
              width: pos.width,
              height: pos.height,
              background: isDimmed ? d.bgColorDim : d.bgColor,
              border: isSelected ? `2px solid ${d.color}` : `1px solid ${d.color.replace('0.8', '0.4').replace('0.6', '0.3')}`,
              borderRadius: 3,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: expanded ? 16 : 8,
              transition: 'all 0.15s ease',
              boxShadow: isSelected ? `0 0 8px ${d.color}` : 'none',
            }}
          >
            {d.icon}
          </div>
        )
      })}

      {/* Dynamic overlay layer — runtime buildings added by approved proposals. */}
      {dynamic.map((loc) => {
        const b = loc.bounds as number[]
        const pos = tileToMinimap(b[0], b[1], b[2] - b[0], b[3] - b[1], mapWidth, mapHeight)
        return (
          <div
            key={`dyn-${loc.slug}`}
            title={`${loc.name ?? loc.slug}（新增）`}
            style={{
              position: 'absolute',
              left: pos.left,
              top: pos.top,
              width: pos.width,
              height: pos.height,
              background: 'rgba(20,184,166,0.25)',
              border: '1px dashed rgba(20,184,166,0.7)',
              borderRadius: 3,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: expanded ? 16 : 8,
              opacity: selected !== null ? 0.4 : 1,
            }}
          >
            📍
          </div>
        )
      })}
    </>
  )
}
