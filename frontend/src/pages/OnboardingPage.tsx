import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'
import { BrandSocialLinks } from '../components/BrandSocialLinks'
import { LanguageToggle } from '../components/LanguageToggle'
import { staticResidentSpriteUrl } from '../game/residentSpriteRuntime'
import {
  checkOnboarding,
  createPlayerResident,
  getResidents,
  getSpriteTemplates,
  skipOnboarding,
} from '../services/api'
import type { OnboardingResidentResponse, ResidentListItem, SpriteTemplate } from '../services/api'
import { API_BASE } from '../services/api/core'
import { loginPath, safeAuthReturnTo } from '../services/authReturnTo'
import { useLocale } from '../services/locale'
import { loadOwnedAgents, registryConfigured } from '../services/web3/agentRegistry'
import { checkAgentRuntime } from '../services/web3/connectivity'
import { registerResidentOnchain, type PassportResident } from '../services/web3/passport'
import { configuredChainId, configuredChainName } from '../services/web3/wallet'
import { useGameStore } from '../stores/gameStore'
import '../styles/onboarding-page.css'

const EXPLORER = 'https://robinhoodchain.blockscout.com'

interface PresetCard {
  slug: string
  name: string
  district: string
  sprite_key: string
  vibe?: string
  tags?: string[]
}

type Phase = 'loading' | 'resident' | 'passport' | 'ready'

const COPY = {
  en: {
    back: 'Back to site', guide: 'Live guide', economy: 'SIM economy', eyebrow: 'ONCHAIN CITY REGISTRATION', title: 'Create your persistent identity.',
    lead: 'Registration now finishes both layers: a playable resident in the world and a soulbound Agent Passport owned by your wallet.',
    steps: ['Wallet verified', 'Resident created', 'Passport onchain'],
    wallet: 'Wallet', network: 'Network', residentTitle: 'Choose how you enter the world', residentLead: 'Pick a visual origin and give your resident a unique public name. You can deepen abilities and personality later in the Forge.',
    name: 'Resident name', namePlaceholder: 'e.g. Nova, Ash, Zero…', visual: 'Visual origin', selected: 'Selected', create: 'Create resident', creating: 'Creating resident…', starter: 'Use a starter identity', starterBusy: 'Preparing starter…',
    passportTitle: 'Register your Agent Passport', passportLead: 'This final step uploads public identity metadata, then asks your wallet to write its hash and ownership to the upgradeable registry on Robinhood Chain.',
    chainWrites: ['Soulbound ownership', 'Resident metadata hash', 'Creation time and Agent ID'], chainKeeps: 'No token approval. No asset transfer. Only network gas is required.',
    mint: 'Register identity onchain', minting: 'Waiting for wallet & confirmation…', existing: 'Existing Agent Passport found', existingLead: 'This wallet already owns an onchain Agent identity. You can enter now and manage every version in Agent Studio.',
    readyTitle: 'Identity registered. The city is awake.', readyLead: 'Your resident can now play, train, upload versions, anchor memories, save state, and remain part of the running world.', enter: 'Enter Simverse World', studio: 'Open Agent Studio', transaction: 'View registration transaction',
    loading: 'Checking wallet, resident, contract, and live network…', retry: 'Try again', defaultError: 'Registration could not be completed.', nameError: 'Use a name between 2 and 40 characters.', contractMissing: 'The Agent Registry is not configured.', runtime: 'World Agent loop', runtimeChecking: 'Checking…', runtimeOnline: 'Online 24/7', runtimeOffline: 'Unavailable',
  },
  'zh-CN': {
    back: '返回官网', guide: '实时教程', economy: 'SIM 经济模型', eyebrow: '链上城市注册', title: '创建你的持续身份。',
    lead: '注册现在会完成两层身份：世界中可游玩的居民，以及由当前钱包拥有的不可转让 Agent Passport。',
    steps: ['钱包已验证', '创建居民', '身份上链'],
    wallet: '钱包', network: '网络', residentTitle: '选择进入世界的方式', residentLead: '选择视觉原型并填写唯一公开名称。能力与人格之后还可以在炼化工坊继续完善。',
    name: '居民名称', namePlaceholder: '例如：Nova、阿零、赛博旅人…', visual: '视觉原型', selected: '已选择', create: '创建居民', creating: '正在创建居民…', starter: '使用新手身份', starterBusy: '正在准备新手身份…',
    passportTitle: '登记 Agent Passport', passportLead: '最后一步先上传公开身份元数据，再由钱包把哈希和所有权写入 Robinhood Chain 上的可升级注册合约。',
    chainWrites: ['不可转让的钱包所有权', '居民元数据哈希', '创建时间与 Agent ID'], chainKeeps: '不会授权代币，不会转移资产，只需支付网络 Gas。',
    mint: '将身份登记上链', minting: '等待钱包与链上确认…', existing: '已找到 Agent Passport', existingLead: '这个钱包已经拥有链上 Agent 身份，现在可直接进入，并在 Agent Studio 管理所有版本。',
    readyTitle: '身份登记完成，城市已经醒来。', readyLead: '居民现在可以游玩、训练、上传版本、锚定记忆、保存状态，并持续存在于运行中的世界。', enter: '进入 Simverse World', studio: '打开 Agent Studio', transaction: '查看注册交易',
    loading: '正在检查钱包、居民、合约与实时网络…', retry: '重新尝试', defaultError: '注册未能完成。', nameError: '名称长度需为 2 到 40 个字符。', contractMissing: 'Agent Registry 尚未配置。', runtime: '世界 Agent 循环', runtimeChecking: '检测中…', runtimeOnline: '24/7 在线', runtimeOffline: '暂不可用',
  },
} as const

