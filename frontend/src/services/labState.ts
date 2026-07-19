// T6 — dual state-system resolver (art-spec §任务状态, the 6 parsing rules).
//
// Task (10 canonical states) and Run (6 canonical states) are SEPARATE systems
// and must never be collapsed into one `display_status`. `verifying` is an
// event PHASE, `researching` is a read-only Resident ACTIVITY, and the
// connection state is an OVERLAY — none of them rewrite the Task/Run badges.
// An unknown value shows a static "未知状态" and is flagged (never silently
// falls back to idle/running). This module is pure + Phaser-free so it is unit
// testable and shared by the panel and any HUD.

export type Connection = 'connected' | 'resyncing' | 'disconnected'
export type EventPhase = 'verifying' | null
export type ResidentActivity = 'researching' | null

export interface Badge {
  value: string
  label: string
  known: boolean // false → render the static "未知状态" chip + log telemetry
}

export interface LabDisplayInput {
  taskStatus?: string | null
  runStatus?: string | null
  eventPhase?: string | null       // latest durable event phase (e.g. "verifying")
  residentActivity?: string | null // resident's own activity (e.g. "researching")
  connection?: Connection | string | null
}

export interface LabDisplay {
  task: Badge
  run: Badge | null            // null when there is no run yet
  phase: EventPhase            // "verifying" only when it may overlay a running run
  activity: ResidentActivity   // resident bubble only; never a Task/Run substitute
  connection: Connection
  frozen: boolean              // connection overlay freezes dynamic animations
  approvalActive: boolean      // Run needs_approval (and not terminal)
}

const TASK_LABELS: Record<string, string> = {
  draft: '草稿', funded: '已资助', assigned: '已分派', running: '执行中', review: '待验收',
  completed: '完成', rejected: '已拒收', failed: '失败', expired: '过期', cancelled: '取消',
}
const RUN_LABELS: Record<string, string> = {
  queued: '排队', running: '运行中', needs_approval: '待审批',
  succeeded: '成功', failed: '失败', cancelled: '取消',
}
const RUN_TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])

function badge(value: string | null | undefined, labels: Record<string, string>): Badge {
  const v = value ?? ''
  const label = labels[v]
  // rule 6: unknown → static "未知状态", flagged; never idle/running fallback.
  return label ? { value: v, label, known: true } : { value: v || 'unknown', label: '未知状态', known: false }
}

// Single derivation point for the currently-selected task. The panel holds
// `tasks` (list) and `selected` (id) separately; deriving the task object once
// here keeps the status badge, TaskActions, and any live view reading the SAME
// source instead of an out-of-scope variable (the Phase 1 build regression).
export function selectLabTask<T extends { id: string }>(
  tasks: readonly T[],
  selected: string | null | undefined,
): T | undefined {
  if (!selected) return undefined
  return tasks.find((t) => t.id === selected)
}

export function resolveLabDisplay(input: LabDisplayInput): LabDisplay {
  const task = badge(input.taskStatus, TASK_LABELS)
  const run = input.runStatus == null || input.runStatus === '' ? null : badge(input.runStatus, RUN_LABELS)

  const runTerminal = !!(run && run.known && RUN_TERMINAL.has(run.value))
  const runRunning = !!(run && run.known && run.value === 'running')
  // rule 3: needs_approval outranks a running phase.
  const approvalActive = !!(run && run.known && run.value === 'needs_approval') && !runTerminal

  // rule 3: verifying may overlay ONLY a running run's action icon; rule 2:
  // a terminal run clears transient phase (and approval) animations.
  let phase: EventPhase = null
  if (input.eventPhase === 'verifying' && runRunning && !approvalActive && !runTerminal) {
    phase = 'verifying'
  }

  // rule 5: researching is a read-only Resident activity, nothing else.
  const activity: ResidentActivity = input.residentActivity === 'researching' ? 'researching' : null

  // rule 4: connection overlay never rewrites business status; resyncing/
  // disconnected freeze dynamic frames but keep the text badges below.
  const c = input.connection
  const connection: Connection =
    c === 'resyncing' || c === 'disconnected' || c === 'connected' ? c : 'connected'
  const frozen = connection === 'resyncing' || connection === 'disconnected'

  return { task, run, phase, activity, connection, frozen, approvalActive }
}
