// ─── Helpers ──────────────────────────────────────────────────────

import { parseUTC } from '../../../utils/time'

export function loginMethodIcon(method: string): string {
  if (method === 'github') return '🐙'
  if (method === 'linuxdo') return '🐧'
  if (method === 'password') return '🔑'
  return '🔗'
}

export function formatDate(iso: string): string {
  // 后端 naive-UTC isoformat —— parseUTC 补 Z 防跨日偏移
  return parseUTC(iso).toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
}

export const btnBase: React.CSSProperties = {
  background: 'var(--bg-input)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  padding: '6px 14px',
  fontSize: 12,
  cursor: 'pointer',
  color: 'var(--text-secondary)',
}