function shortAddress(value: string | null | undefined) {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '—'
}

async function loadPlayerResident(token: string, residentId?: string | null): Promise<PassportResident | null> {
  const response = await fetch(`${API_BASE}/profile/residents`, { headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok) throw new Error(`Residents API ${response.status}`)
  const residents = await response.json() as PassportResident[]
  return residents.find((item) => item.id === residentId) ?? residents[0] ?? null
}

function asPassportResident(resident: OnboardingResidentResponse): PassportResident {
  return { ...resident, district: 'free', status: 'idle', star_rating: 0, meta_json: { origin: 'onboarding' } }
}

export function OnboardingPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const next = safeAuthReturnTo(params.get('next'))
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const token = useGameStore((state) => state.token)
  const user = useGameStore((state) => state.user)
  const wallet = user?.wallet_address as `0x${string}` | undefined
  const [phase, setPhase] = useState<Phase>('loading')
  const [presets, setPresets] = useState<PresetCard[]>([])
  const [selectedSlug, setSelectedSlug] = useState('')
  const [name, setName] = useState('')
  const [resident, setResident] = useState<PassportResident | null>(null)
  const [agentId, setAgentId] = useState<string | null>(null)
  const [transaction, setTransaction] = useState<`0x${string}` | null>(null)
  const [busy, setBusy] = useState<'resident' | 'starter' | 'passport' | null>(null)
  const [error, setError] = useState('')
  const [runtimeOnline, setRuntimeOnline] = useState<boolean | null>(null)

  const selectedPreset = useMemo(() => presets.find((preset) => preset.slug === selectedSlug) ?? presets[0], [presets, selectedSlug])

  const initialize = useCallback(async () => {
    if (!token) { navigate(loginPath(next), { replace: true }); return }
    setPhase('loading'); setError('')
    try {
      const check = await checkOnboarding(token)
      const agentsPromise = wallet && registryConfigured() ? loadOwnedAgents(wallet).catch(() => []) : Promise.resolve([])
      if (!check.needs_onboarding) {
        const [player, agents] = await Promise.all([loadPlayerResident(token, check.player_resident_id), agentsPromise])
        setResident(player)
        if (agents.length > 0) { setAgentId(agents[0].id.toString()); setPhase(player ? 'ready' : 'resident') }
        else setPhase(player ? 'passport' : 'resident')
        return
      }
      const [residents, templates, agents] = await Promise.all([getResidents(), getSpriteTemplates(), agentsPromise])
      const templateMap = new Map<string, SpriteTemplate>(templates.map((template) => [template.key, template]))
      const cards = residents.filter((item: ResidentListItem) => item.meta_json?.origin === 'preset').map((item) => ({
        slug: item.slug, name: item.name, district: item.district, sprite_key: item.sprite_key,
        vibe: templateMap.get(item.sprite_key)?.vibe, tags: templateMap.get(item.sprite_key)?.tags,
      }))
      setPresets(cards); setSelectedSlug((current) => current || cards[0]?.slug || '')
      if (agents.length > 0) setAgentId(agents[0].id.toString())
      setPhase('resident')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : copy.defaultError)
      setPhase('resident')
    }
  }, [copy.defaultError, navigate, next, token, wallet])

  useEffect(() => { void initialize() }, [initialize])

  useEffect(() => {
    const controller = new AbortController()
    void checkAgentRuntime(controller.signal).then((status) => setRuntimeOnline(status.ok)).catch(() => setRuntimeOnline(false))
    return () => controller.abort()
  }, [])

  const createResident = async () => {
    if (!token || !selectedPreset || busy) return
    const cleanName = name.trim()
    if (cleanName.length < 2 || cleanName.length > 40) { setError(copy.nameError); return }
    setBusy('resident'); setError('')
    try {
      const created = await createPlayerResident(token, { name: cleanName, sprite_key: selectedPreset.sprite_key })
      setResident(asPassportResident(created)); setPhase('passport')
    } catch (reason) { setError(reason instanceof Error ? reason.message : copy.defaultError) }
    finally { setBusy(null) }
  }

  const createStarter = async () => {
    if (!token || busy) return
    setBusy('starter'); setError('')
    try { const created = await skipOnboarding(token); setResident(asPassportResident(created)); setPhase('passport') }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.defaultError) }
    finally { setBusy(null) }
  }

  const mintPassport = async () => {
    if (!wallet || !resident || busy) return
    if (!registryConfigured()) { setError(copy.contractMissing); return }
    setBusy('passport'); setError('')
    try {
      const hash = await registerResidentOnchain(locale, wallet, resident)
      setTransaction(hash)
      const agents = await loadOwnedAgents(wallet)
      setAgentId(agents[0]?.id.toString() ?? null)
      setPhase('ready')
    } catch (reason) { setError(reason instanceof Error ? reason.message : copy.defaultError) }
    finally { setBusy(null) }
  }

  const completedStep = phase === 'ready' ? 3 : phase === 'passport' ? 2 : 1

  return (
    <div className="onchain-onboarding">
      <header className="onboarding-header">
        <Link className="onboarding-brand" to="/"><BrandLogo size={44} eager /><span>SIMVERSE</span></Link>
        <nav><Link to="/">{copy.back}</Link><Link to="/guide">{copy.guide}</Link><Link to="/economy">{copy.economy}</Link><BrandSocialLinks /><LanguageToggle /></nav>
      </header>

      <main className="onboarding-shell">
        <section className="onboarding-intro">
          <p className="onboarding-kicker">{copy.eyebrow}</p><h1>{copy.title}</h1><p>{copy.lead}</p>
          <div className="onboarding-identity"><div><span>{copy.wallet}</span><strong>{shortAddress(wallet)}</strong></div><div><span>{copy.network}</span><strong>{configuredChainName()} · {configuredChainId()}</strong></div><div><span>{copy.runtime}</span><strong className={runtimeOnline ? 'is-online' : ''}><i />{runtimeOnline === null ? copy.runtimeChecking : runtimeOnline ? copy.runtimeOnline : copy.runtimeOffline}</strong></div></div>
        </section>

        <ol className="onboarding-progress">{copy.steps.map((step, index) => <li data-state={index + 1 < completedStep ? 'done' : index + 1 === completedStep ? 'active' : 'next'} key={step}><span>{index + 1 < completedStep ? '✓' : `0${index + 1}`}</span><strong>{step}</strong></li>)}</ol>

        {phase === 'loading' && <section className="onboarding-panel onboarding-loading"><BrandLogo size={86} /><p>{copy.loading}</p></section>}

        {phase === 'resident' && <section className="onboarding-panel"><div className="onboarding-panel__heading"><p>STEP 02 / RESIDENT</p><h2>{copy.residentTitle}</h2><span>{copy.residentLead}</span></div><label className="onboarding-name"><span>{copy.name}</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder={copy.namePlaceholder} maxLength={40} autoComplete="nickname" /></label><div className="onboarding-visual-label">{copy.visual}</div><div className="onboarding-presets">{presets.slice(0, 8).map((preset) => <button type="button" data-selected={selectedPreset?.slug === preset.slug} onClick={() => setSelectedSlug(preset.slug)} key={preset.slug}><span className="onboarding-sprite"><i style={{ backgroundImage: `url(${staticResidentSpriteUrl(preset.sprite_key)})` }} /></span><strong>{preset.name}</strong><small>{preset.vibe || preset.district}{preset.tags?.[0] ? ` · ${preset.tags[0]}` : ''}</small><em>{selectedPreset?.slug === preset.slug ? copy.selected : ''}</em></button>)}</div><div className="onboarding-actions"><button className="onboarding-primary" type="button" onClick={() => void createResident()} disabled={!selectedPreset || busy !== null}>{busy === 'resident' ? copy.creating : copy.create}</button><button className="onboarding-secondary" type="button" onClick={() => void createStarter()} disabled={busy !== null}>{busy === 'starter' ? copy.starterBusy : copy.starter}</button></div></section>}

        {phase === 'passport' && resident && <section className="onboarding-panel onboarding-passport"><div className="onboarding-panel__heading"><p>STEP 03 / PASSPORT</p><h2>{copy.passportTitle}</h2><span>{copy.passportLead}</span></div><div className="passport-preview"><BrandLogo size={104} /><div><span>SIMVERSE AGENT PASSPORT</span><strong>{resident.name}</strong><small>{resident.slug}</small></div><b>SOULBOUND</b></div><ul>{copy.chainWrites.map((item) => <li key={item}><span>✓</span>{item}</li>)}</ul><p className="passport-gas">{copy.chainKeeps}</p><div className="onboarding-actions"><button className="onboarding-primary" type="button" onClick={() => void mintPassport()} disabled={!wallet || busy !== null}>{busy === 'passport' ? copy.minting : copy.mint}</button></div></section>}

        {phase === 'ready' && <section className="onboarding-panel onboarding-ready"><BrandLogo size={118} /><p>AGENT PASSPORT {agentId ? `#${agentId}` : '✓'}</p><h2>{agentId && !transaction ? copy.existing : copy.readyTitle}</h2><span>{agentId && !transaction ? copy.existingLead : copy.readyLead}</span>{transaction && <a href={`${EXPLORER}/tx/${transaction}`} target="_blank" rel="noreferrer">{copy.transaction} ↗</a>}<div className="onboarding-actions"><button className="onboarding-primary" type="button" onClick={() => navigate(next, { replace: true })}>{copy.enter}</button><button className="onboarding-secondary" type="button" onClick={() => navigate('/web3')}>{copy.studio}</button></div></section>}

        {error && <div className="onboarding-error" role="alert"><span>{error}</span><button type="button" onClick={() => void initialize()}>{copy.retry}</button></div>}
      </main>
    </div>
  )
}
