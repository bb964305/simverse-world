import { Link } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'
import { BrandSocialLinks } from '../components/BrandSocialLinks'
import { LanguageToggle } from '../components/LanguageToggle'
import { useLocale } from '../services/locale'
import '../styles/economy-page.css'

const COPY = {
  en: {
    back: 'World', guide: 'How to play', enter: 'Enter world', status: 'DESIGN DRAFT / TOKEN NOT LAUNCHED',
    title: <>One world.<br />One living currency.</>,
    lead: 'SIM is planned as the settlement and utility token for creation, autonomous Agent services, world commerce, and community coordination across Simverse World.',
    warningTitle: 'No SIM contract or official token address exists yet.',
    warning: 'SIM will be issued later through the selected launchpad. Until an address is published on this page and the official social accounts, every token using the SIM name should be treated as unofficial.',
    facts: [['Ticker', 'SIM (planned)'], ['Network', 'Robinhood Chain'], ['Supply', 'TBD at launch'], ['Token address', 'Not deployed']],
    loopTitle: 'The world value loop',
    loop: [
      ['01', 'Create', 'Players forge residents, publish skills, train Agents, and make useful world assets.'],
      ['02', 'Use', 'SIM pays for scarce world services: compute, premium creation, marketplace settlement, and autonomous runtime.'],
      ['03', 'Earn', 'Creators and service providers can receive transparent protocol-defined shares when their work is used.'],
      ['04', 'Recycle', 'Protocol fees return to rewards, operations, liquidity, and verifiable token sinks instead of disappearing into an opaque ledger.'],
    ],
    utilityTitle: 'Planned utility',
    utility: [
      ['Agent compute', 'Fund hosted Agent runtime, training jobs, memory processing, and higher service limits.'],
      ['World creation', 'Forge premium residents, mint scarce cosmetics, publish skill packs, and register upgraded artifacts.'],
      ['Commerce', 'Settle player-to-player items, Agent services, creator commissions, and event access.'],
      ['Coordination', 'Stake or lock SIM for reputation-weighted proposals, curation, and community programs after governance is activated.'],
    ],
    sinksTitle: 'Sinks and burn policy',
    sinksLead: 'A sink is only activated when it pays for real utility. The final percentages are intentionally unset until launch liquidity, usage, and legal constraints are known.',
    sinks: [
      ['Creation burn', 'A published portion of premium forge and cosmetic fees can be permanently burned.'],
      ['Marketplace burn', 'A defined share of settlement fees can be burned while the remainder funds creators and world operations.'],
      ['Agent runtime sink', 'Compute spending is split between infrastructure/providers and a protocol sink; no fake yield is created.'],
      ['Recovery sink', 'Optional rename, reset, and high-cost state recovery actions can consume SIM to discourage spam.'],
    ],
    tradeTitle: 'Trading and liquidity',
    trade: 'Initial distribution and liquidity will be created through the chosen launchpad. After launch, the website will display only verified contract and pool links. Simverse does not promise price, yield, buybacks, or guaranteed liquidity.',
    rolloutTitle: 'Release gates',
    rollout: [
      ['NOW', 'Identity first', 'Wallet login, soulbound Agent Passport, training proofs, memory anchors, and save anchors are live without a token.'],
      ['NEXT', 'SIM launch', 'Publish audited token address, supply, launchpad parameters, treasury controls, and verified liquidity links.'],
      ['THEN', 'Utility activation', 'Enable one utility at a time behind transparent pricing, receipts, limits, and safety switches.'],
      ['LATER', 'Governance', 'Introduce coordination only after meaningful usage exists and control boundaries are documented.'],
    ],
    footer: 'This page describes product design, not an investment offer. Parameters marked TBD are not commitments.',
  },
  'zh-CN': {
    back: '世界', guide: '使用教程', enter: '进入世界', status: '设计草案 / 代币尚未发行',
    title: <>一个世界。<br />一种持续流通的货币。</>,
    lead: 'SIM 规划为 Simverse World 的结算与效用代币，用于创作、自主 Agent 服务、世界商业与社区协作。',
    warningTitle: '目前不存在 SIM 合约或官方代币地址。',
    warning: 'SIM 将在确定的发射台后续发行。在本页与官方社交账号同时公布地址之前，任何使用 SIM 名称的代币都应视为非官方资产。',
    facts: [['代币符号', 'SIM（规划中）'], ['网络', 'Robinhood Chain'], ['供应量', '发行时确定'], ['代币地址', '尚未部署']],
    loopTitle: '世界价值循环',
    loop: [
      ['01', '创造', '玩家锻造居民、发布技能、训练 Agent，并制作真正有用的世界资产。'],
      ['02', '使用', 'SIM 用于稀缺服务：算力、高级创作、市场结算和自主运行。'],
      ['03', '获得', '创作者与服务提供者的作品被使用时，可按公开规则获得分成。'],
      ['04', '回流', '协议费用回到奖励、运营、流动性与可验证销毁，而不是进入不透明账本。'],
    ],
    utilityTitle: '规划中的效用',
    utility: [
      ['Agent 算力', '支付托管 Agent 挂机、训练任务、记忆处理与更高服务额度。'],
      ['世界创作', '锻造高级居民、铸造稀有外观、发布技能包与登记升级产物。'],
      ['世界商业', '结算玩家物品、Agent 服务、创作者佣金和活动门票。'],
      ['社区协作', '治理启用后，通过质押或锁定参与提案、策展和社区计划。'],
    ],
    sinksTitle: '消耗与销毁机制',
    sinksLead: '只有真实效用才会启用消耗。最终比例必须等发行流动性、真实使用和合规边界明确后再公开确定。',
    sinks: [
      ['创作销毁', '高级锻造与外观费用中按公开比例永久销毁。'],
      ['交易销毁', '市场手续费一部分销毁，其余用于创作者分成与世界运营。'],
      ['挂机消耗', '算力费用拆分给基础设施/模型提供方与协议消耗，不制造虚假收益。'],
      ['恢复消耗', '改名、重置和高成本状态恢复可消耗 SIM，用来抑制垃圾行为。'],
    ],
    tradeTitle: '交易与流动性',
    trade: '初始分发与流动性将通过选定发射台完成。发行后官网只展示已验证的合约与池子链接。Simverse 不承诺价格、收益、回购或保证流动性。',
    rolloutTitle: '分阶段上线门槛',
    rollout: [
      ['现在', '先做身份', '钱包登录、不可转让 Agent Passport、训练证明、记忆与存档锚定已经可用，不依赖代币。'],
      ['下一步', '发行 SIM', '公布审计后的代币地址、供应、发射台参数、金库权限与验证后的流动性链接。'],
      ['随后', '逐项启用效用', '每项功能独立上线，并配套公开定价、收据、额度与安全开关。'],
      ['以后', '治理', '只有形成真实使用后才开启协作治理，并先公开控制边界。'],
    ],
    footer: '本页描述的是产品设计，不构成投资要约。标记为待定的参数不构成承诺。',
  },
} as const

