import type { CaravanPhase } from '../services/api/caravan'

export type MarketHallState = 'closed' | 'opening' | 'open' | 'closing'

const MARKET_HALL_CENTER = { x: 112, y: 94 }
const FULL_VOLUME_RADIUS_TILES = 4
const SILENT_RADIUS_TILES = 22
const MASTER_GAIN = 0.018

export function marketHallState(phase: CaravanPhase | null | undefined): MarketHallState {
  if (phase === 'inbound') return 'opening'
  if (phase === 'trading') return 'open'
  if (phase === 'outbound') return 'closing'
  return 'closed'
}

export function marketAmbienceDistanceGain(tileX: number, tileY: number): number {
  const distance = Math.hypot(tileX - MARKET_HALL_CENTER.x, tileY - MARKET_HALL_CENTER.y)
  if (distance <= FULL_VOLUME_RADIUS_TILES) return 1
  if (distance >= SILENT_RADIUS_TILES) return 0
  return 1 - (distance - FULL_VOLUME_RADIUS_TILES)
    / (SILENT_RADIUS_TILES - FULL_VOLUME_RADIUS_TILES)
}

type BrowserAudioContext = AudioContext & { state: AudioContextState }
type BrowserAudioContextConstructor = new () => BrowserAudioContext

function audioContextConstructor(): BrowserAudioContextConstructor | null {
  if (typeof window === 'undefined') return null
  const candidate = window.AudioContext
    ?? (window as typeof window & { webkitAudioContext?: BrowserAudioContextConstructor }).webkitAudioContext
  return candidate ?? null
}

/**
 * Subtle, source-free market ambience.
 *
 * Browsers do not allow audio before a user gesture, so the controller arms a
 * one-shot pointer/key listener.  It produces a quiet filtered bustle bed and
 * an occasional wooden/bell clink only while the caravan is trading and the
 * player is near the hall.  No remote or unlicensed audio asset is required.
 */
export class MarketAmbienceController {
  private context: BrowserAudioContext | null = null
  private output: GainNode | null = null
  private noise: AudioBufferSourceNode | null = null
  private filter: BiquadFilterNode | null = null
  private clinkTimer: number | null = null
  private stopTimer: number | null = null
  private desiredOpen = false
  private distanceGain = 0
  private armed = false
  private destroyed = false

  arm(): void {
    if (this.armed || this.destroyed || typeof window === 'undefined') return
    this.armed = true
    window.addEventListener('pointerdown', this.unlock, { once: true, passive: true })
    window.addEventListener('keydown', this.unlock, { once: true })
    document.addEventListener('visibilitychange', this.handleVisibility)
  }

  setPhase(phase: CaravanPhase | null | undefined): void {
    const shouldOpen = marketHallState(phase) === 'open'
    if (shouldOpen === this.desiredOpen) return
    this.desiredOpen = shouldOpen
    if (shouldOpen) {
      this.cancelStop()
      if (this.context) this.startBed()
    } else {
      this.fadeAndStop()
    }
  }

  setListenerTile(tileX: number, tileY: number): void {
    const nextGain = marketAmbienceDistanceGain(tileX, tileY)
    if (Math.abs(nextGain - this.distanceGain) < 0.001) return
    this.distanceGain = nextGain
    this.updateGain()
  }

  destroy(): void {
    if (this.destroyed) return
    this.destroyed = true
    if (typeof window !== 'undefined') {
      window.removeEventListener('pointerdown', this.unlock)
      window.removeEventListener('keydown', this.unlock)
      document.removeEventListener('visibilitychange', this.handleVisibility)
    }
    this.cancelStop()
    this.stopBed()
    const context = this.context
    this.context = null
    if (context && context.state !== 'closed') void context.close()
  }

  private unlock = (): void => {
    if (this.destroyed) return
    if (typeof window !== 'undefined') {
      window.removeEventListener('pointerdown', this.unlock)
      window.removeEventListener('keydown', this.unlock)
    }
    const AudioContextClass = audioContextConstructor()
    if (!AudioContextClass) return
    this.context ??= new AudioContextClass()
    if (this.context.state === 'suspended') void this.context.resume()
    if (this.desiredOpen) this.startBed()
  }

  private handleVisibility = (): void => this.updateGain()

  private startBed(): void {
    const context = this.context
    if (!context || this.destroyed || !this.desiredOpen || this.noise) return
    const output = context.createGain()
    output.gain.setValueAtTime(0, context.currentTime)
    output.connect(context.destination)

    const filter = context.createBiquadFilter()
    filter.type = 'bandpass'
    filter.frequency.value = 420
    filter.Q.value = 0.55
    filter.connect(output)

    const seconds = 3
    const buffer = context.createBuffer(1, context.sampleRate * seconds, context.sampleRate)
    const samples = buffer.getChannelData(0)
    let brown = 0
    // Seeded noise makes hot reloads/replays sound stable instead of jumping.
    let seed = 0x51a7c3
    for (let i = 0; i < samples.length; i += 1) {
      seed = (seed * 1664525 + 1013904223) >>> 0
      const white = (seed / 0xffffffff) * 2 - 1
      brown = (brown + 0.02 * white) / 1.02
      samples[i] = brown * 2.4
    }
    const noise = context.createBufferSource()
    noise.buffer = buffer
    noise.loop = true
    noise.connect(filter)
    noise.start()

    this.output = output
    this.filter = filter
    this.noise = noise
    this.updateGain()
    if (typeof window !== 'undefined') {
      this.clinkTimer = window.setInterval(() => this.playClink(), 9000)
    }
  }

  private playClink(): void {
    const context = this.context
    const output = this.output
    const visible = typeof document === 'undefined' || !document.hidden
    if (!context || !output || !this.desiredOpen || !visible || this.distanceGain <= 0.05) return
    const oscillator = context.createOscillator()
    const envelope = context.createGain()
    oscillator.type = 'triangle'
    oscillator.frequency.setValueAtTime(720, context.currentTime)
    oscillator.frequency.exponentialRampToValueAtTime(510, context.currentTime + 0.18)
    envelope.gain.setValueAtTime(0.0001, context.currentTime)
    envelope.gain.exponentialRampToValueAtTime(0.025 * this.distanceGain, context.currentTime + 0.01)
    envelope.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.22)
    oscillator.connect(envelope)
    envelope.connect(context.destination)
    oscillator.start()
    oscillator.stop(context.currentTime + 0.24)
  }

  private updateGain(): void {
    const context = this.context
    const output = this.output
    if (!context || !output) return
    const visible = typeof document === 'undefined' || !document.hidden
    const target = this.desiredOpen && visible ? MASTER_GAIN * this.distanceGain : 0
    output.gain.cancelScheduledValues(context.currentTime)
    output.gain.setTargetAtTime(target, context.currentTime, 0.25)
  }

  private fadeAndStop(): void {
    this.updateGain()
    if (!this.noise || typeof window === 'undefined') return
    this.cancelStop()
    this.stopTimer = window.setTimeout(() => this.stopBed(), 900)
  }

  private cancelStop(): void {
    if (this.stopTimer !== null && typeof window !== 'undefined') window.clearTimeout(this.stopTimer)
    this.stopTimer = null
  }

  private stopBed(): void {
    if (this.clinkTimer !== null && typeof window !== 'undefined') window.clearInterval(this.clinkTimer)
    this.clinkTimer = null
    try { this.noise?.stop() } catch { /* already stopped */ }
    this.noise?.disconnect()
    this.filter?.disconnect()
    this.output?.disconnect()
    this.noise = null
    this.filter = null
    this.output = null
  }
}
