import { apiFetch } from './core'

export type MarketPhase = 'waiting' | 'inbound' | 'trading' | 'outbound' | null

export interface MarketOffer {
  code: string
  type: 'good' | 'service' | 'contract'
  name: string
  description: string
  icon: string
  price_sc: number
  stock: number
  stock_total: number
  per_user_limit: number
  purchased: boolean
  eligible: boolean
  unavailable_reason?: string | null
  available: boolean
  effect_key: string
}

export interface MarketResearchCandidate {
  id: string
  title: string
  summary: string
  offer_type: 'good' | 'service' | 'contract'
  suggested_price_sc: number
  status: string
}

export interface CurrentMarket {
  active: boolean
  enabled: boolean
  purchase_enabled: boolean
  visit_id: string | null
  phase: MarketPhase
  catalog_version: string
  closes_at: string | null
  offers: MarketOffer[]
  research_candidates: MarketResearchCandidate[]
  message: string
}

export interface MarketPurchaseResult {
  ok: boolean
  purchase_id: string
  visit_id: string
  offer_code: string
  offer_type: string
  qty: number
  total_sc: number
  effect: Record<string, unknown>
  idempotent: boolean
  created_at: string | null
}

export function getCurrentMarket(): Promise<CurrentMarket> {
  return apiFetch('/markets/current')
}

export function purchaseMarketOffer(
  visitId: string,
  offerCode: string,
  idempotencyKey: string,
): Promise<MarketPurchaseResult> {
  return apiFetch('/markets/current/purchases', {
    method: 'POST',
    body: JSON.stringify({
      visit_id: visitId,
      offer_code: offerCode,
      idempotency_key: idempotencyKey,
    }),
  })
}
