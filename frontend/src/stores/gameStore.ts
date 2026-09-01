import { create } from 'zustand'

interface User {
  id: string
  name: string
  email: string
  avatar: string | null
  soul_coin_balance: number
  is_admin?: boolean
  lab_enabled?: boolean
  wallet_address?: string | null
}

export interface OnlinePlayer {
  player_id: string
  name: string
  x: number
  y: number
  direction: string
  agent_controlled?: boolean
}

export type ChatTarget =
  | { type: 'npc'; slug: string; name: string; role: string }
  | { type: 'player'; userId: string; name: string }

export interface PlayerChatMessage {
  from: string
  text: string
  isAuto: boolean
  timestamp: number
}

/**
 * WS connection state for the UI. 'reconnecting' drives the ConnectionBanner;
 * 'connected' means "no banner" (covers both an open socket and the idle state
 * after a deliberate disconnectWS, e.g. logout/unmount).
 */
export type WsStatus = 'connected' | 'reconnecting'

export interface NotificationItem {
  id: string
  kind: string
  title: string
  body: string
  payload: Record<string, unknown>
  read: boolean
  created_at: string | null
}

interface GameState {
  user: User | null
  token: string | null
  wsStatus: WsStatus
  setWsStatus: (status: WsStatus) => void
  notifications: NotificationItem[]
  unreadCount: number
  playerSpriteKey: string
  chatOpen: boolean
  chatResident: { slug: string; name: string; role: string } | null
  chatTarget: ChatTarget | null
  playerChatMessages: PlayerChatMessage[]
  inputFocused: boolean
  profileTab: 'residents' | 'creator' | 'conversations' | 'transactions' | 'achievements' | 'feed' | 'recap' | 'codex' | 'settings'
  onlinePlayers: Map<string, OnlinePlayer>
  spawnX: number
  spawnY: number
  minimapTextureUrl: string | null
  playerTileX: number
  playerTileY: number
  cameraViewport: { x: number; y: number; w: number; h: number } | null
  setMinimapTexture: (url: string) => void
  setPlayerTile: (x: number, y: number) => void
  setCameraViewport: (vp: { x: number; y: number; w: number; h: number }) => void

  setAuth: (user: User, token: string) => void
  logout: () => void
  setPlayerSpriteKey: (key: string) => void
  openChat: (resident: { slug: string; name: string; role: string }) => void
  closeChat: () => void
  setChatTarget: (target: ChatTarget) => void
  clearChatTarget: () => void
  addPlayerChatMessage: (msg: PlayerChatMessage) => void
  setInputFocused: (v: boolean) => void
  updateBalance: (balance: number) => void
  setProfileTab: (tab: 'residents' | 'creator' | 'conversations' | 'transactions' | 'achievements' | 'feed' | 'recap' | 'codex' | 'settings') => void
  setOnlinePlayer: (p: OnlinePlayer) => void
  removeOnlinePlayer: (id: string) => void
  clearOnlinePlayers: () => void
  setSpawnPosition: (x: number, y: number) => void
  setNotifications: (items: NotificationItem[], unread: number) => void
  addNotification: (item: NotificationItem) => void
  setUnreadCount: (n: number) => void
  achievementToast: { code: string; title: string; reward_sc: number } | null
  showAchievementToast: (t: { code: string; title: string; reward_sc: number }) => void
  clearAchievementToast: () => void
  digestUnread: boolean
  setDigestUnread: (v: boolean) => void
  pendingEncounter: { resident_slug: string; resident_name: string; location_id: string; opener: string } | null
  setPendingEncounter: (e: { resident_slug: string; resident_name: string; location_id: string; opener: string }) => void
  clearPendingEncounter: () => void
}

const DEFAULT_SPAWN_X = 76 * 32
const DEFAULT_SPAWN_Y = 50 * 32
const DEFAULT_PLAYER_TILE_X = 76
const DEFAULT_PLAYER_TILE_Y = 50

function createSessionState() {
  return {
    wsStatus: 'connected' as const,
    notifications: [] as NotificationItem[],
    unreadCount: 0,
    achievementToast: null,
    digestUnread: false,
    pendingEncounter: null,
    playerSpriteKey: '埃迪',
    chatOpen: false,
    chatResident: null,
    chatTarget: null,
    playerChatMessages: [] as PlayerChatMessage[],
    inputFocused: false,
    profileTab: 'residents' as const,
    onlinePlayers: new Map<string, OnlinePlayer>(),
    spawnX: DEFAULT_SPAWN_X,
    spawnY: DEFAULT_SPAWN_Y,
    minimapTextureUrl: null,
    playerTileX: DEFAULT_PLAYER_TILE_X,
    playerTileY: DEFAULT_PLAYER_TILE_Y,
    cameraViewport: null,
  }
}

