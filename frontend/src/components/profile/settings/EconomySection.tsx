import { useState } from 'react'
import { updateEconomy, type AllSettings } from '../../../services/api'
import { FieldLabel, SaveButton, SectionCard, SectionHeader } from './shared'
import { useSectionForm } from './useSectionForm'
import { useLocale } from '../../../services/locale'

export function EconomySection({ settings }: { settings: AllSettings }) {
  const en = useLocale((state) => state.locale === 'en')
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
      <SectionHeader icon="💰" title={en ? 'SC gameplay credits' : 'SC 游戏积分'} />

      <div style={{ marginBottom: 20 }}>
        <FieldLabel>{en ? 'Current offchain balance' : '当前链下余额'}</FieldLabel>
        <div style={{
          display: 'inline-flex', alignItems: 'baseline', gap: 6,
          padding: '10px 16px',
          background: 'var(--bg-input)', border: '1px solid var(--border)',
          borderRadius: 8,
        }}>
          <span style={{ fontSize: 22, fontWeight: 700, color: 'var(--text-primary)' }}>
            {balance !== undefined ? balance.toLocaleString() : '—'}
          </span>
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>SC</span>
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <FieldLabel>{en ? 'Low-balance alert threshold' : '低余额提醒阈值'}</FieldLabel>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input
            type="number"
            value={alertThreshold}
            onChange={(e) => setAlertThreshold(e.target.value)}
            placeholder={en ? 'For example, 100' : '例如 100'}
            min={0}
            style={{
              width: 140, padding: '8px 12px',
              background: 'var(--bg-input)', border: '1px solid var(--border)',
              borderRadius: 6, color: 'var(--text-primary)', fontSize: 13,
              outline: 'none', boxSizing: 'border-box',
            }}
          />
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>SC</span>
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
          {en ? 'SC is an offchain, non-transferable gameplay credit—not the SIM token. You will receive a reminder below this value.' : 'SC 是链下、不可转让的游戏积分，不是 SIM 代币；余额低于此值时会收到提醒。'}
        </div>
      </div>

      <SaveButton onClick={() => void handleSave()} saving={saving} saved={saved} />
    </SectionCard>
  )
}
