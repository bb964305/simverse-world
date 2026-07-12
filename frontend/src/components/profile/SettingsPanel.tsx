import { useEffect, useState } from 'react'
import { getSettings, type AllSettings } from '../../services/api'
import { AccountSection } from './settings/AccountSection'
import { CharacterSection } from './settings/CharacterSection'
import { InteractionSection } from './settings/InteractionSection'
import { PrivacySection } from './settings/PrivacySection'
import { LLMSection } from './settings/LLMSection'
import { EconomySection } from './settings/EconomySection'

export function SettingsPanel() {
  const [settings, setSettings] = useState<AllSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const load = async () => {
      setLoading(true)
      setError(null)
      try {
        const data = await getSettings()
        setSettings(data)
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : '加载失败')
      } finally {
        setLoading(false)
      }
    }
    void load()
  }, [])

  if (loading) {
    return (
      <div style={{ color: 'var(--text-muted)', padding: 40, textAlign: 'center' }}>
        加载设置...
      </div>
    )
  }

  if (error || !settings) {
    return (
      <div style={{ color: '#ff6b6b', padding: 40, textAlign: 'center' }}>
        {error ?? '加载失败，请刷新重试'}
      </div>
    )
  }

  return (
    <div style={{ maxWidth: 720, margin: '0 auto' }}>
      <h1 style={{ fontSize: 20, fontWeight: 700, marginBottom: 24, color: 'var(--text-primary)' }}>
        设置
      </h1>
      <AccountSection settings={settings} />
      <CharacterSection settings={settings} />
      <InteractionSection settings={settings} />
      <PrivacySection settings={settings} />
      <LLMSection settings={settings} />
      <EconomySection settings={settings} />
    </div>
  )
}
