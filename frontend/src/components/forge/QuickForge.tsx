import { useState, useRef, useEffect, useCallback } from 'react'
import { forgeQuick } from '../../services/api'
import type { ForgeStatusResponse } from '../../services/api'
import { onWSMessage } from '../../services/ws'
import {
  FORGE_TERMINAL_RECOVERY_MESSAGE,
  isForgeRecoveryAbort,
  pollForgeTerminalStatus,
  recoverForgeTerminalStatus,
} from './terminalRecovery'
import { useLocale } from '../../services/locale'

interface QuickForgeProps {
  onStateUpdate: (state: ForgeStatusResponse) => void
  onComplete: (residentId: string) => void
}

const GENERATION_STAGES = [
  '正在分析人物经历...',
  '提取能力层...',
  '构建人格模型...',
  '提炼灵魂内核...',
  '评估质量 & 分配街区...',
]

const GENERATION_STAGES_EN = [
  'Analyzing the person’s history…',
  'Extracting abilities…',
  'Building a personality model…',
  'Distilling the soul core…',
  'Assessing quality and assigning a district…',
]

const PLACEHOLDER = `在这里粘贴任何关于这个人的文字，例如：

• 个人简历 / LinkedIn 介绍
• 聊天记录片段
• 别人对他/她的评价
• 采访或文章摘录
• 你自己写的人物描述

系统会自动从文字中抽取：能力、人格、价值观。
文字越丰富，炼化结果越精准。`

const PLACEHOLDER_EN = `Paste any text about this person, such as:

• A résumé or LinkedIn introduction
• Chat excerpts
• Other people’s observations
• An interview or article
• Your own character description

The system extracts abilities, personality, and values. Richer source material produces a more faithful resident.`

