import { useState, useCallback, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { TopNav } from '../components/TopNav'
import { ForgeChat } from '../components/forge/ForgeChat'
import { ForgePreview } from '../components/forge/ForgePreview'
import { QuickForge } from '../components/forge/QuickForge'
import { DeepForge } from '../components/forge/DeepForge'
import { connectWS } from '../services/ws'
import { useIsMobile } from '../hooks/useIsMobile'
import type { ForgeStatusResponse, DeepForgeStatusResponse } from '../services/api'

type Mode = 'guided' | 'quick' | 'deep'
// Mobile only: which half of the (now stacked) split is visible. Both halves
// stay mounted (toggled via CSS display, see below) so switching back to
// "edit" doesn't reset in-progress ForgeChat/QuickForge/DeepForge state.
type MobilePane = 'edit' | 'preview'

export function ForgePage() {
  const navigate = useNavigate()
  const [forgeState, setForgeState] = useState<ForgeStatusResponse | null>(null)
  const [mode, setMode] = useState<Mode>('guided')
  const isMobile = useIsMobile()
  const [mobilePane, setMobilePane] = useState<MobilePane>('edit')

  // Forge progress now arrives over WS (P1-5). GamePage owns the socket, but
  // navigating to /forge unmounts GamePage and tears it down — so re-establish
  // it here. connectWS is idempotent; we intentionally do not disconnect on
  // unmount so the connection persists across authenticated routes.
  useEffect(() => {
    connectWS()
  }, [])

  const handleStateUpdate = useCallback((state: ForgeStatusResponse | DeepForgeStatusResponse) => {
    // ForgePreview uses ForgeStatusResponse shape; DeepForgeStatusResponse is compatible for preview
    setForgeState(state as ForgeStatusResponse)
  }, [])

  const handleComplete = useCallback(() => {
    navigate('/play')
  }, [navigate])

  return (
    <div style={{
      height: '100vh',
      background: 'var(--bg-page)',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
    }}>
      <TopNav />

      {/* Breadcrumb + mode switcher */}
      <div style={{
        marginTop: 'var(--nav-height)',
        padding: '10px 24px',
        borderBottom: '1px solid var(--border)',
        fontSize: 13,
        color: 'var(--text-muted)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexShrink: 0,
        flexWrap: isMobile ? 'wrap' : 'nowrap',
        gap: isMobile ? 8 : 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ cursor: 'pointer', color: 'var(--accent-blue)' }} onClick={() => navigate('/play')}>
            Simverse World
          </span>
          <span>/</span>
          <span style={{ color: 'var(--text-primary)' }}>炼化新居民</span>
        </div>

        {/* Mode tabs — on mobile the row can't fit three labels at once, so
            it scrolls horizontally instead of squeezing/wrapping button text. */}
        <div style={{
          display: 'flex',
          background: 'var(--bg-input)',
          borderRadius: 8,
          padding: 3,
          gap: 2,
          overflowX: isMobile ? 'auto' : 'visible',
          maxWidth: isMobile ? '100%' : undefined,
        }}>
          {([
            { key: 'guided', label: '📝 引导式炼化', desc: '5步问答引导' },
            { key: 'quick',  label: '⚡ 快速炼化',   desc: '粘贴文本即提取' },
            { key: 'deep',   label: '🧪 深度蒸馏',   desc: '多阶段 AI 管线' },
          ] as { key: Mode; label: string; desc: string }[]).map((m) => (
            <button
              key={m.key}
              onClick={() => { setMode(m.key); setForgeState(null) }}
              title={m.desc}
              style={{
                padding: '5px 14px',
                borderRadius: 6,
                border: 'none',
                cursor: 'pointer',
                fontSize: 12,
                fontWeight: 600,
                background: mode === m.key
                  ? m.key === 'deep'
                    ? 'linear-gradient(135deg, #8b5cf6, #6d28d9)'
                    : 'var(--accent-red)'
                  : 'transparent',
                color: mode === m.key ? 'white' : 'var(--text-muted)',
                transition: 'all 0.15s',
                whiteSpace: isMobile ? 'nowrap' : undefined,
                flexShrink: isMobile ? 0 : undefined,
              }}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {/* Mobile-only edit/preview toggle — the split below becomes a stack,
          so pick which half is visible instead of showing both squeezed. */}
      {isMobile && (
        <div style={{
          display: 'flex',
          borderBottom: '1px solid var(--border)',
          flexShrink: 0,
        }}>
          {([
            { key: 'edit', label: '✏️ 编辑' },
            { key: 'preview', label: '👁️ 预览' },
          ] as { key: MobilePane; label: string }[]).map((p) => (
            <button
              key={p.key}
              onClick={() => setMobilePane(p.key)}
              style={{
                flex: 1,
                padding: '10px 0',
                border: 'none',
                background: 'transparent',
                borderBottom: mobilePane === p.key ? '2px solid var(--accent-red)' : '2px solid transparent',
                color: mobilePane === p.key ? 'var(--text-primary)' : 'var(--text-muted)',
                fontWeight: mobilePane === p.key ? 700 : 400,
                fontSize: 13,
                cursor: 'pointer',
              }}
            >
              {p.label}
            </button>
          ))}
        </div>
      )}

      {/* Main split layout — fills remaining height, inner panels scroll.
          Below the mobile breakpoint this stacks instead of splitting
          left/right, and only the active `mobilePane` is shown (the other
          stays mounted with display:none so its in-progress state survives
          switching back and forth). */}
      <div data-testid="forge-split" style={{ flex: 1, display: 'flex', flexDirection: isMobile ? 'column' : 'row', minHeight: 0 }}>
        {/* Left panel (editor) */}
        <div
          data-testid="forge-edit-pane"
          style={{
            flex: 1,
            minWidth: 0,
            borderRight: isMobile ? 'none' : '1px solid var(--border)',
            display: isMobile ? (mobilePane === 'edit' ? 'flex' : 'none') : 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {mode === 'guided' && (
            <ForgeChat onStateUpdate={handleStateUpdate} onComplete={handleComplete} />
          )}
          {mode === 'quick' && (
            <QuickForge onStateUpdate={handleStateUpdate} onComplete={handleComplete} />
          )}
          {mode === 'deep' && (
            <DeepForge onStateUpdate={handleStateUpdate} onComplete={handleComplete} />
          )}
        </div>

        {/* Right preview */}
        <div
          data-testid="forge-preview-pane"
          style={{
            width: isMobile ? '100%' : 460,
            minWidth: isMobile ? 0 : 340,
            flexShrink: isMobile ? undefined : 0,
            flex: isMobile ? 1 : undefined,
            display: isMobile ? (mobilePane === 'preview' ? 'flex' : 'none') : 'flex',
            flexDirection: 'column',
            background: 'var(--bg-card)',
            overflow: 'hidden',
          }}
        >
          <ForgePreview state={forgeState} />
        </div>
      </div>
    </div>
  )
}
