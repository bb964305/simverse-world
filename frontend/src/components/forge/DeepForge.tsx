import { useState, useRef, useEffect, useCallback } from 'react'
import { deepForgeStart, deepForgeStatus, apiFetch } from '../../services/api'
import type { DeepForgeStage, DeepForgeStatusResponse } from '../../services/api'
import { useGameStore } from '../../stores/gameStore'
import { onWSMessage } from '../../services/ws'
import { useLocale } from '../../services/locale'
import {
  DEEP_FORGE_TERMINAL_RECOVERY_MESSAGE,
  isForgeRecoveryAbort,
  pollTerminalStatus,
  recoverTerminalStatus,
} from './terminalRecovery'

interface DeepForgeProps {
  onStateUpdate?: (state: DeepForgeStatusResponse) => void
  onComplete?: (residentId: string) => void
}

interface StageInfo {
  key: DeepForgeStage
  zh: string
  en: string
  icon: string
}

const STAGES: StageInfo[] = [
  { key: 'routing', zh: '路由中', en: 'Routing', icon: '🔀' },
  { key: 'researching', zh: '调研中', en: 'Researching', icon: '🔍' },
  { key: 'extracting', zh: '提取中', en: 'Extracting', icon: '⚗️' },
  { key: 'building', zh: '构建中', en: 'Building', icon: '🏗️' },
  { key: 'validating', zh: '验证中', en: 'Validating', icon: '✅' },
  { key: 'refining', zh: '精炼中', en: 'Refining', icon: '💎' },
  { key: 'done', zh: '完成', en: 'Complete', icon: '🎉' },
]

type UIStatus = 'idle' | 'running' | 'done' | 'error'

function getStageIndex(stage: DeepForgeStage): number {
  return STAGES.findIndex((s) => s.key === stage)
}

