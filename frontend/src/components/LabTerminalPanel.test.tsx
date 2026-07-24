import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import { bridge } from '../game/phaserBridge'
import { LabTerminalPanel } from './LabTerminalPanel'
import type { LabTask, LabRun } from '../services/api'

const getLabTasks = vi.fn()
const getLabTask = vi.fn()

vi.mock('../services/api', () => ({
  getLabTasks: (...a: unknown[]) => getLabTasks(...a),
  getLabTask: (...a: unknown[]) => getLabTask(...a),
}))

function task(over: Partial<LabTask> = {}): LabTask {
  return {
    id: 't1', issuer_user_id: 'u1', researcher_slug: 'r1', title: '测算喷泉造价',
    brief_md: '', scopes: ['web_search'], reward_sc: 50, platform_fee_sc: 5,
    deliverable_kind: 'report', status: 'running', accepted_run_id: 'run-1',
    reject_count: 0, result_summary_md: null, deadline_at: null,
    review_deadline_at: null, created_at: null, completed_at: null, ...over,
  }
}

const RUN: LabRun = {
  id: 'run-1', task_id: 't1', researcher_slug: 'r1', adapter: 'claude',
  status: 'running', scopes: ['web_search'], budget_usd_cents: 100,
  cost_usd_cents: 10, approvals: [], error: null, started_at: null, ended_at: null,
}

beforeEach(() => {
  getLabTasks.mockReset()
  getLabTask.mockReset().mockResolvedValue({ task: task(), run: RUN, artifacts: [] })
})

afterEach(cleanup)

async function openPanel() {
  render(<LabTerminalPanel />)
  act(() => { bridge.emit('labterminal:open') })
  return waitFor(() => expect(getLabTasks).toHaveBeenCalledWith('mine'))
}

describe('LabTerminalPanel', () => {
  it('stays closed until the bridge opens it', () => {
    render(<LabTerminalPanel />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('lists my lab tasks with a status badge', async () => {
    getLabTasks.mockResolvedValue({ tasks: [task()] })
    await openPanel()
    await waitFor(() => expect(screen.getByText('测算喷泉造价')).toBeInTheDocument())
    // running task → 执行中 badge from resolveLabDisplay TASK_LABELS
    expect(screen.getByText('执行中')).toBeInTheDocument()
  })

  it('shows an empty state when there are no tasks', async () => {
    getLabTasks.mockResolvedValue({ tasks: [] })
    await openPanel()
    await waitFor(() => expect(screen.getByText(/还没有委托/)).toBeInTheDocument())
  })

  it('renders the run status when a task is selected — with no write controls', async () => {
    getLabTasks.mockResolvedValue({ tasks: [task()] })
    await openPanel()
    fireEvent.click(await screen.findByText('测算喷泉造价'))
    await waitFor(() => expect(getLabTask).toHaveBeenCalledWith('t1'))
    // read-only: no publish/settle/approve buttons anywhere
    expect(screen.queryByRole('button', { name: /发布|放款|批准|拒收|取消委托/ })).not.toBeInTheDocument()
  })
})
