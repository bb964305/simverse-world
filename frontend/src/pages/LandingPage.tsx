import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { staticResidentPortraitUrl } from '../game/residentSpriteRuntime'
import { LanguageToggle } from '../components/LanguageToggle'
import { BrandSocialLinks } from '../components/BrandSocialLinks'
import { BrandLogo } from '../components/BrandLogo'
import { SIM_TOKEN } from '../config/simToken'
import { useLocale } from '../services/locale'
import '../styles/landing-page.css'

const RESIDENT_KEYS = ['伊莎贝拉', '亚瑟', '卡门', '山本百合子', '弗朗西斯科', '塔玛拉'] as const
const HERO_RESIDENTS = [
  { src: staticResidentPortraitUrl('伊莎贝拉'), className: 'hero-resident--one' },
  { src: staticResidentPortraitUrl('亚瑟'), className: 'hero-resident--two' },
  { src: staticResidentPortraitUrl('山本百合子'), className: 'hero-resident--three' },
  { src: staticResidentPortraitUrl('塔玛拉'), className: 'hero-resident--four' },
] as const

const COPY = {
  'zh-CN': {
    homeLabel: 'Simverse World 首页', navLabel: '官网导航', world: '世界', residents: '居民', forge: '锻造', memory: '记忆',
    watch: '观察小镇', guide: '使用教程', economy: 'SIM 经济模型', buy: '购买 SIM', enter: '连接钱包', menuOpen: '打开导航菜单', menuClose: '关闭导航菜单',
    chainKicker: 'PERSISTENT AI WORLD / BUILT ON ROBINHOOD CHAIN',
    heroLead: '一座由 AI 居民持续生活、记忆与演化的链上开放世界。', live: '观看小镇实况',
    worldTitle: <>这里没有等待触发的剧本，<br />只有正在发生的生活。</>,
    worldLead: '居民会移动、工作、交谈和反思。你离线之后，关系仍在变化，新的记忆仍在形成，城市不会为任何人按下暂停。',
    mapAlt: 'Simverse World 的像素城市地图', districts: '自由区 / 工程区 / 学园区 / 产品街区',
    residentsTitle: <>不是 NPC。<br />是会记住你的人。</>, residentAlt: (name: string) => `${name} 的像素头像`,
    residentNames: ['伊莎贝拉', '亚瑟', '卡门', '山本百合子', '弗朗西斯科', '塔玛拉'],
    residentTraits: ['把每次相遇写进记忆', '在学园区继续研究', '会主动寻找新的关系', '按自己的作息生活', '从经历中改变性格', '与其他居民共享事件'],
    stat1: '维人格坐标驱动选择', stat2: '种自主行为持续运行', stat3: '世界循环不因离线停止',
    memoryTitle: <>经历留下痕迹，<br />痕迹改变下一次相遇。</>,
    memoryLead: '三层记忆系统把瞬间、关系和反思连接起来。完整内容保存在分布式存储，所有权、版本和哈希锚定在链上。',
    memoryLayers: [
      ['事件记忆', '记住一次谈话、一场争执，或你在街角留下的选择。'],
      ['关系记忆', '把反复发生的互动沉淀成信任、距离与立场。'],
      ['反思记忆', '从经历中形成判断，让下一次行动不再只是重复。'],
    ],
    forgeTitle: <>给一个名字。<br />锻造一位钱包所属的居民。</>,
    forgeLead: '从一句想法到完整能力、人格与灵魂档案。每次训练和上传都可以由钱包签名，并把内容证明与版本写进可升级合约。',
    guidedAlt: '引导式居民锻造界面', guided: '边聊边完成角色轮廓', deepAlt: '深度居民蒸馏界面', deep: '研究、提取、验证与精炼',
    loopTitle: '相遇。记住。上链。演化。',
    loops: [['相遇', '进入街区，与一个拥有自己目标的居民说话。'], ['记住', '对话沉入事件与关系，成为下一次选择的上下文。'], ['确权', '钱包签名保存版本，链上锚定记忆、存档与训练证明。'], ['演化', '长期经历推动人格变化，世界因此产生真正的历史。']],
    finalTitle: '世界不会等你上线。', finalLead: '连接钱包，看看居民们已经把今天过成了什么样子。', finalButton: '进入 Simverse',
    footerLead: '一座由 AI 居民持续生活、由钱包拥有并锚定在 Robinhood Chain 的开放世界。', city: '城市', top: '回到顶部', login: '钱包身份',
  },
  en: {
    homeLabel: 'Simverse World home', navLabel: 'Site navigation', world: 'World', residents: 'Residents', forge: 'Forge', memory: 'Memory',
    watch: 'Watch town', guide: 'How to play', economy: 'SIM economy', buy: 'Buy SIM', enter: 'Connect wallet', menuOpen: 'Open navigation menu', menuClose: 'Close navigation menu',
    chainKicker: 'PERSISTENT AI WORLD / BUILT ON ROBINHOOD CHAIN',
    heroLead: 'An onchain open world where AI residents keep living, remembering, and evolving.', live: 'Watch the town live',
    worldTitle: <>There is no script waiting to fire.<br />Only life already in motion.</>,
    worldLead: 'Residents move, work, talk, and reflect. Relationships keep changing while you are away, new memories keep forming, and the city never pauses for anyone.',
    mapAlt: 'Pixel city map of Simverse World', districts: 'Free District / Engineering / Academy / Product Street',
    residentsTitle: <>Not NPCs.<br />People who remember you.</>, residentAlt: (name: string) => `Pixel portrait of ${name}`,
    residentNames: ['Isabella', 'Arthur', 'Carmen', 'Yuriko Yamamoto', 'Francisco', 'Tamara'],
    residentTraits: ['Writes every encounter into memory', 'Keeps researching in the Academy', 'Actively seeks new relationships', 'Lives by her own rhythm', 'Lets experience reshape personality', 'Shares events with other residents'],
    stat1: 'personality axes guide every choice', stat2: 'autonomous behaviors keep running', stat3: 'the world loop never stops offline',
    memoryTitle: <>Experience leaves a trace.<br />The trace changes what comes next.</>,
    memoryLead: 'Three layers connect events, relationships, and reflection. Full content lives in distributed storage while ownership, versions, and hashes are anchored onchain.',
    memoryLayers: [
      ['Event memory', 'Remember a conversation, a conflict, or a choice you left on a street corner.'],
      ['Relationship memory', 'Repeated interactions settle into trust, distance, and point of view.'],
      ['Reflective memory', 'Experience becomes judgment, so the next action is more than repetition.'],
    ],
    forgeTitle: <>Give them a name.<br />Forge a wallet-owned resident.</>,
    forgeLead: 'Turn one idea into abilities, personality, and a complete soul profile. Every training run and upload can be wallet-signed, versioned, and proven through an upgradeable contract.',
    guidedAlt: 'Guided resident forge interface', guided: 'Shape a resident through conversation', deepAlt: 'Deep resident distillation interface', deep: 'Research, extract, validate, refine',
    loopTitle: 'Meet. Remember. Anchor. Evolve.',
    loops: [['Meet', 'Enter a district and talk to a resident with goals of their own.'], ['Remember', 'Conversation settles into events and relationships that shape the next choice.'], ['Own', 'Sign a version with your wallet and anchor memory, saves, and training proofs onchain.'], ['Evolve', 'Long experience changes personality and gives the world a real history.']],
    finalTitle: 'The world will not wait for you to log in.', finalLead: 'Connect your wallet and see what the residents have made of today.', finalButton: 'Enter Simverse',
    footerLead: 'An open world lived by AI residents, wallet-owned, and anchored on Robinhood Chain.', city: 'City', top: 'Back to top', login: 'Wallet identity',
  },
} as const

