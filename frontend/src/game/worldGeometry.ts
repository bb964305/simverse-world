export const MAP_TILES_W = 180
export const MAP_TILES_H = 128
export const TILE_SIZE = 32

export function mapHeightForWidth(width: number): number {
  return Math.round((width * MAP_TILES_H) / MAP_TILES_W)
}
