import type { CSSProperties } from 'react'
import type { LabDisplay } from '../services/labState'
import { useLocale } from '../services/locale'

// Four-track timeline (art-spec §任务状态, the 6 parsing rules): Task, Run, the
// event PHASE, and the CONNECTION overlay are FOUR SEPARATE tracks — never
// collapsed into one status. Resident ACTIVITY (researching) is a distinct chip,
// not a track. A frozen connection (resyncing/disconnected) OR reduced-motion
// renders every track STATIC — no pulse — and truthful (an unknown status shows
// the flagged "未知状态" chip, never a fabricated idle/running). Pure +
// framework-only-React so it is unit-testable and shared by the panel and any HUD.

const ACCENT = '#14b8a6'
const UNKNOWN = '#a1a1aa'
const TASK_EN: Record<string, string> = { draft: 'Draft', funded: 'Funded', assigned: 'Assigned', running: 'Running', review: 'Review', completed: 'Complete', rejected: 'Rejected', failed: 'Failed', expired: 'Expired', cancelled: 'Cancelled' }
const RUN_EN: Record<string, string> = { queued: 'Queued', running: 'Running', needs_approval: 'Needs approval', succeeded: 'Succeeded', failed: 'Failed', cancelled: 'Cancelled' }

function lane(): CSSProperties {
  return { display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, minHeight: 22 }
}
function trackName(): CSSProperties {
  return { width: 44, color: 'var(--text-muted)', flex: '0 0 auto' }
}

export function LabTimeline({ display, reducedMotion = false }: {
  display: LabDisplay
  reducedMotion?: boolean
}) {
  const en = useLocale((state) => state.locale === 'en')
  const frozen = display.frozen || reducedMotion
  const pulse = (active: boolean): CSSProperties =>
    active && !frozen ? { animation: 'sv-pulse 1.4s ease-in-out infinite' } : {}

  const connLabel =
    display.connection === 'connected' ? (en ? 'Connected' : '在线')
      : display.connection === 'resyncing' ? (en ? 'Resyncing' : '重新同步中') : (en ? 'Disconnected' : '已断开')

  return (
    <div
      role="list"
      aria-label="lab-timeline"
      data-frozen={frozen ? 'true' : 'false'}
      style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8 }}
    >
      {/* Track 1 — Task */}
      <div role="listitem" data-track="task" style={lane()}>
        <span style={trackName()}>{en ? 'Task' : '任务'}</span>
        <b data-testid="track-task" style={{ color: display.task.known ? ACCENT : UNKNOWN }}>
          {en ? (TASK_EN[display.task.value] ?? 'Unknown') : display.task.label}
        </b>
        {display.activity === 'researching' && (
          <span data-testid="activity" style={{ color: 'var(--text-muted)', fontSize: 11 }}>· {en ? 'Researching' : '研究中'}</span>
        )}
      </div>

      {/* Track 2 — Run (absent until a run exists) */}
      <div role="listitem" data-track="run" style={lane()}>
        <span style={trackName()}>{en ? 'Run' : '运行'}</span>
        {display.run
          ? <b data-testid="track-run" style={{ color: display.run.known ? ACCENT : UNKNOWN }}>{en ? (RUN_EN[display.run.value] ?? 'Unknown') : display.run.label}</b>
          : <span data-testid="track-run" style={{ color: 'var(--text-muted)' }}>—</span>}
        {display.approvalActive && (
          <span data-testid="approval" style={{ color: '#f59e0b' }}>· {en ? 'Approval needed' : '待审批'}</span>
        )}
      </div>

      {/* Track 3 — event phase (verifying overlays only a running run) */}
      <div role="listitem" data-track="phase" style={lane()}>
        <span style={trackName()}>{en ? 'Phase' : '阶段'}</span>
        <span
          data-testid="track-phase"
          style={{ color: display.phase === 'verifying' ? '#3b82f6' : 'var(--text-muted)', ...pulse(display.phase === 'verifying') }}
        >
          {display.phase === 'verifying' ? (en ? 'Verifying' : '验证中') : '—'}
        </span>
      </div>

      {/* Track 4 — connection overlay */}
      <div role="listitem" data-track="connection" style={lane()}>
        <span style={trackName()}>{en ? 'Link' : '连接'}</span>
        <span
          data-testid="track-connection"
          style={{
            color: display.connection === 'connected' ? ACCENT
              : display.connection === 'resyncing' ? '#f59e0b' : '#ef4444',
            ...pulse(display.connection === 'resyncing'),
          }}
        >
          {connLabel}
        </span>
      </div>
    </div>
  )
}
