import { apiFetch } from './core'

// ─── Shop API (S3/D2) ────────────────────────────────────────────

export interface ShopItemData {
  code: string
  kind: string
  name: string
  description: string
  icon: string | null
  price_sc: number
  payload: Record<string, unknown>
  active: boolean
}

export interface ShopInventoryRow {
  item_code: string
  qty: number
  total_sc: number
  name: string
  icon: string | null
  kind: string
}

export interface ShopPurchaseResult {
  ok: boolean
  item_code: string
  qty: number
  total_sc: number
  effect: Record<string, unknown> | null
}

export function getShopCatalog(): Promise<{ items: ShopItemData[] }> {
  return apiFetch('/shop/catalog')
}

export function getShopInventory(): Promise<{ inventory: ShopInventoryRow[] }> {
  return apiFetch('/shop/inventory')
}

export function purchaseShopItem(
  item_code: string, qty = 1, context?: Record<string, unknown>,
): Promise<ShopPurchaseResult> {
  return apiFetch('/shop/purchase', {
    method: 'POST',
    body: JSON.stringify({ item_code, qty, context: context ?? null }),
  })
}
