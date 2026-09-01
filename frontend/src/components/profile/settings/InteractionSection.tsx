import { useState } from 'react'
import { updateInteraction, type AllSettings } from '../../../services/api'
import { FieldLabel, SaveButton, SectionCard, SectionHeader, Toggle } from './shared'
import { useSectionForm } from './useSectionForm'
import { useLocale } from '../../../services/locale'

export function InteractionSection({ settings }: { settings: AllSettings }) {
  const en = useLocale((state) => state.locale === 'en')
  const interaction = settings.interaction as {
    reply_mode?: string
    offline_auto_reply?: boolean
    notification_chat?: boolean
    notification_system?: boolean
  }

  const [replyMode, setReplyMode] = useState<'manual' | 'auto'>(
    (interaction.reply_mode as 'manual' | 'auto') ?? 'manual'
  )
  const [offlineAutoReply, setOfflineAutoReply] = useState(interaction.offline_auto_reply ?? false)
  const [notificationChat, setNotificationChat] = useState(interaction.notification_chat ?? true)
  const [notificationSystem, setNotificationSystem] = useState(interaction.notification_system ?? true)

  const { saving, saved, handleSave } = useSectionForm(async () => {
    await updateInteraction({
      reply_mode: replyMode,
      offline_auto_reply: offlineAutoReply,
      notification_chat: notificationChat,
      notification_system: notificationSystem,
    })
  })

  return (
    <SectionCard>
      <SectionHeader icon="💬" title={en ? 'Interaction settings' : '互动设置'} />

      <div style={{ marginBottom: 16 }}>
        <FieldLabel>{en ? 'Reply mode' : '回复模式'}</FieldLabel>
        <div style={{ display: 'flex', gap: 8 }}>
          {(['manual', 'auto'] as const).map((mode) => (
            <button
              key={mode}
              onClick={() => setReplyMode(mode)}
              style={{
                padding: '7px 18px', borderRadius: 6, fontSize: 13,
                fontWeight: replyMode === mode ? 600 : 400,
                cursor: 'pointer',
                background: replyMode === mode ? 'var(--accent)' : 'var(--bg-input)',
                color: replyMode === mode ? 'white' : 'var(--text-secondary)',
                border: replyMode === mode ? '1px solid var(--accent)' : '1px solid var(--border)',
                transition: 'background 0.15s',
              }}
            >
              {mode === 'manual' ? (en ? 'Manual' : '手动') : (en ? 'Automatic' : '自动')}
            </button>
          ))}
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
          {replyMode === 'manual'
            ? (en ? 'Choose manually whether the AI responds after a visitor starts a conversation.' : '访客发起对话后，由你手动决定是否用 AI 回复')
            : (en ? 'Your AI character responds to visitor messages automatically.' : '访客消息自动由 AI 角色回复')}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginBottom: 16 }}>
        <Toggle value={offlineAutoReply} onChange={setOfflineAutoReply} label={en ? 'Reply automatically while offline' : '离线时自动回复'} />
        <Toggle value={notificationChat} onChange={setNotificationChat} label={en ? 'New conversation notifications' : '新对话通知'} />
        <Toggle value={notificationSystem} onChange={setNotificationSystem} label={en ? 'System notifications' : '系统通知'} />
      </div>

      <SaveButton onClick={() => void handleSave()} saving={saving} saved={saved} />
    </SectionCard>
  )
}
