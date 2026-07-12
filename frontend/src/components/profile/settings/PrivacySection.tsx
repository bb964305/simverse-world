import { useState } from 'react'
import { updatePrivacy, type AllSettings } from '../../../services/api'
import { FieldLabel, SaveButton, SectionCard, SectionHeader, Toggle } from './shared'
import { useSectionForm } from './useSectionForm'

type PersonaVisibility = 'full' | 'identity_card_only' | 'hidden'

export function PrivacySection({ settings }: { settings: AllSettings }) {
  const privacy = settings.privacy as {
    map_visible?: boolean
    persona_visibility?: PersonaVisibility
    allow_conversation_stats?: boolean
  }

  const [mapVisible, setMapVisible] = useState(privacy.map_visible ?? true)
  const [personaVisibility, setPersonaVisibility] = useState<PersonaVisibility>(
    privacy.persona_visibility ?? 'full'
  )
  const [allowStats, setAllowStats] = useState(privacy.allow_conversation_stats ?? true)

  const { saving, saved, handleSave } = useSectionForm(async () => {
    await updatePrivacy({
      map_visible: mapVisible,
      persona_visibility: personaVisibility,
      allow_conversation_stats: allowStats,
    })
  })

  const visibilityOptions: { value: PersonaVisibility; label: string; desc: string }[] = [
    { value: 'full', label: '完整公开', desc: '所有人可查看角色的完整设定' },
    { value: 'identity_card_only', label: '仅身份卡', desc: '只显示名称和基本信息' },
    { value: 'hidden', label: '隐藏', desc: '对他人完全隐藏角色信息' },
  ]

  return (
    <SectionCard>
      <SectionHeader icon="🔒" title="隐私设置" />

      <div style={{ marginBottom: 16 }}>
        <Toggle value={mapVisible} onChange={setMapVisible} label="在地图上显示我的角色" />
      </div>

      <div style={{ marginBottom: 16 }}>
        <FieldLabel>角色信息可见范围</FieldLabel>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {visibilityOptions.map((opt) => (
            <label
              key={opt.value}
              onClick={() => setPersonaVisibility(opt.value)}
              style={{
                display: 'flex', alignItems: 'flex-start', gap: 10,
                padding: '10px 12px', borderRadius: 8, cursor: 'pointer',
                background: personaVisibility === opt.value ? 'var(--accent)10' : 'var(--bg-input)',
                border: personaVisibility === opt.value
                  ? '1px solid var(--accent)'
                  : '1px solid var(--border)',
                transition: 'border-color 0.15s',
              }}
            >
              <div style={{
                width: 16, height: 16, borderRadius: '50%', flexShrink: 0, marginTop: 1,
                border: `2px solid ${personaVisibility === opt.value ? 'var(--accent)' : 'var(--border)'}`,
                background: personaVisibility === opt.value ? 'var(--accent)' : 'transparent',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                {personaVisibility === opt.value && (
                  <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'white' }} />
                )}
              </div>
              <div>
                <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-primary)' }}>{opt.label}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{opt.desc}</div>
              </div>
            </label>
          ))}
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <Toggle value={allowStats} onChange={setAllowStats} label="允许统计对话数据（用于排行榜）" />
      </div>

      <SaveButton onClick={() => void handleSave()} saving={saving} saved={saved} />
    </SectionCard>
  )
}