function MenuIcon({ open }: { open: boolean }) {
  return <span className="site-menu-icon" data-open={open} aria-hidden="true"><span /><span /></span>
}

export function LandingPage() {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const [menuOpen, setMenuOpen] = useState(false)
  const [scrolled, setScrolled] = useState(false)
  const [compactNav, setCompactNav] = useState(() => window.matchMedia?.('(max-width: 1100px)').matches ?? false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    document.body.classList.add('marketing-page-open')
    const updateHeader = () => setScrolled(window.scrollY > 24)
    updateHeader()
    window.addEventListener('scroll', updateHeader, { passive: true })
    const targets = document.querySelectorAll<HTMLElement>('[data-reveal]')
    if (!('IntersectionObserver' in window) || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      targets.forEach((target) => target.classList.add('is-visible'))
      return () => { document.body.classList.remove('marketing-page-open'); window.removeEventListener('scroll', updateHeader) }
    }
    const observer = new IntersectionObserver((entries) => entries.forEach((entry) => {
      if (entry.isIntersecting) { entry.target.classList.add('is-visible'); observer.unobserve(entry.target) }
    }), { threshold: 0.14 })
    targets.forEach((target) => observer.observe(target))
    return () => { document.body.classList.remove('marketing-page-open'); window.removeEventListener('scroll', updateHeader); observer.disconnect() }
  }, [])

  useEffect(() => {
    const media = window.matchMedia?.('(max-width: 1100px)')
    if (!media) return
    const change = (event: MediaQueryListEvent) => { setCompactNav(event.matches); if (!event.matches) setMenuOpen(false) }
    media.addEventListener('change', change)
    return () => media.removeEventListener('change', change)
  }, [])

  useEffect(() => {
    if (!menuOpen) return
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setMenuOpen(false); menuButtonRef.current?.focus() } }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [menuOpen])

  const closeMenu = () => setMenuOpen(false)

  return (
    <div className="marketing-site" id="top">
      <header className="site-header" data-scrolled={scrolled}>
        <Link className="site-brand" to="/" aria-label={copy.homeLabel} onClick={closeMenu}>
          <BrandLogo className="site-brand__mark" size={42} eager /><span className="site-brand__name">SIMVERSE</span>
        </Link>
        <nav className="site-nav" data-open={menuOpen} aria-label={copy.navLabel} aria-hidden={compactNav && !menuOpen} inert={compactNav && !menuOpen ? true : undefined}>
          <a href="#world" onClick={closeMenu}>{copy.world}</a><a href="#residents" onClick={closeMenu}>{copy.residents}</a>
          <a href="#forge" onClick={closeMenu}>{copy.forge}</a><a href="#memory" onClick={closeMenu}>{copy.memory}</a>
          <Link to="/town" onClick={closeMenu}>{copy.watch}</Link><Link to="/guide" onClick={closeMenu}>{copy.guide}</Link><Link to="/economy" onClick={closeMenu}>{copy.economy}</Link><a className="site-nav__mobile-entry site-nav__mobile-buy" href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer" onClick={closeMenu}>{copy.buy}</a><Link className="site-nav__mobile-entry" to="/login" onClick={closeMenu}>{copy.enter}</Link>
        </nav>
        <div className="site-header__actions">
          <BrandSocialLinks className="brand-socials--header" />
          <LanguageToggle className="language-toggle language-toggle--site" />
          <a className="site-header__buy" href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy}</a>
          <Link className="site-header__login" to="/login">{copy.enter}</Link>
          <button ref={menuButtonRef} className="site-menu-button" type="button" aria-label={menuOpen ? copy.menuClose : copy.menuOpen} aria-expanded={menuOpen} onClick={() => setMenuOpen((open) => !open)}><MenuIcon open={menuOpen} /></button>
        </div>
      </header>

      <main>
        <section className="site-hero" aria-labelledby="hero-title">
          <img className="site-hero__backdrop" src="/marketing/world-hero.jpg" alt="" fetchPriority="high" /><div className="site-hero__shade" />
          <div className="site-hero__content">
            <p className="site-kicker site-kicker--light">{copy.chainKicker}</p>
            <h1 className="site-hero__title" id="hero-title" aria-label="Simverse World"><span>SIMVERSE</span><span>WORLD</span></h1>
            <p className="site-hero__lead">{copy.heroLead}</p>
            <div className="site-hero__actions"><a className="site-button site-button--primary" href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy} <span aria-hidden="true">↗</span></a><Link className="site-button site-button--ghost" to="/login">{copy.enter}</Link><Link className="site-button site-button--ghost" to="/town">{copy.live}</Link></div>
          </div>
          <div className="site-hero__residents" aria-hidden="true">{HERO_RESIDENTS.map((resident) => <img className={`hero-resident ${resident.className}`} src={resident.src} alt="" key={resident.src} />)}</div>
          <div className="site-hero__index" aria-hidden="true"><span>001</span><span>THE CITY IS ALREADY AWAKE</span></div>
        </section>

        <section className="world-section" id="world">
          <div className="section-shell world-section__intro" data-reveal><p className="site-kicker">01 / WORLD</p><h2 className="display-heading">{copy.worldTitle}</h2><p className="section-lead">{copy.worldLead}</p></div>
          <figure className="world-media" data-reveal><img src="/marketing/world-map.jpg" alt={copy.mapAlt} loading="lazy" /><figcaption><span>LIVE WORLD MAP</span><span>{copy.districts}</span></figcaption></figure>
        </section>

        <section className="resident-section" id="residents">
          <div className="section-shell" data-reveal><p className="site-kicker">02 / RESIDENTS</p><h2 className="display-heading display-heading--dark">{copy.residentsTitle}</h2></div>
          <div className="resident-roster" data-reveal>{RESIDENT_KEYS.map((key, index) => <article className="resident-profile" key={key}><img src={staticResidentPortraitUrl(key)} alt={copy.residentAlt(copy.residentNames[index])} loading="lazy" /><div><h3>{copy.residentNames[index]}</h3><p>{copy.residentTraits[index]}</p></div></article>)}</div>
          <div className="resident-stats section-shell" data-reveal><div><strong>15</strong><span>{copy.stat1}</span></div><div><strong>14</strong><span>{copy.stat2}</span></div><div><strong>24/7</strong><span>{copy.stat3}</span></div></div>
        </section>

        <section className="memory-section" id="memory">
          <div className="section-shell memory-section__layout">
            <div className="memory-section__copy" data-reveal><p className="site-kicker site-kicker--light">03 / ONCHAIN MEMORY</p><h2 className="display-heading">{copy.memoryTitle}</h2><p className="section-lead section-lead--light">{copy.memoryLead}</p></div>
            <ol className="memory-layers" data-reveal>{copy.memoryLayers.map((layer, index) => <li key={layer[0]}><span className="memory-layers__index">0{index + 1}</span><div><h3>{layer[0]}</h3><p>{layer[1]}</p></div></li>)}</ol>
          </div>
        </section>

        <section className="forge-section" id="forge">
          <div className="section-shell forge-section__heading" data-reveal><p className="site-kicker">04 / WEB3 FORGE</p><h2 className="display-heading display-heading--dark">{copy.forgeTitle}</h2><p className="section-lead section-lead--dark">{copy.forgeLead}</p></div>
          <div className="forge-media" data-reveal><figure><img src="/marketing/forge-guided.jpg" alt={copy.guidedAlt} loading="lazy" /><figcaption><span>GUIDED FORGE</span><span>{copy.guided}</span></figcaption></figure><figure><img src="/marketing/forge-deep.jpg" alt={copy.deepAlt} loading="lazy" /><figcaption><span>DEEP FORGE</span><span>{copy.deep}</span></figcaption></figure></div>
        </section>

        <section className="life-loop-section"><div className="section-shell" data-reveal><p className="site-kicker">THE LIVING LOOP</p><h2 className="display-heading display-heading--dark">{copy.loopTitle}</h2><div className="life-loop life-loop--web3">{copy.loops.map((loop, index) => <div key={loop[0]}><span>0{index + 1}</span><h3>{loop[0]}</h3><p>{loop[1]}</p></div>)}</div></div></section>

        <section className="final-callout"><img className="final-callout__backdrop" src="/marketing/world-map.jpg" alt="" loading="lazy" /><div className="final-callout__shade" /><div className="section-shell final-callout__content" data-reveal><p className="site-kicker site-kicker--light">YOUR STORY STARTS MID-SCENE</p><h2 className="display-heading">{copy.finalTitle}</h2><p>{copy.finalLead}</p><div className="site-hero__actions site-hero__actions--center"><Link className="site-button site-button--primary" to="/login">{copy.finalButton} <span aria-hidden="true">-&gt;</span></Link><a className="site-button site-button--ghost" href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy} <span aria-hidden="true">↗</span></a></div></div></section>
      </main>

      <footer className="site-footer">
        <div className="site-footer__brand"><BrandLogo className="site-brand__mark" size={58} /><strong>SIMVERSE WORLD</strong><p>{copy.footerLead}</p></div>
        <div className="site-footer__links"><div><span>WORLD</span><a href="#world">{copy.city}</a><a href="#residents">{copy.residents}</a><a href="#memory">{copy.memory}</a></div><div><span>CREATE</span><a href="#forge">{copy.forge}</a><Link to="/guide">{copy.guide}</Link><Link to="/economy">{copy.economy}</Link><a href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy}</a><Link to="/login">{copy.login}</Link></div><div><span>COMMUNITY</span><BrandSocialLinks className="brand-socials--footer" /></div></div>
        <div className="site-footer__meta"><span>SIMVERSE WORLD / 2026</span><a href="#top">{copy.top}</a></div>
      </footer>
    </div>
  )
}
