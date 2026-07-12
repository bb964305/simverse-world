import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { bridge } from '../game/phaserBridge'
import { useGameStore } from '../stores/gameStore'
import { sendWS, onWSMessage, sendPlayerChat } from '../services/ws'
import { ttsSpeak, API_BASE } from '../services/api'
import type { ResidentData } from '../game/GameScene'
import { RatingPopup } from './RatingPopup'

interface Message {
  role: 'user' | 'npc'
  sender: string
  text: string
}

// TTS (E5): a single module-level audio element — starting a new playback
// always stops the previous one, so two clips never overlap.
let ttsAudio: HTMLAudioElement | null = null

function stopTtsAudio(): void {
  if (ttsAudio) {
    ttsAudio.pause()
    ttsAudio.src = ''
    ttsAudio = null
  }
}

// Create + register + play in module scope so the component never reassigns
// the module-level element during render/handlers (react-hooks/globals).
function playTtsAudio(url: string, handlers: { onEnded: () => void; onError: () => void }): Promise<void> {
  stopTtsAudio()
  const audio = new Audio(url)
  ttsAudio = audio
  audio.onended = handlers.onEnded
  audio.onerror = handlers.onError
  return audio.play()
}

// Per-message TTS button state, keyed by message index.
type TtsState = { idx: number; status: 'loading' | 'playing' | 'quota' | 'error' } | null

