const ROOT = '/assets/village/caravan'
const VERSION = encodeURIComponent(__CARAVAN_ASSET_VERSION__)

function versioned(path: string): string {
  return `${ROOT}/${path}?v=${VERSION}`
}

export const CARAVAN_MERCHANT_TEXTURE_URL = versioned('merchant/texture.png')
export const CARAVAN_MERCHANT_ATLAS_URL = versioned('merchant/atlas.json')
export const CARAVAN_CONVOY_TEXTURE_URL = versioned('convoy/texture.png')
export const CARAVAN_CONVOY_ATLAS_URL = versioned('convoy/atlas.json')
export const CARAVAN_STALL_TEXTURE_URL = versioned('stall/texture.png')
