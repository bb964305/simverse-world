import { useState, useCallback, useEffect, useRef } from 'react'
import { MinimapCanvas } from './MinimapCanvas'
import { DistrictZones } from './DistrictZones'
import type { DistrictKey } from './districtZonesData'
import { ResidentPanel } from './ResidentPanel'
import { bridge } from '../../game/phaserBridge'
import { MAP_TILES_H, MAP_TILES_W, mapHeightForWidth } from '../../game/worldGeometry'
import { useLocale } from '../../services/locale'

const SMALL_W = 180
const SMALL_H = mapHeightForWidth(SMALL_W)
const LARGE_W = 560
const EXPANDED_VIEWPORT_GUTTER = 32

function getExpandedMapWidth(): number {
  if (typeof window === 'undefined') return LARGE_W
  return Math.max(1, Math.min(LARGE_W, window.innerWidth - EXPANDED_VIEWPORT_GUTTER))
}

export function MinimapOverlay() {
  const locale = useLocale((state) => state.locale)
  const [selectedDistrict, setSelectedDistrict] = useState<DistrictKey | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [expandedMapWidth, setExpandedMapWidth] = useState(getExpandedMapWidth)
  const smallMapRef = useRef<HTMLDivElement>(null)
  const largeMapRef = useRef<HTMLDivElement>(null)
  const expandedMapHeight = mapHeightForWidth(expandedMapWidth)

  const handleSelectDistrict = useCallback((key: DistrictKey) => {
    setSelectedDistrict((prev) => (prev === key ? null : key))
  }, [])

  const handleClosePanel = useCallback(() => {
    setSelectedDistrict(null)
  }, [])

  const handleDoubleClick = useCallback(() => {
    setExpanded((prev) => !prev)
    setSelectedDistrict(null)
  }, [])

  // Convert click position to tile coordinates and teleport
  const handleMapClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    // Don't teleport if clicking a zone or resident — let those handlers take priority
    if (e.target !== e.currentTarget) return

    const container = e.currentTarget
    const rect = container.getBoundingClientRect()
    const clickX = e.clientX - rect.left
    const clickY = e.clientY - rect.top
    const mapW = container.clientWidth
    const mapH = container.clientHeight

    const tileX = Math.floor((clickX / mapW) * MAP_TILES_W)
    const tileY = Math.floor((clickY / mapH) * MAP_TILES_H)

    // Clamp to valid map bounds
    const clampedX = Math.max(0, Math.min(MAP_TILES_W - 1, tileX))
    const clampedY = Math.max(0, Math.min(MAP_TILES_H - 1, tileY))

    bridge.emit('minimap:teleport', { tileX: clampedX, tileY: clampedY })
  }, [])

  // ESC to close expanded minimap
  useEffect(() => {
    if (!expanded) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setExpanded(false)
        setSelectedDistrict(null)
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [expanded])

  useEffect(() => {
    const handleResize = () => setExpandedMapWidth(getExpandedMapWidth())
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // Expanded: centered overlay with large map
  if (expanded) {
    return (
      <div
        className="game-minimap__backdrop"
        onClick={(e) => {
          // Click backdrop to close panel (not collapse — use double-click to collapse)
          if (e.target === e.currentTarget) {
            setSelectedDistrict(null)
          }
        }}
      >
        <div className="game-minimap__expanded-layout">
          {/* Large map container */}
          <div
            ref={largeMapRef}
            className="game-minimap__map game-minimap__map--expanded"
            style={{
              width: expandedMapWidth,
              height: expandedMapHeight,
            }}
            onClick={(e) => {
              // Click on empty map area → teleport
              if (e.target === e.currentTarget) {
                handleMapClick(e)
              } else {
                // Click on zone or other element → clear panel
                setSelectedDistrict(null)
              }
            }}
            onDoubleClick={handleDoubleClick}
          >
            <MinimapCanvas width={expandedMapWidth} height={expandedMapHeight} />
            <DistrictZones
              selected={selectedDistrict}
              onSelect={handleSelectDistrict}
              mapWidth={expandedMapWidth}
              mapHeight={expandedMapHeight}
            />
            <button
              className="game-minimap__close"
              onClick={() => { setExpanded(false); setSelectedDistrict(null) }}
              aria-label={locale === 'en' ? 'Close expanded map' : '关闭大地图'}
            >
              ✕
            </button>
          </div>

          {/* Resident panel to the right of the large map */}
          {selectedDistrict && (
            <ResidentPanel
              district={selectedDistrict}
              onClose={handleClosePanel}
              variant="expanded"
            />
          )}
        </div>
      </div>
    )
  }

  // Collapsed: small minimap in top-left
  return (
    <div
      className="game-minimap__collapsed"
    >
      {/* Small map container */}
      <div
        ref={smallMapRef}
        className="game-minimap__map game-minimap__map--collapsed"
        style={{ width: SMALL_W, height: SMALL_H }}
        onClick={(e) => {
          // Click on empty map area → teleport
          if (e.target === e.currentTarget) {
            handleMapClick(e)
          } else {
            // Click on zone → clear any open panel from previous selection
            setSelectedDistrict(null)
          }
        }}
        onDoubleClick={handleDoubleClick}
      >
        <MinimapCanvas width={SMALL_W} height={SMALL_H} />
        <DistrictZones
          selected={selectedDistrict}
          onSelect={handleSelectDistrict}
          mapWidth={SMALL_W}
          mapHeight={SMALL_H}
        />
      </div>

      {/* Resident panel */}
      {selectedDistrict && (
        <ResidentPanel district={selectedDistrict} onClose={handleClosePanel} variant="collapsed" />
      )}
    </div>
  )
}
