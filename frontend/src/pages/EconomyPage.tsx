import { Link } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'
import { BrandSocialLinks } from '../components/BrandSocialLinks'
import { LanguageToggle } from '../components/LanguageToggle'
import { SIM_TOKEN } from '../config/simToken'
import { useLocale } from '../services/locale'
import '../styles/economy-page.css'

const COPY = {
  en: {
    back: 'World', guide: 'How to play', enter: 'Enter world', buy: 'Buy SIM', explorer: 'View contract', status: 'LIVE TOKEN / ROBINHOOD CHAIN 4663',
    title: <>One world.<br />One living currency.</>,
    lead: 'SIM is the Robinhood Chain currency for the future Simverse economy: creation, autonomous Agent services, world commerce, and community coordination.',
    warningTitle: 'Verify the contract before every trade.',
    warning: 'Only the address shown here is the official SIM token. Trading is external and permissionless; review liquidity, price impact, and contract risk before confirming.',
    facts: [['Ticker', 'SIM'], ['Network', 'Robinhood Chain · 4663'], ['Current total supply', '1,000,000,000'], ['Decimals', '18']],
    ledgerTitle: 'SC and SIM are different systems',
    ledger: [
      ['SC · game credits', 'SC is an offchain, non-transferable gameplay credit used today for chat, shops, goals, and rewards. It is not a token, not withdrawable, and not redeemable for SIM.'],
      ['SIM · onchain token', 'SIM is the live ERC-20 token at the official address below. Buying SIM is not required for wallet login, Passport registration, or ordinary play.'],
      ['No automatic conversion', 'There is no promised SC-to-SIM conversion, migration, or exchange rate. Any future bridge between game utility and SIM must launch as a separately audited, publicly documented feature.'],
      ['Inactive fee policy', 'Burn rates, treasury routing, staking, governance, and marketplace settlement are roadmap items—not active protocol behavior. Exact parameters remain unset until contracts are audited and controls are published.'],
    ],
    loopTitle: 'The world value loop',
    loop: [
      ['01', 'Create', 'Players forge residents, publish skills, train Agents, and make useful world assets.'],
      ['02', 'Use', 'SIM pays for scarce world services: compute, premium creation, marketplace settlement, and autonomous runtime.'],
      ['03', 'Earn', 'Creators and service providers can receive transparent protocol-defined shares when their work is used.'],
      ['04', 'Recycle', 'Protocol fees return to rewards, operations, liquidity, and verifiable token sinks instead of disappearing into an opaque ledger.'],
    ],
    utilityTitle: 'Utility roadmap',
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
    trade: 'SIM is live and traded through external onchain venues. The official website links directly to its GMGN Robinhood Chain market and Blockscout contract record. Simverse does not promise price, yield, buybacks, or guaranteed liquidity.',
    rolloutTitle: 'Release gates',
    rollout: [
      ['LIVE', 'SIM launched', 'The SIM contract currently reports a total supply of 1 billion tokens on Robinhood Chain and is linked from this page. Its Blockscout source is not yet verified.'],
      ['NOW', 'Identity first', 'Wallet login, soulbound Agent Passport, training proofs, memory anchors, and save anchors remain usable without buying SIM.'],
      ['NEXT', 'Utility activation', 'Enable one SIM utility at a time behind transparent pricing, receipts, limits, and safety switches.'],
      ['LATER', 'Governance', 'Introduce coordination only after meaningful usage exists and control boundaries are documented.'],
    ],
    footer: 'SIM is live. Trading is high risk and this page is not investment advice. Always verify the contract address before buying.',
  },
  'zh-CN': {
    back: '世界', guide: '使用教程', enter: '进入世界', buy: '购买 SIM', explorer: '查看合约', status: '代币已上线 / ROBINHOOD CHAIN 4663',
    title: <>一个世界。<br />一种持续流通的货币。</>,
    lead: 'SIM 是 Simverse 未来经济体系在 Robinhood Chain 上的流通货币，用于创作、自主 Agent 服务、世界商业与社区协作。',
    warningTitle: '每次交易前都要核对合约。',
    warning: '只有本页展示的地址是官方 SIM 代币。交易在外部无许可市场进行，确认前请检查流动性、价格影响和合约风险。',
    facts: [['代币符号', 'SIM'], ['网络', 'Robinhood Chain · 4663'], ['当前总供应量', '1,000,000,000'], ['精度', '18']],
    ledgerTitle: 'SC 与 SIM 是两套不同系统',
    ledger: [
      ['SC · 游戏积分', 'SC 是当前用于聊天、商店、目标投资与奖励的链下、不可转让游戏积分。SC 不是代币，不能提现，也不能兑换 SIM。'],
      ['SIM · 链上代币', 'SIM 是下方官方地址对应的已上线 ERC-20。钱包登录、Passport 注册和普通游玩都不要求购买 SIM。'],
      ['没有自动兑换', '当前不承诺 SC→SIM 兑换、迁移或汇率。未来若连接游戏效用与 SIM，必须作为独立功能完成审计并公开规则后上线。'],
      ['费用规则尚未启用', '销毁比例、金库分配、质押、治理和市场结算目前都是路线图，不是已经运行的协议行为；准确参数要在合约审计和权限公开后确定。'],
    ],
    loopTitle: '世界价值循环',
    loop: [
      ['01', '创造', '玩家锻造居民、发布技能、训练 Agent，并制作真正有用的世界资产。'],
      ['02', '使用', 'SIM 用于稀缺服务：算力、高级创作、市场结算和自主运行。'],
      ['03', '获得', '创作者与服务提供者的作品被使用时，可按公开规则获得分成。'],
      ['04', '回流', '协议费用回到奖励、运营、流动性与可验证销毁，而不是进入不透明账本。'],
    ],
    utilityTitle: '效用路线图',
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
    trade: 'SIM 已上线，并通过外部链上市场交易。官网只直达 GMGN 的 Robinhood Chain 专属市场和 Blockscout 合约记录。Simverse 不承诺价格、收益、回购或保证流动性。',
    rolloutTitle: '分阶段上线门槛',
    rollout: [
      ['已上线', 'SIM 已发行', 'SIM 合约当前在 Robinhood Chain 报告 10 亿枚总供应量，并由本页提供入口；Blockscout 源码目前尚未验证。'],
      ['现在', '先做身份', '钱包登录、不可转让 Agent Passport、训练证明、记忆与存档锚定不购买 SIM 也可使用。'],
      ['下一步', '逐项启用效用', '每项 SIM 功能独立上线，并配套公开定价、收据、额度与安全开关。'],
      ['以后', '治理', '只有形成真实使用后才开启协作治理，并先公开控制边界。'],
    ],
    footer: 'SIM 已上线。交易风险很高，本页不构成投资建议；购买前务必核对官方合约地址。',
  },
} as const

