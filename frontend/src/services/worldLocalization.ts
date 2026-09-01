import type { ActiveEventData } from './api'
import type { Locale } from './locale'

type LocalizedEvent = Pick<ActiveEventData, 'title' | 'description'>

const WEATHER_EN: Record<string, Record<string, LocalizedEvent>> = {
  sunny: {
    default: { title: 'Clear Skies', description: 'Sunlight fills the town. It is a fine day.' },
    spring: { title: 'Clear Spring Day', description: 'Warm spring light draws residents toward the flower beds.' },
    summer: { title: 'High Summer Sun', description: 'The summer sun is fierce; the shade is the place to be.' },
    autumn: { title: 'Crisp Autumn Sky', description: 'The air is clear and cool—perfect weather for a walk.' },
    winter: { title: 'Winter Sunshine', description: 'A rare patch of winter sun warms the town.' },
  },
  cloudy: {
    default: { title: 'Cloudy', description: 'The clouds are thickening and the sky has turned grey.' },
    spring: { title: 'Cloudy Spring Day', description: 'Thin clouds cover the sun, but the breeze remains warm.' },
    autumn: { title: 'Autumn Overcast', description: 'Low autumn clouds bring a chill on the wind.' },
    winter: { title: 'Winter Overcast', description: 'Heavy grey clouds hang low, carrying the promise of snow.' },
  },
  rain: {
    default: { title: 'Rain', description: 'Steady rain taps across the rooftops.' },
    spring: { title: 'Spring Rain', description: 'A gentle spring rain drips from every eave.' },
    summer: { title: 'Summer Shower', description: 'A sudden summer shower sends everyone under cover.' },
    winter: { title: 'Freezing Rain', description: 'Cold rain strikes the empty streets.' },
  },
  storm: {
    default: { title: 'Storm', description: 'Wind drives heavy rain through town as thunder rolls overhead.' },
    summer: { title: 'Summer Thunderstorm', description: 'Lightning cuts across the sky and rain pours down.' },
  },
  snow: {
    default: { title: 'Snow', description: 'Snowflakes settle over the town in a coat of white.' },
  },
}

const KNOWN_EVENT_EN: Record<string, LocalizedEvent> = {
  元旦: { title: "New Year's Day", description: 'A new year begins as the town lights up and residents exchange greetings.' },
  情人节: { title: "Valentine's Day", description: 'There is warmth in the air, and residents speak more freely about love and companionship.' },
  儿童节: { title: "Children's Day", description: 'The town rediscovers its playful side through childhood games.' },
  丰收节: { title: 'Harvest Festival', description: 'Residents share the harvest with gratitude and good cheer.' },
  万圣节: { title: 'Halloween', description: 'Jack-o’-lanterns glow while masked residents trade tricks and treats.' },
  冬日庆典: { title: 'Winter Festival', description: 'The first snow falls as warm lights and fireside stories fill the town.' },
  神秘旅人: { title: 'Mysterious Traveler', description: 'A traveler has passed through town carrying rumors from distant places.' },
  流星雨: { title: 'Meteor Shower', description: 'Meteors crossed the sky last night, and everyone is comparing wishes.' },
  集市日: { title: 'Market Day', description: 'Stalls are open and the market is alive with trade and conversation.' },
  旧物展: { title: 'Town Relics Exhibition', description: 'Old objects on display at the library are stirring long-held memories.' },
}

function payloadText(payload: Record<string, unknown> | null, key: string): string | null {
  const value = payload?.[key]
  return typeof value === 'string' && value.trim() ? value : null
}

export function localizeWorldEvent(event: ActiveEventData, locale: Locale): LocalizedEvent {
  if (locale === 'zh-CN') return event

  const titleEn = payloadText(event.payload_json, 'title_en')
  const descriptionEn = payloadText(event.payload_json, 'description_en')
  if (titleEn && descriptionEn) return { title: titleEn, description: descriptionEn }

  if (event.type === 'weather') {
    const kind = payloadText(event.payload_json, 'kind') ?? ''
    const season = payloadText(event.payload_json, 'season') ?? 'default'
    const table = WEATHER_EN[kind]
    if (table) return table[season] ?? table.default
  }

  return KNOWN_EVENT_EN[event.title] ?? {
    title: titleEn ?? 'Live World Event',
    description: descriptionEn ?? 'A live event is unfolding in Simverse World.',
  }
}