export function DeepForge({ onStateUpdate, onComplete }: DeepForgeProps) {
  const en = useLocale((state) => state.locale === 'en')
  const [characterName, setCharacterName] = useState('')
  const [userMaterial, setUserMaterial] = useState('')
  const [uiStatus, setUiStatus] = useState<UIStatus>('idle')
  const [currentStage, setCurrentStage] = useState<DeepForgeStage | null>(null)
  const [result, setResult] = useState<DeepForgeStatusResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const unsubRef = useRef<(() => void) | null>(null)
  const forgeIdRef = useRef<string | null>(null)
  const recoveryAbortRef = useRef<AbortController | null>(null)
  const completionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const terminalSettledRef = useRef(false)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      unsubRef.current?.()
      unsubRef.current = null
      recoveryAbortRef.current?.abort()
      recoveryAbortRef.current = null
      if (completionTimerRef.current !== null) {
        clearTimeout(completionTimerRef.current)
        completionTimerRef.current = null
      }
    }
  }, [])

  // WS keeps the stage indicator responsive. Durable bounded polling is the
  // convergence path when a terminal frame is lost; a terminal frame also starts
  // a short retry sequence so one cross-worker 404/network race cannot strand the
  // UI. Both paths share one AbortController and a 20-minute safety deadline.
  const subscribeStatus = useCallback((forgeId: string) => {
    const token = sessionStorage.getItem('token') ?? ''
    const recoveryMessage = en
      ? 'The final deep-distillation result could not be confirmed. Refresh and check your resident list.'
      : DEEP_FORGE_TERMINAL_RECOVERY_MESSAGE
    let recoveryInFlight: Promise<void> | null = null
    let wsFallbackError: string | null = null

    terminalSettledRef.current = false
    recoveryAbortRef.current?.abort()
    unsubRef.current?.()
    const sessionController = new AbortController()
    recoveryAbortRef.current = sessionController

    const fetchStatus = (signal: AbortSignal) => deepForgeStatus(token, forgeId, signal)
    const stopWatching = () => {
      unsubRef.current?.()
      unsubRef.current = null
    }

    const applyPending = (status: DeepForgeStatusResponse) => {
      if (!mountedRef.current || terminalSettledRef.current) return
      onStateUpdate?.(status)
      setCurrentStage(status.stage ?? status.status)
    }

    const applyStatus = (status: DeepForgeStatusResponse): boolean => {
      if (!mountedRef.current || terminalSettledRef.current) return false
      applyPending(status)
      const stage = status.stage ?? status.status

      if (stage === 'done') {
        terminalSettledRef.current = true
        sessionController.abort()
        stopWatching()
        setUiStatus('done')
        setCurrentStage('done')
        setResult(status)
        // Refresh balance (forge reward was added server-side).
        const gameToken = useGameStore.getState().token
        if (gameToken) {
          apiFetch<{ soul_coin_balance: number }>('/users/me', {
            headers: { Authorization: `Bearer ${gameToken}` },
          }).then((user) => {
            useGameStore.getState().updateBalance(user.soul_coin_balance)
          }).catch(() => {})
        }
        if (status.resident_id) {
          completionTimerRef.current = setTimeout(() => {
            completionTimerRef.current = null
            if (mountedRef.current) onComplete?.(status.resident_id!)
          }, 300)
        }
        return true
      }

      if (stage === 'error') {
        terminalSettledRef.current = true
        sessionController.abort()
        stopWatching()
        setUiStatus('error')
        setCurrentStage('error')
        setError(en ? 'Deep distillation failed. Your source material was not deleted.' : (status.error ?? wsFallbackError ?? '深度蒸馏过程中出现错误'))
        return true
      }
      return false
    }

    const showRecoveryFailure = () => {
      if (!mountedRef.current || terminalSettledRef.current) return
      terminalSettledRef.current = true
      sessionController.abort()
      stopWatching()
      setUiStatus('error')
      setError(en ? recoveryMessage : (wsFallbackError ?? recoveryMessage))
    }

    const recoverTerminal = () => {
      if (terminalSettledRef.current || recoveryInFlight !== null) return
      recoveryInFlight = (async () => {
        try {
          applyStatus(await recoverTerminalStatus(
            fetchStatus,
            sessionController.signal,
            recoveryMessage,
          ))
        } catch (recoveryError) {
          if (!isForgeRecoveryAbort(recoveryError)) showRecoveryFailure()
        } finally {
          recoveryInFlight = null
        }
      })()
    }

    unsubRef.current = onWSMessage((data) => {
      if (data.forge_id !== forgeId) return
      if (data.type === 'forge_progress') {
        // status field mirrors the DeepForge STAGES keys (researching, …).
        if (typeof data.status === 'string') setCurrentStage(data.status as DeepForgeStage)
      } else if (data.type === 'forge_done' || data.type === 'forge_error') {
        if (data.type === 'forge_error' && typeof data.error === 'string') {
          wsFallbackError = data.error
        }
        recoverTerminal()
      }
    })

    void pollTerminalStatus(
      fetchStatus,
      sessionController.signal,
      applyPending,
      recoveryMessage,
    ).then(applyStatus).catch((pollError: unknown) => {
      if (!isForgeRecoveryAbort(pollError)) showRecoveryFailure()
    })
  }, [onStateUpdate, onComplete, en])

  const handleStart = async () => {
    if (!characterName.trim()) return
    setError(null)
    setUiStatus('running')
    setCurrentStage('routing')

    const token = sessionStorage.getItem('token') ?? ''

    try {
      const resp = await deepForgeStart(token, {
        character_name: characterName.trim(),
        user_material: userMaterial.trim() || undefined,
      })
      if (!mountedRef.current) return
      forgeIdRef.current = resp.forge_id
      subscribeStatus(resp.forge_id)
    } catch (e) {
      if (!mountedRef.current) return
      setUiStatus('error')
      setError(en ? 'Request failed. Check your connection and try again.' : (e instanceof Error ? e.message : '请求失败，请重试'))
    }
  }

  const handleRetry = () => {
    unsubRef.current?.(); unsubRef.current = null
    recoveryAbortRef.current?.abort(); recoveryAbortRef.current = null
    terminalSettledRef.current = false
    if (completionTimerRef.current !== null) {
      clearTimeout(completionTimerRef.current)
      completionTimerRef.current = null
    }
    setUiStatus('idle')
    setCurrentStage(null)
    setResult(null)
    setError(null)
    forgeIdRef.current = null
  }

  const activeStageIdx = currentStage ? getStageIndex(currentStage) : -1

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
        <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 2 }}>🧪 {en ? 'Deep distillation' : '深度蒸馏'}</div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
          {en ? 'Multi-stage AI pipeline · research + extraction + refinement' : '多阶段 AI 管线 · 全自动调研 + 萃取 + 精炼'}
        </div>
      </div>

      {/* Scrollable content */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '20px' }}>

        {/* Input form — hidden once running */}
        {uiStatus === 'idle' && (
          <>
            <div style={{ marginBottom: 16 }}>
              <label style={{
                fontSize: 12, color: 'var(--text-muted)', display: 'block',
                marginBottom: 6, fontWeight: 600, letterSpacing: '0.3px',
              }}>
                {en ? 'Character name *' : '角色名称 *'}
              </label>
              <input
                value={characterName}
                onChange={(e) => setCharacterName(e.target.value)}
                placeholder={en ? 'For example: Ada Lovelace / Nikola Tesla' : '例如：埃隆·马斯克 / 诸葛亮 / 特斯拉'}
                style={{
                  width: '100%', boxSizing: 'border-box',
                  background: 'var(--bg-input)', border: '1px solid var(--border)',
                  color: 'var(--text-primary)', padding: '10px 14px',
                  borderRadius: 'var(--radius)', fontSize: 14, outline: 'none',
                }}
              />
            </div>

            <div style={{ marginBottom: 16 }}>
              <label style={{
                fontSize: 12, color: 'var(--text-muted)', display: 'block',
                marginBottom: 6, fontWeight: 600, letterSpacing: '0.3px',
              }}>
                {en ? 'Source material' : '补充素材'}
                <span style={{ fontWeight: 400, marginLeft: 6, opacity: 0.7 }}>{en ? '(optional)' : '（可选）'}</span>
              </label>
              <textarea
                value={userMaterial}
                onChange={(e) => setUserMaterial(e.target.value)}
                placeholder={en
                  ? 'Paste source material about this character, such as:\n\n• Biography or encyclopedia summary\n• Interviews or quotations\n• Autobiography or letters\n• Commentary from others\n\nLeave blank to let the system research automatically.'
                  : '粘贴任何关于此角色的文字材料，例如：\n\n• 人物传记 / 维基百科摘要\n• 采访内容 / 语录集\n• 自传或书信\n• 别人对他/她的评价\n\n留空时系统将自动联网调研。'}
                rows={10}
                style={{
                  width: '100%', boxSizing: 'border-box',
                  background: 'var(--bg-input)', border: '1px solid var(--border)',
                  color: 'var(--text-primary)', padding: '12px 14px',
                  borderRadius: 'var(--radius)', fontSize: 13, outline: 'none',
                  resize: 'vertical', lineHeight: 1.7, fontFamily: 'inherit',
                }}
              />
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4, textAlign: 'right' }}>
                {userMaterial.length} {en ? 'characters' : '字'}
              </div>
            </div>

            {/* Info box */}
            <div style={{
              background: '#8b5cf610', border: '1px solid #8b5cf630',
              borderRadius: 8, padding: '12px 14px', marginBottom: 4,
              fontSize: 12, color: '#a78bfa', lineHeight: 1.7,
            }}>
              {en
                ? <>🧪 <strong>Deep distillation</strong> researches, extracts, validates, and refines a three-layer Skill profile. Estimated time: <strong>60–120 seconds</strong>.</>
                : <>🧪 <strong>深度蒸馏</strong>比快速炼化更彻底：系统将逐步调研、提取、验证并精炼角色的三层 Skill。预计耗时 <strong>60–120 秒</strong>。</>}
            </div>
          </>
        )}

        {/* Running: stage indicator */}
        {(uiStatus === 'running' || uiStatus === 'done') && (
          <div style={{ marginBottom: 20 }}>
            {/* Character name display */}
            <div style={{
              fontSize: 14, fontWeight: 700, marginBottom: 16,
              color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: 8,
            }}>
              <span style={{
                width: 28, height: 28, background: 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
                borderRadius: 6, display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 12, color: 'white', fontWeight: 700, flexShrink: 0,
              }}>{en ? 'AI' : '深'}</span>
              {en ? `Distilling: ${characterName}` : `正在蒸馏：${characterName}`}
            </div>

            {/* Stage list */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {STAGES.map((s, idx) => {
                const isActive = idx === activeStageIdx && uiStatus === 'running'
                const isDone = idx < activeStageIdx || (uiStatus === 'done' && s.key !== 'error')
                const isWaiting = idx > activeStageIdx

                return (
                  <div
                    key={s.key}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 12,
                      padding: '10px 14px', borderRadius: 8,
                      border: `1px solid ${isActive ? '#8b5cf680' : 'var(--border)'}`,
                      background: isActive ? '#8b5cf610' : isDone ? '#53d76908' : 'var(--bg-input)',
                      transition: 'all 0.3s ease',
                    }}
                  >
                    {/* Status icon */}
                    <div style={{ width: 20, flexShrink: 0, textAlign: 'center' }}>
                      {isActive ? (
                        <span style={{
                          display: 'inline-block',
                          width: 14, height: 14,
                          border: '2px solid #8b5cf6', borderTopColor: 'transparent',
                          borderRadius: '50%',
                          animation: 'deepSpin 0.8s linear infinite',
                        }} />
                      ) : isDone ? (
                        <span style={{ color: 'var(--accent-green)', fontSize: 13 }}>✓</span>
                      ) : (
                        <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>○</span>
                      )}
                    </div>

                    {/* Stage emoji */}
                    <span style={{ fontSize: 16, flexShrink: 0 }}>{s.icon}</span>

                    {/* Label */}
                    <span style={{
                      flex: 1, fontSize: 13, fontWeight: isActive ? 700 : 500,
                      color: isActive
                        ? '#a78bfa'
                        : isDone
                          ? 'var(--text-secondary)'
                          : isWaiting
                            ? 'var(--text-muted)'
                            : 'var(--text-primary)',
                    }}>
                      {en ? s.en : s.zh}
                    </span>

                    {/* Status badge */}
                    <span style={{
                      fontSize: 10, padding: '2px 8px', borderRadius: 4,
                      background: isActive
                        ? '#8b5cf620'
                        : isDone
                          ? '#53d76920'
                          : 'transparent',
                      color: isActive ? '#a78bfa' : isDone ? 'var(--accent-green)' : 'transparent',
                      fontWeight: 600,
                    }}>
                      {isActive ? (en ? 'RUNNING' : '进行中') : isDone ? (en ? 'DONE' : '完成') : ''}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Error state */}
        {uiStatus === 'error' && (
          <div style={{
            background: '#e9456015', border: '1px solid #e9456040',
            borderRadius: 10, padding: '16px 18px', marginBottom: 16,
          }}>
            <div style={{ fontWeight: 700, color: 'var(--accent-red)', fontSize: 14, marginBottom: 6 }}>
              {en ? 'Deep distillation failed' : '深度蒸馏失败'}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
              {error}
            </div>
          </div>
        )}

        {/* Done success */}
        {uiStatus === 'done' && result && (
          <div style={{
            background: '#53d76915', border: '1px solid #53d76940',
            borderRadius: 10, padding: '16px 18px', marginTop: 4,
          }}>
            <div style={{ fontWeight: 700, color: 'var(--accent-green)', fontSize: 15, marginBottom: 6 }}>
              {en ? 'Distillation complete!' : '蒸馏完成！'}
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
              <strong>{result.name}</strong> {en ? 'has moved into ' : '已入住'}{' '}
              {({ engineering: en ? 'Engineering District' : '工程街区', product: en ? 'Product District' : '产品街区', academy: en ? 'Academy District' : '学院区', free: en ? 'Free District' : '自由区' })[result.district] ?? result.district}
              <br />
              {en ? 'Quality rating: ' : '质量评级：'}{'⭐'.repeat(result.star_rating)}
              <br />
              {en ? 'You received ' : '你获得了 '}<strong style={{ color: 'var(--accent-green)' }}>50 SC</strong>{en ? ' in offchain gameplay credits.' : ' 链下游戏积分奖励！'}
            </div>
            <button
              onClick={() => onComplete?.(result.resident_id ?? '')}
              style={{
                marginTop: 12, background: 'var(--accent-green)', color: '#000',
                border: 'none', padding: '10px 20px', borderRadius: 'var(--radius)',
                fontSize: 13, fontWeight: 700, cursor: 'pointer', width: '100%',
              }}
            >
              {en ? 'View the new resident in town →' : '前往城市查看新居民 →'}
            </button>
          </div>
        )}
      </div>

      {/* Action bar */}
      {uiStatus !== 'done' && (
        <div style={{ padding: '14px 20px', borderTop: '1px solid var(--border)', flexShrink: 0 }}>
          {uiStatus === 'error' ? (
            <button
              onClick={handleRetry}
              style={{
                width: '100%', background: 'var(--accent-red)', color: 'white',
                border: 'none', padding: '12px 20px', borderRadius: 'var(--radius)',
                fontSize: 14, fontWeight: 700, cursor: 'pointer',
              }}
            >
              {en ? 'Retry' : '重试'}
            </button>
          ) : uiStatus === 'running' ? (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '10px 14px', background: '#8b5cf610',
              borderRadius: 'var(--radius)', border: '1px solid #8b5cf630',
            }}>
              <span style={{
                width: 16, height: 16,
                border: '2px solid #8b5cf6', borderTopColor: 'transparent',
                borderRadius: '50%', display: 'inline-block', flexShrink: 0,
                animation: 'deepSpin 0.8s linear infinite',
              }} />
              <span style={{ fontSize: 13, color: '#a78bfa', fontWeight: 600 }}>
                {en ? 'Deep distillation is running. Please keep this tab open…' : '深度蒸馏进行中，请耐心等待…'}
              </span>
            </div>
          ) : (
            <button
              onClick={() => void handleStart()}
              disabled={!characterName.trim()}
              style={{
                width: '100%',
                background: !characterName.trim() ? 'var(--bg-input)' : 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
                color: !characterName.trim() ? 'var(--text-muted)' : 'white',
                border: 'none', padding: '12px 20px', borderRadius: 'var(--radius)',
                fontSize: 14, fontWeight: 700,
                cursor: !characterName.trim() ? 'not-allowed' : 'pointer',
                transition: 'opacity 0.2s',
              }}
            >
              🧪 {en ? 'Start deep distillation' : '开始深度蒸馏'}
            </button>
          )}
        </div>
      )}

      <style>{`@keyframes deepSpin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
