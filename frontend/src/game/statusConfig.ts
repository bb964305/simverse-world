// Pure status config (no Phaser import) so it is unit-testable and reusable by
// non-Phaser UI (e.g. the experiment panel / resident tooltip). StatusVisuals.ts
// re-exports these and adds the Phaser render behaviour.

export interface StatusConfig {
  label: string
  labelEn: string
  canChat: boolean
  bubble: string
  alpha: number
  tint: number | null
}

export const STATUS_CONFIG: Record<string, StatusConfig> = {
  idle:        { label: '🟢 空闲', labelEn: '🟢 Available', canChat: true,  bubble: '💭', alpha: 1.0, tint: null },
  sleeping:    { label: '💤 沉睡', labelEn: '💤 Sleeping', canChat: false, bubble: '💤', alpha: 0.5, tint: 0x8888cc },
  chatting:    { label: '💬 对话中', labelEn: '💬 In conversation', canChat: false, bubble: '💬', alpha: 1.0, tint: null },
  popular:     { label: '🔥 热门', labelEn: '🔥 Popular', canChat: true,  bubble: '🔥', alpha: 1.0, tint: null },
  walking:     { label: '🚶 移动中', labelEn: '🚶 Walking', canChat: false, bubble: '🚶', alpha: 1.0, tint: null },
  socializing: { label: '🗣️ 交谈中', labelEn: '🗣️ Socializing', canChat: false, bubble: '🗣️', alpha: 1.0, tint: 0x22c55e },
  // researching is a READ-ONLY Resident activity (art-spec §Resident activity):
  // teal research ring + 🔬 icon overhead. It is never inferred from a Task/Run
  // running state — only set when the resident's own activity says so.
  researching: { label: '🔬 研究中', labelEn: '🔬 Researching', canChat: false, bubble: '🔬', alpha: 1.0, tint: 0x14b8a6 },
}
