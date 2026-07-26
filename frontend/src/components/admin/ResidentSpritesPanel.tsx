import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  RESIDENT_SPRITE_REVIEW_KEYS,
  approveAdminResidentSpriteRun,
  createAdminResidentSpriteRun,
  getAdminResidentSpriteCandidate,
  getAdminResidentSpriteRun,
  getAdminResidentSpriteRuns,
  getAdminResidents,
  progressAdminResidentSpriteRun,
  publishAdminResidentSpriteRun,
  rejectAdminResidentSpriteRun,
  reviewAdminResidentSpriteRun,
  rollbackAdminResidentSpriteRun,
  type AdminResident,
  type AdminResidentSpriteRun,
  type ResidentSpriteChecklist,
  type ResidentSpriteReviewKey,
} from '../../services/api'
import {
  formatCostUpperBound,
  recoveryControlForStatus,
  selectedRunIdAfterMutation,
} from './residentSpriteAdminState'
import { STATIC_RESIDENT_ATLAS_JSON_URL } from '../../game/residentSpriteRuntime'

const CHECKLIST_LABELS: Record<ResidentSpriteReviewKey, string> = {
  identity_consistency: '四方向身份与服装一致',
  down_direction: '向下方向清晰',
  left_direction: '向左方向清晰',
  right_direction: '向右方向清晰',
  up_direction: '向上方向清晰',
  walk_animation: '三帧步行动画连贯',
  transparent_background: '背景透明且无残边',
  limited_palette: '色板克制且像素边缘干净',
  phaser_preview: 'Phaser 尺寸、基线与碰撞观感正常',
}

const STATUS_LABELS: Record<string, string> = {
  requested: '排队中',
  generating: '生成中',
  retrying: '重新排队',
  retry_spawned: '已派生重试',
  interrupted: '已中断',
  failed: '失败',
  quarantined: '已隔离',
  candidate_ready: '待审核',
  in_review: '审核中',
  approved: '已批准',
  rejected: '已拒绝',
  published: '已发布',
  rolled_back: '已回滚',
}

const STATUS_COLORS: Record<string, string> = {
  requested: '#94a3b8',
  generating: '#38bdf8',
  retrying: '#38bdf8',
  retry_spawned: '#94a3b8',
  interrupted: '#f59e0b',
  failed: '#ef4444',
  quarantined: '#f97316',
  candidate_ready: '#f59e0b',
  in_review: '#a78bfa',
  approved: '#22c55e',
  rejected: '#ef4444',
  published: '#14b8a6',
  rolled_back: '#94a3b8',
}

const POLLED_STATUSES = new Set(['requested', 'retrying', 'generating', 'interrupted'])
const PROGRESS_POLL_MS = 4000

function emptyChecklist(): ResidentSpriteChecklist {
  return Object.fromEntries(RESIDENT_SPRITE_REVIEW_KEYS.map((key) => [key, false])) as ResidentSpriteChecklist
}