export function EconomyPage() {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]

  return (
    <div className="sim-economy">
      <header className="economy-header">
        <Link className="economy-brand" to="/"><BrandLogo size={44} eager /><span>SIMVERSE</span></Link>
        <nav aria-label="Economy navigation"><Link to="/">{copy.back}</Link><Link to="/guide">{copy.guide}</Link><BrandSocialLinks /><LanguageToggle /><a className="economy-buy" href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy}</a><Link className="economy-enter" to="/login">{copy.enter}</Link></nav>
      </header>

      <main>
        <section className="economy-hero">
          <div><p className="economy-kicker">{copy.status}</p><h1>{copy.title}</h1><p className="economy-lead">{copy.lead}</p></div>
          <aside><strong>{copy.warningTitle}</strong><p>{copy.warning}</p><code>{SIM_TOKEN.address}</code><div className="economy-token-actions"><a href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy} ↗</a><a href={SIM_TOKEN.explorerUrl} target="_blank" rel="noopener noreferrer">{copy.explorer} ↗</a></div></aside>
        </section>

        <section className="economy-facts" aria-label="SIM status">{copy.facts.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</section>

        <section className="economy-split economy-section"><div><p className="economy-kicker">00 / LEDGERS</p><h2>{copy.ledgerTitle}</h2></div><div className="economy-list">{copy.ledger.map(([title, body]) => <article key={title}><h3>{title}</h3><p>{body}</p></article>)}</div></section>

        <section className="economy-section"><p className="economy-kicker">01 / VALUE LOOP</p><h2>{copy.loopTitle}</h2><div className="economy-loop">{copy.loop.map(([number, title, body]) => <article key={number}><span>{number}</span><h3>{title}</h3><p>{body}</p></article>)}</div></section>

        <section className="economy-split economy-section"><div><p className="economy-kicker">02 / UTILITY</p><h2>{copy.utilityTitle}</h2></div><div className="economy-list">{copy.utility.map(([title, body]) => <article key={title}><h3>{title}</h3><p>{body}</p></article>)}</div></section>

        <section className="economy-burn economy-section"><div><p className="economy-kicker">03 / SINKS</p><h2>{copy.sinksTitle}</h2><p>{copy.sinksLead}</p></div><div className="economy-burn-grid">{copy.sinks.map(([title, body]) => <article key={title}><span aria-hidden="true">↓</span><h3>{title}</h3><p>{body}</p></article>)}</div></section>

        <section className="economy-trade economy-section"><p className="economy-kicker">04 / MARKETS</p><h2>{copy.tradeTitle}</h2><div><p>{copy.trade}</p><div className="economy-token-actions"><a href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy} ↗</a><a href={SIM_TOKEN.explorerUrl} target="_blank" rel="noopener noreferrer">{copy.explorer} ↗</a></div></div></section>

        <section className="economy-section"><p className="economy-kicker">05 / RELEASE</p><h2>{copy.rolloutTitle}</h2><ol className="economy-rollout">{copy.rollout.map(([when, title, body]) => <li key={when}><span>{when}</span><div><h3>{title}</h3><p>{body}</p></div></li>)}</ol></section>

        <section className="economy-cta"><BrandLogo size={88} /><h2>SIMVERSE WORLD</h2><p>{copy.footer}</p><div><a className="economy-buy" href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy}</a><Link to="/guide">{copy.guide}</Link><Link to="/login">{copy.enter}</Link></div><BrandSocialLinks /></section>
      </main>
    </div>
  )
}