export function EconomyPage() {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]

  return (
    <div className="sim-economy">
      <header className="economy-header">
        <Link className="economy-brand" to="/"><BrandLogo size={44} eager /><span>SIMVERSE</span></Link>
        <nav aria-label="Economy navigation"><Link to="/">{copy.back}</Link><Link to="/guide">{copy.guide}</Link><BrandSocialLinks /><LanguageToggle /><Link className="economy-enter" to="/login">{copy.enter}</Link></nav>
      </header>

      <main>
        <section className="economy-hero">
          <div><p className="economy-kicker">{copy.status}</p><h1>{copy.title}</h1><p className="economy-lead">{copy.lead}</p></div>
          <aside><strong>{copy.warningTitle}</strong><p>{copy.warning}</p></aside>
        </section>

        <section className="economy-facts" aria-label="SIM status">{copy.facts.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</section>

        <section className="economy-section"><p className="economy-kicker">01 / VALUE LOOP</p><h2>{copy.loopTitle}</h2><div className="economy-loop">{copy.loop.map(([number, title, body]) => <article key={number}><span>{number}</span><h3>{title}</h3><p>{body}</p></article>)}</div></section>

        <section className="economy-split economy-section"><div><p className="economy-kicker">02 / UTILITY</p><h2>{copy.utilityTitle}</h2></div><div className="economy-list">{copy.utility.map(([title, body]) => <article key={title}><h3>{title}</h3><p>{body}</p></article>)}</div></section>

        <section className="economy-burn economy-section"><div><p className="economy-kicker">03 / SINKS</p><h2>{copy.sinksTitle}</h2><p>{copy.sinksLead}</p></div><div className="economy-burn-grid">{copy.sinks.map(([title, body]) => <article key={title}><span aria-hidden="true">↓</span><h3>{title}</h3><p>{body}</p></article>)}</div></section>

        <section className="economy-trade economy-section"><p className="economy-kicker">04 / MARKETS</p><h2>{copy.tradeTitle}</h2><p>{copy.trade}</p></section>

        <section className="economy-section"><p className="economy-kicker">05 / RELEASE</p><h2>{copy.rolloutTitle}</h2><ol className="economy-rollout">{copy.rollout.map(([when, title, body]) => <li key={when}><span>{when}</span><div><h3>{title}</h3><p>{body}</p></div></li>)}</ol></section>

        <section className="economy-cta"><BrandLogo size={88} /><h2>SIMVERSE WORLD</h2><p>{copy.footer}</p><div><Link to="/guide">{copy.guide}</Link><Link to="/login">{copy.enter}</Link></div><BrandSocialLinks /></section>
      </main>
    </div>
  )
}