function mergeChecklist(value: Partial<ResidentSpriteChecklist> | null): ResidentSpriteChecklist {
  return { ...emptyChecklist(), ...(value ?? {}) }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function useCandidateUrl(
  token: string,
  runId: string | null,
  kind: 'texture' | 'portrait',
  available: boolean,
  revision: string,
): { url: string | null; loading: boolean; error: string | null } {
  const requestKey = runId && available ? `${runId}:${kind}:${revision}` : null
  const [state, setState] = useState<{ key: string; url: string | null; error: string | null }>({
    key: '',
    url: null,
    error: null,
  })

  useEffect(() => {
    if (!runId || !requestKey) return
    const controller = new AbortController()
    let objectUrl: string | null = null
    getAdminResidentSpriteCandidate(token, runId, kind, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(blob)
        setState({ key: requestKey, url: objectUrl, error: null })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setState({
          key: requestKey,
          url: null,
          error: error instanceof Error ? error.message : '候选资源加载失败',
        })
      })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [kind, requestKey, runId, token])

  if (!requestKey) return { url: null, loading: false, error: null }
  if (state.key !== requestKey) return { url: null, loading: true, error: null }
  return { url: state.url, loading: false, error: state.error }
}

function SpriteAnimationPreview({ url }: { url: string }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let game: import('phaser').Game | null = null
    setError(null)

    void import('phaser').then(({ default: Phaser }) => {
      if (cancelled || !containerRef.current) return
      const textureKey = `admin-review-${crypto.randomUUID()}`
      const animationPrefix = `${textureKey}-walk`
      game = new Phaser.Game({
        type: Phaser.CANVAS,
        parent: containerRef.current,
        width: 416,
        height: 128,
        backgroundColor: '#20242b',
        pixelArt: true,
        render: { antialias: false },
        scene: {
          preload() {
            this.load.atlas(textureKey, url, STATIC_RESIDENT_ATLAS_JSON_URL)
            this.load.once(Phaser.Loader.Events.FILE_LOAD_ERROR, () => {
              if (!cancelled) setError('Phaser 候选资源加载失败')
            })
          },
          create() {
            const directions = [
              ['down', '下'],
              ['left', '左'],
              ['right', '右'],
              ['up', '上'],
            ] as const
            const missing = directions.flatMap(([direction]) => [0, 1, 2]
              .map((frame) => `${direction}-walk.${String(frame).padStart(3, '0')}`)
              .filter((frame) => !this.textures.get(textureKey).has(frame)))
            if (missing.length > 0) {
              if (!cancelled) setError(`Phaser 缺少帧：${missing.join(', ')}`)
              return
            }
            directions.forEach(([direction, label], index) => {
              const animationKey = `${animationPrefix}-${direction}`
              this.anims.create({
                key: animationKey,
                frames: [0, 1, 2].map((frame) => ({
                  key: textureKey,
                  frame: `${direction}-walk.${String(frame).padStart(3, '0')}`,
                })),
                frameRate: 6,
                repeat: -1,
              })
              this.add.sprite(52 + index * 104, 50, textureKey, `${direction}-walk.001`)
                .setScale(2)
                .play(animationKey)
              this.add.text(48 + index * 104, 99, label, {
                fontFamily: 'sans-serif',
                fontSize: '12px',
                color: '#cbd5e1',
              })
            })
          },
        },
      })
      game.canvas.style.width = '100%'
      game.canvas.style.height = '100%'
    }).catch(() => {
      if (!cancelled) setError('Phaser 预览初始化失败')
    })

    return () => {
      cancelled = true
      game?.destroy(true)
    }
  }, [url])

  return (
    <div>
      <div
        ref={containerRef}
        aria-label="Phaser 四方向动画预览"
        style={{
          width: 'min(416px, 100%)',
          aspectRatio: '13 / 4',
          overflow: 'hidden',
          border: '1px solid var(--border)',
          background: '#20242b',
        }}
      />
      {error && <div style={{ marginTop: 6, color: 'var(--danger)', fontSize: 12 }}>{error}</div>}
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const color = STATUS_COLORS[status] ?? '#94a3b8'
  return (
    <span style={{
      display: 'inline-flex',
      padding: '2px 7px',
      borderRadius: 4,
      color,
      background: `${color}18`,
      border: `1px solid ${color}44`,
      fontSize: 11,
      fontWeight: 700,
      whiteSpace: 'nowrap',
    }}>
      {STATUS_LABELS[status] ?? status}
    </span>
  )
}

