// Location data + types live in districtZonesData.ts so this file only
// exports a component (react-refresh/only-export-components).
import { LOCATIONS, MAP_TILES_W, MAP_TILES_H, type LocationKey } from './districtZonesData'

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

export function DistrictZones({ selected, onSelect, mapWidth = 180, mapHeight = 130 }: Props) {
  const expanded = mapWidth > 200
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
    </>
  )
}
