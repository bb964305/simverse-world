import { useEffect, useState } from 'react'
import {
  updateCharacter,
  getSpriteTemplates,
  type AllSettings,
  type SpriteTemplate,
} from '../../../services/api'
import { FieldLabel, SaveButton, SectionCard, SectionHeader, TextInput } from './shared'
import { useSectionForm } from './useSectionForm'

export function CharacterSection({ settings }: { settings: AllSettings }) {
  const character = settings.character
  const [sprites, setSprites] = useState<SpriteTemplate[]>([])
  const [selectedSprite, setSelectedSprite] = useState(character?.sprite_key ?? '')
  const [characterName, setCharacterName] = useState(character?.name ?? '')
  const [loadingSprites, setLoadingSprites] = useState(true)

  useEffect(() => {
    const loadSprites = async () => {
      try {
        const templates = await getSpriteTemplates()
        setSprites(templates)
      } catch { /* ignore */ }
      finally { setLoadingSprites(false) }
    }
    void loadSprites()
  }, [])

  const { saving, saved, handleSave } = useSectionForm(async () => {
    await updateCharacter({ name: characterName, sprite_key: selectedSprite })
  })

  if (!character) {
    return (
      <SectionCard>
        <SectionHeader icon="🧑‍🎤" title="角色" />
        <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: '20px 0' }}>
          暂无绑定角色。前往地图创建你的居民后再来设置。
        </div>
      </SectionCard>
    )
  }

  return (
    <SectionCard>
      <SectionHeader icon="🧑‍🎤" title="角色" />

      <div style={{ display: 'flex', gap: 24, marginBottom: 20 }}>
        {/* Current sprite preview */}
        <div style={{ flexShrink: 0 }}>
          <FieldLabel>当前形象</FieldLabel>
          <div style={{
            width: 72, height: 72,
            background: 'var(--bg-input)', border: '1px solid var(--border)',
            borderRadius: 10,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            overflow: 'visible',
          }}>
            {selectedSprite ? (
              <div style={{
                width: 32,
                height: 32,
                backgroundImage: `url(/assets/village/agents/${encodeURIComponent(selectedSprite)}/texture.png)`,
                backgroundPosition: '-32px 0px',
                backgroundSize: '96px 128px',
                imageRendering: 'pixelated',
                transform: 'scale(2)',
                transformOrigin: 'center',
              }} />
            ) : (
              <span style={{ fontSize: 32 }}>🧑</span>
            )}
          </div>
        </div>

        {/* Character name */}
        <div style={{ flex: 1 }}>
          <FieldLabel>角色名称</FieldLabel>
          <TextInput value={characterName} onChange={setCharacterName} placeholder="输入角色名称" />
        </div>
      </div>

      {/* Sprite selection grid */}
      <div>
        <FieldLabel>选择形象（点击切换）</FieldLabel>
        {loadingSprites ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>加载形象列表...</div>
        ) : (
          <div style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(56px, 1fr))',
            gap: 8,
          }}>
            {sprites.map((sprite) => (
              <div
                key={sprite.key}
                title={`${sprite.key} · ${sprite.vibe}`}
                onClick={() => setSelectedSprite(sprite.key)}
                style={{
                  width: 56, height: 56,
                  background: selectedSprite === sprite.key ? 'var(--accent)10' : 'var(--bg-input)',
                  border: selectedSprite === sprite.key
                    ? '2px solid var(--accent)'
                    : '1px solid var(--border)',
                  borderRadius: 8, cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  transition: 'border-color 0.15s',
                  overflow: 'visible',
                }}
              >
                <div style={{
                  width: 32,
                  height: 32,
                  backgroundImage: `url(/assets/village/agents/${encodeURIComponent(sprite.key)}/texture.png)`,
                  backgroundPosition: '-32px 0px',
                  backgroundSize: '96px 128px',
                  imageRendering: 'pixelated',
                  transform: 'scale(1.25)',
                  transformOrigin: 'center',
                }} />
              </div>
            ))}
          </div>
        )}
        {selectedSprite && (
          <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-muted)' }}>
            已选：{selectedSprite}
          </div>
        )}
      </div>

      <SaveButton onClick={() => void handleSave()} saving={saving} saved={saved} />
    </SectionCard>
  )
}
