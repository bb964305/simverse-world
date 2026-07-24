type EventCallback = (...args: unknown[]) => void

class PhaserBridge {
  private listeners = new Map<string, Set<EventCallback>>()

  on(event: string, cb: EventCallback): () => void {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set())
    this.listeners.get(event)!.add(cb)
    return () => this.listeners.get(event)?.delete(cb)
  }

  emit(event: string, ...args: unknown[]): void {
    this.listeners.get(event)?.forEach((cb) => cb(...args))
  }
}

export const bridge = new PhaserBridge()

// Events emitted from Phaser:
// "npc:nearby"      -> ResidentData | null                            (when player walks near/away from NPC)
// "npc:interact"    -> ResidentData                                   (when player presses E on NPC)
// "player:nearby"   -> { userId: string; name: string; x: number; y: number } | null  (when player walks near/away from online player)
// "player:interact" -> { userId: string; name: string; x: number; y: number }         (when player presses E near online player)
// Phaser → React  minimap:texture    { dataUrl: string }
// React → Phaser  minimap:teleport   { tileX: number, tileY: number, residentSlug?: string }
// Phaser → React  teleport:complete  { tileX: number, tileY: number }
// React → Phaser  photo:take         { residentSlug: string }            (E10 group photo)
// Phaser → React  photo:result       { dataUrl: string, residentSlug: string }
// React → Phaser  commissions:changed  (no payload — CommissionModal accepted/abandoned; GameScene re-pulls ❗ markers) (B1)
// React → React  experiment:open   (no payload — open ExperimentPanel; emitted by TopNav button + ws.ts on experiment_prompt) (Lab)
// React → React  experiment:close  (no payload — close ExperimentPanel) (Lab)
// React → React  townhall:open     (no payload — open read-only TownHallPanel; emitted by TopNav button) (Society §10)
// React → React  townhall:close    (no payload — close TownHallPanel)
// React → React  labterminal:open  (no payload — open read-only LabTerminalPanel; emitted by TopNav button, lab-gated) (Society §10)
// React → React  labterminal:close (no payload — close LabTerminalPanel)
