import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { BrandSocialLinks } from '../components/BrandSocialLinks'
import { LanguageToggle } from '../components/LanguageToggle'
import { AGENT_REGISTRY_ADDRESS } from '../services/web3/agentRegistry'
import { checkProductionConnectivity, type ConnectivityItem, type ConnectivityReport } from '../services/web3/connectivity'
import { configuredChainId, configuredChainName, configuredRpcUrl } from '../services/web3/wallet'
import { useLocale } from '../services/locale'
import '../styles/web3-guide.css'

const EXPLORER = 'https://robinhoodchain.blockscout.com'

const COPY = {
  'zh-CN': {
    back: '返回官网', connect: '连接钱包开始', eyebrow: 'OFFICIAL PLAYBOOK / LIVE VERIFIED', title: '从钱包到链上居民，真实走通。',
    lead: '这不是概念说明。按下面顺序操作，你会完成钱包签名登录、创建游戏居民、铸造不可转让的 Agent Passport，并把训练、记忆和存档真实锚定到 Robinhood Chain。',
    liveTitle: '实时通信自检', liveLead: '页面直接检查生产 API、Robinhood Chain RPC 和已部署 UUPS Proxy。三项全绿才表示官网链路完整。',
    browser: '官网页面', api: '游戏 API', chain: 'Robinhood Chain', contract: 'Agent Registry', online: '已连接', checking: '检测中…', retry: '重新检测', notChecked: '等待检测',
    stepsTitle: '真实使用流程', factsTitle: '链上与链下分别保存什么', networkTitle: '正式网络参数', safetyTitle: '签名与 Gas 安全',
    steps: [
      ['01', '安装并准备钱包', '安装 Rabby Wallet，或使用 RabbyKit 列出的兼容 EVM 钱包。钱包中准备少量 Robinhood Chain ETH 作为创建 Passport 与保存版本的 Gas。'],
      ['02', '连接钱包并签名登录', '点击“连接钱包开始”。首次进入会要求签署一次登录消息；它只证明地址所有权，不发送交易、不扣 Gas。'],
      ['03', '创建你的游戏居民', '首次登录会进入角色创建。选择名称、形象和人物设定后进入小镇；移动、聊天、锻造与原游戏玩法保持一致。'],
      ['04', '创建链上 Agent 身份', '打开“Agent Studio”，上传身份描述并确认交易。系统会铸造一个不可转让的 Agent Passport，它永久归当前钱包所有。'],
      ['05', '训练与上传确权', '上传训练产物或新版本。完整文件进入钱包隔离的私有内容服务，SHA-256、版本号和内容 URI 写入可升级合约。'],
      ['06', '链上记忆、保存与恢复', '同步居民记忆或保存当前游戏状态，再由钱包确认上链。恢复时网页重新下载内容并核对链上哈希，校验通过才恢复角色与坐标。'],
    ],
    storage: [
      ['链上', '钱包所有权、Agent ID、训练版本、内容哈希、记忆/存档修订号、时间戳与父哈希。'],
      ['私有内容服务', '训练文件、完整记忆和游戏存档。下载必须携带同一钱包签名产生的短期会话。'],
      ['浏览器钱包', '私钥与交易确认。Simverse、后端和合约都不会读取或保存私钥。'],
    ],
    safety: ['登录签名不消耗 Gas；创建身份、训练确权、记忆和存档上链会消耗少量 ETH。', '只确认域名为 simverse.space，签名消息中的 URI 与 Chain ID 必须匹配。', '不要把助记词或私钥上传到训练区，也不要发送给任何管理员。'],
    openStudio: '登录后打开 Agent Studio', explorer: '查看主网合约', social: '官方社区', checked: '检测时间',
  },
  en: {
    back: 'Back to site', connect: 'Connect wallet & start', eyebrow: 'OFFICIAL PLAYBOOK / LIVE VERIFIED', title: 'From wallet to onchain resident—end to end.',
    lead: 'This is an executable guide. Follow it to sign in with a wallet, create a game resident, mint a soulbound Agent Passport, and anchor training, memories, and saves on Robinhood Chain.',
    liveTitle: 'Live connectivity check', liveLead: 'This page directly checks the production API, Robinhood Chain RPC, and deployed UUPS proxy. All three must be green for an end-to-end connection.',
    browser: 'Website', api: 'Game API', chain: 'Robinhood Chain', contract: 'Agent Registry', online: 'Online', checking: 'Checking…', retry: 'Check again', notChecked: 'Not checked',
    stepsTitle: 'Real usage flow', factsTitle: 'What lives onchain vs offchain', networkTitle: 'Production network', safetyTitle: 'Signature & gas safety',
    steps: [
      ['01', 'Prepare a wallet', 'Install Rabby Wallet or another EVM wallet shown by RabbyKit. Keep a small amount of Robinhood Chain ETH for Passport and version-anchor gas.'],
      ['02', 'Connect and sign in', 'Select “Connect wallet & start”. The first prompt signs a login message proving address ownership; it sends no transaction and costs no gas.'],
      ['03', 'Create your game resident', 'First-time players choose a name, sprite, and character profile before entering town. Movement, chat, forging, and the existing game remain intact.'],
      ['04', 'Create an onchain Agent identity', 'Open Agent Studio, upload the identity description, and confirm the transaction. A non-transferable Agent Passport is minted to the connected wallet.'],
      ['05', 'Prove training and uploads', 'Upload a training artifact or version. The private content service stores the full file while its SHA-256, version, and URI are written to the upgradeable contract.'],
      ['06', 'Anchor and restore memory or saves', 'Sync resident memory or save the current game, then confirm onchain. Restore downloads the content and verifies its hash against the contract before applying appearance and position.'],
    ],
    storage: [
      ['Onchain', 'Wallet ownership, Agent ID, training versions, hashes, memory/save revisions, timestamps, and parent hashes.'],
      ['Private content service', 'Training files, full memories, and game saves. Downloads require a short session created by the same wallet signature.'],
      ['Your wallet', 'Private keys and transaction approval. Simverse, its API, and the contract never read or store your private key.'],
    ],
    safety: ['Login signatures cost no gas; identity creation, training proofs, memories, and saves use a small amount of ETH.', 'Confirm the domain is simverse.space and the signature URI and Chain ID match.', 'Never upload a seed phrase or private key as training data or send it to an administrator.'],
    openStudio: 'Open Agent Studio after sign-in', explorer: 'View mainnet contract', social: 'Official community', checked: 'Checked',
  },
} as const

