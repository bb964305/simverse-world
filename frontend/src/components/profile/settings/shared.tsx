import type { ReactNode } from 'react'

// ─── Shared sub-components ────────────────────────────────────────

export function SectionHeader({ icon, title }: { icon: string; title: string }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      marginBottom: 20, paddingBottom: 12,
      borderBottom: '1px solid var(--border)',
    }}>
      <span style={{ fontSize: 18 }}>{icon}</span>
      <h2 style={{ fontSize: 15, fontWeight: 700, margin: 0, color: 'var(--text-primary)' }}>{title}</h2>
    </div>
  )
}

export function FieldLabel({ children }: { children: ReactNode }) {
  return (
    <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6, fontWeight: 500 }}>
      {children}
    </div>
  )
}

export function TextInput({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        width: '100%', padding: '8px 12px',
        background: 'var(--bg-input)', border: '1px solid var(--border)',
        borderRadius: 6, color: 'var(--text-primary)', fontSize: 13,
        outline: 'none', boxSizing: 'border-box',
      }}
    />
  )
}

export function SaveButton({
  onClick,
  saving,
  saved,
}: {
  onClick: () => void
  saving: boolean
  saved: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={saving}
      style={{
        marginTop: 12, padding: '7px 20px',
        background: saved ? '#53d769' : 'var(--accent)',
        color: 'white', border: 'none', borderRadius: 6,
        fontSize: 13, fontWeight: 600, cursor: saving ? 'default' : 'pointer',
        transition: 'background 0.2s ease', opacity: saving ? 0.7 : 1,
      }}
    >
      {saving ? '保存中...' : saved ? '已保存 ✓' : '保存'}
    </button>
  )
}

export function Toggle({
  value,
  onChange,
  label,
}: {
  value: boolean
  onChange: (v: boolean) => void
  label: string
}) {
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }}>
      <div
        onClick={() => onChange(!value)}
        style={{
          width: 36, height: 20, borderRadius: 10,
          background: value ? 'var(--accent)' : 'var(--bg-input)',
          border: '1px solid var(--border)',
          position: 'relative', cursor: 'pointer', transition: 'background 0.2s',
          flexShrink: 0,
        }}
      >
        <div style={{
          position: 'absolute', top: 2,
          left: value ? 17 : 2,
          width: 14, height: 14, borderRadius: '50%',
          background: 'white', transition: 'left 0.2s',
        }} />
      </div>
      <span style={{ fontSize: 13, color: 'var(--text-primary)' }}>{label}</span>
    </label>
  )
}

export function Badge({
  label,
  active,
}: {
  label: string
  active: boolean
}) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: '3px 10px', borderRadius: 20,
      fontSize: 12, fontWeight: 500,
      background: active ? '#53d76920' : 'var(--bg-input)',
      color: active ? '#53d769' : 'var(--text-muted)',
      border: `1px solid ${active ? '#53d769' : 'var(--border)'}`,
    }}>
      {`${active ? '✓' : '✗'} ${label}`}
    </span>
  )
}

export function SectionCard({ children }: { children: ReactNode }) {
  return (
    <div style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 10, padding: '20px 24px', marginBottom: 16,
    }}>
      {children}
    </div>
  )
}
