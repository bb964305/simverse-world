import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../services/api/core'
import { useLocale } from '../services/locale'
import { loadOwnedAgents, registryConfigured } from '../services/web3/agentRegistry'
import { configuredChainName } from '../services/web3/wallet'
import { useGameStore } from '../stores/gameStore'
import { BrandLogo } from './BrandLogo'
import { BrandSocialLinks } from './BrandSocialLinks'

const COPY = {
  en: { label: 'Web3 command deck', passport: 'Agent Passport', runtime: 'World runtime', online: 'ONLINE', checking: 'CHECKING', offline: 'CHECK', unregistered: 'NOT REGISTERED', studio: 'Agent Studio', guide: 'New player guide', economy: 'SIM economy', community: 'Official community', close: 'Collapse Web3 deck', open: 'Open Web3 deck' },
  'zh-CN': { label: 'Web3 控制台', passport: 'Agent Passport', runtime: '世界运行', online: '在线', checking: '检测中', offline: '待检测', unregistered: '尚未注册', studio: '链上工作台', guide: '新手教程', economy: 'SIM 经济模型', community: '官方社区', close: '收起 Web3 控制台', open: '打开 Web3 控制台' },
} as const

export function WorldWeb3Dock() {
  const navigate = useNavigate()
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const wallet = useGameStore((state) => state.user?.wallet_address) as `0x${string}` | undefined
  const [open, setOpen] = useState(true)
  const [runtimeOnline, setRuntimeOnline] = useState<boolean | null>(null)
  const [agentCount, setAgentCount] = useState<number | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const runtime = fetch(`${API_BASE}/health/loops`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) return false
        const body = await response.json() as { status?: string; loops?: { agent?: { state?: string } } }
        return body.status === 'ok' && body.loops?.agent?.state === 'ok'
      })
      .catch(() => false)
    const agents = wallet && registryConfigured() ? loadOwnedAgents(wallet).then((items) => items.length).catch(() => null) : Promise.resolve(null)
    void Promise.all([runtime, agents]).then(([online, count]) => {
      if (!controller.signal.aborted) { setRuntimeOnline(online); setAgentCount(count) }
    })
    return () => controller.abort()
  }, [wallet])

  if (!open) return <button className="world-web3-dock__opener" type="button" onClick={() => setOpen(true)} aria-label={copy.open}><BrandLogo size={44} /><span>WEB3</span></button>

  return (
    <aside className="world-web3-dock" aria-label={copy.label}>
      <header><BrandLogo size={42} /><div><strong>SIMVERSE / WEB3</strong><span>{configuredChainName()}</span></div><button type="button" onClick={() => setOpen(false)} aria-label={copy.close}>×</button></header>
      <div className="world-web3-dock__status"><div><span>{copy.passport}</span><strong>{agentCount === null ? '—' : agentCount > 0 ? `× ${agentCount}` : copy.unregistered}</strong></div><div><span>{copy.runtime}</span><strong data-online={runtimeOnline === true}><i />{runtimeOnline === null ? copy.checking : runtimeOnline ? copy.online : copy.offline}</strong></div></div>
      <nav><button type="button" onClick={() => navigate('/web3')}>{copy.studio}<span>↗</span></button><button type="button" onClick={() => navigate('/guide')}>{copy.guide}<span>↗</span></button><button type="button" onClick={() => navigate('/economy')}>{copy.economy}<span>↗</span></button></nav>
      <footer><span>{copy.community}</span><BrandSocialLinks /></footer>
    </aside>
  )
}
