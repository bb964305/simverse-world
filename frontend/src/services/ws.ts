import { useGameStore } from '../stores/gameStore'

let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
const wsListeners = new Set<(data: Record<string, unknown>) => void>()
// Queue important messages that arrive before any listener is registered (e.g. daily_reward)
const earlyMessageQueue: Record<string, unknown>[] = []
const QUEUED_TYPES = new Set(['daily_reward', 'coin_earned'])

export function connectWS(): void {
  const token = useGameStore.getState().token
  if (!token || socket?.readyState === WebSocket.OPEN) return

  const API_WS = (import.meta.env.VITE_API_URL ?? 'http://localhost:8000').replace(/^http/, 'ws')
  // Token goes in the first message, not the URL, so it never lands in access logs (P0-4c)
  socket = new WebSocket(`${API_WS}/ws`)

  socket.onopen = () => {
    socket?.send(JSON.stringify({ type: 'auth', token }))
  }

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data as string) as Record<string, unknown>
      if (data.type === 'coin_update' && typeof data.balance === 'number') {
        useGameStore.getState().updateBalance(data.balance)
      }
      if (data.type === 'daily_reward' && typeof data.new_balance === 'number') {
        useGameStore.getState().updateBalance(data.new_balance)
      }
      // Handle online players
      if (data.type === 'player_moved') {
        useGameStore.getState().setOnlinePlayer({
          player_id: data.player_id as string,
          name: (data.name as string) || '?',
          x: data.x as number,
          y: data.y as number,
          direction: (data.direction as string) || 'down',
        })
      }
      if (data.type === 'player_joined') {
        useGameStore.getState().setOnlinePlayer({
          player_id: data.player_id as string,
          name: (data.name as string) || '?',
          x: (data.x as number) ?? 0,
          y: (data.y as number) ?? 0,
          direction: (data.direction as string) || 'down',
        })
      }
      if (data.type === 'player_left') {
        useGameStore.getState().removeOnlinePlayer(data.player_id as string)
      }
      if (data.type === 'spawn_position') {
        useGameStore.getState().setSpawnPosition(data.x as number, data.y as number)
      }
      // Rate-limited: server rejected a chat_msg before any DB/LLM cost.
      // Surface to the user via listeners (chat panel can show a notice);
      // also log so it's observable without a listener (P1-1 limit).
      if (data.type === 'rate_limited') {
        console.warn('rate_limited:', data.message ?? '请求过快，请稍后再试')
      }
      // Budget exceeded: per-user daily LLM allowance spent (P1-1, E-24).
      // No charge or reply happened; surface a friendly notice.
      if (data.type === 'budget_exceeded') {
        console.warn('budget_exceeded:', data.message ?? '今日对话额度已用完，明天再来吧')
      }
      // Player-to-player chat: reply from the target player (or auto-reply)
      if (data.type === 'player_chat_reply') {
        useGameStore.getState().addPlayerChatMessage({
          from: (data.from_name as string) || '对方',
          text: (data.text as string) || '',
          isAuto: (data.is_auto as boolean) ?? false,
          timestamp: typeof data.timestamp === 'number' ? data.timestamp : Date.now(),
        })
      }
      // Incoming player_chat: another player messaged you
      if (data.type === 'player_chat') {
        useGameStore.getState().addPlayerChatMessage({
          from: (data.from_name as string) || '对方',
          text: (data.text as string) || '',
          isAuto: false,
          timestamp: typeof data.timestamp === 'number' ? data.timestamp : Date.now(),
        })
      }
      if (data.type === 'online_players') {
        const players = data.players as Array<Record<string, unknown>>
        players.forEach((p) => useGameStore.getState().setOnlinePlayer({
          player_id: p.player_id as string,
          name: (p.name as string) || '?',
          x: p.x as number,
          y: p.y as number,
          direction: (p.direction as string) || 'down',
        }))
      }
      // Notification (S4): push a live notification into the store; the bell
      // badge reads unreadCount and the drawer reads the list.
      if (data.type === 'notification' && typeof data.id === 'string') {
        useGameStore.getState().addNotification({
          id: data.id as string,
          kind: (data.kind as string) || 'system',
          title: (data.title as string) || '',
          body: (data.body as string) || '',
          payload: (data.payload as Record<string, unknown>) || {},
          read: false,
          created_at: (data.created_at as string) ?? null,
        })
      }

      // Village digest ready (A5): light up the newspaper icon's red dot.
      if (data.type === 'digest_ready') {
        useGameStore.getState().setDigestUnread(true)
      }

      // Location encounter (B2): surface a card the player can accept.
      if (data.type === 'encounter_prompt' && typeof data.resident_slug === 'string') {
        useGameStore.getState().setPendingEncounter({
          resident_slug: data.resident_slug as string,
          resident_name: (data.resident_name as string) || (data.resident_slug as string),
          location_id: (data.location_id as string) || '',
          opener: (data.opener as string) || '',
        })
      }

      // Achievement unlocked (D1): pop a celebratory toast. The durable copy
      // also arrives as an S4 notification (bell), so no store list needed here.
      if (data.type === 'achievement_unlocked' && typeof data.code === 'string') {
        useGameStore.getState().showAchievementToast({
          code: data.code as string,
          title: (data.title as string) || '成就解锁',
          reward_sc: (data.reward_sc as number) || 0,
        })
      }

      // Forge progress (P1-5): forge_progress / forge_done / forge_error are
      // consumed by the Forge components (DeepForge/QuickForge/ForgeChat) via
      // onWSMessage below — no store state to update here, they flow through as
      // listener events, replacing the old setInterval status polling.

      // If no listeners registered yet, queue important messages for replay
      if (wsListeners.size === 0 && QUEUED_TYPES.has(data.type as string)) {
        earlyMessageQueue.push(data)
      } else {
        wsListeners.forEach((cb) => cb(data))
      }
    } catch {
      // ignore malformed messages
    }
  }

  socket.onclose = () => {
    socket = null
    useGameStore.getState().clearOnlinePlayers()
    reconnectTimer = setTimeout(connectWS, 3000)
  }

  socket.onerror = () => {
    socket?.close()
  }
}

export function onWSMessage(cb: (data: Record<string, unknown>) => void): () => void {
  wsListeners.add(cb)
  // Replay any messages that arrived before this listener was registered
  if (earlyMessageQueue.length > 0) {
    const queued = [...earlyMessageQueue]
    earlyMessageQueue.length = 0
    queued.forEach((msg) => cb(msg))
  }
  return () => wsListeners.delete(cb)
}

export function sendWS(data: Record<string, unknown>): void {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(data))
  }
}

export function disconnectWS(): void {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  socket?.close()
  socket = null
}

let lastSentX = -1
let lastSentY = -1

export function sendPosition(x: number, y: number, direction: string): void {
  // Only send if moved more than 4px
  if (Math.abs(x - lastSentX) < 4 && Math.abs(y - lastSentY) < 4) return
  lastSentX = x
  lastSentY = y
  sendWS({ type: 'move', x: Math.round(x), y: Math.round(y), direction })
}

export function sendPlayerChat(targetId: string, text: string): void {
  sendWS({ type: 'player_chat', target_id: targetId, text })
}
