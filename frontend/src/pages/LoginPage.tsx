import { useEffect } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { WalletLoginButton } from '../components/WalletLoginButton'
import { safeAuthReturnTo } from '../services/authReturnTo'
import { LanguageToggle } from '../components/LanguageToggle'
import { useLocale } from '../services/locale'
import { configuredChainName } from '../services/web3/wallet'
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
    identityCopy: '首次连接自动创建城市身份；再次连接会恢复同一位居民、记忆和存档。',
    storage: '内容链下保存，证明链上保存',
    storageCopy: '训练文件、记忆与存档保留原始内容；哈希、URI、版本和所有权写入可升级合约。',
    network: '目标网络',
    note: '首次进入后将继续完成居民创建与世界引导。请只签署域名与内容均正确的登录消息。',
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
    identityCopy: 'Your first connection creates a city identity. Returning with the same wallet restores your residents, memories, and saves.',
    storage: 'Content offchain, proof onchain',
    storageCopy: 'Training files, memories, and saves keep their full content while hashes, URIs, versions, and ownership live in an upgradeable contract.',
    network: 'Network',
    note: 'New players continue to resident creation and world onboarding. Only sign when the domain and message are correct.',
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
          <span className="auth-brand__mark" aria-hidden="true">S/</span>
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
