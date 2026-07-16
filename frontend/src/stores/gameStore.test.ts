import { describe, it, expect, beforeEach } from 'vitest'
import { useGameStore } from './gameStore'

const testUser = {
  id: 'u1',
  name: 'Jimmy',
  email: 'j@example.com',
  avatar: null,
  soul_coin_balance: 100,
}

function resetStore() {
  localStorage.clear()
  useGameStore.setState({
    user: null,
    token: null,
    notifications: [],
    unreadCount: 0,
    chatOpen: false,
    chatResident: null,
    chatTarget: null,
    playerChatMessages: [],
    onlinePlayers: new Map(),
    wsStatus: 'connected',
    achievementToast: null,
    pendingEncounter: null,
  })
}

describe('auth slice', () => {
  beforeEach(resetStore)

  it('setAuth persists token+user to localStorage and state', () => {
    useGameStore.getState().setAuth(testUser, 'tok-123')
    expect(useGameStore.getState().token).toBe('tok-123')
    expect(useGameStore.getState().user?.name).toBe('Jimmy')
    expect(localStorage.getItem('token')).toBe('tok-123')
    expect(JSON.parse(localStorage.getItem('user')!)).toMatchObject({ id: 'u1' })
  })

  it('logout clears auth and ephemeral gameplay UI', () => {
    useGameStore.getState().setAuth(testUser, 'tok-123')
    useGameStore.setState({
      wsStatus: 'reconnecting',
      achievementToast: { code: 'first', title: 'First Visit', reward_sc: 5 },
      pendingEncounter: { resident_slug: 'mei', resident_name: '梅', location_id: 'square', opener: '你好' },
    })
    useGameStore.getState().logout()
    expect(useGameStore.getState()).toMatchObject({
      user: null,
      token: null,
      wsStatus: 'connected',
      achievementToast: null,
      pendingEncounter: null,
    })
    expect(localStorage.getItem('token')).toBeNull()
    expect(localStorage.getItem('user')).toBeNull()
  })

  it('updateBalance only touches balance and is a no-op when logged out', () => {
    useGameStore.getState().updateBalance(999)
    expect(useGameStore.getState().user).toBeNull()

    useGameStore.getState().setAuth(testUser, 'tok')
    useGameStore.getState().updateBalance(42)
    const u = useGameStore.getState().user!
    expect(u.soul_coin_balance).toBe(42)
    expect(u.name).toBe('Jimmy')
  })
})

describe('chat slice', () => {
  beforeEach(resetStore)

  it('openChat/closeChat toggle drawer and resident', () => {
    useGameStore.getState().openChat({ slug: 'klaus', name: '克劳斯', role: 'blacksmith' })
    expect(useGameStore.getState().chatOpen).toBe(true)
    expect(useGameStore.getState().chatResident?.slug).toBe('klaus')

    useGameStore.getState().closeChat()
    expect(useGameStore.getState().chatOpen).toBe(false)
    expect(useGameStore.getState().chatResident).toBeNull()
    expect(useGameStore.getState().chatTarget).toBeNull()
  })

  it('setChatTarget(npc) mirrors resident; (player) clears resident and message log', () => {
    useGameStore.getState().addPlayerChatMessage({ from: 'x', text: 'old', isAuto: false, timestamp: 1 })
    useGameStore.getState().setChatTarget({ type: 'npc', slug: 'klaus', name: '克劳斯', role: 'smith' })
    expect(useGameStore.getState().chatResident?.slug).toBe('klaus')
    // npc target must NOT wipe the player-chat log
    expect(useGameStore.getState().playerChatMessages).toHaveLength(1)

    useGameStore.getState().setChatTarget({ type: 'player', userId: 'u2', name: 'Bob' })
    expect(useGameStore.getState().chatResident).toBeNull()
    expect(useGameStore.getState().playerChatMessages).toHaveLength(0)
    expect(useGameStore.getState().chatOpen).toBe(true)
  })
})

describe('notifications slice', () => {
  beforeEach(resetStore)

  it('addNotification prepends and bumps unread', () => {
    const s = useGameStore.getState()
    s.setNotifications(
      [{ id: 'a', kind: 'k', title: 't', body: 'b', payload: {}, read: true, created_at: null }],
      0,
    )
    useGameStore.getState().addNotification({
      id: 'b', kind: 'k', title: 'new', body: 'b', payload: {}, read: false, created_at: null,
    })
    expect(useGameStore.getState().notifications[0].id).toBe('b')
    expect(useGameStore.getState().unreadCount).toBe(1)
  })
})

describe('online players slice', () => {
  beforeEach(resetStore)

  it('set/remove/clear keep the Map immutable per update', () => {
    const p = { player_id: 'p1', name: 'A', x: 0, y: 0, direction: 'down' }
    useGameStore.getState().setOnlinePlayer(p)
    const first = useGameStore.getState().onlinePlayers
    expect(first.get('p1')?.name).toBe('A')

    useGameStore.getState().setOnlinePlayer({ ...p, player_id: 'p2' })
    // immutability: a new Map instance per update (zustand shallow-compare relies on it)
    expect(useGameStore.getState().onlinePlayers).not.toBe(first)

    useGameStore.getState().removeOnlinePlayer('p1')
    expect(useGameStore.getState().onlinePlayers.has('p1')).toBe(false)
    useGameStore.getState().clearOnlinePlayers()
    expect(useGameStore.getState().onlinePlayers.size).toBe(0)
  })
})