export function QuickForge({ onStateUpdate, onComplete }: QuickForgeProps) {
  const locale = useLocale((state) => state.locale)
  const en = locale === 'en'
  const [name, setName] = useState('')
  const [rawText, setRawText] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [isDone, setIsDone] = useState(false)
  const [progress, setProgress] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ForgeStatusResponse | null>(null)

  const unsubRef = useRef<(() => void) | null>(null)
  const stageRef = useRef(0)
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

  // P1-5: WS-driven instead of an open-ended poll. forge_progress advances the
  // caption; one terminal signal starts bounded status convergence with a
  // visible failure state if every API attempt fails.
  const subscribeStatus = useCallback((forgeId: string) => {
    stageRef.current = 0
    let recoveryInFlight: Promise<void> | null = null
    let wsFallbackError: string | null = null

    terminalSettledRef.current = false
    recoveryAbortRef.current?.abort()
    unsubRef.current?.()
    const sessionController = new AbortController()
    recoveryAbortRef.current = sessionController

    const stopWatching = () => {
      unsubRef.current?.()
      unsubRef.current = null
    }

    const applyStatus = (status: ForgeStatusResponse): boolean => {
      if (!mountedRef.current || terminalSettledRef.current) return false
      onStateUpdate(status)
      if (status.status === 'done') {
        terminalSettledRef.current = true
        recoveryAbortRef.current?.abort()
        stopWatching()
        setIsGenerating(false)
        setIsDone(true)
        setProgress('')
        setResult(status)
        if (status.resident_id) {
          completionTimerRef.current = setTimeout(() => {
            completionTimerRef.current = null
            if (mountedRef.current) onComplete(status.resident_id!)
          }, 300)
        }
        return true
      }
      if (status.status === 'error') {
        terminalSettledRef.current = true
        recoveryAbortRef.current?.abort()
        stopWatching()
        setIsGenerating(false)
        setProgress('')
          setError(status.error ?? (en ? 'Generation failed. Please try again.' : '生成失败，请重试'))
        return true
      }
      return false
    }

    const showRecoveryFailure = () => {
      if (!mountedRef.current || terminalSettledRef.current) return
      terminalSettledRef.current = true
      sessionController.abort()
      stopWatching()
      setIsGenerating(false)
      setProgress('')
      setError(wsFallbackError ?? (en ? 'The final result could not be recovered. Please try again.' : FORGE_TERMINAL_RECOVERY_MESSAGE))
    }

    const recoverTerminal = () => {
      if (terminalSettledRef.current || recoveryInFlight !== null) return
      recoveryInFlight = (async () => {
        try {
          applyStatus(await recoverForgeTerminalStatus(forgeId, sessionController.signal))
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
        const stages = en ? GENERATION_STAGES_EN : GENERATION_STAGES
        if (stageRef.current < stages.length) {
          setProgress(stages[stageRef.current])
          stageRef.current++
        }
      } else if (data.type === 'forge_done' || data.type === 'forge_error') {
        if (data.type === 'forge_error' && typeof data.error === 'string') {
          wsFallbackError = data.error
        }
        recoverTerminal()
      }
    })

    void pollForgeTerminalStatus(
      forgeId,
      sessionController.signal,
      (status) => {
        if (mountedRef.current && !terminalSettledRef.current) onStateUpdate(status)
      },
    ).then(applyStatus).catch((pollError: unknown) => {
      if (!isForgeRecoveryAbort(pollError)) showRecoveryFailure()
    })
  }, [en, onStateUpdate, onComplete])

  const handleSubmit = async () => {
    if (!name.trim() || !rawText.trim()) {
      setError(en ? 'Enter a name and source text' : '请填写姓名和文字内容')
      return
    }
    setError(null)
    setIsGenerating(true)
    setProgress(en ? 'Calling the AI extraction pipeline (about 20–60 seconds)…' : '正在调用 AI 提取（约 20-60 秒）…')

    try {
      // Submit — returns immediately with forge_id + "generating"
      const resp = await forgeQuick(name.trim(), rawText.trim())
      const forgeId = resp.forge_id
      if (!forgeId) throw new Error('No forge_id returned')

      // Listen for WS progress/completion
      subscribeStatus(forgeId)
    } catch (e) {
      setIsGenerating(false)
      setError(e instanceof Error ? e.message : (en ? 'Request failed' : '请求失败'))
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        padding: '14px 20px',
        borderBottom: '1px solid var(--border)',
        flexShrink: 0,
      }}>
        <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 2 }}>⚡ {en ? 'Quick extraction' : '快速提取'}</div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
          {en ? 'Paste source text → automatically distill a three-layer Skill' : '粘贴任意文字 → 自动提炼三层 Skill'}
        </div>
      </div>

      {/* Scrollable form area */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '20px' }}>

        {/* Name */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'block', marginBottom: 6, fontWeight: 600 }}>
            {en ? 'Resident name' : '居民姓名'} *
          </label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={en ? 'Example: Ada Lovelace / an original character' : '例如：张三 / 埃隆·马斯克 / 诸葛亮'}
            disabled={isGenerating || isDone}
            style={{
              width: '100%', boxSizing: 'border-box',
              background: 'var(--bg-input)', border: '1px solid var(--border)',
              color: 'var(--text-primary)', padding: '10px 14px',
              borderRadius: 'var(--radius)', fontSize: 14, outline: 'none',
            }}
          />
        </div>

        {/* Raw text */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'block', marginBottom: 6, fontWeight: 600 }}>
            {en ? 'Source material' : '人物文字材料'} *
          </label>
          <textarea
            value={rawText}
            onChange={(e) => setRawText(e.target.value)}
            placeholder={en ? PLACEHOLDER_EN : PLACEHOLDER}
            disabled={isGenerating || isDone}
            rows={14}
            style={{
              width: '100%', boxSizing: 'border-box',
              background: 'var(--bg-input)', border: '1px solid var(--border)',
              color: 'var(--text-primary)', padding: '12px 14px',
              borderRadius: 'var(--radius)', fontSize: 13, outline: 'none',
              resize: 'vertical', lineHeight: 1.7,
              fontFamily: 'inherit',
            }}
          />
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4, textAlign: 'right' }}>
            {rawText.length} {en ? 'characters' : '字'}
          </div>
        </div>

        {/* Tips */}
        {!isGenerating && !isDone && (
          <div style={{
            background: '#0ea5e910', border: '1px solid #0ea5e930',
            borderRadius: 8, padding: '12px 14px', marginBottom: 16,
            fontSize: 12, color: '#0ea5e9', lineHeight: 1.7,
          }}>
            💡 <strong>{en ? 'Tip:' : '提示：'}</strong>{en
              ? ' Richer material yields better results. 100+ characters is useful; 500+ is ideal. Chinese and English are supported.'
              : '文字越丰富，结果越好。100字以上效果明显，500字以上效果极佳。中英文都支持。'}
          </div>
        )}

        {/* Error */}
        {error && (
          <div style={{
            background: '#e9456015', border: '1px solid #e9456030',
            borderRadius: 8, padding: '10px 14px', marginBottom: 16,
            fontSize: 12, color: 'var(--accent-red)',
          }}>
            {error}
          </div>
        )}

        {/* Generating progress */}
        {isGenerating && (
          <div style={{
            background: 'var(--bg-input)', border: '1px solid var(--border)',
            borderRadius: 10, padding: '16px 18px',
            display: 'flex', alignItems: 'center', gap: 12,
          }}>
            <span style={{
              width: 18, height: 18,
              border: '2px solid var(--accent-red)', borderTopColor: 'transparent',
              borderRadius: '50%', display: 'inline-block', flexShrink: 0,
              animation: 'spin 0.8s linear infinite',
            }} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>{en ? 'Forging resident…' : '正在炼化中…'}</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>{progress}</div>
            </div>
          </div>
        )}

        {/* Done */}
        {isDone && result && (
          <div style={{
            background: '#53d76915', border: '1px solid #53d76940',
            borderRadius: 10, padding: '16px 18px',
          }}>
            <div style={{ fontWeight: 700, color: 'var(--accent-green)', fontSize: 15, marginBottom: 6 }}>
              ✅ {en ? 'Forge complete!' : '炼化完成！'}
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
              <strong>{result.name}</strong> {en ? 'has moved into' : '已入住'}{' '}
              {(en
                ? ({ engineering: 'Engineering District', product: 'Product District', academy: 'Academy District', free: 'Free District' })
                : ({ engineering: '工程街区', product: '产品街区', academy: '学院区', free: '自由区' }))[result.district] ?? result.district}
              <br />
              {en ? 'Quality rating' : '质量评级'}: {'⭐'.repeat(result.star_rating)}
              <br />
              {en ? 'You earned' : '你获得了'} <strong style={{ color: 'var(--accent-green)' }}>50 SC</strong> {en ? 'in offchain game credits.' : '链下游戏积分奖励！'}
            </div>
            <button
              onClick={() => onComplete(result.resident_id ?? '')}
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

      {/* Submit button — fixed at bottom */}
      {!isDone && (
        <div style={{
          padding: '14px 20px',
          borderTop: '1px solid var(--border)',
          flexShrink: 0,
        }}>
          <button
            onClick={() => void handleSubmit()}
            disabled={isGenerating || !name.trim() || !rawText.trim()}
            style={{
              width: '100%',
              background: isGenerating || !name.trim() || !rawText.trim()
                ? 'var(--bg-input)' : 'var(--accent-red)',
              color: isGenerating || !name.trim() || !rawText.trim()
                ? 'var(--text-muted)' : 'white',
              border: 'none', padding: '12px 20px',
              borderRadius: 'var(--radius)', fontSize: 14,
              fontWeight: 700, cursor: isGenerating ? 'not-allowed' : 'pointer',
              transition: 'background 0.2s',
            }}
          >
            {isGenerating ? (en ? 'Forging…' : '炼化中…') : (en ? '⚡ Extract Skill now' : '⚡ 立即提取 Skill')}
          </button>
        </div>
      )}

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
