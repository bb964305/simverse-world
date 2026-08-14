import { useGameStore } from '../stores/gameStore'
import { bridge } from '../game/phaserBridge'
import {
  INITIAL_CONVERGENCE, isNewerRevision, advanceConvergence, type WorldConvergence,
} from './worldRevision'
import {
  convergeCaravanState, refreshCaravanProjection, resetCaravanProjection,
} from './caravanProjection'

let socket: WebSocket | null = null
// Token the current module socket was built (and will authenticate) with.
// connectWS compares it against the store token so switching accounts in the
// same tab (setAuth) tears the old identity's socket down instead of silently
// riding it (F9 cross-account socket reuse).
let socketToken: string | null = null
// Highest applied world source_cursor, so a re-delivered world_changed cannot
// fire a duplicate convergence effect (Phase 9). A reconnect refetches world
// state through the normal 'world:changed' consumers, which converge forward.
let worldConvergence: WorldConvergence = INITIAL_CONVERGENCE
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let caravanStaleTimer: ReturnType<typeof setTimeout> | null = null
let caravanResyncTimer: ReturnType<typeof setInterval> | null = null
// Consecutive failed-connection counter driving the exponential backoff.
// Reset to 0 when the server confirms auth (auth_ok) — that is the only
// point where we know the connection is genuinely usable (a socket that
// opens but fails auth gets closed by the server and must keep backing off).
let reconnectAttempt = 0

const RECONNECT_BASE_MS = 3000
const RECONNECT_MAX_MS = 30000
export const CARAVAN_DISCONNECT_STALE_MS = 30000
export const CARAVAN_RESYNC_INTERVAL_MS = 30000

function clearCaravanStaleTimer(): void {
  if (caravanStaleTimer !== null) {
    clearTimeout(caravanStaleTimer)
    caravanStaleTimer = null
  }
}

function clearCaravanResyncTimer(): void {
  if (caravanResyncTimer !== null) {
    clearInterval(caravanResyncTimer)
    caravanResyncTimer = null
  }
}

function startCaravanResyncTimer(): void {
  clearCaravanResyncTimer()
  // Redis pub/sub is intentionally non-durable: an API subscriber can miss one
  // terminal frame while the browser WebSocket itself remains open. Keep one
  // bounded REST convergence request in flight via refreshCaravanProjection's
  // shared promise so a missed departed/cancelled frame self-heals.
  caravanResyncTimer = setInterval(() => {
    void refreshCaravanProjection().catch(() => { /* optional projection */ })
  }, CARAVAN_RESYNC_INTERVAL_MS)
}

/**
 * Exponential backoff delay for reconnect attempt N (0-based):
 * 3s · 2^N capped at 30s, with ±20% jitter so a fleet of clients dropped by
 * the same server restart doesn't reconnect in lockstep.
 * Exported for tests.
 */
export function computeBackoffDelay(attempt: number): number {
  const base = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS)
  const jitter = base * 0.2 * (Math.random() * 2 - 1)
  return Math.round(base + jitter)
}

const wsListeners = new Set<(data: Record<string, unknown>) => void>()
// Queue important messages that arrive before any listener is registered (e.g. daily_reward)
const earlyMessageQueue: Record<string, unknown>[] = []
const QUEUED_TYPES = new Set(['daily_reward', 'coin_earned'])