function StatusCard({ label, item, checking }: { label: string; item?: ConnectivityItem; checking: boolean }) {
  const status = checking ? 'checking' : item?.ok ? 'ok' : item ? 'error' : 'idle'
  return <div className="guide-status" data-status={status}><span className="guide-status__dot" aria-hidden="true" /><div><strong>{label}</strong><small>{checking ? '…' : item?.detail ?? '—'}</small></div></div>
}

export function Web3GuidePage() {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const [report, setReport] = useState<ConnectivityReport | null>(null)
  const [checking, setChecking] = useState(false)

  const runCheck = useCallback(async (signal?: AbortSignal) => {
    setChecking(true)
    try { setReport(await checkProductionConnectivity(signal)) } finally { setChecking(false) }
  }, [])

  useEffect(() => {
    document.body.classList.add('guide-page-open')
    const controller = new AbortController()
    void runCheck(controller.signal)
    return () => { controller.abort(); document.body.classList.remove('guide-page-open') }
  }, [runCheck])

  return (
    <div className="web3-guide">
      <header className="guide-header">
        <Link className="guide-brand" to="/"><span aria-hidden="true">S/</span> SIMVERSE</Link>
        <nav aria-label="Guide navigation"><Link to="/">{copy.back}</Link><BrandSocialLinks className="brand-socials--guide" /><LanguageToggle /></nav>
      </header>

      <main>
        <section className="guide-hero">
          <p>{copy.eyebrow}</p><h1>{copy.title}</h1><div className="guide-hero__lead">{copy.lead}</div>
          <div className="guide-actions"><Link className="guide-button guide-button--primary" to="/login">{copy.connect}</Link><a className="guide-button" href={`${EXPLORER}/address/${AGENT_REGISTRY_ADDRESS}`} target="_blank" rel="noreferrer">{copy.explorer}</a></div>
        </section>

        <section className="guide-live" aria-labelledby="live-check-title">
          <div><p className="guide-kicker">LIVE / 01</p><h2 id="live-check-title">{copy.liveTitle}</h2><p>{copy.liveLead}</p></div>
          <div className="guide-live__panel" aria-live="polite">
            <StatusCard label={copy.browser} item={{ ok: true, detail: `${window.location.host} · ${copy.online}` }} checking={false} />
            <StatusCard label={copy.api} item={report?.api} checking={checking} />
            <StatusCard label={copy.chain} item={report?.chain} checking={checking} />
            <StatusCard label={copy.contract} item={report?.contract} checking={checking} />
            <div className="guide-live__footer"><button type="button" onClick={() => void runCheck()} disabled={checking}>{checking ? copy.checking : copy.retry}</button><small>{report ? `${copy.checked}: ${new Date(report.checkedAt).toLocaleTimeString(locale)}` : copy.notChecked}</small></div>
          </div>
        </section>

        <section className="guide-steps" aria-labelledby="steps-title"><p className="guide-kicker">PLAYBOOK / 02</p><h2 id="steps-title">{copy.stepsTitle}</h2><ol>{copy.steps.map(([number, title, body]) => <li key={number}><span>{number}</span><div><h3>{title}</h3><p>{body}</p></div></li>)}</ol></section>

        <section className="guide-facts">
          <div><p className="guide-kicker">DATA / 03</p><h2>{copy.factsTitle}</h2><div className="guide-storage">{copy.storage.map(([title, body]) => <article key={title}><h3>{title}</h3><p>{body}</p></article>)}</div></div>
          <aside><p className="guide-kicker">NETWORK</p><h2>{copy.networkTitle}</h2><dl><div><dt>Network</dt><dd>{configuredChainName()}</dd></div><div><dt>Chain ID</dt><dd>{configuredChainId()}</dd></div><div><dt>RPC</dt><dd>{configuredRpcUrl()}</dd></div><div><dt>UUPS Proxy</dt><dd>{AGENT_REGISTRY_ADDRESS}</dd></div><div><dt>Gas</dt><dd>ETH</dd></div></dl></aside>
        </section>

        <section className="guide-safety"><div><p className="guide-kicker">SAFETY / 04</p><h2>{copy.safetyTitle}</h2></div><ul>{copy.safety.map((item) => <li key={item}>{item}</li>)}</ul></section>

        <section className="guide-cta"><h2>{copy.connect}</h2><div className="guide-actions"><Link className="guide-button guide-button--primary" to="/login">{copy.connect}</Link><Link className="guide-button" to="/web3">{copy.openStudio}</Link></div><p>{copy.social}</p><BrandSocialLinks className="brand-socials--guide-cta" /></section>
      </main>
    </div>
  )
}