export function ChatDrawer() {
  const {
    chatOpen,
    chatResident,
    chatTarget,
    playerChatMessages,
    openChat,
    closeChat,
    clearChatTarget,
    setInputFocused,
  } = useGameStore()

  // NPC chat local state
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [streamingText, setStreamingText] = useState('')
  const [isThinking, setIsThinking] = useState(false)
  const [pendingRating, setPendingRating] = useState<{ conversationId: string; residentName: string } | null>(null)
  const streamingRef = useRef('')
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // Player chat local input state
  const [playerInput, setPlayerInput] = useState('')
  // Wake/queue state
  const [wakePrompt, setWakePrompt] = useState<{ slug: string; name: string; cost: number } | null>(null)
  const [queueInfo, setQueueInfo] = useState<{ slug: string; name: string; position: number } | null>(null)

  const isPlayerChat = chatTarget?.type === 'player'

  // Track whether we have an active conversation (for close logic)
  const [hasActiveConv, setHasActiveConv] = useState(false)

  // TTS button state (E5) — only one clip at a time, so a single slot suffices.
  const [tts, setTts] = useState<TtsState>(null)

  // Stop any playing TTS when the drawer closes (external-system sync only;
  // the button state is reset in the conversation-reset callbacks below,
  // which also clear `messages` and thus the indexes tts points at).
  useEffect(() => {
    if (!chatOpen) stopTtsAudio()
  }, [chatOpen])

  const handleTts = async (idx: number, text: string) => {
    // Clicking the currently-playing message stops it.
    if (tts?.idx === idx && tts.status === 'playing') {
      stopTtsAudio()
      setTts(null)
      return
    }
    if (tts?.status === 'loading') return
    const slug = useGameStore.getState().chatResident?.slug
    if (!slug) return
    stopTtsAudio()
    setTts({ idx, status: 'loading' })
    try {
      const resp = await ttsSpeak(slug, text.slice(0, 300))
      await playTtsAudio(`${API_BASE}${resp.url}`, {
        onEnded: () => setTts((cur) => (cur?.idx === idx && cur.status === 'playing' ? null : cur)),
        onError: () => setTts((cur) => (cur?.idx === idx ? { idx, status: 'error' } : cur)),
      })
      setTts({ idx, status: 'playing' })
    } catch (err) {
      const msg = err instanceof Error ? err.message : ''
      // apiFetch throws "API 429: ..." when the daily quota is exhausted.
      setTts({ idx, status: msg.includes('API 429') ? 'quota' : 'error' })
    }
  }

  // Listen for NPC interact events from Phaser
  useEffect(() => {
    return bridge.on('npc:interact', (data: unknown) => {
      const npc = data as ResidentData
      if (npc.status === 'sleeping' || npc.status === 'chatting') {
        // Don't open chat yet — send start_chat and wait for backend response
        // Backend will reply with wake_required or chat_queued
        sendWS({ type: 'start_chat', resident_slug: npc.slug })
      } else {
        // Normal idle/popular NPC — open chat immediately
        openChat({ slug: npc.slug, name: npc.name, role: npc.meta_json?.role ?? '' })
        sendWS({ type: 'start_chat', resident_slug: npc.slug })
        setMessages([])
        setStreamingText('')
        streamingRef.current = ''
        setTts(null)
        setHasActiveConv(true)
      }
    })
  }, [openChat])

  // Listen for WebSocket messages
  useEffect(() => {
    return onWSMessage((data) => {
      if (data.type === 'chat_started') {
        // Backend confirmed chat — open drawer if not already open (wake/queue flow)
        const slug = data.resident_slug as string
        if (!useGameStore.getState().chatOpen) {
          openChat({ slug, name: slug, role: '' })
          setMessages([])
          setStreamingText('')
          streamingRef.current = ''
          setTts(null)
        }
        setHasActiveConv(true)
      } else if (data.type === 'chat_reply') {
        setIsThinking(false)
        if (data.done === true) {
          const finalText = streamingRef.current
          if (finalText) {
            setMessages((prev) => [
              ...prev,
              { role: 'npc', sender: useGameStore.getState().chatResident?.name ?? '', text: finalText },
            ])
          }
          setStreamingText('')
          setIsThinking(false)
          streamingRef.current = ''
        } else if (typeof data.text === 'string') {
          setIsThinking(false)
          streamingRef.current += data.text
          setStreamingText(streamingRef.current)
        }
      } else if (data.type === 'chat_ended') {
        setIsThinking(false)
        setHasActiveConv(false)
        const convId = data.conversation_id as string | undefined
        const resident = useGameStore.getState().chatResident
        if (convId && resident) {
          setPendingRating({ conversationId: convId, residentName: resident.name })
        }
      } else if (data.type === 'wake_required') {
        setWakePrompt({
          slug: data.resident_slug as string,
          name: data.resident_name as string,
          cost: data.cost as number,
        })
      } else if (data.type === 'chat_queued') {
        setQueueInfo({
          slug: data.resident_slug as string,
          name: data.resident_name as string,
          position: data.position as number,
        })
      } else if (data.type === 'queue_ready') {
        setQueueInfo(null)
        const slug = data.resident_slug as string
        const name = data.resident_name as string
        openChat({ slug, name, role: '' })
        sendWS({ type: 'start_chat', resident_slug: slug })
        setMessages([])
        setStreamingText('')
        streamingRef.current = ''
        setTts(null)
        setHasActiveConv(true)
      }
    })
    // openChat is a zustand store action — its identity is stable for the
    // store's lifetime, so this effect still runs exactly once per mount.
  }, [openChat])

  const send = () => {
    const text = input.trim()
    if (!text || !chatResident) return
    setMessages((prev) => [...prev, { role: 'user', sender: '你', text }])
    sendWS({ type: 'chat_msg', text })
    setInput('')
    setIsThinking(true)
  }

  const sendPlayer = () => {
    const text = playerInput.trim()
    if (!text || chatTarget?.type !== 'player') return
    useGameStore.getState().addPlayerChatMessage({
      from: '你',
      text,
      isAuto: false,
      timestamp: Date.now(),
    })
    sendPlayerChat(chatTarget.userId, text)
    setPlayerInput('')
  }

  const close = () => {
    if (isPlayerChat) {
      clearChatTarget()
    } else if (hasActiveConv) {
      sendWS({ type: 'end_chat' })
      // Fallback: close drawer after 2 seconds if chat_ended never arrives
      setTimeout(() => {
        if (useGameStore.getState().chatOpen && !pendingRating) {
          closeChat()
        }
      }, 2000)
    } else {
      closeChat()
    }
  }

  const handleRate = (rating: number) => {
    if (pendingRating) {
      sendWS({ type: 'rate_chat', rating, conversation_id: pendingRating.conversationId })
    }
    setPendingRating(null)
    closeChat()
  }

  const handleSkipRating = () => {
    setPendingRating(null)
    closeChat()
  }

  // Scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingText, playerChatMessages])

  // Escape key to close. `close` is recreated every render (it captures local
  // state), so listing it as a dep would re-subscribe the listener on every
  // render; useEffectEvent keeps the subscription keyed on chatOpen only while
  // the handler always sees the latest close logic.
  const closeOnEscape = useEffectEvent(close)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape' && chatOpen) closeOnEscape() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [chatOpen])

  // Header display info
  const headerName = isPlayerChat
    ? chatTarget.name
    : (chatResident?.name ?? '')
  const headerSub = isPlayerChat
    ? '在线玩家'
    : (chatResident?.role ?? '')
  const headerIcon = isPlayerChat ? '🧑‍🤝‍🧑' : '🧑‍💻'

  return (<>
    <div style={{
      position: 'fixed', top: 48, right: 0, bottom: 0, width: 380,
      background: 'var(--bg-card)', borderLeft: '1px solid var(--border)',
      display: 'flex', flexDirection: 'column', zIndex: 20,
      transform: chatOpen ? 'translateX(0)' : 'translateX(100%)',
      transition: 'transform 0.3s ease',
    }}>
      {/* Header */}
      <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{ width: 40, height: 40, background: 'var(--bg-input)', borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 22 }}>{headerIcon}</div>
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 700, fontSize: 14 }}>{headerName}</div>
          <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>{headerSub}</div>
        </div>
        <button onClick={close} style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: 18, cursor: 'pointer', padding: '4px 8px', borderRadius: 6 }}>✕</button>
      </div>

      {isPlayerChat ? (
        <>
          {/* Player Chat Messages */}
          <div style={{ flex: 1, overflowY: 'auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
            {playerChatMessages.map((m, i) => {
              const isSelf = m.from === '你'
              return (
                <div key={i} style={{
                  maxWidth: '85%', padding: '10px 14px', borderRadius: 12, fontSize: 13, lineHeight: 1.6,
                  ...(isSelf
                    ? { background: 'var(--accent-red)', color: 'white', alignSelf: 'flex-end', borderBottomRightRadius: 4 }
                    : { background: 'var(--bg-input)', color: '#d4d4d8', alignSelf: 'flex-start', borderBottomLeftRadius: 4 }),
                }}>
                  {!isSelf && (
                    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                      {m.from}
                      {m.isAuto && (
                        <span style={{
                          fontSize: 10, background: 'rgba(139,92,246,0.3)', color: '#c4b5fd',
                          padding: '1px 6px', borderRadius: 4, fontWeight: 600,
                        }}>AI 代答</span>
                      )}
                    </div>
                  )}
                  {m.text}
                </div>
              )
            })}
            <div ref={messagesEndRef} />
          </div>

          {/* Player Chat Input */}
          <div style={{ padding: '12px 16px', borderTop: '1px solid var(--border)', display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              value={playerInput}
              onChange={(e) => setPlayerInput(e.target.value)}
              onFocus={() => setInputFocused(true)}
              onBlur={() => setInputFocused(false)}
              onKeyDown={(e) => { e.stopPropagation(); if (e.key === 'Enter') sendPlayer() }}
              placeholder="发消息给玩家..."
              style={{
                flex: 1, background: 'var(--bg-input)', border: '1px solid var(--border)',
                color: 'var(--text-primary)', padding: '10px 14px', borderRadius: 'var(--radius)',
                fontSize: 13, outline: 'none',
              }}
            />
            <button onClick={sendPlayer} style={{
              background: 'var(--accent-red)', color: 'white', border: 'none',
              padding: '10px 16px', borderRadius: 'var(--radius)', fontSize: 13, fontWeight: 700, cursor: 'pointer',
            }}>发送</button>
          </div>
        </>
      ) : (
        <>
          {/* NPC Messages */}
          <div style={{ flex: 1, overflowY: 'auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
            {messages.map((m, i) => (
              <div key={i} style={{
                maxWidth: '85%', padding: '10px 14px', borderRadius: 12, fontSize: 13, lineHeight: 1.6,
                ...(m.role === 'user'
                  ? { background: 'var(--accent-red)', color: 'white', alignSelf: 'flex-end', borderBottomRightRadius: 4 }
                  : { background: 'var(--bg-input)', color: '#d4d4d8', alignSelf: 'flex-start', borderBottomLeftRadius: 4 }),
              }}>
                {m.role === 'npc' && (
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span>{m.sender}</span>
                    {/* TTS (E5): 🔊 idle / ⏳ loading / 🔈 playing (click stops) / ⚠️ error */}
                    <button
                      onClick={() => void handleTts(i, m.text)}
                      disabled={tts?.idx === i && tts.status === 'loading'}
                      title={
                        tts?.idx === i && tts.status === 'error' ? '播放失败，请稍后重试'
                          : tts?.idx === i && tts.status === 'playing' ? '停止朗读'
                          : '朗读'
                      }
                      style={{
                        background: 'none', border: 'none', padding: 0, fontSize: 11,
                        lineHeight: 1, color: 'var(--text-muted)',
                        cursor: tts?.idx === i && tts.status === 'loading' ? 'wait' : 'pointer',
                      }}
                      onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.color = 'var(--accent-red)' }}
                      onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-muted)' }}
                    >
                      {tts?.idx === i && tts.status === 'loading' ? '⏳'
                        : tts?.idx === i && tts.status === 'playing' ? '🔈'
                        : tts?.idx === i && tts.status === 'error' ? '⚠️'
                        : '🔊'}
                    </button>
                    {tts?.idx === i && tts.status === 'quota' && (
                      <span style={{ fontSize: 10, color: 'var(--accent-red)' }}>今日配额已用完</span>
                    )}
                  </div>
                )}
                {m.text}
              </div>
            ))}
            {isThinking && !streamingText && (
              <div style={{ maxWidth: '85%', padding: '10px 14px', borderRadius: 12, fontSize: 13, lineHeight: 1.6, background: 'var(--bg-input)', color: '#d4d4d8', alignSelf: 'flex-start', borderBottomLeftRadius: 4 }}>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>{chatResident?.name ?? ''}</div>
                <span style={{ color: 'var(--text-muted)', fontStyle: 'italic' }}>思考中<span className="thinking-dots">...</span></span>
              </div>
            )}
            {streamingText && (
              <div style={{ maxWidth: '85%', padding: '10px 14px', borderRadius: 12, fontSize: 13, lineHeight: 1.6, background: 'var(--bg-input)', color: '#d4d4d8', alignSelf: 'flex-start', borderBottomLeftRadius: 4 }}>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>{chatResident?.name ?? ''}</div>
                {streamingText}<span style={{ opacity: 0.5 }}>▌</span>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* NPC Input */}
          <div style={{ padding: '12px 16px', borderTop: '1px solid var(--border)', display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onFocus={() => setInputFocused(true)}
              onBlur={() => setInputFocused(false)}
              onKeyDown={(e) => { e.stopPropagation(); if (e.key === 'Enter') send() }}
              placeholder="输入消息..."
              style={{
                flex: 1, background: 'var(--bg-input)', border: '1px solid var(--border)',
                color: 'var(--text-primary)', padding: '10px 14px', borderRadius: 'var(--radius)',
                fontSize: 13, outline: 'none',
              }}
            />
            <button onClick={send} style={{
              background: 'var(--accent-red)', color: 'white', border: 'none',
              padding: '10px 16px', borderRadius: 'var(--radius)', fontSize: 13, fontWeight: 700, cursor: 'pointer',
            }}>发送</button>
            <span style={{ color: 'var(--text-muted)', fontSize: 11, whiteSpace: 'nowrap' }}>1🪙</span>
          </div>
          {pendingRating && (
            <RatingPopup
              residentName={pendingRating.residentName}
              conversationId={pendingRating.conversationId}
              onRate={handleRate}
              onSkip={handleSkipRating}
            />
          )}
        </>
      )}

    </div>

    {/* Wake confirmation popup — rendered outside sliding drawer */}
    {wakePrompt && (
      <div style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50,
      }}>
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border)',
          borderRadius: 12, padding: 24, maxWidth: 280, textAlign: 'center',
        }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>💤</div>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 8 }}>{wakePrompt.name} 正在沉睡</div>
          <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 16 }}>
            花费 <span style={{ color: '#fbbf24', fontWeight: 700 }}>{wakePrompt.cost} 🪙</span> 唤醒并开始对话？
          </div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
            <button
              onClick={() => {
                sendWS({ type: 'start_chat', resident_slug: wakePrompt.slug, wake: true })
                setWakePrompt(null)
              }}
              style={{
                background: '#fbbf24', color: '#18181b', border: 'none',
                padding: '8px 20px', borderRadius: 8, fontSize: 13, fontWeight: 700, cursor: 'pointer',
              }}
            >唤醒</button>
            <button
              onClick={() => { setWakePrompt(null) }}
              style={{
                background: 'var(--bg-input)', color: 'var(--text-muted)', border: '1px solid var(--border)',
                padding: '8px 20px', borderRadius: 8, fontSize: 13, cursor: 'pointer',
              }}
            >取消</button>
          </div>
        </div>
      </div>
    )}

    {/* Queue status toast — rendered outside sliding drawer */}
    {queueInfo && (
      <div style={{
        position: 'fixed', bottom: 20, right: 20, zIndex: 50,
        background: 'var(--bg-card)', border: '1px solid var(--border)',
        borderRadius: 12, padding: '14px 20px', boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
        display: 'flex', alignItems: 'center', gap: 12, minWidth: 240,
      }}>
        <div style={{ fontSize: 24 }}>⏳</div>
        <div>
          <div style={{ fontWeight: 700, fontSize: 13 }}>排队等候 {queueInfo.name}</div>
          <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
            当前排位：第 {queueInfo.position} 位
          </div>
        </div>
        <button
          onClick={() => {
            sendWS({ type: 'cancel_queue', resident_slug: queueInfo.slug })
            setQueueInfo(null)
          }}
          style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            fontSize: 16, cursor: 'pointer', marginLeft: 'auto', padding: '4px 8px',
          }}
        >✕</button>
      </div>
    )}
  </>
  )
}