export function ResidentSpritesPanel({ token }: { token: string }) {
  const [runs, setRuns] = useState<AdminResidentSpriteRun[]>([])
  const [residents, setResidents] = useState<AdminResident[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [detail, setDetail] = useState<AdminResidentSpriteRun | null>(null)
  const [selectedResidentId, setSelectedResidentId] = useState('')
  const [directionPolicy, setDirectionPolicy] = useState<'mirror_right' | 'generate_right'>('mirror_right')
  const [appearance, setAppearance] = useState('')
  const [gender, setGender] = useState<'male' | 'female' | 'neutral'>('neutral')
  const [ageGroup, setAgeGroup] = useState<'young' | 'adult' | 'elder'>('adult')
  const [vibe, setVibe] = useState('')
  const [tagsInput, setTagsInput] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [checklist, setChecklist] = useState<ResidentSpriteChecklist>(emptyChecklist)
  const [notes, setNotes] = useState('')
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [action, setAction] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const residentNames = useMemo(
    () => new Map(residents.map((resident) => [resident.id, resident.name])),
    [residents],
  )
  const tags = useMemo(
    () => tagsInput.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
    [tagsInput],
  )
  const tagsValid = tags.length <= 8
    && tags.every((tag) => tag.length <= 32)
    && new Set(tags.map((tag) => tag.toLocaleLowerCase())).size === tags.length
  const createValid = Boolean(selectedResidentId)
    && appearance.trim().length >= 1
    && appearance.trim().length <= 1200
    && vibe.trim().length >= 1
    && vibe.trim().length <= 40
    && tagsValid

  const loadRuns = useCallback(async (keepSelection = true, preferredRunId?: string) => {
    setLoading(true)
    setError(null)
    try {
      const response = await getAdminResidentSpriteRuns(token, {
        status: statusFilter || undefined,
        page: 1,
        per_page: 100,
      })
      setRuns(response.items)
      setSelectedRunId((current) => {
        if (preferredRunId && response.items.some((run) => run.run_id === preferredRunId)) return preferredRunId
        if (keepSelection && current && response.items.some((run) => run.run_id === current)) return current
        return response.items[0]?.run_id ?? null
      })
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '形象任务加载失败')
    } finally {
      setLoading(false)
    }
  }, [statusFilter, token])

  useEffect(() => {
    let cancelled = false
    getAdminResidents(token, { page: 1, per_page: 100 })
      .then((response) => {
        if (cancelled) return
        setResidents(response.items)
        setSelectedResidentId((current) => current || response.items[0]?.id || '')
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '居民列表加载失败')
      })
    return () => { cancelled = true }
  }, [token])

  useEffect(() => {
    void loadRuns(false)
  }, [loadRuns])

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetailLoading(true)
    setError(null)
    getAdminResidentSpriteRun(token, selectedRunId)
      .then((run) => {
        if (cancelled) return
        setDetail(run)
        setChecklist(mergeChecklist(run.review_checklist_json))
        setNotes(run.review_notes ?? run.rejection_reason ?? run.rollback_reason ?? '')
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '任务详情加载失败')
      })
      .finally(() => { if (!cancelled) setDetailLoading(false) })
    return () => { cancelled = true }
  }, [selectedRunId, token])

  const hasRunningTask = runs.some((run) => POLLED_STATUSES.has(run.status))
    || Boolean(detail && POLLED_STATUSES.has(detail.status))
  const currentDetailVersion = detail?.version

  useEffect(() => {
    if (!hasRunningTask) return
    let cancelled = false
    let inFlight = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const controller = new AbortController()

    const schedule = (delay = PROGRESS_POLL_MS) => {
      if (cancelled) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { void poll() }, delay)
    }
    const poll = async () => {
      if (cancelled || inFlight) return
      if (document.visibilityState === 'hidden') {
        schedule()
        return
      }
      inFlight = true
      try {
        const [response, selected] = await Promise.all([
          getAdminResidentSpriteRuns(token, {
            status: statusFilter || undefined,
            page: 1,
            per_page: 100,
          }, controller.signal),
          selectedRunId ? getAdminResidentSpriteRun(token, selectedRunId, controller.signal) : Promise.resolve(null),
        ])
        if (cancelled) return
        setRuns(response.items)
        setSelectedRunId((current) => {
          if (current && response.items.some((run) => run.run_id === current)) return current
          return response.items[0]?.run_id ?? null
        })
        if (selected && (currentDetailVersion == null || selected.version >= currentDetailVersion)) {
          setDetail(selected)
          setChecklist(mergeChecklist(selected.review_checklist_json))
          setNotes(selected.review_notes ?? selected.rejection_reason ?? selected.rollback_reason ?? '')
        }
        setError(null)
      } catch (err: unknown) {
        if (!cancelled) setError(err instanceof Error ? err.message : '进度刷新失败')
      } finally {
        inFlight = false
        schedule()
      }
    }
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') schedule(0)
    }

    schedule()
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      cancelled = true
      controller.abort()
      if (timer) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [currentDetailVersion, hasRunningTask, selectedRunId, statusFilter, token])

  const texture = useCandidateUrl(
    token,
    detail?.run_id ?? null,
    'texture',
    Boolean(detail?.candidate_texture_path || detail?.candidate_texture_url),
    detail?.candidate_texture_sha256 ?? String(detail?.version ?? ''),
  )
  const portrait = useCandidateUrl(
    token,
    detail?.run_id ?? null,
    'portrait',
    Boolean(detail?.candidate_portrait_path || detail?.candidate_portrait_url),
    detail?.candidate_portrait_sha256 ?? String(detail?.version ?? ''),
  )

  const allChecked = RESIDENT_SPRITE_REVIEW_KEYS.every((key) => checklist[key])
  const isBusy = action !== null
  const recoveryControl = detail ? recoveryControlForStatus(detail.status) : null

  const refreshAfterAction = useCallback(async (updated: AdminResidentSpriteRun, message: string) => {
    const nextRunId = selectedRunIdAfterMutation(selectedRunId, updated)
    const clearsFilter = Boolean(statusFilter && updated.status !== statusFilter)
    setSelectedRunId(nextRunId)
    setDetail(updated)
    setChecklist(mergeChecklist(updated.review_checklist_json))
    setNotice(message)
    setRuns((current) => [updated, ...current.filter((run) => run.run_id !== updated.run_id)])
    if (clearsFilter) {
      setStatusFilter('')
    } else {
      await loadRuns(true, nextRunId)
    }
  }, [loadRuns, selectedRunId, statusFilter])

  const runAction = useCallback(async (
    label: string,
    operation: () => Promise<AdminResidentSpriteRun>,
    successMessage: string,
  ) => {
    setAction(label)
    setError(null)
    setNotice(null)
    try {
      await refreshAfterAction(await operation(), successMessage)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : `${label}失败`)
    } finally {
      setAction(null)
    }
  }, [refreshAfterAction])

  const createRun = async () => {
    if (!createValid) return
    await runAction('创建', async () => {
      const created = await createAdminResidentSpriteRun(token, {
        resident_id: selectedResidentId,
        appearance: appearance.trim(),
        gender,
        age_group: ageGroup,
        vibe: vibe.trim(),
        tags,
        direction_policy: directionPolicy,
      })
      setSelectedRunId(created.run_id)
      setAppearance('')
      setVibe('')
      setTagsInput('')
      return created
    }, '形象任务已创建')
  }

  const saveReview = async () => {
    if (!detail) return
    await runAction('保存审核', () => reviewAdminResidentSpriteRun(token, detail.run_id, {
      expected_version: detail.version,
      evidence: {
        source: 'admin_sprite_workbench',
        texture_sha256: detail.candidate_texture_sha256,
        portrait_sha256: detail.candidate_portrait_sha256,
      },
      checklist,
      notes: notes.trim(),
    }), '审核记录已保存')
  }

  const buttonStyle = (tone: 'default' | 'primary' | 'danger' | 'success' = 'default'): React.CSSProperties => ({
    border: `1px solid ${tone === 'danger' ? '#ef444466' : tone === 'success' ? '#22c55e66' : 'var(--border)'}`,
    borderRadius: 6,
    background: tone === 'primary' ? 'var(--accent-red)' : tone === 'success' ? '#15803d' : 'var(--bg-input)',
    color: tone === 'danger' ? '#f87171' : 'white',
    padding: '7px 11px',
    fontSize: 12,
    fontWeight: 600,
    cursor: isBusy ? 'wait' : 'pointer',
  })
  const formControlStyle: React.CSSProperties = {
    padding: '7px 9px',
    borderRadius: 6,
    border: '1px solid var(--border)',
    background: 'var(--bg-input)',
    color: 'white',
    fontSize: 12,
    boxSizing: 'border-box',
  }

  return (
    <div>
      <div style={{ paddingBottom: 12, marginBottom: 12, borderBottom: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', alignItems: 'end', gap: 8, flexWrap: 'wrap' }}>
          <label style={{ display: 'grid', gap: 4, minWidth: 170, flex: '1 1 190px', fontSize: 11, color: 'var(--text-muted)' }}>
            居民
            <select
              value={selectedResidentId}
              onChange={(event) => setSelectedResidentId(event.target.value)}
              disabled={isBusy || residents.length === 0}
              style={formControlStyle}
            >
              {residents.map((resident) => <option key={resident.id} value={resident.id}>{resident.name}</option>)}
            </select>
          </label>
          <label style={{ display: 'grid', gap: 4, minWidth: 100, fontSize: 11, color: 'var(--text-muted)' }}>
            性别
            <select value={gender} onChange={(event) => setGender(event.target.value as typeof gender)} disabled={isBusy} style={formControlStyle}>
              <option value="neutral">中性</option>
              <option value="female">女性</option>
              <option value="male">男性</option>
            </select>
          </label>
          <label style={{ display: 'grid', gap: 4, minWidth: 100, fontSize: 11, color: 'var(--text-muted)' }}>
            年龄
            <select value={ageGroup} onChange={(event) => setAgeGroup(event.target.value as typeof ageGroup)} disabled={isBusy} style={formControlStyle}>
              <option value="young">青年</option>
              <option value="adult">成年</option>
              <option value="elder">长者</option>
            </select>
          </label>
          <label style={{ display: 'grid', gap: 4, minWidth: 135, fontSize: 11, color: 'var(--text-muted)' }}>
            右向策略
            <select
              value={directionPolicy}
              onChange={(event) => setDirectionPolicy(event.target.value as typeof directionPolicy)}
              disabled={isBusy}
              style={formControlStyle}
            >
              <option value="mirror_right">镜像</option>
              <option value="generate_right">独立生成</option>
            </select>
          </label>
        </div>
        <div style={{ display: 'flex', alignItems: 'stretch', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
          <label style={{ display: 'grid', gap: 4, flex: '2 1 360px', fontSize: 11, color: 'var(--text-muted)' }}>
            <span style={{ display: 'flex', justifyContent: 'space-between' }}><span>外观描述</span><span>{appearance.trim().length}/1200</span></span>
            <textarea
              value={appearance}
              onChange={(event) => setAppearance(event.target.value)}
              maxLength={1200}
              rows={3}
              required
              disabled={isBusy}
              style={{ ...formControlStyle, resize: 'vertical', minHeight: 68 }}
            />
          </label>
          <div style={{ display: 'grid', gridTemplateRows: '1fr 1fr', gap: 7, flex: '1 1 250px' }}>
            <label style={{ display: 'grid', gap: 4, fontSize: 11, color: 'var(--text-muted)' }}>
              <span style={{ display: 'flex', justifyContent: 'space-between' }}><span>气质</span><span>{vibe.trim().length}/40</span></span>
              <input
                value={vibe}
                onChange={(event) => setVibe(event.target.value)}
                maxLength={40}
                required
                disabled={isBusy}
                style={formControlStyle}
              />
            </label>
            <label style={{ display: 'grid', gap: 4, fontSize: 11, color: tagsValid ? 'var(--text-muted)' : '#f87171' }}>
              <span style={{ display: 'flex', justifyContent: 'space-between' }}><span>标签（逗号分隔）</span><span>{tags.length}/8</span></span>
              <input
                value={tagsInput}
                onChange={(event) => setTagsInput(event.target.value)}
                maxLength={263}
                disabled={isBusy}
                aria-invalid={!tagsValid}
                style={{ ...formControlStyle, borderColor: tagsValid ? 'var(--border)' : '#ef4444' }}
              />
            </label>
          </div>
          <button
            type="button"
            onClick={() => void createRun()}
            disabled={isBusy || !createValid}
            style={{ ...buttonStyle('primary'), alignSelf: 'end', minHeight: 34, opacity: createValid ? 1 : 0.45 }}
          >
            {action === '创建' ? '提交中…' : '提交生成'}
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14, flexWrap: 'wrap' }}>
        <select
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
          aria-label="按状态筛选"
          style={{
            padding: '7px 10px',
            borderRadius: 6,
            border: '1px solid var(--border)',
            background: 'var(--bg-input)',
            color: 'white',
          }}
        >
          <option value="">全部状态</option>
          {Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <button type="button" onClick={() => void loadRuns()} disabled={loading || isBusy} title="刷新任务" style={buttonStyle()}>
          {loading ? '刷新中…' : '刷新'}
        </button>
        <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 11 }}>
          {loading ? '加载中…' : `${runs.length} / 100`}
        </span>
      </div>

      {(error || notice) && (
        <div role={error ? 'alert' : 'status'} style={{
          marginBottom: 12,
          padding: '8px 11px',
          borderRadius: 6,
          border: `1px solid ${error ? '#ef444455' : '#22c55e55'}`,
          background: error ? '#ef444412' : '#22c55e12',
          color: error ? '#f87171' : '#4ade80',
          fontSize: 12,
          overflowWrap: 'anywhere',
        }}>
          {error ?? notice}
        </div>
      )}

      <div className="admin-sprite-workspace" style={{ display: 'flex', alignItems: 'stretch', gap: 14, flexWrap: 'wrap' }}>
        <div className="admin-sprite-run-list" style={{ width: 330, flex: '0 1 330px', minWidth: 270 }}>
          <div style={{ color: 'var(--text-muted)', fontSize: 11, marginBottom: 7 }}>
            {loading ? '正在加载任务' : `${runs.length} 个任务`}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 'calc(100vh - 300px)', overflowY: 'auto' }}>
            {!loading && runs.length === 0 && (
              <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', border: '1px dashed var(--border)', borderRadius: 6 }}>
                暂无形象任务
              </div>
            )}
            {runs.map((run) => {
              const selected = run.run_id === selectedRunId
              return (
                <button
                  type="button"
                  key={run.run_id}
                  onClick={() => setSelectedRunId(run.run_id)}
                  style={{
                    width: '100%',
                    padding: '10px 11px',
                    borderRadius: 6,
                    border: `1px solid ${selected ? '#64748b' : 'var(--border)'}`,
                    background: selected ? '#ffffff0b' : 'transparent',
                    color: 'white',
                    cursor: 'pointer',
                    textAlign: 'left',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                    <strong style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {residentNames.get(run.resident_id) ?? run.resident_id.slice(0, 8)}
                    </strong>
                    <span style={{ marginLeft: 'auto' }}><StatusBadge status={run.status} /></span>
                  </div>
                  <div style={{ marginTop: 6, color: 'var(--text-muted)', fontSize: 11, display: 'flex', gap: 10 }}>
                    <span>{run.request_count} 次请求</span>
                    <span>{formatCostUpperBound(run.estimated_cost_usd)}</span>
                    <span style={{ marginLeft: 'auto' }}>v{run.version}</span>
                  </div>
                </button>
              )
            })}
          </div>
        </div>

        <div className="admin-sprite-run-detail" style={{ flex: '1 1 560px', minWidth: 0, borderLeft: '1px solid var(--border)', paddingLeft: 16 }}>
          {detailLoading ? (
            <div style={{ padding: 40, color: 'var(--text-muted)', textAlign: 'center' }}>加载详情中…</div>
          ) : !detail ? (
            <div style={{ padding: 40, color: 'var(--text-muted)', textAlign: 'center' }}>选择一个任务查看候选形象</div>
          ) : (
            <>
              <div style={{ display: 'flex', alignItems: 'start', gap: 10, marginBottom: 14 }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <h2 style={{ margin: 0, fontSize: 16 }}>{residentNames.get(detail.resident_id) ?? '居民形象'}</h2>
                    <StatusBadge status={detail.status} />
                  </div>
                  <div style={{ marginTop: 4, color: 'var(--text-muted)', fontSize: 11, overflowWrap: 'anywhere' }}>
                    {detail.run_id} · 更新于 {formatTime(detail.updated_at)}
                  </div>
                </div>
                <div style={{ marginLeft: 'auto', textAlign: 'right', color: 'var(--text-muted)', fontSize: 11, whiteSpace: 'nowrap' }}>
                  <div>{detail.request_count} 次请求</div>
                  <div>{detail.attempts} 次尝试</div>
                  <div>{formatCostUpperBound(detail.estimated_cost_usd)}</div>
                  <div>{detail.direction_policy === 'generate_right' ? '右向独立生成' : '右向镜像'}</div>
                </div>
              </div>

              {(detail.error_code || detail.error_message) && (
                <div style={{ marginBottom: 12, padding: 9, borderRadius: 6, background: '#ef444412', color: '#f87171', fontSize: 12 }}>
                  {detail.error_code ? `[${detail.error_code}] ` : ''}{detail.error_message}
                </div>
              )}

              <div style={{ display: 'flex', gap: 18, alignItems: 'start', flexWrap: 'wrap', marginBottom: 18 }}>
                <div className="admin-sprite-preview-pane" style={{ minWidth: 312 }}>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 7 }}>四方向三帧预览</div>
                  {texture.loading ? (
                    <div className="admin-sprite-preview-state" style={{ width: 312, height: 88, display: 'grid', placeItems: 'center', color: 'var(--text-muted)' }}>纹理加载中…</div>
                  ) : texture.url ? (
                    <SpriteAnimationPreview url={texture.url} />
                  ) : (
                    <div className="admin-sprite-preview-state" style={{ width: 312, height: 88, display: 'grid', placeItems: 'center', color: texture.error ? '#f87171' : 'var(--text-muted)', border: '1px dashed var(--border)', borderRadius: 6, fontSize: 12 }}>
                      {texture.error ? '纹理加载失败' : '尚无候选纹理'}
                    </div>
                  )}
                </div>
                <div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 7 }}>头像</div>
                  <div style={{ width: 88, height: 88, display: 'grid', placeItems: 'center', border: '1px solid var(--border)', borderRadius: 6, background: '#20242b', overflow: 'hidden' }}>
                    {portrait.loading ? (
                      <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>加载中…</span>
                    ) : portrait.url ? (
                      <img src={portrait.url} alt="候选居民头像" style={{ width: '100%', height: '100%', objectFit: 'contain', imageRendering: 'pixelated' }} />
                    ) : (
                      <span style={{ color: portrait.error ? '#f87171' : 'var(--text-muted)', fontSize: 11 }}>暂无</span>
                    )}
                  </div>
                </div>
              </div>

              <div style={{ borderTop: '1px solid var(--border)', paddingTop: 14 }}>
                <div style={{ display: 'flex', alignItems: 'center', marginBottom: 9 }}>
                  <strong style={{ fontSize: 13 }}>发布检查清单</strong>
                  <span style={{ marginLeft: 'auto', color: allChecked ? '#4ade80' : 'var(--text-muted)', fontSize: 11 }}>
                    {RESIDENT_SPRITE_REVIEW_KEYS.filter((key) => checklist[key]).length} / 9
                  </span>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: '7px 14px' }}>
                  {RESIDENT_SPRITE_REVIEW_KEYS.map((key) => (
                    <label key={key} style={{ display: 'flex', alignItems: 'center', gap: 7, color: checklist[key] ? 'var(--text-primary)' : 'var(--text-secondary)', fontSize: 12 }}>
                      <input
                        type="checkbox"
                        checked={checklist[key]}
                        onChange={(event) => setChecklist((current) => ({ ...current, [key]: event.target.checked }))}
                        disabled={isBusy || !['candidate_ready', 'in_review'].includes(detail.status)}
                      />
                      {CHECKLIST_LABELS[key]}
                    </label>
                  ))}
                </div>
                <textarea
                  value={notes}
                  onChange={(event) => setNotes(event.target.value)}
                  placeholder="审核备注；拒绝或回滚时填写原因"
                  rows={3}
                  disabled={isBusy}
                  style={{
                    width: '100%',
                    boxSizing: 'border-box',
                    marginTop: 12,
                    resize: 'vertical',
                    padding: '8px 10px',
                    borderRadius: 6,
                    border: '1px solid var(--border)',
                    background: 'var(--bg-input)',
                    color: 'white',
                    fontSize: 12,
                  }}
                />
              </div>

              <div style={{ display: 'flex', gap: 7, marginTop: 12, flexWrap: 'wrap' }}>
                {recoveryControl && (
                  <button
                    type="button"
                    disabled={isBusy}
                    onClick={() => void runAction('推进任务', () => progressAdminResidentSpriteRun(token, detail.run_id, {
                      action: recoveryControl.action,
                      expected_version: detail.version,
                    }), recoveryControl.label === '重新生成' ? '新任务已进入队列' : '任务已重新排队')}
                    style={buttonStyle()}
                  >
                    {action === '推进任务' ? '提交中…' : recoveryControl.label}
                  </button>
                )}
                {['candidate_ready', 'in_review'].includes(detail.status) && (detail.candidate_texture_path || detail.candidate_texture_url) && (
                  <button type="button" disabled={isBusy} onClick={() => void saveReview()} style={buttonStyle()}>
                    {action === '保存审核' ? '保存中…' : '保存审核'}
                  </button>
                )}
                {detail.status === 'in_review' && (
                  <button
                    type="button"
                    disabled={isBusy || !allChecked}
                    title={!allChecked ? '完成九项检查后才能批准' : undefined}
                    onClick={() => void runAction('批准', () => approveAdminResidentSpriteRun(token, detail.run_id, detail.version), '候选形象已批准')}
                    style={{ ...buttonStyle('success'), opacity: allChecked ? 1 : 0.45 }}
                  >批准</button>
                )}
                {detail.status === 'approved' && (
                  <button
                    type="button"
                    disabled={isBusy}
                    onClick={() => void runAction('发布', () => publishAdminResidentSpriteRun(token, detail.run_id, detail.version), '新形象已原子发布')}
                    style={buttonStyle('primary')}
                  >{action === '发布' ? '发布中…' : '发布'}</button>
                )}
                {['candidate_ready', 'in_review', 'approved'].includes(detail.status) && (detail.candidate_texture_path || detail.candidate_texture_url) && (
                  <button
                    type="button"
                    disabled={isBusy || notes.trim().length === 0}
                    title={notes.trim().length === 0 ? '请先填写拒绝原因' : undefined}
                    onClick={() => void runAction('拒绝', () => rejectAdminResidentSpriteRun(token, detail.run_id, detail.version, notes.trim()), '候选形象已拒绝')}
                    style={{ ...buttonStyle('danger'), opacity: notes.trim() ? 1 : 0.45 }}
                  >拒绝</button>
                )}
                {detail.status === 'published' && (
                  <button
                    type="button"
                    disabled={isBusy || notes.trim().length === 0}
                    title={notes.trim().length === 0 ? '请先填写回滚原因' : undefined}
                    onClick={() => void runAction('回滚', () => rollbackAdminResidentSpriteRun(token, detail.run_id, detail.version, notes.trim()), '居民形象已回滚')}
                    style={{ ...buttonStyle('danger'), opacity: notes.trim() ? 1 : 0.45 }}
                  >{action === '回滚' ? '回滚中…' : '回滚'}</button>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
