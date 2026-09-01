import { useState } from 'react'
import type { ForgeStatusResponse } from '../../services/api'
import { useLocale } from '../../services/locale'

interface ForgePreviewProps {
  state: ForgeStatusResponse | null
}

const DISTRICT_NAMES: Record<string, { zh: string; en: string }> = {
  engineering: { zh: '工程街区', en: 'Engineering District' },
  product: { zh: '产品街区', en: 'Product District' },
  academy: { zh: '学院区', en: 'Academy District' },
  free: { zh: '自由区', en: 'Free District' },
}

function MarkdownSection({ title, content, icon, isWaiting, en }: {
  title: string
  content: string
  icon: string
  isWaiting: boolean
  en: boolean
}) {
  const [expanded, setExpanded] = useState(true)

  return (
    <div style={{
      border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden',
      marginBottom: 12,
    }}>
      {/* Section header */}
      <button
        onClick={() => setExpanded(!expanded)}
        style={{
          width: '100%', background: 'var(--bg-page)', border: 'none',
          padding: '10px 14px', display: 'flex', alignItems: 'center',
          gap: 8, cursor: 'pointer', textAlign: 'left',
        }}
      >
        <span style={{ fontSize: 16 }}>{icon}</span>
        <span style={{ flex: 1, fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>{title}</span>
        {isWaiting && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)', background: 'var(--bg-input)', padding: '2px 8px', borderRadius: 4 }}>
            {en ? 'Waiting for input…' : '等待输入…'}
          </span>
        )}
        <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div style={{
          padding: '12px 14px', borderTop: '1px solid var(--border)',
          background: 'var(--bg-card)',
        }}>
          {isWaiting || !content ? (
            <div style={{ color: 'var(--text-muted)', fontSize: 12, fontStyle: 'italic' }}>
              {en ? 'Waiting for input…' : '等待输入…'}
            </div>
          ) : (
            <pre style={{
              margin: 0, fontSize: 12, lineHeight: 1.7, color: 'var(--text-secondary)',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'inherit',
            }}>
              {content}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

export function ForgePreview({ state }: ForgePreviewProps) {
  const en = useLocale((localeState) => localeState.locale === 'en')
  const isGenerating = state?.status === 'generating'
  const isDone = state?.status === 'done'
  const isError = state?.status === 'error'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        padding: '14px 20px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 14, color: 'var(--text-primary)' }}>
            {en ? 'Live preview' : '实时预览'}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
            {state?.name ? (en ? `Resident: ${state.name}` : `居民：${state.name}`) : (en ? 'Waiting for your input…' : '等待你的输入…')}
          </div>
        </div>

        {isDone && state && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4 }}>
            <div style={{ fontSize: 14 }}>{'⭐'.repeat(state.star_rating)}</div>
            <div style={{
              fontSize: 11, background: '#53d76920', color: 'var(--accent-green)',
              padding: '2px 8px', borderRadius: 4,
            }}>
              {DISTRICT_NAMES[state.district]?.[en ? 'en' : 'zh'] ?? state.district}
            </div>
          </div>
        )}

        {isGenerating && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--accent-red)' }}>
            <span style={{
              width: 12, height: 12, border: '2px solid var(--accent-red)',
              borderTopColor: 'transparent', borderRadius: '50%',
              display: 'inline-block', animation: 'spin 0.8s linear infinite', flexShrink: 0,
            }} />
            {en ? 'Forging…' : '炼化中…'}
          </div>
        )}
      </div>

      {/* Content */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '16px 20px' }}>

        {/* Collected answers summary */}
        {state?.answers && Object.keys(state.answers).length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: 8 }}>
              {en ? 'Collected information' : '已收集信息'}
            </div>
            {Object.entries(state.answers).map(([step, answer]) => {
              const labels: Record<string, string> = en
                ? { '1': 'Name', '2': 'Skills', '3': 'Persona', '4': 'Soul', '5': 'Sources' }
                : { '1': '名字', '2': '能力', '3': '性格', '4': '灵魂', '5': '素材' }
              return (
                <div key={step} style={{
                  display: 'flex', gap: 8, marginBottom: 6, fontSize: 12,
                }}>
                  <span style={{
                    flexShrink: 0, width: 36, height: 18, background: 'var(--bg-input)',
                    borderRadius: 3, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 10, color: 'var(--text-muted)',
                  }}>
                    {labels[step] ?? `Q${step}`}
                  </span>
                  <span style={{ color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                    {answer.length > 60 ? answer.slice(0, 60) + '...' : answer}
                  </span>
                </div>
              )
            })}
          </div>
        )}

        {/* Three-layer sections */}
        <MarkdownSection
          title={en ? '📋 Ability layer' : '📋 Ability（能力层）'}
          icon="📋"
          content={state?.ability_md ?? ''}
          isWaiting={!state?.ability_md}
          en={en}
        />
        <MarkdownSection
          title={en ? '🎭 Persona layer' : '🎭 Persona（人格层）'}
          icon="🎭"
          content={state?.persona_md ?? ''}
          isWaiting={!state?.persona_md}
          en={en}
        />
        <MarkdownSection
          title={en ? '💎 Soul layer' : '💎 Soul（灵魂层）'}
          icon="💎"
          content={state?.soul_md ?? ''}
          isWaiting={!state?.soul_md}
          en={en}
        />

        {/* Done state badge */}
        {isDone && state && (
          <div style={{
            background: '#53d76915', border: '1px solid #53d76940',
            borderRadius: 10, padding: '14px 16px', marginTop: 8,
          }}>
            <div style={{ fontWeight: 700, color: 'var(--accent-green)', fontSize: 14, marginBottom: 4 }}>
              ✅ {en ? 'Forge complete!' : '炼化完成！'}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
              {en ? 'Resident ' : '居民 '}<strong>{state.name}</strong>{en ? ' has moved into ' : ' 已入住'}{' '}
              <strong>{DISTRICT_NAMES[state.district]?.[en ? 'en' : 'zh'] ?? state.district}</strong>
              <br />
              {en ? 'Quality rating: ' : '质量评级：'}{'⭐'.repeat(state.star_rating)}
            </div>
          </div>
        )}

        {/* Error state */}
        {isError && state?.error && (
          <div style={{
            background: '#e9456015', border: '1px solid #e9456040',
            borderRadius: 10, padding: '14px 16px', marginTop: 8,
          }}>
            <div style={{ fontWeight: 700, color: 'var(--accent-red)', fontSize: 14, marginBottom: 4 }}>
              ❌ {en ? 'Forge failed' : '炼化失败'}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{en ? 'Generation failed. You can retry from the Forge panel.' : state.error}</div>
          </div>
        )}

        {/* Empty state */}
        {!state && (
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
            padding: 40, color: 'var(--text-muted)', textAlign: 'center',
          }}>
            <div style={{ fontSize: 40, marginBottom: 16 }}>✨</div>
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>{en ? 'Create your resident' : '开始创建你的居民'}</div>
            <div style={{ fontSize: 12, lineHeight: 1.7 }}>
              {en ? <>Answer the Forge AI in the left panel.<br />The resident’s three Skill layers will appear here as they are created.</> : <>在左侧对话面板<br />回答炼化师的问题<br />居民的三层 Skill 将在这里实时生成</>}
            </div>
          </div>
        )}
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