// Keep bearer credentials out of persistent localStorage. Existing sessions
// are migrated once so an in-progress launch is not interrupted, then removed
// from durable storage. sessionStorage survives refreshes but not a closed tab.
function migrateLegacyAuth(): void {
  const legacyToken = localStorage.getItem('token')
  const legacyUser = localStorage.getItem('user')
  if (!sessionStorage.getItem('token') && legacyToken) sessionStorage.setItem('token', legacyToken)
  if (!sessionStorage.getItem('user') && legacyUser) sessionStorage.setItem('user', legacyUser)
  localStorage.removeItem('token')
  localStorage.removeItem('user')
}

migrateLegacyAuth()

export const useGameStore = create<GameState>((set) => ({
  user: (() => { try { return JSON.parse(sessionStorage.getItem('user') || 'null') } catch { return null } })(),
  token: sessionStorage.getItem('token'),
  ...createSessionState(),
  setWsStatus: (status) => set({ wsStatus: status }),
  setMinimapTexture: (url) => set({ minimapTextureUrl: url }),
  setPlayerTile: (x, y) => set({ playerTileX: x, playerTileY: y }),
  setCameraViewport: (vp) => set({ cameraViewport: vp }),

  setAuth: (user, token) => {
    sessionStorage.setItem('token', token)
    sessionStorage.setItem('user', JSON.stringify(user))
    set((state) => state.user?.id === user.id && state.token === token
      ? { user, token }
      : { ...createSessionState(), user, token })
  },
  logout: () => {
    sessionStorage.removeItem('token')
    sessionStorage.removeItem('user')
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    set({
      ...createSessionState(),
      user: null,
      token: null,
    })
  },
  setPlayerSpriteKey: (key) => set({ playerSpriteKey: key }),
  openChat: (resident) => set({
    chatOpen: true,
    chatResident: resident,
    chatTarget: null,
    playerChatMessages: [],
    inputFocused: false,
  }),
  closeChat: () => set({ chatOpen: false, chatResident: null, chatTarget: null, inputFocused: false }),
  setChatTarget: (target) => set({
    chatTarget: target,
    chatOpen: true,
    ...(target.type === 'player' ? { playerChatMessages: [] } : {}),
    ...(target.type === 'npc'
      ? { chatResident: { slug: target.slug, name: target.name, role: target.role } }
      : { chatResident: null }),
  }),
  clearChatTarget: () => set({ chatTarget: null, chatOpen: false, chatResident: null, inputFocused: false }),
  addPlayerChatMessage: (msg) => set((s) => ({ playerChatMessages: [...s.playerChatMessages, msg] })),
  setInputFocused: (v) => set({ inputFocused: v }),
  updateBalance: (balance) => set((s) => s.user ? { user: { ...s.user, soul_coin_balance: balance } } : {}),
  setNotifications: (items, unread) => set({ notifications: items, unreadCount: unread }),
  addNotification: (item) => set((s) => ({ notifications: [item, ...s.notifications], unreadCount: s.unreadCount + 1 })),
  setUnreadCount: (n) => set({ unreadCount: n }),
  showAchievementToast: (t) => set({ achievementToast: t }),
  clearAchievementToast: () => set({ achievementToast: null }),
  setDigestUnread: (v) => set({ digestUnread: v }),
  setPendingEncounter: (e) => set({ pendingEncounter: e }),
  clearPendingEncounter: () => set({ pendingEncounter: null }),
  setProfileTab: (tab) => set({ profileTab: tab }),
  setOnlinePlayer: (p) => set((s) => {
    const next = new Map(s.onlinePlayers)
    const current = next.get(p.player_id)
    const merged: OnlinePlayer = {
      ...current,
      ...p,
      ...(p.agent_controlled === undefined && current?.agent_controlled !== undefined
        ? { agent_controlled: current.agent_controlled }
        : {}),
    }
    next.set(p.player_id, merged)
    return { onlinePlayers: next }
  }),
  removeOnlinePlayer: (id) => set((s) => {
    const next = new Map(s.onlinePlayers)
    next.delete(id)
    return { onlinePlayers: next }
  }),
  clearOnlinePlayers: () => set({ onlinePlayers: new Map() }),
  setSpawnPosition: (x, y) => set({ spawnX: x, spawnY: y }),
}))
