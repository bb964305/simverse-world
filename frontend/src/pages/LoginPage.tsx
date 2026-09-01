import { useEffect } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { WalletLoginButton } from '../components/WalletLoginButton'
import { safeAuthReturnTo } from '../services/authReturnTo'
import { LanguageToggle } from '../components/LanguageToggle'
import { useLocale } from '../services/locale'
import { configuredChainName } from '../services/web3/wallet'
import { BrandLogo } from '../components/BrandLogo'
import '../styles/login-page.css'

const COPY = {
  'zh-CN': {
    brandLabel: '返回 Simverse World 官网',
    back: '返回官网',
    node: '钱包身份 / 城市节点 07',
    title: <>回到一座<br />持续生活的城市。</>,
    story: '居民、关系与记忆都在继续。用钱包证明你是谁，从上次离开的地方重新进入。',
    kicker: 'WEB3 CITY ACCESS',
    heading: '连接你的链上身份',
    lead: '连接钱包并签署一次性登录消息。签名免费，不会发起交易或授权资产。',
    identity: '一个钱包就是一个持续身份',
    identityCopy: '首次连接进入三步注册：创建居民并将 Agent Passport 上链；再次连接恢复同一身份、记忆和存档。',
    storage: '内容链下保存，证明链上保存',
    storageCopy: '训练文件、记忆与存档保留原始内容；哈希、URI、版本和所有权写入可升级合约。',
    network: '目标网络',
    note: '登录签名免费；后续 Passport 注册是明确的链上交易，需要少量 ETH Gas。请只确认域名与内容均正确的请求。',
  },
  en: {
    brandLabel: 'Back to the Simverse World site',
    back: 'Back to site',
    node: 'WALLET IDENTITY / CITY NODE 07',
    title: <>Return to a city<br />that keeps living.</>,
    story: 'Residents, relationships, and memories continue. Prove your identity with your wallet and resume where you left off.',
    kicker: 'WEB3 CITY ACCESS',
    heading: 'Connect your onchain identity',
    lead: 'Connect a wallet and sign a one-time login message. Signing is free and never submits a transaction or asset approval.',
    identity: 'One wallet, one persistent identity',
    identityCopy: 'Your first connection opens a three-step registration: create a resident and mint its Agent Passport. Returning restores the same identity, memories, and saves.',
    storage: 'Content offchain, proof onchain',
    storageCopy: 'Training files, memories, and saves keep their full content while hashes, URIs, versions, and ownership live in an upgradeable contract.',
    network: 'Network',
    note: 'Login signing is free. Passport registration is a clearly labeled onchain transaction and requires a small amount of ETH for gas.',
  },
} as const

export function LoginPage() {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const [params] = useSearchParams()
  const next = safeAuthReturnTo(params.get('next'))

  useEffect(() => {
    document.body.classList.add('auth-page-open')
    return () => document.body.classList.remove('auth-page-open')
  }, [])

  return (
    <main className="auth-page">
      <img className="auth-page__backdrop" src="/marketing/world-hero.jpg" alt="" />
      <div className="auth-page__shade" />

      <header className="auth-header">
        <Link className="auth-brand" to="/" aria-label={copy.brandLabel}>
          <BrandLogo className="auth-brand__mark" size={42} eager />
          <span>SIMVERSE</span>
        </Link>
        <div className="auth-header__actions">
          <LanguageToggle className="language-toggle language-toggle--auth" />
          <Link className="auth-header__back" to="/">{copy.back}</Link>
        </div>
      </header>

      <section className="auth-story" aria-labelledby="auth-page-title">
        <p>{copy.node}</p>
        <h1 id="auth-page-title">{copy.title}</h1>
        <span>{copy.story}</span>
      </section>

      <section className="auth-panel" aria-labelledby="auth-form-title">
        <div className="auth-panel__heading">
          <p>{copy.kicker}</p>
          <h2 id="auth-form-title">{copy.heading}</h2>
          <span>{copy.lead}</span>
        </div>

        <div className="wallet-benefits">
          <article>
            <span aria-hidden="true">01</span>
            <div><strong>{copy.identity}</strong><p>{copy.identityCopy}</p></div>
          </article>
          <article>
            <span aria-hidden="true">02</span>
            <div><strong>{copy.storage}</strong><p>{copy.storageCopy}</p></div>
          </article>
        </div>

        <div className="wallet-network">
          <span>{copy.network}</span>
          <strong><i aria-hidden="true" />{configuredChainName()}</strong>
        </div>

        <WalletLoginButton next={next} />
        <p className="auth-panel__note">{copy.note}</p>
      </section>
    </main>
  )
}
