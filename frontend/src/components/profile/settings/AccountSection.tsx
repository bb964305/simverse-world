import { useState } from 'react'
import { useGameStore } from '../../../stores/gameStore'
import { updateAccount, type AllSettings } from '../../../services/api'
import { Badge, FieldLabel, SaveButton, SectionCard, SectionHeader, TextInput } from './shared'
import { useSectionForm } from './useSectionForm'

export function AccountSection({ settings }: { settings: AllSettings }) {
  const setAuthUser = useGameStore((s) => s.setAuth)
  const token = useGameStore((s) => s.token)
  const user = useGameStore((s) => s.user)
  const [displayName, setDisplayName] = useState(settings.account.display_name)

  const { saving, saved, handleSave } = useSectionForm(async () => {
    const result = await updateAccount({ display_name: displayName })
    // Update store so TopNav/Sidebar reflects new name immediately
    if (user && token) {
      setAuthUser({ ...user, name: result.display_name }, token)
    }
  })

  return (
    <SectionCard>
      <SectionHeader icon="👤" title="账号" />

      <div style={{ marginBottom: 16 }}>
        <FieldLabel>显示名称</FieldLabel>
        <div style={{ display: 'flex', gap: 8 }}>
          <TextInput value={displayName} onChange={setDisplayName} placeholder="输入显示名称" />
          <SaveButton onClick={() => void handleSave()} saving={saving} saved={saved} />
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <FieldLabel>邮箱</FieldLabel>
        <div style={{ fontSize: 13, color: 'var(--text-secondary)', padding: '8px 12px', background: 'var(--bg-input)', borderRadius: 6, border: '1px solid var(--border)' }}>
          {settings.account.email}
        </div>
      </div>

      <div>
        <FieldLabel>登录方式</FieldLabel>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Badge label="邮箱" active={settings.account.has_password} />
          <Badge label="GitHub" active={settings.account.github_bound} />
          <Badge label="LinuxDo" active={settings.account.linuxdo_bound} />
        </div>
      </div>
    </SectionCard>
  )
}
