import { useState } from 'react'
import { useGameStore } from '../../../stores/gameStore'
import { updateAccount, type AllSettings } from '../../../services/api'
import { useLocale } from '../../../services/locale'
import { Badge, FieldLabel, SaveButton, SectionCard, SectionHeader, TextInput } from './shared'
import { useSectionForm } from './useSectionForm'

const COPY = {
  'zh-CN': {
    title: '钱包身份', name: '显示名称', namePlaceholder: '输入显示名称', wallet: '已验证钱包',
    connected: 'Robinhood Chain 钱包', note: '钱包签名是唯一登录凭证；平台不会保存你的私钥。', missing: '钱包身份未关联',
  },
  en: {
    title: 'Wallet identity', name: 'Display name', namePlaceholder: 'Enter display name', wallet: 'Verified wallet',
    connected: 'Robinhood Chain wallet', note: 'Your wallet signature is the only sign-in credential. Simverse never stores your private key.', missing: 'No wallet identity linked',
  },
} as const

export function AccountSection({ settings }: { settings: AllSettings }) {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const setAuthUser = useGameStore((s) => s.setAuth)
  const token = useGameStore((s) => s.token)
  const user = useGameStore((s) => s.user)
  const [displayName, setDisplayName] = useState(settings.account.display_name)
  const walletAddress = settings.account.wallet_address ?? user?.wallet_address ?? null

  const { saving, saved, handleSave } = useSectionForm(async () => {
    const result = await updateAccount({ display_name: displayName })
    // Update store so TopNav/Sidebar reflects new name immediately
    if (user && token) {
      setAuthUser({ ...user, name: result.display_name }, token)
    }
  })

  return (
    <SectionCard>
      <SectionHeader icon="◇" title={copy.title} />

      <div style={{ marginBottom: 16 }}>
        <FieldLabel>{copy.name}</FieldLabel>
        <div style={{ display: 'flex', gap: 8 }}>
          <TextInput value={displayName} onChange={setDisplayName} placeholder={copy.namePlaceholder} />
          <SaveButton onClick={() => void handleSave()} saving={saving} saved={saved} />
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <FieldLabel>{copy.wallet}</FieldLabel>
        <div data-testid="wallet-identity-address" style={{ fontSize: 13, color: 'var(--text-secondary)', padding: '8px 12px', background: 'var(--bg-input)', borderRadius: 6, border: '1px solid var(--border)', fontFamily: 'monospace', overflowWrap: 'anywhere' }}>
          {walletAddress ?? copy.missing}
        </div>
      </div>

      <div>
        <FieldLabel>{copy.note}</FieldLabel>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Badge label={copy.connected} active={Boolean(walletAddress)} />
        </div>
      </div>
    </SectionCard>
  )
}
