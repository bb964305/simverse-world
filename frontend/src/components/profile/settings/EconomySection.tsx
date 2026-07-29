import { useState } from 'react'
import { updateEconomy, type AllSettings } from '../../../services/api'
import { FieldLabel, SaveButton, SectionCard, SectionHeader } from './shared'
import { useSectionForm } from './useSectionForm'

export function EconomySection({ settings }: { settings: AllSettings }) {
  const economy = settings.economy

  const [alertThreshold, setAlertThreshold] = useState(
    String(economy.low_balance_alert ?? '')
  )

  const { saving, saved, handleSave } = useSectionForm(async () => {
    const parsed = parseFloat(alertThreshold)
    await updateEconomy({ low_balance_alert: isNaN(parsed) ? undefined : parsed })
  })

  const balance = economy.soul_coin_balance

  return (
    <SectionCard>
      <SectionHeader icon="💰" title="灵魂币 & 经济" />

      <div style={{ marginBottom: 20 }}>
        <FieldLabel>当前余额</FieldLabel>
        <div style={{
          display: 'inline-flex', alignItems: 'baseline', gap: 6,
          padding: '10px 16px',
          background: 'var(--bg-input)', border: '1px solid var(--border)',
          borderRadius: 8,
        }}>
          <span style={{ fontSize: 22, fontWeight: 700, color: 'var(--text-primary)' }}>
            {balance !== undefined ? balance.toLocaleString() : '—'}
          </span>
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>灵魂币</span>
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <FieldLabel>低余额提醒阈值</FieldLabel>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input
            type="number"
            value={alertThreshold}
            onChange={(e) => setAlertThreshold(e.target.value)}
            placeholder="例如 100"
            min={0}
            style={{
              width: 140, padding: '8px 12px',
              background: 'var(--bg-input)', border: '1px solid var(--border)',
              borderRadius: 6, color: 'var(--text-primary)', fontSize: 13,
              outline: 'none', boxSizing: 'border-box',
            }}
          />
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>灵魂币</span>
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
          余额低于此值时，系统将发送通知提醒你充值
        </div>
      </div>

      <SaveButton onClick={() => void handleSave()} saving={saving} saved={saved} />
    </SectionCard>
  )
}
