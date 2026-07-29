import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { NpcTooltip } from './NpcTooltip'
import { bridge } from '../game/phaserBridge'
import { useGameStore } from '../stores/gameStore'
import type { ResidentData } from '../game/GameScene'

// NpcTooltip fetches the resident's active life goal on every 'npc:nearby'
// event; stub it out (resolves to "no goal") so tests stay focused on the
// role/sbti text and don't depend on network state.
vi.mock('../services/api', () => ({
  getResidentGoals: vi.fn().mockResolvedValue({ active: null, resolved: [] }),
  getMe: vi.fn(),
  investInGoal: vi.fn(),
}))

// NpcTooltip only needs the plain STATUS_CONFIG data, but it imports it from
// StatusVisuals.ts, which also pulls in Phaser (canvas feature detection —
// unavailable under jsdom without the native `canvas` package). Swap in the
// dependency-free source module (statusConfig.ts is explicitly documented as
// "no Phaser import" for exactly this reason).
vi.mock('../game/StatusVisuals', async () => {
  const actual = await vi.importActual<typeof import('../game/statusConfig')>('../game/statusConfig')
  return { STATUS_CONFIG: actual.STATUS_CONFIG }
})

function makeNpc(metaJson: ResidentData['meta_json']): ResidentData {
  return {
    id: 'resident-1',
    slug: 'su-xiaoman',
    name: '苏小满',
    status: 'idle',
    sprite_key: 'student_01',
    tile_x: 0,
    tile_y: 0,
    district: 'academy',
    meta_json: metaJson,
    token_cost_per_turn: 1,
    star_rating: 0,
    heat: 0,
  }
}

beforeEach(() => {
  useGameStore.setState({ chatOpen: false })
})

afterEach(() => {
  cleanup()
})

// E2E-10: role text and the SBTI badge used to render adjacent with no real
// text separator between them (only a 4px CSS margin on the badge), so
// role="学生" + sbti.type="SEXY" rendered/serialized as "学生SEXY".
describe('NpcTooltip role/sbti separator', () => {
  it('inserts a visible separator between role and sbti badge when both are present', async () => {
    render(<NpcTooltip />)
    act(() => {
      bridge.emit('npc:nearby', makeNpc({ role: '学生', sbti: { type: 'SEXY', type_name: '尤物' } }))
    })

    const badge = await screen.findByText('SEXY')
    const roleRow = badge.closest('div')!
    expect(roleRow.textContent).toBe('学生 · SEXY')
    expect(roleRow.textContent).not.toContain('学生SEXY')
  })

  it('shows only the role, with no stray separator, when sbti is absent', async () => {
    render(<NpcTooltip />)
    act(() => {
      bridge.emit('npc:nearby', makeNpc({ role: '学生' }))
    })

    const roleRow = await screen.findByText('学生')
    expect(roleRow.textContent).toBe('学生')
  })

  it('renders without crashing when both role and sbti are missing', async () => {
    render(<NpcTooltip />)
    act(() => {
      bridge.emit('npc:nearby', makeNpc({}))
    })

    const nameEl = await screen.findByText('苏小满')
    const roleRow = nameEl.parentElement!.children[1] as HTMLElement
    expect(roleRow.textContent).toBe('')
  })
})
