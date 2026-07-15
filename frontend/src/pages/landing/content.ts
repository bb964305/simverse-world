export const FEATURES = [
  {
    title: '有灵魂的 AI 居民',
    body: '每个 NPC 拥有独立 persona / soul / ability，用 LLM 对话回应你，而不是脚本分支。',
    tone: 'cyan' as const,
  },
  {
    title: '三层记忆系统',
    body: '事件记忆、关系记忆与反思记忆叠加，居民真的会记住你说过的话与共同经历。',
    tone: 'mag' as const,
  },
  {
    title: 'SBTI 人格演化',
    body: '15 维度 × 27 人格类型驱动作息与决策；日常渐变与关键事件跳变让性格持续生长。',
    tone: 'cyan' as const,
  },
  {
    title: '自主生活循环',
    body: 'AgentLoop 调度 14 种行为：闲逛、工作、聊天、反思……居民会在城市中主动相遇。',
    tone: 'mag' as const,
  },
  {
    title: '角色锻造 Forge',
    body: '从调研到蒸馏的多阶段 pipeline，把灵感炼成可交互的完整灵魂档案。',
    tone: 'cyan' as const,
  },
  {
    title: '像素开放世界',
    body: 'Phaser 等距村落、小地图传送、WebSocket 实时同步——一座永不关闭的赛博城市。',
    tone: 'mag' as const,
  },
]

export const STEPS = [
  {
    title: '进入城市',
    body: 'GitHub / LinuxDo 或邮箱登录，完成 Onboarding 创建你的居民身份。',
  },
  {
    title: '遇见灵魂',
    body: '在像素世界中走动、对话、交易；居民带着记忆与关系回应你。',
  },
  {
    title: '锻造与演化',
    body: '用 Forge 炼出新角色，看着人格与关系在时间中真实变化。',
  },
]

export const STACK = [
  'FastAPI',
  'PostgreSQL + pgvector',
  'Redis',
  'React 19',
  'Phaser 3',
  'WebSocket',
  'LLM Agents',
  'SBTI',
]

export const SHOWCASES = [
  {
    src: '/marketing/game-overview.webp',
    alt: 'Simverse World 游戏主界面与公告栏',
    caption: '开放世界主界面',
  },
  {
    src: '/marketing/game-minimap.webp',
    alt: '小地图与工坊区域',
    caption: '小地图与工坊',
  },
  {
    src: '/marketing/forge-main.webp',
    alt: '角色锻造引导式炼化界面',
    caption: 'Forge 引导炼化',
  },
  {
    src: '/marketing/forge-deep.webp',
    alt: '深度蒸馏锻造模式',
    caption: '深度蒸馏模式',
  },
]

export const DEMOS = [
  {
    src: '/marketing/chat-demo.webm',
    poster: '/marketing/chat-poster.webp',
    title: '与 AI 居民对话',
    body: '实时对话录屏，居民带着记忆回应。',
  },
  {
    src: '/marketing/teleport-demo.webm',
    poster: '/marketing/teleport-poster.webp',
    title: '世界内传送',
    body: '在像素城市中穿梭不同区域。',
  },
]

export const FAQ = [
  {
    q: '免费吗？',
    a: '可以免费进入城市体验核心玩法。部分高级能力可能依赖你配置的 LLM 提供商。',
  },
  {
    q: '怎么登录？',
    a: '支持 GitHub、LinuxDo OAuth，或邮箱注册登录。',
  },
  {
    q: '和普通 AI 聊天有什么不同？',
    a: '居民生活在共享开放世界里，有作息、记忆、关系与人格演化，而不是单次会话的聊天机器人。',
  },
  {
    q: '数据与隐私？',
    a: '对话与记忆服务于角色体验。请勿提交敏感个人信息；项目以 MIT 开源，可自建部署。',
  },
]

export const STATS = [
  { value: '15×27', label: 'SBTI 人格维度' },
  { value: '3 层', label: '记忆检索架构' },
  { value: '14', label: '自主行为类型' },
  { value: '24/7', label: '城市持续运转' },
]

export const PROOF = ['MIT 开源', 'Generative Agents 启发', 'Nuwa Skill 锻造灵感', 'LinuxDo 社区']