export function connectWS(): void {
  const token = useGameStore.getState().token
  if (!token) return
  // Bail if a socket is already OPEN *or* still CONNECTING — opening a second
  // socket during a race (e.g. React StrictMode double-mount / reconnect) makes
  // the module `socket` point at the still-connecting one, so the first
  // socket's onopen `send()` throws "Still in CONNECTING state" (verify-before-done).
  if (
    socket?.readyState === WebSocket.OPEN ||
    socket?.readyState === WebSocket.CONNECTING
  ) {
    if (socketToken === token) return
    // The live socket authenticated as a DIFFERENT identity (setAuth switched
    // accounts in this tab). Null the module `socket` before closing so its
    // onclose sees `socket !== ws` and skips the reconnect + banner, then fall
    // through to build a fresh socket with the current token.
    const stale = socket
    socket = null
    clearCaravanResyncTimer()
    stale.close()
  }

  const API_WS = (import.meta.env.VITE_API_URL ?? 'http://localhost:8000').replace(/^http/, 'ws')
  // Token goes in the first message, not the URL, so it never lands in access logs (P0-4c)
  // Capture the instance in `ws` so each handler acts on *its own* socket, not
  // whatever the module `socket` variable happens to point at when it fires.
  const ws = new WebSocket(`${API_WS}/ws`)
  socket = ws
  socketToken = token

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'auth', token }))
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data as string) as Record<string, unknown>
      // Server accepted our token — the connection is fully usable. Reset the
      // reconnect backoff and clear the "reconnecting" banner.
      if (data.type === 'auth_ok') {
        clearCaravanStaleTimer()
        reconnectAttempt = 0
        useGameStore.getState().setWsStatus('connected')
        // Initial connect and every reconnect converge through the durable REST
        // snapshot. The shared reducer prevents a slow, older GET from replacing
        // a newer caravan_state frame that arrived while it was in flight.
        void refreshCaravanProjection().catch(() => { /* optional projection */ })
        startCaravanResyncTimer()
      }
      if (data.type === 'caravan_state') convergeCaravanState(data)
      if (data.type === 'coin_update' && typeof data.balance === 'number') {
        useGameStore.getState().updateBalance(data.balance)
      }
      if (data.type === 'daily_reward' && typeof data.new_balance === 'number') {
        useGameStore.getState().updateBalance(data.new_balance)
      }
      // Lab: walking into the experiment building opens the ExperimentPanel
      // (self-mounted in TopNav, listens for the bridge event).
      if (data.type === 'experiment_prompt') {
        bridge.emit('experiment:open')
      }
      // Lab run/task live frames. `lab_task_update` and `lab_run_step` are
      // consumed by ExperimentPanel via the onWSMessage listener fan-out below
      // (same pattern as forge_progress). A pending sensitive-action approval
      // additionally surfaces the panel so the player can respond.
      if (data.type === 'lab_run_approval') {
        bridge.emit('experiment:open')
      }
      // World governance: an applied/reverted proposal changed the map. Minimap
      // /codex re-pull GET /world/locations on this signal (they listen for the
      // bridge event); the dynamic layer is runtime data, not a compile-time key.
      // Revision-aware convergence (Phase 9): only emit when the world_changed
      // source_cursor (seq) advances past what we've applied, so a re-delivered
      // or out-of-order event cannot trigger a duplicate convergence/refetch.
      if (data.type === 'world_changed') {
        const ev = { seq: Number(data.seq ?? 0), world_revision_id: (data.world_revision_id as string) ?? null }
        if (isNewerRevision(worldConvergence, ev)) {
          worldConvergence = advanceConvergence(worldConvergence, ev)
          bridge.emit('world:changed')
        }
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

      // Follow feed (E11): a followed resident did something notable. Merge it
      // into the bell (store notification, client-side id since there is no
      // durable row) and let the fan-out below deliver it to FeedList too.
      if (data.type === 'feed_event' && typeof data.resident_slug === 'string') {
        const payload = (data.payload as Record<string, unknown>) || {}
        const body = (typeof payload.title === 'string' && payload.title)
          || (typeof payload.text === 'string' && payload.text)
          || (data.kind as string)
          || ''
        useGameStore.getState().addNotification({
          id: `feed-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          kind: 'feed',
          title: '你关注的居民有新动态',
          body: `${data.resident_slug as string}：${body}`,
          payload,
          read: false,
          created_at: new Date().toISOString(),
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

  ws.onclose = () => {
    // Only tear down + schedule a reconnect if this is still the active socket.
    // A stale socket from a raced re-connect closing must not null out the live
    // one (which would leak the good socket and churn reconnects). This guard
    // also covers deliberate disconnects: disconnectWS() nulls the module
    // `socket` before close fires, so we neither reconnect nor show the banner.
    if (socket !== ws) return
    socket = null
    clearCaravanResyncTimer()
    useGameStore.getState().clearOnlinePlayers()
    // Passive drop: tell the UI and schedule a reconnect with exponential backoff.
    useGameStore.getState().setWsStatus('reconnecting')
    // Preserve short-drop continuity, but never leave an inbound/outbound or
    // trading projection visible forever while the client is offline. The
    // successful reconnect GET will restore the authoritative state.
    if (caravanStaleTimer === null) {
      caravanStaleTimer = setTimeout(() => {
        caravanStaleTimer = null
        resetCaravanProjection()
      }, CARAVAN_DISCONNECT_STALE_MS)
    }
    const delay = computeBackoffDelay(reconnectAttempt)
    reconnectAttempt += 1
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connectWS()
    }, delay)
  }

  ws.onerror = () => {
    ws.close()
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
  reconnectAttempt = 0
  clearCaravanStaleTimer()
  clearCaravanResyncTimer()
  // Deliberate disconnect (logout / page unmount): null the module `socket`
  // BEFORE closing so the socket's onclose sees `socket !== ws` and skips both
  // the reconnect and the 'reconnecting' status.
  const ws = socket
  socket = null
  socketToken = null
  ws?.close()
  resetCaravanProjection()
  // Hide the banner if a reconnect cycle was in flight when the user left.
  useGameStore.getState().setWsStatus('connected')
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
