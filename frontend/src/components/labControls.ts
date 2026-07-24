import type { CSSProperties } from 'react'

// Lab (player-facing ExperimentPanel) control styles, extracted from inline
// literals so touch-target sizing is enforced in one place and unit-tested.
// Every interactive control clears a 44px minimum (WCAG 2.5.5 / iOS HIG); the
// visual contract (accent colours, borders, disabled opacity) is unchanged.
// Scoped to the Lab panel — the shared global .game-dialog-close is left alone.

const ACCENT = '#14b8a6'
// Center content and give a real box so minHeight is honoured on <button>/<label>.
const TOUCH: CSSProperties = {
  minHeight: 44,
  display: 'inline-flex',
  alignItems: 'center',
  boxSizing: 'border-box',
}

export const labInput: CSSProperties = {
  width: '100%', background: '#0b0b0d', color: 'var(--text)',
  border: '1px solid var(--border)', borderRadius: 8, padding: '8px 10px',
  fontSize: 13, boxSizing: 'border-box', minHeight: 44,
}

export function labTab(active: boolean): CSSProperties {
  return {
    ...TOUCH,
    background: 'none', border: 'none', cursor: 'pointer', padding: '8px 10px',
    fontSize: 13, fontWeight: 600, color: active ? ACCENT : 'var(--text-muted)',
    borderBottom: `2px solid ${active ? ACCENT : 'transparent'}`,
  }
}

export function labChip(active: boolean): CSSProperties {
  return {
    ...TOUCH,
    fontSize: 12, padding: '4px 10px', borderRadius: 999, cursor: 'pointer',
    border: `1px solid ${active ? ACCENT : 'var(--border)'}`,
    color: active ? ACCENT : 'var(--text-muted)',
  }
}

export function labPublishBtn(busy: boolean): CSSProperties {
  return {
    ...TOUCH, justifyContent: 'center',
    background: ACCENT, color: '#04110f', border: 'none', borderRadius: 8,
    padding: '10px', fontWeight: 700, cursor: busy ? 'default' : 'pointer',
    opacity: busy ? 0.6 : 1,
  }
}

export function labTaskRow(selected: boolean): CSSProperties {
  return {
    // Column layout keeps the two-line title/status stacked while the row still
    // presents a >=44px tap target.
    minHeight: 44, display: 'flex', flexDirection: 'column', justifyContent: 'center',
    boxSizing: 'border-box',
    width: '100%', textAlign: 'left', background: selected ? '#14b8a614' : 'none',
    border: 'none', borderRadius: 6, padding: '6px 8px', cursor: 'pointer', marginBottom: 4,
    color: 'var(--text)', fontSize: 12,
  }
}

export function labBtn(color: string): CSSProperties {
  return {
    ...TOUCH, justifyContent: 'center',
    background: 'none', color, border: `1px solid ${color}66`, borderRadius: 6,
    padding: '6px 12px', fontSize: 12, fontWeight: 600, cursor: 'pointer',
  }
}

// Layered on top of the shared .game-dialog-close className to enlarge the tap
// target inside the Lab panel only (does not mutate the global rule).
export function labClose(): CSSProperties {
  return { minWidth: 44, minHeight: 44 }
}
