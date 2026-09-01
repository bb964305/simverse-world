import { useCallback, useEffect, useMemo, useState, type ChangeEvent } from 'react'
import { TopNav } from '../components/TopNav'
import { updateCharacter, updatePlayerPosition } from '../services/api'
import { API_BASE } from '../services/api/core'
import { useLocale } from '../services/locale'
import {
  anchorMemory,
  anchorSave,
  createAgentPassport,
  loadLatestSaveAnchor,
  loadOwnedAgents,
  publishTrainingVersion,
  registryConfigured,
  type AgentChainState,
} from '../services/web3/agentRegistry'
import { readPrivateAnchoredJson, snapshotGameMemory, uploadWeb3Content } from '../services/web3/content'
import { configuredChainId, configuredChainName } from '../services/web3/wallet'
import { useGameStore } from '../stores/gameStore'
import '../styles/agent-studio.css'

type AnchorKind = 'training' | 'memory' | 'save'

interface LocalResident {
  id: string
  slug: string
  name: string
  district: string
  status: string
  star_rating: number
  sprite_key: string
  meta_json: Record<string, unknown> | null
}

interface ChainAgent {
  id: bigint
  uri: string
  state: AgentChainState
}

interface SaveSnapshot {
  schema: 'simverse-save-v1'
  wallet: string
  agent_id: string
  recorded_at: string
  player: { sprite_key: string; tile_x: number; tile_y: number }
}

const COPY = {
  'zh-CN': {
    eyebrow: 'WALLET-OWNED AGENT INFRASTRUCTURE', title: '链上 Agent 工作台',
    lead: '把现有居民铸造成不可转让的链上身份，并为训练、上传、记忆与游戏存档建立可验证的版本链。',
    account: '钱包身份', network: '网络', contract: '可升级合约', connected: '已接通', missing: '尚未配置地址',
    createTitle: '01 / 创建 Agent 身份', createLead: '选择你在游戏中锻造的居民。元数据先保存到私有内容层，哈希与所有权由钱包写入链上。',
    resident: '游戏居民', noResident: '请先在炼化工坊创建一位居民', create: '创建链上身份', creating: '正在上传并等待钱包…',
    anchorTitle: '02 / 保存与确权', anchorLead: '选择 Agent 和内容类型。每次写入都会形成不可篡改的新版本，不会转移资金。',
    agent: '链上 Agent', kind: '内容类型', file: '选择内容文件', training: '训练 / 上传版本', memory: '记忆快照', save: '游戏存档',
    anchor: '上传并写入链上', anchoring: '正在等待链上确认…', quickSave: '保存当前游戏状态', memorySync: '同步居民记忆上链', restoreSave: '恢复最新链上存档', restoring: '正在校验并恢复…', restored: '链上存档已恢复', noSave: '这个 Agent 还没有链上存档', invalidSave: '链上存档内容无效',
    passports: '03 / 我的链上身份', empty: '这个钱包还没有 Agent Passport。', version: '训练版本', memories: '记忆版本', saves: '存档版本', metadata: '元数据',
    tx: '交易已确认', refresh: '刷新链上状态', privacy: '隐私提示：该内容接口需要钱包会话才能下载；正式接入 IPFS/Arweave 时，请先加密敏感记忆。',
  },
  en: {
    eyebrow: 'WALLET-OWNED AGENT INFRASTRUCTURE', title: 'Onchain Agent Studio',
    lead: 'Turn an existing resident into a non-transferable onchain identity, then build verifiable version chains for training, uploads, memories, and game saves.',
    account: 'Wallet identity', network: 'Network', contract: 'Upgradeable contract', connected: 'Connected', missing: 'Address not configured',
    createTitle: '01 / Create Agent identity', createLead: 'Choose a resident forged in the game. Metadata enters the private content layer first; your wallet anchors its hash and ownership onchain.',
    resident: 'Game resident', noResident: 'Create a resident in the Forge first', create: 'Create onchain identity', creating: 'Uploading and waiting for wallet…',
    anchorTitle: '02 / Save and prove', anchorLead: 'Choose an Agent and content type. Every write creates an immutable version without moving funds.',
    agent: 'Onchain Agent', kind: 'Content type', file: 'Choose content file', training: 'Training / upload version', memory: 'Memory snapshot', save: 'Game save',
    anchor: 'Upload and anchor', anchoring: 'Waiting for onchain confirmation…', quickSave: 'Save current game state', memorySync: 'Anchor resident memory', restoreSave: 'Restore latest onchain save', restoring: 'Verifying and restoring…', restored: 'Onchain save restored', noSave: 'This Agent has no onchain save yet', invalidSave: 'The onchain save is invalid',
    passports: '03 / My onchain identities', empty: 'This wallet has no Agent Passport yet.', version: 'Training versions', memories: 'Memory versions', saves: 'Save versions', metadata: 'Metadata',
    tx: 'Transaction confirmed', refresh: 'Refresh onchain state', privacy: 'Privacy: downloads require the wallet session. Encrypt sensitive memories before moving content to IPFS or Arweave in production.',
  },
} as const

