import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useLocale } from '../services/locale'
import { useGameStore } from '../stores/gameStore'

const COPY = {
  en: {
    aria: 'New player guide', skip: 'Skip guide', next: 'Next', done: 'Start exploring', openStudio: 'Open Agent Studio', step: 'STEP',
    items: [
      ['Move through the living town', 'Use WASD or the arrow keys. The town keeps running while you are away; residents follow their own schedules.'],
      ['Meet people and enter buildings', 'Walk close to a resident, player, or highlighted building and press E to interact. Your conversations become world history.'],
      ['Keep your progress onchain', 'The Web3 deck stays visible in the game. Use Agent Studio to publish training, anchor memory, and save or restore verified state.'],
    ],
  },
  'zh-CN': {
    aria: '新手引导', skip: '跳过引导', next: '下一步', done: '开始探索', openStudio: '打开 Agent Studio', step: '步骤',
    items: [
      ['在持续运行的小镇中移动', '使用 WASD 或方向键。即使你离线，小镇也会继续运行，居民按照自己的日程生活。'],
      ['与居民互动并进入建筑', '靠近居民、玩家或高亮建筑后按 E 互动。你的对话会成为世界历史的一部分。'],
      ['把进度保存在链上', '游戏内常驻 Web3 控制台。前往 Agent Studio 发布训练、锚定记忆，并保存或恢复经过哈希校验的状态。'],
    ],
  },
} as const

export function WorldWelcomeGuide() {
  const navigate = useNavigate()
  const locale = useLocale((state) => state.locale)
  const wallet = useGameStore((state) => state.user?.wallet_address ?? 'guest')
  const key = `simverse-world-guide-v2:${wallet.toLowerCase()}`
  const [step, setStep] = useState(0)
  const [visible, setVisible] = useState(() => localStorage.getItem(key) !== 'done')
  const copy = COPY[locale]
  if (!visible) return null

  const finish = () => { localStorage.setItem(key, 'done'); setVisible(false) }
  const current = copy.items[step]
  return (
    <aside className="world-welcome-guide" aria-label={copy.aria}>
      <div className="world-welcome-guide__progress">{copy.items.map((_, index) => <i data-active={index <= step} key={index} />)}</div>
      <button className="world-welcome-guide__close" type="button" onClick={finish} aria-label={copy.skip}>×</button>
      <p>{copy.step} 0{step + 1} / 0{copy.items.length}</p>
      <h2>{current[0]}</h2>
      <span>{current[1]}</span>
      <div>{step === copy.items.length - 1 && <button type="button" onClick={() => { finish(); navigate('/web3') }}>{copy.openStudio}</button>}<button type="button" onClick={() => step < copy.items.length - 1 ? setStep((value) => value + 1) : finish()}>{step < copy.items.length - 1 ? copy.next : copy.done}</button></div>
    </aside>
  )
}