const KNOWN_DYNAMIC_EN: Record<string, string> = {
  居民: 'Resident', 摊贩: 'Vendor', 镇长: 'Mayor', 市政厅文书: 'Town Clerk',
  工程街区: 'Engineering District', 产品街区: 'Product District', 学院区: 'Academy District', 自由区: 'Free District',
}

const LOCATION_NAMES_EN: Record<string, string> = {
  academy: 'Academy', tavern: 'Tavern', cafe: 'Café', workshop: 'Workshop', library: 'Library',
  shop: 'General Store', town_hall: 'Town Hall', experiment_building: 'Research Lab',
  house_a: 'House A', house_b: 'House B', house_c: 'House C', house_d: 'House D', house_e: 'House E', house_f: 'House F',
  house_g: 'South House G', house_h: 'South House H', house_i: 'South House I',
  apt_star: 'Starlight Apartments', apt_moon: 'Moonlight Apartments', apt_dawn: 'Dawn Apartments',
  apt_pine: 'Pinewind Apartments', apt_lake: 'Lakeside Apartments', apt_sunrise: 'Sunrise Apartments',
  apt_river: 'Riverbend Apartments', apt_garden: 'Garden Apartments', apt_orchard: 'Orchard Apartments', apt_harbor: 'Harbor Apartments',
  market_hall: 'Market Hall', north_path: 'North Promenade', central_plaza: 'Central Plaza', south_lawn: 'South Lawn',
  town_entrance: 'Town Gate', east_gardens: 'East Gardens', south_quarter: 'South Quarter',
  post_office: 'Post Office', theater: 'Theater',
}

const LOCATION_LORE_EN: Record<string, string> = {
  academy: 'The academy halls echo with generations of study. An abandoned classroom is said to lie beneath them.',
  library: 'The library is older than the town itself, and its deepest shelves are said to rearrange themselves.',
  tavern: 'Names cover the tavern tables. Every carving belongs to a traveler who never returned.',
  cafe: 'The café owner keeps no ledger, yet remembers every regular’s favorite drink and private worry.',
  workshop: 'Unfinished objects fill the workshop corners—each one the remains of someone’s unfinished dream.',
  shop: 'The store sells almost everything. If you truly need something, rumor says it will appear on a shelf.',
  town_hall: 'The Town Hall bell has been silent for a century. Everyone is waiting for its next reason to ring.',
  market_hall: 'Caravan marks cover the beams of Market Hall, each symbol pointing toward a distant trade route.',
  central_plaza: 'The fountain in Central Plaza has witnessed every arrival and farewell in town.',
  post_office: 'Unsent letters fill the post office cubbies. Some are said to address recipients not yet born.',
  theater: 'The theater still smells of new timber. After closing, its stories are said to replay for the empty seats.',
}

function titleFromSlug(slug: string): string {
  return slug.split('_').filter(Boolean).map((part) => part[0]?.toUpperCase() + part.slice(1)).join(' ')
}

export function localizeLocationName(locationId: string, original: string, locale: Locale): string {
  if (locale === 'zh-CN') return original
  return LOCATION_NAMES_EN[locationId] ?? titleFromSlug(locationId) ?? original
}

export function localizeLocationLore(locationId: string, original: string | null, locale: Locale): string | null {
  if (locale === 'zh-CN') return original
  return LOCATION_LORE_EN[locationId] ?? (original ? 'Community-authored lore is available in its original language.' : null)
}

export function localizeDynamicText(value: string | null | undefined, locale: Locale, fallback = ''): string {
  if (!value) return fallback
  if (locale === 'zh-CN') return value
  return KNOWN_DYNAMIC_EN[value] ?? value
}