function shortAddress(value: string | null | undefined) {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '—'
}

function validatedSave(
  value: unknown,
  wallet: `0x${string}`,
  agentId: string,
  invalidMessage: string,
): SaveSnapshot {
  const snapshot = value as Partial<SaveSnapshot> | null
  const player = snapshot?.player
  if (
    snapshot?.schema !== 'simverse-save-v1' ||
    typeof snapshot.wallet !== 'string' || snapshot.wallet.toLowerCase() !== wallet.toLowerCase() ||
    snapshot.agent_id !== agentId || !player ||
    typeof player.sprite_key !== 'string' || player.sprite_key.length < 1 || player.sprite_key.length > 100 ||
    !Number.isInteger(player.tile_x) || player.tile_x < 0 || player.tile_x > 4095 ||
    !Number.isInteger(player.tile_y) || player.tile_y < 0 || player.tile_y > 4095
  ) throw new Error(invalidMessage)
  return snapshot as SaveSnapshot
}

export function AgentStudioPage() {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const token = useGameStore((state) => state.token)
  const user = useGameStore((state) => state.user)
  const playerSpriteKey = useGameStore((state) => state.playerSpriteKey)
  const playerTileX = useGameStore((state) => state.playerTileX)
  const playerTileY = useGameStore((state) => state.playerTileY)
  const setPlayerSpriteKey = useGameStore((state) => state.setPlayerSpriteKey)
  const setPlayerTile = useGameStore((state) => state.setPlayerTile)
  const wallet = user?.wallet_address as `0x${string}` | undefined
  const [residents, setResidents] = useState<LocalResident[]>([])
  const [residentId, setResidentId] = useState('')
  const [agents, setAgents] = useState<ChainAgent[]>([])
  const [agentId, setAgentId] = useState('')
  const [kind, setKind] = useState<AnchorKind>('training')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState<'create' | 'anchor' | 'save' | 'restore' | 'memory' | 'refresh' | null>(null)
  const [error, setError] = useState('')
  const [transaction, setTransaction] = useState<`0x${string}` | null>(null)
  const [restored, setRestored] = useState(false)

  const selectedResident = useMemo(() => residents.find((resident) => resident.id === residentId), [residentId, residents])
  const contractReady = registryConfigured()

  const refreshAgents = useCallback(async () => {
    if (!wallet || !contractReady) return
    setBusy('refresh')
    try {
      const next = await loadOwnedAgents(wallet)
      setAgents(next)
      setAgentId((current) => current && next.some((agent) => agent.id.toString() === current) ? current : next[0]?.id.toString() || '')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to read contract')
    } finally {
      setBusy(null)
    }
  }, [contractReady, wallet])

  useEffect(() => {
    if (!token) return
    fetch(`${API_BASE}/profile/residents`, { headers: { Authorization: `Bearer ${token}` } })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`Residents API ${response.status}`)))
      .then((items: LocalResident[]) => { setResidents(items); setResidentId(items[0]?.id || '') })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Unable to load residents'))
  }, [token])

  useEffect(() => { void refreshAgents() }, [refreshAgents])

  const createPassport = async () => {
    if (!selectedResident || !wallet) return
    setBusy('create'); setError(''); setTransaction(null); setRestored(false)
    try {
      const metadata = {
        name: selectedResident.name,
        description: `Simverse resident ${selectedResident.slug}`,
        external_url: `${window.location.origin}/profile`,
        simverse: {
          resident_id: selectedResident.id, slug: selectedResident.slug, district: selectedResident.district,
          status: selectedResident.status, sprite_key: selectedResident.sprite_key, star_rating: selectedResident.star_rating,
          profile: selectedResident.meta_json,
        },
      }
      const blob = new Blob([JSON.stringify(metadata, null, 2)], { type: 'application/json' })
      const content = await uploadWeb3Content(blob, `${selectedResident.slug}-metadata.json`)
      const hash = await createAgentPassport(locale, wallet, content.content_uri, content.content_hash)
      setTransaction(hash)
      await refreshAgents()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Agent creation failed')
    } finally { setBusy(null) }
  }

  const writeAnchor = async (contentFile: File | Blob, filename?: string, operation: AnchorKind = kind) => {
    if (!wallet || !agentId) return
    const content = await uploadWeb3Content(contentFile, filename)
    const id = BigInt(agentId)
    if (operation === 'training') return publishTrainingVersion(locale, wallet, id, content.content_uri, content.content_hash, content.content_hash)
    if (operation === 'memory') return anchorMemory(locale, wallet, id, content.content_uri, content.content_hash)
    return anchorSave(locale, wallet, id, content.content_uri, content.content_hash)
  }

  const anchorFile = async () => {
    if (!file) return
    setBusy('anchor'); setError(''); setTransaction(null); setRestored(false)
    try {
      const hash = await writeAnchor(file)
      setTransaction(hash || null); setFile(null); await refreshAgents()
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Anchor failed') }
    finally { setBusy(null) }
  }

  const saveGame = async () => {
    setBusy('save'); setError(''); setTransaction(null); setRestored(false)
    try {
      const snapshot = {
        schema: 'simverse-save-v1', wallet, agent_id: agentId, recorded_at: new Date().toISOString(),
        player: { sprite_key: playerSpriteKey, tile_x: playerTileX, tile_y: playerTileY },
      }
      const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' })
      const hash = await writeAnchor(blob, `simverse-save-${Date.now()}.json`, 'save')
      setTransaction(hash || null); await refreshAgents()
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Save failed') }
    finally { setBusy(null) }
  }

  const restoreGame = async () => {
    if (!wallet || !agentId) return
    setBusy('restore'); setError(''); setTransaction(null); setRestored(false)
    try {
      const anchor = await loadLatestSaveAnchor(BigInt(agentId))
      if (!anchor) throw new Error(copy.noSave)
      const raw = await readPrivateAnchoredJson<unknown>(anchor.contentURI, anchor.contentHash)
      const snapshot = validatedSave(raw, wallet, agentId, copy.invalidSave)
      await Promise.all([
        updatePlayerPosition(snapshot.player.tile_x, snapshot.player.tile_y),
        updateCharacter({ sprite_key: snapshot.player.sprite_key }),
      ])
      setPlayerTile(snapshot.player.tile_x, snapshot.player.tile_y)
      setPlayerSpriteKey(snapshot.player.sprite_key)
      setRestored(true)
    } catch (reason) { setError(reason instanceof Error ? reason.message : copy.invalidSave) }
    finally { setBusy(null) }
  }

  const syncMemory = async () => {
    if (!selectedResident || !wallet || !agentId) return
    setBusy('memory'); setError(''); setTransaction(null); setRestored(false)
    try {
      const content = await snapshotGameMemory(selectedResident.id)
      const hash = await anchorMemory(locale, wallet, BigInt(agentId), content.content_uri, content.content_hash)
      setTransaction(hash); await refreshAgents()
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Memory snapshot failed') }
    finally { setBusy(null) }
  }

  const onFile = (event: ChangeEvent<HTMLInputElement>) => setFile(event.target.files?.[0] || null)

  return (
    <div className="agent-studio-page">
      <TopNav />
      <main className="agent-studio">
        <header className="agent-studio__hero">
          <p>{copy.eyebrow}</p><h1>{copy.title}</h1><span>{copy.lead}</span>
          <div className="agent-studio__status">
            <div><span>{copy.account}</span><strong>{shortAddress(wallet)}</strong></div>
            <div><span>{copy.network}</span><strong>{configuredChainName()} · {configuredChainId()}</strong></div>
            <div><span>{copy.contract}</span><strong data-ready={contractReady}>{contractReady ? copy.connected : copy.missing}</strong></div>
          </div>
        </header>

        <div className="agent-studio__grid">
          <section className="agent-studio-card">
            <p className="agent-studio-card__index">{copy.createTitle}</p><h2>{copy.resident}</h2><span>{copy.createLead}</span>
            <label><span>{copy.resident}</span><select value={residentId} onChange={(event) => setResidentId(event.target.value)} disabled={!residents.length}>{residents.length ? residents.map((resident) => <option value={resident.id} key={resident.id}>{resident.name} / {resident.slug}</option>) : <option>{copy.noResident}</option>}</select></label>
            <button type="button" onClick={() => void createPassport()} disabled={!selectedResident || !wallet || !contractReady || busy !== null}>{busy === 'create' ? copy.creating : copy.create}</button>
          </section>

          <section className="agent-studio-card">
            <p className="agent-studio-card__index">{copy.anchorTitle}</p><h2>{copy.kind}</h2><span>{copy.anchorLead}</span>
            <div className="agent-studio-card__row">
              <label><span>{copy.agent}</span><select value={agentId} onChange={(event) => setAgentId(event.target.value)} disabled={!agents.length}>{agents.length ? agents.map((agent) => <option value={agent.id.toString()} key={agent.id.toString()}>Agent #{agent.id.toString()}</option>) : <option>{copy.empty}</option>}</select></label>
              <label><span>{copy.kind}</span><select value={kind} onChange={(event) => setKind(event.target.value as AnchorKind)}><option value="training">{copy.training}</option><option value="memory">{copy.memory}</option><option value="save">{copy.save}</option></select></label>
            </div>
            <label className="agent-studio-file"><span>{copy.file}</span><input type="file" onChange={onFile} /></label>
            <div className="agent-studio-card__actions"><button type="button" onClick={() => void anchorFile()} disabled={!file || !agentId || busy !== null}>{busy === 'anchor' ? copy.anchoring : copy.anchor}</button><button className="secondary" type="button" onClick={() => void syncMemory()} disabled={!selectedResident || !agentId || busy !== null}>{busy === 'memory' ? copy.anchoring : copy.memorySync}</button><button className="secondary" type="button" onClick={() => void saveGame()} disabled={!agentId || busy !== null}>{busy === 'save' ? copy.anchoring : copy.quickSave}</button><button className="secondary" type="button" onClick={() => void restoreGame()} disabled={!agentId || busy !== null}>{busy === 'restore' ? copy.restoring : copy.restoreSave}</button></div>
          </section>
        </div>

        {(error || transaction || restored) && <div className={`agent-studio__message ${error ? 'is-error' : 'is-success'}`} role={error ? 'alert' : 'status'}>{error || (restored ? copy.restored : `${copy.tx}: ${shortAddress(transaction)}`)}</div>}
        <p className="agent-studio__privacy">{copy.privacy}</p>

        <section className="agent-passports">
          <div className="agent-passports__heading"><h2>{copy.passports}</h2><button type="button" onClick={() => void refreshAgents()} disabled={!contractReady || busy !== null}>{copy.refresh}</button></div>
          {agents.length === 0 ? <p className="agent-passports__empty">{copy.empty}</p> : <div className="agent-passports__list">{agents.map((agent) => <article key={agent.id.toString()}><div className="agent-passport__id"><span>AGENT PASSPORT</span><strong>#{agent.id.toString()}</strong></div><dl><div><dt>{copy.version}</dt><dd>{agent.state.version.toString()}</dd></div><div><dt>{copy.memories}</dt><dd>{agent.state.memoryRevision.toString()}</dd></div><div><dt>{copy.saves}</dt><dd>{agent.state.saveRevision.toString()}</dd></div></dl><a href={agent.uri} target="_blank" rel="noreferrer">{copy.metadata} ↗</a></article>)}</div>}
        </section>
      </main>
    </div>
  )
}
