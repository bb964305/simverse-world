import Phaser from 'phaser'
import { bridge } from './phaserBridge'
import { observeContainerResize, waitForNonZeroSize } from './canvasSize'
import { applyStatusVisuals, clearStatusVisuals, releaseAllStatusVisuals, STATUS_CONFIG } from './StatusVisuals'
import { useGameStore } from '../stores/gameStore'
import { sendPosition, sendWS, onWSMessage } from '../services/ws'
import { API_BASE, updatePlayerPosition, getHomeDecor, decorEmoji, getActiveEvents, getCommissions, HOUSING_BOUNDS, type DecorItem } from '../services/api'
import { TILE_SIZE } from './worldGeometry'
import {
  STATIC_RESIDENT_ATLAS_JSON_URL,
  decideResidentTextureLoad,
  parseResidentSpriteUpdatedMessage,
  residentTextureKey,
  resolveResidentSpriteUrl,
  staticResidentSpriteUrl,
  type ResidentSpriteUpdatedMessage,
} from './residentSpriteRuntime'
import {
  caravanRenderMode,
  projectCaravanPose,
  refreshCaravanProjection,
  subscribeCaravanProjection,
  type CaravanPose,
  type CaravanProjection,
} from '../services/caravanProjection'
import {
  CARAVAN_CONVOY_ATLAS_URL,
  CARAVAN_CONVOY_TEXTURE_URL,
  CARAVAN_MERCHANT_ATLAS_URL,
  CARAVAN_MERCHANT_TEXTURE_URL,
  CARAVAN_STALL_TEXTURE_URL,
} from './caravanAssetRuntime'
import {
  caravanTileKey,
  resolveCaravanWorldPlacement,
} from './caravanRenderRuntime'
import { MarketHallRuntime } from './marketHallRuntime'
import { MarketAmbienceController } from './marketAmbience'
import { MarketPurchaseRuntime } from './marketPurchaseRuntime'

const PLAYER_SPEED = 160
const NPC_INTERACT_DISTANCE = 60
const PLAYER_INTERACT_DISTANCE = 80
const CARAVAN_MERCHANT_TEXTURE = 'caravan-merchant'
const CARAVAN_CONVOY_TEXTURE = 'caravan-convoy'
const CARAVAN_STALL_TEXTURE = 'caravan-stall'
const REQUIRED_RESIDENT_FRAMES = [
  'down', 'left', 'right', 'up',
  'down-walk.000', 'down-walk.001', 'down-walk.002',
  'left-walk.000', 'left-walk.001', 'left-walk.002',
  'right-walk.000', 'right-walk.001', 'right-walk.002',
  'up-walk.000', 'up-walk.001', 'up-walk.002',
] as const

const TILESET_IMAGE_MAP: Record<string, string> = {
  blocks_1: 'blocks_1.png',
  walls: 'Room_Builder_32x32.png',
  interiors_pt1: 'interiors_pt1.png',
  interiors_pt2: 'interiors_pt2.png',
  interiors_pt3: 'interiors_pt3.png',
  interiors_pt4: 'interiors_pt4.png',
  interiors_pt5: 'interiors_pt5.png',
  CuteRPG_Field_B: 'CuteRPG_Field_B.png',
  CuteRPG_Field_C: 'CuteRPG_Field_C.png',
  CuteRPG_Harbor_C: 'CuteRPG_Harbor_C.png',
  CuteRPG_Village_B: 'CuteRPG_Village_B.png',
  CuteRPG_Forest_B: 'CuteRPG_Forest_B.png',
  CuteRPG_Desert_C: 'CuteRPG_Desert_C.png',
  CuteRPG_Mountains_B: 'CuteRPG_Mountains_B.png',
  CuteRPG_Desert_B: 'CuteRPG_Desert_B.png',
  CuteRPG_Forest_C: 'CuteRPG_Forest_C.png',
}

export interface ResidentData {
  id: string
  slug: string
  name: string
  status: string
  sprite_key: string
  sprite_url?: string | null
  sprite_content_hash?: string | null
  sprite_generation_run_id?: string | null
  portrait_url?: string | null
  tile_x: number
  tile_y: number
  district: string
  home_location_id?: string | null
  meta_json: { role?: string; sbti?: { type: string; type_name: string } }
  token_cost_per_turn: number
  star_rating: number
  heat: number
  mood_label?: string
}

interface CaravanView {
  container: Phaser.GameObjects.Container
  convoy: Phaser.GameObjects.Sprite
  merchant: Phaser.GameObjects.Sprite
  stall: Phaser.GameObjects.Sprite
  label: Phaser.GameObjects.Text
}

let gameInstance: Phaser.Game | null = null
let stopResizeObserver: (() => void) | null = null
let initGeneration = 0
let gameOwner: symbol | null = null
let pendingOwner: symbol | null = null
let gameZoom = 1

export function destroyGame(owner?: symbol): void {
  const ownsPending = owner === undefined || pendingOwner === owner
  const ownsGame = owner === undefined || gameOwner === owner
  // A delayed cleanup from React StrictMode (or a fast route swap) must not
  // tear down a newer mount which has already adopted the game instance.
  if (!ownsPending && !ownsGame) return
  if (ownsPending) {
    initGeneration += 1
    pendingOwner = null
  }
  if (stopResizeObserver) {
    stopResizeObserver()
    stopResizeObserver = null
  }
  if (gameInstance && ownsGame) {
    gameInstance.destroy(true)
    gameInstance = null
    gameOwner = null
  }
}

function bindGameContainer(container: HTMLElement): void {
  if (!gameInstance) return
  if (gameInstance.canvas.parentElement !== container) container.appendChild(gameInstance.canvas)
  stopResizeObserver?.()
  stopResizeObserver = observeContainerResize(container, (w, h) => {
    if (gameInstance && w > 0 && h > 0) {
      gameInstance.scale.resize(w / gameZoom, h / gameZoom)
    }
  })
}

export async function initGame(container: HTMLElement, owner?: symbol): Promise<void> {
  if (gameInstance) {
    gameOwner = owner ?? gameOwner
    bindGameContainer(container)
    return
  }
  // burn-in 修复：首登画布只渲染左上角 150x90——Phaser 在布局稳定前读了一次
  // 容器尺寸（scale mode NONE 不会自愈）。先等容器有真实尺寸再建 Game，
  // 之后 ResizeObserver 跟随容器变化（顺带修 chatOpen 380px 推移不重排）。
  const generation = ++initGeneration
  pendingOwner = owner ?? null
  const { width, height } = await waitForNonZeroSize(container)
  if (generation !== initGeneration || gameInstance
      || (owner !== undefined && pendingOwner !== owner)) return // unmount 竞态防护
  const zoom = Math.max(1, window.innerWidth / 4400)
  gameZoom = zoom
  gameOwner = owner ?? null
  pendingOwner = null
  gameInstance = new Phaser.Game({
    type: Phaser.AUTO,
    width: width / zoom,
    height: height / zoom,
    parent: container,
    pixelArt: true,
    physics: { default: 'arcade', arcade: { gravity: { x: 0, y: 0 } } },
    scene: [MainScene],
    scale: { zoom },
  })
  bindGameContainer(container)
}

class MainScene extends Phaser.Scene {
  private player!: Phaser.Physics.Arcade.Sprite
  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys
  private wasd!: { up: Phaser.Input.Keyboard.Key; down: Phaser.Input.Keyboard.Key; left: Phaser.Input.Keyboard.Key; right: Phaser.Input.Keyboard.Key }
  private eKey!: Phaser.Input.Keyboard.Key
  private npcSprites: Phaser.Physics.Arcade.Sprite[] = []
  private npcLabels: Phaser.GameObjects.Text[] = []
  private residentSpritesById = new Map<string, Phaser.Physics.Arcade.Sprite>()
  private desiredResidentTextureKeys = new Map<string, string>()
  private dynamicResidentTextureKeys = new Set<string>()
  private residentTextureLoads = new Map<string, Promise<boolean>>()
  private residents: ResidentData[] = []
  private mapReady = false
  private isTeleporting = false
  private otherPlayerSprites: Map<string, { sprite: Phaser.Physics.Arcade.Sprite; label: Phaser.GameObjects.Text }> = new Map()
  // B3 home decor: emoji objects per resident slug, rendered below characters.
  private decorLayer: Phaser.GameObjects.Layer | null = null
  private decorTexts: Map<string, Phaser.GameObjects.Text[]> = new Map()
  private decorLoaded: Set<string> = new Set()
  private lastDecorScan = 0
  // B1 commissions: '❗' over residents that currently have an open commission.
  // Keyed by resident slug; each entry keeps its sprite so update() can make the
  // marker follow (npc labels are static, but resident_move tweens sprites).
  private commissionMarkers: Map<string, { text: Phaser.GameObjects.Text; sprite: Phaser.Physics.Arcade.Sprite }> = new Map()
  // E6 weather: screen-space (scrollFactor 0) particle + tint layers driven by
  // world_event(type=weather) broadcasts; at most one emitter alive at a time.
  private weatherEmitter: Phaser.GameObjects.Particles.ParticleEmitter | null = null
  private weatherOverlay: Phaser.GameObjects.Rectangle | null = null
  private stormFlashTimer: Phaser.Time.TimerEvent | null = null
  // Temporary market-day entity. It deliberately lives outside residents[] and
  // residentSpritesById: a caravan is not a Resident and never enters NPC UI.
  private caravanProjection: CaravanProjection | null = null
  private caravanView: CaravanView | null = null
  private caravanBlockedTiles = new Set<string>()
  private marketHallRuntime: MarketHallRuntime | null = null
  private marketAmbience: MarketAmbienceController | null = null
  private marketPurchaseRuntime: MarketPurchaseRuntime | null = null
  // Unsubscribers for listeners registered on module-level singletons
  // (ws.ts wsListeners, phaserBridge) — Phaser does NOT clean these up when
  // the scene dies, so without explicit teardown every StrictMode
  // destroyGame→initGame remount leaks a full scene graph via `this` captures.
  private externalCleanups: Array<() => void> = []
  private isShutdown = false

  init() {
    // Scene-level cleanup hooks. SHUTDOWN fires on scene stop/restart, DESTROY
    // on game.destroy(true) (our destroyGame path) — register both, teardown
    // is idempotent.
    this.events.once(Phaser.Scenes.Events.SHUTDOWN, this.teardown, this)
    this.events.once(Phaser.Scenes.Events.DESTROY, this.teardown, this)
  }

  private teardown(): void {
    if (this.isShutdown) return
    this.isShutdown = true
    for (const unsub of this.externalCleanups) unsub()
    this.externalCleanups = []
    // Drop instance collections so a lingering async create() / snapshot
    // callback can't resurrect dead sprites via them.
    this.otherPlayerSprites.clear()
    this._chatBubbles.clear()
    this.npcSprites = []
    this.npcLabels = []
    this.residentSpritesById.clear()
    this.desiredResidentTextureKeys.clear()
    this.dynamicResidentTextureKeys.clear()
    this.residentTextureLoads.clear()
    this.caravanBlockedTiles.clear()
    // Decor objects die with the scene; drop the references so a late
    // getHomeDecor() resolution can't touch dead text objects.
    this.decorTexts.clear()
    this.decorLoaded.clear()
    this.decorLayer = null
    // B1 commission markers die with the scene; drop the refs so a late
    // getCommissions() resolution can't touch dead text objects.
    this.commissionMarkers.clear()
    // E6 weather: destroy the emitter/overlay and stop the storm flash timer
    // here (SHUTDOWN fires before children are torn down), so a scene swap
    // leaves no particles or ticking timers behind.
    this._clearWeather()
    this._destroyCaravanView()
    this.marketHallRuntime?.destroy()
    this.marketHallRuntime = null
    this.marketAmbience?.destroy()
    this.marketAmbience = null
    this.marketPurchaseRuntime?.destroy()
    this.marketPurchaseRuntime = null
    // StatusVisuals keeps a module-level sprite→visuals registry; the objects
    // themselves die with the scene, but the Map entries must be released here.
    releaseAllStatusVisuals()
  }

  preload() {
    const base = '/assets/village/tilemap/'
    for (const [key, filename] of Object.entries(TILESET_IMAGE_MAP)) {
      this.load.image(key, base + filename)
    }
    this.load.tilemapTiledJSON('map', base + 'tilemap.json')

    // Caravan assets have their own contract and lifecycle; they are not added
    // to the canonical 25 resident slots. Missing optional assets fail cosmetic.
    this.load.atlas(
      CARAVAN_MERCHANT_TEXTURE,
      CARAVAN_MERCHANT_TEXTURE_URL,
      CARAVAN_MERCHANT_ATLAS_URL,
    )
    this.load.atlas(
      CARAVAN_CONVOY_TEXTURE,
      CARAVAN_CONVOY_TEXTURE_URL,
      CARAVAN_CONVOY_ATLAS_URL,
    )
    this.load.image(CARAVAN_STALL_TEXTURE, CARAVAN_STALL_TEXTURE_URL)

    const spriteKey = useGameStore.getState().playerSpriteKey
    this.load.atlas(
      'player_atlas',
      staticResidentSpriteUrl(spriteKey),
      STATIC_RESIDENT_ATLAS_JSON_URL,
    )
  }

  async create() {
    try {
      const resp = await fetch(`${API_BASE}/residents`)
      this.residents = resp.ok ? (await resp.json() as ResidentData[]) : []
    } catch {
      this.residents = []
    }

    // The scene can be destroyed while the fetch above is in flight (React
    // StrictMode mounts, then immediately unmounts → destroyGame). Touching
    // this.textures / this.load on a dead scene throws, so bail out here.
    if (this.isShutdown) return

    // Load resident sprites
    const spritesToLoad = new Set<string>()
    for (const r of this.residents) {
      // Always keep the checked-in sprite available as a fallback. A bad or
      // temporarily unavailable generated asset must not make an NPC vanish.
      if (!this.textures.exists(r.sprite_key) && !spritesToLoad.has(r.sprite_key)) {
        spritesToLoad.add(r.sprite_key)
        this.load.atlas(
          r.sprite_key,
          staticResidentSpriteUrl(r.sprite_key),
          STATIC_RESIDENT_ATLAS_JSON_URL,
        )
      }
      const generatedUrl = resolveResidentSpriteUrl(r.sprite_url, API_BASE, r.sprite_content_hash)
      const generatedKey = residentTextureKey(r)
      if (generatedUrl && !this.textures.exists(generatedKey) && !spritesToLoad.has(generatedKey)) {
        spritesToLoad.add(generatedKey)
        this.dynamicResidentTextureKeys.add(generatedKey)
        this.load.atlas(generatedKey, generatedUrl, STATIC_RESIDENT_ATLAS_JSON_URL)
      }
    }

    if (spritesToLoad.size > 0 && !this.load.isLoading()) {
      this.load.start()
      await new Promise<void>((resolve) => this.load.once('complete', resolve))
    }

    this.setupWorld()
  }

  private _residentAtlasIsReady(textureKey: string): boolean {
    if (!this.textures.exists(textureKey)) return false
    const texture = this.textures.get(textureKey)
    return REQUIRED_RESIDENT_FRAMES.every((frame) => texture.has(frame))
  }

  private generateMinimapTexture(): void {
    const cam = this.cameras.main
    const origScrollX = cam.scrollX
    const origScrollY = cam.scrollY
    const origZoom = cam.zoom

    // Temporarily zoom out to capture entire map
    const mapW = cam.getBounds().width
    const mapH = cam.getBounds().height
    const zoom = Math.min(cam.width / mapW, cam.height / mapH)

    cam.setZoom(zoom)
    cam.setScroll(0, 0)
    cam.stopFollow()

    // Wait one frame for the camera to render, then snapshot
    this.time.delayedCall(50, () => {
      this.game.renderer.snapshotArea(
        0, 0,
        Math.ceil(mapW * zoom),
        Math.ceil(mapH * zoom),
        (image) => {
          // Restore camera
          cam.setZoom(origZoom)
          cam.setScroll(origScrollX, origScrollY)
          if (this.player) {
            cam.startFollow(this.player, true, 0.1, 0.1)
          }

          const img = image as HTMLImageElement
          if (img.src) {
            useGameStore.getState().setMinimapTexture(img.src)
          }
        }
      )
    })
  }

  private setupWorld() {
    // create() awaits before calling this — never build the world (or register
    // external listeners below) on a scene that was destroyed mid-await.
    if (this.isShutdown) return
    const map = this.make.tilemap({ key: 'map' })
    this.physics.world.setBounds(0, 0, map.widthInPixels, map.heightInPixels)

    const tilesetMap: Record<string, Phaser.Tilemaps.Tileset | null> = {}
    for (const key of Object.keys(TILESET_IMAGE_MAP)) {
      const tiled_name = key === 'walls' ? 'Room_Builder_32x32' : key === 'blocks_1' ? 'blocks' : key
      tilesetMap[key] = map.addTilesetImage(tiled_name, key)
    }

    const allTilesets = Object.entries(tilesetMap)
      .filter(([k, v]) => k !== 'blocks_1' && v !== null)  // blocks_1 is collision-only, not rendered
      .map(([, v]) => v!) as Phaser.Tilemaps.Tileset[]

    const layerNames = [
      'Bottom Ground', 'Exterior Ground', 'Exterior Decoration L1', 'Exterior Decoration L2',
      'Interior Ground', 'Interior Furniture L1', 'Interior Furniture L2 ',
      'Foreground L1', 'Foreground L2',
    ]
    for (const name of layerNames) {
      const layer = map.createLayer(name, name === 'Wall' ? [tilesetMap.CuteRPG_Field_C!, tilesetMap.walls!] : allTilesets, 0, 0)
      if (name.startsWith('Foreground')) layer?.setDepth(2)
    }

    // Wall layer separately
    const wallLayer = map.createLayer('Wall', [tilesetMap.CuteRPG_Field_C!, tilesetMap.walls!].filter(Boolean) as Phaser.Tilemaps.Tileset[], 0, 0)
    wallLayer?.setDepth(1)

    const collisionLayer = map.createLayer('Collisions', tilesetMap.blocks_1!, 0, 0)
    collisionLayer?.setCollisionByExclusion([-1])
    collisionLayer?.setVisible(false)
    this.caravanBlockedTiles.clear()
    collisionLayer?.layer.data.forEach((row) => {
      row.forEach((tile) => {
        if (tile.index !== -1) this.caravanBlockedTiles.add(caravanTileKey(tile.x, tile.y))
      })
    })

    // B3 decor layer: above ground tiles (depth 0), below characters (depth 1).
    this.decorLayer = this.add.layer().setDepth(0.5)

    // Player — use spawn position from store (set by backend spawn_position message)
    const { spawnX, spawnY } = useGameStore.getState()
    this.player = this.physics.add.sprite(spawnX, spawnY, 'player_atlas', 'down')
      .setSize(24, 24).setOffset(4, 8).setDepth(1).setCollideWorldBounds(true)
    this.player.displayWidth = 40
    this.player.scaleY = this.player.scaleX

    if (collisionLayer) {
      this.physics.add.collider(this.player, collisionLayer)
    }

    for (const dir of ['left', 'right', 'down', 'up']) {
      this.anims.create({
        key: `player-${dir}-walk`,
        frames: this.anims.generateFrameNames('player_atlas', {
          prefix: `${dir}-walk.`, start: 0, end: 3, zeroPad: 3,
        }),
        frameRate: 8,
        repeat: -1,
      })
    }
    this._setupCaravanAnimations()
    this.marketHallRuntime = new MarketHallRuntime(this)
    this.marketAmbience = new MarketAmbienceController()
    this.marketAmbience.arm()
    this.marketPurchaseRuntime = new MarketPurchaseRuntime(this)

    // NPCs
    for (const r of this.residents) {
      const x = r.tile_x * TILE_SIZE + TILE_SIZE / 2
      const y = r.tile_y * TILE_SIZE + TILE_SIZE

      const preferredTextureKey = residentTextureKey(r)
      const textureKey = this._residentAtlasIsReady(preferredTextureKey) ? preferredTextureKey : r.sprite_key
      const sprite = this.physics.add.sprite(x, y, textureKey, 'down')
        .setSize(24, 24).setOffset(4, 8).setDepth(1).setImmovable(true)
      sprite.displayWidth = 40
      sprite.scaleY = sprite.scaleX
      ;(sprite as unknown as Record<string, unknown>).__residentData = r

      applyStatusVisuals(this, sprite, r.status, x, y)

      const label = this.add.text(x, y - 32, r.name, {
        fontSize: '13px',
        color: '#ffffff',
        backgroundColor: '#18181bcc',
        padding: { x: 6, y: 2 },
      }).setOrigin(0.5).setDepth(3).setAlpha(0.3)

      this.npcSprites.push(sprite)
      this.npcLabels.push(label)
      this.residentSpritesById.set(r.id, sprite)
      this.desiredResidentTextureKeys.set(r.id, textureKey)
      this.marketPurchaseRuntime?.registerResident(r.slug, sprite, r.tile_x, r.tile_y)
    }

    // Camera
    this.cameras.main.startFollow(this.player, true, 0.1, 0.1)
    this.cameras.main.setBounds(0, 0, map.widthInPixels, map.heightInPixels)

    // Input
    this.cursors = this.input.keyboard!.createCursorKeys()
    this.wasd = this.input.keyboard!.addKeys({
      up: Phaser.Input.Keyboard.KeyCodes.W,
      down: Phaser.Input.Keyboard.KeyCodes.S,
      left: Phaser.Input.Keyboard.KeyCodes.A,
      right: Phaser.Input.Keyboard.KeyCodes.D,
    }) as { up: Phaser.Input.Keyboard.Key; down: Phaser.Input.Keyboard.Key; left: Phaser.Input.Keyboard.Key; right: Phaser.Input.Keyboard.Key }
    this.eKey = this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.E)

    // Handle late-arriving spawn_position and resident status updates.
    // onWSMessage registers on a module-level Set — keep the unsubscriber and
    // release it in teardown(), or the dead scene stays reachable forever.
    this.externalCleanups.push(onWSMessage((msg) => {
      if (msg.type === 'spawn_position' && this.player && !this.isTeleporting) {
        const x = msg.x as number
        const y = msg.y as number
        this.player.setPosition(x, y)
        this.cameras.main.centerOn(x, y)
      }
      if (msg.type === 'resident_status') {
        this._handleResidentStatusUpdate(msg as { resident_slug: string; status: string; mood_label?: string })
      }
      if (msg.type === 'sprite_updated') {
        const update = parseResidentSpriteUpdatedMessage(msg)
        if (update) void this._handleResidentSpriteUpdated(update)
      }
      if (msg.type === 'resident_move') {
        this._handleResidentMove(msg as { resident_slug: string; tile_x: number; tile_y: number; status: string })
      }
      if (msg.type === 'market_purchase') {
        this.marketPurchaseRuntime?.handleMessage(msg, this.time.now)
      }
      if (msg.type === 'resident_chat') {
        this._handleResidentChatStart(msg as { initiator_slug: string; target_slug: string; summary: string | null })
      }
      if (msg.type === 'resident_chat_end') {
        this._handleResidentChatEnd(msg as { initiator_slug: string; target_slug: string; summary: string; mood?: string })
      }
      if (msg.type === 'resident_greeting') {
        this._handleResidentGreeting(msg as { resident_slug: string; text: string })
      }
      if (msg.type === 'decor_updated') {
        this._handleDecorUpdated(msg as unknown as { resident_slug: string; home_location_id?: string | null; decor: DecorItem[] })
      }
      if (msg.type === 'world_event') {
        this._handleWorldEvent(msg as { event?: { type?: string; payload_json?: Record<string, unknown> | null }; phase?: string })
      }
      // B1: the backend emits no dedicated commission WS frame — the S4
      // notification(kind=commission) pushed to the acceptor on completion is
      // the only live signal, so it drives both the coin feedback and a
      // marker re-pull (the completed commission's issuer may drop its ❗).
      if (msg.type === 'notification' && msg.kind === 'commission') {
        this._handleCommissionNotification(msg)
      }
    }))

    // A full snapshot is delivered for every accepted WS update. The same
    // convergence store is refreshed via GET on initial load/auth reconnect.
    this.externalCleanups.push(subscribeCaravanProjection((projection) => {
      if (this.isShutdown) return
      this.caravanProjection = projection
      this._updateCaravanProjection()
    }))
    void refreshCaravanProjection().catch(() => { /* caravan visuals are optional */ })

    // E6: seed the weather layer from the currently active events (the WS
    // broadcast only covers transitions that happen while we're connected).
    getActiveEvents()
      .then(({ events }) => {
        if (this.isShutdown) return
        const weather = events.find((e) => e.type === 'weather')
        if (weather) this._applyWeatherFromPayload(weather.payload_json)
      })
      .catch(() => { /* weather is cosmetic — ignore fetch failures */ })

    // B1: seed the ❗ markers from the current open commissions, and re-pull
    // when the CommissionModal reports an accept/abandon (no WS broadcast
    // exists for those transitions — the modal is the only local mutator).
    this._refreshCommissionMarkers()
    this.externalCleanups.push(bridge.on('commissions:changed', () => this._refreshCommissionMarkers()))

    // Listen for camera pan requests from React UI (search, bulletin board).
    // bridge is a module singleton too — same teardown treatment as above.
    this.externalCleanups.push(bridge.on('camera:pan', (data: unknown) => {
      const { tile_x, tile_y } = data as { tile_x: number; tile_y: number }
      const targetX = tile_x * TILE_SIZE + TILE_SIZE / 2
      const targetY = tile_y * TILE_SIZE + TILE_SIZE
      this.cameras.main.pan(targetX, targetY, 600, 'Power2')
      // Move player to the target location
      this.player.setPosition(targetX, targetY)
    }))

    this.externalCleanups.push(bridge.on('minimap:teleport', (data: unknown) => {
      const { tileX, tileY } = data as { tileX: number; tileY: number; residentSlug?: string }
      this.teleportTo(tileX, tileY)
    }))

    // E10 group photo: snapshot the canvas area framing the player + NPC.
    this.externalCleanups.push(bridge.on('photo:take', (data: unknown) => {
      const { residentSlug } = data as { residentSlug: string }
      const idx = this.residents.findIndex((r) => r.slug === residentSlug)
      const npc = idx >= 0 ? this.npcSprites[idx] : null
      if (!npc || !this.player) return
      const cam = this.cameras.main
      const midX = ((this.player.x + npc.x) / 2 - cam.worldView.x) * cam.zoom
      const midY = ((this.player.y + npc.y) / 2 - cam.worldView.y) * cam.zoom
      const w = Math.min(480, cam.width * cam.zoom)
      const h = Math.min(360, cam.height * cam.zoom)
      const x = Math.max(0, Math.min(midX - w / 2, cam.width * cam.zoom - w))
      const y = Math.max(0, Math.min(midY - h / 2, cam.height * cam.zoom - h))
      const residentName = this.residents[idx].name
      this.game.renderer.snapshotArea(Math.round(x), Math.round(y), Math.round(w), Math.round(h), (image) => {
        const img = image as HTMLImageElement
        if (img.src) bridge.emit('photo:result', { dataUrl: img.src, residentSlug, residentName })
      })
    }))

    this.mapReady = true

    // Generate minimap texture after world is set up
    this.time.delayedCall(500, () => this.generateMinimapTexture())
  }

  private teleportTo(tileX: number, tileY: number): void {
    if (this.isTeleporting) return
    this.isTeleporting = true

    const cam = this.cameras.main
    const targetX = tileX * TILE_SIZE + TILE_SIZE / 2
    const targetY = tileY * TILE_SIZE + TILE_SIZE

    // Phase 1: Fade out (300ms)
    cam.fadeOut(300, 0, 0, 0)

    cam.once('camerafadeoutcomplete', () => {
      // Phase 2: Instant teleport — stop follow to prevent camera snap-back
      cam.stopFollow()
      this.player.setPosition(targetX, targetY)
      ;(this.player.body as Phaser.Physics.Arcade.Body).reset(targetX, targetY)
      cam.centerOn(targetX, targetY)
      // Restore follow after position is set
      cam.startFollow(this.player, true, 0.1, 0.1)

      // Phase 3: Fade in (500ms)
      cam.fadeIn(500, 0, 0, 0)

      cam.once('camerafadeincomplete', () => {
        this.isTeleporting = false
        // Persist teleported position to backend (bypass 4px dead-zone)
        sendWS({ type: 'move', x: Math.round(targetX), y: Math.round(targetY), direction: 'down' })
        // Also persist via REST API to ensure DB survives WS reconnect
        updatePlayerPosition(tileX, tileY).catch(() => { /* silent fail — WS path already sent */ })
        bridge.emit('teleport:complete', { tileX, tileY })
      })
    })
  }

  update() {
    if (!this.mapReady) return
    this._updateCaravanProjection()
    if (!this.player?.body) return

    // B1: ❗ markers track their resident sprite (which resident_move may be
    // tweening) — same follow pattern as the other-player labels below. Runs
    // before the teleport/input-focus early returns so markers never desync.
    this.commissionMarkers.forEach(({ text, sprite }) => text.setPosition(sprite.x, sprite.y - 52))

    if (this.isTeleporting) return

    // Pause movement when chat input is focused
    if (useGameStore.getState().inputFocused) {
      (this.player.body as Phaser.Physics.Arcade.Body).setVelocity(0, 0)
      this.player.anims.stop()
      return
    }

    const body = this.player.body as Phaser.Physics.Arcade.Body
    body.setVelocity(0, 0)

    const left = this.cursors.left.isDown || this.wasd.left.isDown
    const right = this.cursors.right.isDown || this.wasd.right.isDown
    const up = this.cursors.up.isDown || this.wasd.up.isDown
    const down = this.cursors.down.isDown || this.wasd.down.isDown

    // Allow both axes for diagonal movement
    if (left) body.setVelocityX(-PLAYER_SPEED)
    else if (right) body.setVelocityX(PLAYER_SPEED)

    if (up) body.setVelocityY(-PLAYER_SPEED)
    else if (down) body.setVelocityY(PLAYER_SPEED)

    // Normalize diagonal speed so it doesn't exceed PLAYER_SPEED
    if (body.velocity.length() > 0) {
      body.velocity.normalize().scale(PLAYER_SPEED)
    }

    // Determine animation direction (priority: horizontal > vertical)
    const moving = left || right || up || down
    if (!moving) {
      this.player.anims.stop()
    } else if (left) {
      this.player.anims.play('player-left-walk', true)
    } else if (right) {
      this.player.anims.play('player-right-walk', true)
    } else if (up) {
      this.player.anims.play('player-up-walk', true)
    } else if (down) {
      this.player.anims.play('player-down-walk', true)
    }

    // Broadcast player position when moving
    if (left || right || up || down) {
      const dir = left ? 'left' : right ? 'right' : up ? 'up' : 'down'
      sendPosition(this.player.x, this.player.y, dir)
    }

    // Broadcast player tile position for minimap
    const tileX = Math.floor(this.player.x / TILE_SIZE)
    const tileY = Math.floor(this.player.y / TILE_SIZE)
    const store = useGameStore.getState()
    if (tileX !== store.playerTileX || tileY !== store.playerTileY) {
      store.setPlayerTile(tileX, tileY)
    }

    // B3: lazily load decor for homes entering the camera view (throttled).
    if (this.time.now - this.lastDecorScan > 500) {
      this.lastDecorScan = this.time.now
      this._scanVisibleHomeDecor()
    }

    // Broadcast camera viewport for minimap
    const cam = this.cameras.main
    store.setCameraViewport({
      x: cam.scrollX / TILE_SIZE,
      y: cam.scrollY / TILE_SIZE,
      w: cam.width / cam.zoom / TILE_SIZE,
      h: cam.height / cam.zoom / TILE_SIZE,
    })

    // Render/update other players as sprites with name labels
    const onlinePlayers = useGameStore.getState().onlinePlayers
    onlinePlayers.forEach((p, playerId) => {
      if (!this.otherPlayerSprites.has(playerId)) {
        // Create sprite using shared player_atlas
        const sprite = this.physics.add.sprite(p.x, p.y, 'player_atlas', p.direction || 'down')
          .setSize(24, 24).setOffset(4, 8).setDepth(1)
        sprite.displayWidth = 40
        sprite.scaleY = sprite.scaleX
        // Tint to distinguish from local player
        sprite.setTint(0x88ccff)

        const label = this.add.text(p.x, p.y - 28, p.name, {
          fontSize: '11px', color: '#ffffff',
          backgroundColor: '#0ea5e9cc', padding: { x: 4, y: 2 },
        }).setOrigin(0.5).setDepth(5)

        this.otherPlayerSprites.set(playerId, { sprite, label })
      }

      const entry = this.otherPlayerSprites.get(playerId)!
      const { sprite, label } = entry

      // Smoothly move toward target position
      const dx = p.x - sprite.x
      const dy = p.y - sprite.y
      const dist = Math.sqrt(dx * dx + dy * dy)

      if (dist > 2) {
        sprite.setPosition(sprite.x + dx * 0.3, sprite.y + dy * 0.3)
        // Play walk animation based on direction
        const animKey = `player-${p.direction}-walk`
        if (sprite.anims.currentAnim?.key !== animKey) {
          sprite.anims.play(animKey, true)
        }
      } else {
        sprite.setPosition(p.x, p.y)
        sprite.anims.stop()
        sprite.setFrame(p.direction || 'down')
      }

      label.setPosition(sprite.x, sprite.y - 28)
    })
    // Remove disconnected players
    this.otherPlayerSprites.forEach((entry, playerId) => {
      if (!onlinePlayers.has(playerId)) {
        entry.sprite.destroy()
        entry.label.destroy()
        this.otherPlayerSprites.delete(playerId)
      }
    })

    // NPC proximity
    let nearest: ResidentData | null = null
    let nearestDist = Infinity
    let nearestIndex = -1
    for (let i = 0; i < this.npcSprites.length; i++) {
      const npc = this.npcSprites[i]
      const dist = Phaser.Math.Distance.Between(this.player.x, this.player.y, npc.x, npc.y)
      if (dist < NPC_INTERACT_DISTANCE && dist < nearestDist) {
        nearestDist = dist
        nearest = (npc as unknown as Record<string, unknown>).__residentData as ResidentData
        nearestIndex = i
      }
    }
    // Update label visibility: highlight only the nearest NPC's label
    for (let i = 0; i < this.npcLabels.length; i++) {
      this.npcLabels[i].setAlpha(i === nearestIndex ? 1 : 0.3)
    }
    bridge.emit('npc:nearby', nearest)

    if (Phaser.Input.Keyboard.JustDown(this.eKey) && nearest) {
      const cfg = STATUS_CONFIG[nearest.status]
      if (cfg?.canChat) {
        bridge.emit('npc:interact', nearest)
      } else if (nearest.status === 'sleeping') {
        bridge.emit('npc:interact', nearest) // ChatDrawer handles wake confirmation
      } else if (nearest.status === 'chatting') {
        bridge.emit('npc:interact', nearest) // ChatDrawer handles queueing
      }
    }

    // Other player proximity
    let nearestPlayer: { userId: string; name: string; x: number; y: number } | null = null
    let nearestPlayerDist = Infinity
    this.otherPlayerSprites.forEach(({ sprite }, playerId) => {
      const dist = Phaser.Math.Distance.Between(this.player.x, this.player.y, sprite.x, sprite.y)
      if (dist < PLAYER_INTERACT_DISTANCE && dist < nearestPlayerDist) {
        nearestPlayerDist = dist
        const p = onlinePlayers.get(playerId)
        if (p) {
          nearestPlayer = { userId: playerId, name: p.name, x: sprite.x, y: sprite.y }
        }
      }
    })
    bridge.emit('player:nearby', nearestPlayer)

    if (Phaser.Input.Keyboard.JustDown(this.eKey) && nearestPlayer && !nearest) {
      bridge.emit('player:interact', nearestPlayer)
    }
  }

  private _handleResidentMove(msg: {
    resident_slug: string
    tile_x: number
    tile_y: number
    status: string
  }): void {
    const idx = this.residents.findIndex(r => r.slug === msg.resident_slug)
    if (idx < 0) return

    const sprite = this.npcSprites[idx]
    if (!sprite) return

    this.marketPurchaseRuntime?.setResidentMoving(msg.resident_slug)

    const targetX = msg.tile_x * TILE_SIZE + TILE_SIZE / 2
    const targetY = msg.tile_y * TILE_SIZE + TILE_SIZE / 2

    // Update logical position
    this.residents[idx].tile_x = msg.tile_x
    this.residents[idx].tile_y = msg.tile_y
    this.residents[idx].status = msg.status

    // Animate sprite to new position
    clearStatusVisuals(this, sprite)
    applyStatusVisuals(this, sprite, 'walking', sprite.x, sprite.y)

    this.tweens.add({
      targets: sprite,
      x: targetX,
      y: targetY,
      duration: 800,
      ease: 'Linear',
      onComplete: () => {
        clearStatusVisuals(this, sprite)
        applyStatusVisuals(this, sprite, 'idle', targetX, targetY)
        this.residents[idx].status = 'idle'
        this.marketPurchaseRuntime?.setResidentTile(msg.resident_slug, msg.tile_x, msg.tile_y)
      },
    })
  }

  private _loadResidentAtlas(textureKey: string, textureUrl: string): Promise<boolean> {
    if (this.isShutdown) return Promise.resolve(false)
    if (this._residentAtlasIsReady(textureKey)) return Promise.resolve(true)
    if (this.textures.exists(textureKey)) this.textures.remove(textureKey)
    const activeLoad = this.residentTextureLoads.get(textureKey)
    if (activeLoad) return activeLoad

    const load = new Promise<boolean>((resolve) => {
      let settled = false
      const finish = () => {
        if (settled) return
        settled = true
        this.load.off(Phaser.Loader.Events.COMPLETE, finish)
        this.events.off(Phaser.Scenes.Events.SHUTDOWN, finish)
        resolve(!this.isShutdown && this._residentAtlasIsReady(textureKey))
      }
      this.load.on(Phaser.Loader.Events.COMPLETE, finish)
      this.events.once(Phaser.Scenes.Events.SHUTDOWN, finish)
      this.load.atlas(textureKey, textureUrl, STATIC_RESIDENT_ATLAS_JSON_URL)
      if (!this.load.isLoading()) this.load.start()
    })
    this.residentTextureLoads.set(textureKey, load)
    void load.finally(() => this.residentTextureLoads.delete(textureKey))
    return load
  }

  private _releaseUnusedResidentTexture(textureKey: string): void {
    if (!this.dynamicResidentTextureKeys.has(textureKey)) return
    if (this.npcSprites.some((sprite) => sprite.active && sprite.texture.key === textureKey)) return
    if (this.textures.exists(textureKey)) this.textures.remove(textureKey)
    this.dynamicResidentTextureKeys.delete(textureKey)
  }

  private async _handleResidentSpriteUpdated(msg: ResidentSpriteUpdatedMessage): Promise<void> {
    const resident = this.residents.find((entry) => entry.id === msg.resident_id)
      ?? (msg.slug ? this.residents.find((entry) => entry.slug === msg.slug) : undefined)
    if (!resident || this.isShutdown) return

    const sprite = this.residentSpritesById.get(resident.id)
    if (!sprite) return

    const nextResident: ResidentData = {
      ...resident,
      sprite_key: msg.sprite_key,
      sprite_url: msg.sprite_url,
      sprite_content_hash: msg.content_hash,
      sprite_generation_run_id: msg.run_id ?? null,
    }
    const nextTextureKey = residentTextureKey(nextResident)
    const nextTextureUrl = resolveResidentSpriteUrl(msg.sprite_url, API_BASE, msg.content_hash)
      ?? staticResidentSpriteUrl(msg.sprite_key)

    // Last event wins. If a slower prior download completes later, it is
    // discarded instead of reverting a newer published/rolled-back texture.
    this.desiredResidentTextureKeys.set(resident.id, nextTextureKey)
    if (msg.sprite_url) this.dynamicResidentTextureKeys.add(nextTextureKey)

    const loaded = await this._loadResidentAtlas(nextTextureKey, nextTextureUrl)
    const decision = decideResidentTextureLoad(
      loaded,
      this.isShutdown,
      this.desiredResidentTextureKeys.get(resident.id),
      nextTextureKey,
    )
    if (decision !== 'apply') {
      this._releaseUnusedResidentTexture(nextTextureKey)
      if (decision === 'keep-current') {
        console.warn(`Resident sprite update failed for ${resident.slug}; keeping current texture`)
      }
      return
    }

    const previousTextureKey = sprite.texture.key
    const previousFrame = sprite.frame.name
    sprite.setTexture(nextTextureKey)
    const nextTexture = this.textures.get(nextTextureKey)
    sprite.setFrame(nextTexture.has(previousFrame) ? previousFrame : 'down')

    Object.assign(resident, nextResident)
    ;(sprite as unknown as Record<string, unknown>).__residentData = resident
    this._releaseUnusedResidentTexture(previousTextureKey)
  }

  private _chatBubbles: Map<string, Phaser.GameObjects.Text> = new Map()

  private _handleResidentChatStart(msg: {
    initiator_slug: string
    target_slug: string
    summary: string | null
  }): void {
    for (const slug of [msg.initiator_slug, msg.target_slug]) {
      const idx = this.residents.findIndex(r => r.slug === slug)
      if (idx < 0) continue
      const sprite = this.npcSprites[idx]
      if (!sprite) continue

      clearStatusVisuals(this, sprite)
      applyStatusVisuals(this, sprite, 'socializing', sprite.x, sprite.y)
      this.residents[idx].status = 'socializing'

      // Add chat bubble
      const bubble = this.add.text(sprite.x, sprite.y - 60, '💬 ...', {
        fontSize: '12px',
        backgroundColor: '#1e293bdd',
        padding: { x: 6, y: 3 },
        color: '#e2e8f0',
        wordWrap: { width: 120 },
      }).setOrigin(0.5).setDepth(10)
      this._chatBubbles.set(slug, bubble)
    }
  }

  private _handleResidentChatEnd(msg: {
    initiator_slug: string
    target_slug: string
    summary: string
    mood?: string
  }): void {
    for (const slug of [msg.initiator_slug, msg.target_slug]) {
      const idx = this.residents.findIndex(r => r.slug === slug)
      if (idx < 0) continue
      const sprite = this.npcSprites[idx]
      if (!sprite) continue

      clearStatusVisuals(this, sprite)
      applyStatusVisuals(this, sprite, 'idle', sprite.x, sprite.y)
      this.residents[idx].status = 'idle'

      // Remove old bubble
      const oldBubble = this._chatBubbles.get(slug)
      oldBubble?.destroy()
      this._chatBubbles.delete(slug)
    }

    // Show summary bubble on initiator for 5 seconds
    const initIdx = this.residents.findIndex(r => r.slug === msg.initiator_slug)
    if (initIdx >= 0 && msg.summary) {
      const sprite = this.npcSprites[initIdx]
      if (sprite) {
        const summaryBubble = this.add.text(sprite.x, sprite.y - 80, msg.summary, {
          fontSize: '11px',
          backgroundColor: '#0f172add',
          padding: { x: 8, y: 4 },
          color: '#f1f5f9',
          wordWrap: { width: 150 },
        }).setOrigin(0.5).setDepth(10)

        this.time.delayedCall(5000, () => summaryBubble.destroy())
      }
    }
  }

  private _handleResidentGreeting(msg: { resident_slug: string; text: string }): void {
    // A3: show the greeting as a speech bubble over the resident for ~6s.
    const idx = this.residents.findIndex(r => r.slug === msg.resident_slug)
    if (idx < 0) return
    const sprite = this.npcSprites[idx]
    if (!sprite) return
    const bubble = this.add.text(sprite.x, sprite.y - 74, `👋 ${msg.text}`, {
      fontSize: '11px',
      backgroundColor: '#7c3aeedd',
      padding: { x: 8, y: 4 },
      color: '#f5f3ff',
      wordWrap: { width: 170 },
    }).setOrigin(0.5).setDepth(11)
    this.time.delayedCall(6000, () => bubble.destroy())
  }

  // ── B1 commission markers & reward feedback ──────────────────────

  /** Pull the open commissions once and sync the ❗ markers to their issuers. */
  private _refreshCommissionMarkers(): void {
    getCommissions('open')
      .then(({ commissions }) => {
        if (this.isShutdown) return
        const issuerIds = new Set(commissions.map((c) => c.issuer_resident_id))
        const slugs = new Set<string>()
        for (const r of this.residents) {
          if (issuerIds.has(r.id)) slugs.add(r.slug)
        }
        this._applyCommissionMarkers(slugs)
      })
      .catch(() => { /* markers are cosmetic — keep the previous set on failure */ })
  }

  /** Create/destroy marker texts so exactly `slugs` carry one; update() makes them follow. */
  private _applyCommissionMarkers(slugs: Set<string>): void {
    for (const [slug, entry] of this.commissionMarkers) {
      if (slugs.has(slug)) continue
      this.tweens.killTweensOf(entry.text)
      entry.text.destroy()
      this.commissionMarkers.delete(slug)
    }
    for (const slug of slugs) {
      if (this.commissionMarkers.has(slug)) continue
      const idx = this.residents.findIndex((r) => r.slug === slug)
      const sprite = idx >= 0 ? this.npcSprites[idx] : undefined
      if (!sprite) continue
      const text = this.add.text(sprite.x, sprite.y - 52, '❗', { fontSize: '16px' })
        .setOrigin(0.5).setDepth(4)
      // Gentle pulse so the marker reads as actionable without being loud.
      this.tweens.add({ targets: text, alpha: 0.45, duration: 700, yoyo: true, repeat: -1, ease: 'Sine.easeInOut' })
      this.commissionMarkers.set(slug, { text, sprite })
    }
  }

  /** Completion notification → coin float + marker re-pull. */
  private _handleCommissionNotification(msg: Record<string, unknown>): void {
    const payload = (msg.payload ?? {}) as Record<string, unknown>
    const reward = typeof payload.reward_sc === 'number' ? payload.reward_sc : 0
    if (reward > 0) this._playCoinReward(reward)
    this._refreshCommissionMarkers()
  }

  /** Float "+N 💰" up from the player and fade out — text + tween, no assets. */
  private _playCoinReward(amount: number): void {
    if (this.isShutdown) return
    const cam = this.cameras.main
    const x = this.player?.x ?? cam.worldView.centerX
    const y = this.player ? this.player.y - 24 : cam.worldView.centerY
    const text = this.add.text(x, y, `+${amount} 💰`, {
      fontSize: '18px',
      fontStyle: 'bold',
      color: '#fbbf24',
      stroke: '#78350f',
      strokeThickness: 3,
    }).setOrigin(0.5).setDepth(30)
    this.tweens.add({
      targets: text,
      y: y - 70,
      alpha: { from: 1, to: 0 },
      scale: { from: 1, to: 1.2 },
      duration: 1400,
      ease: 'Cubic.easeOut',
      onComplete: () => text.destroy(),
    })
  }

  // ── B3 home decor ────────────────────────────────────────────────

  /** Fetch decor once per resident whose home bbox intersects the camera view. */
  private _scanVisibleHomeDecor(): void {
    const view = this.cameras.main.worldView
    const viewL = view.x / TILE_SIZE - 2
    const viewT = view.y / TILE_SIZE - 2
    const viewR = (view.x + view.width) / TILE_SIZE + 2
    const viewB = (view.y + view.height) / TILE_SIZE + 2
    for (const r of this.residents) {
      if (!r.home_location_id || this.decorLoaded.has(r.slug)) continue
      const b = HOUSING_BOUNDS[r.home_location_id]
      if (!b) continue
      if (b[2] < viewL || b[0] > viewR || b[3] < viewT || b[1] > viewB) continue
      this.decorLoaded.add(r.slug)
      getHomeDecor(r.slug)
        .then((resp) => {
          if (!this.isShutdown) this._renderDecor(r.slug, resp.items)
        })
        .catch(() => {
          // Allow a retry on the next scan pass.
          this.decorLoaded.delete(r.slug)
        })
    }
  }

  /** Replace a resident's decor objects. Emoji stand-ins — no decor atlas yet. */
  private _renderDecor(slug: string, items: DecorItem[]): void {
    const old = this.decorTexts.get(slug)
    if (old) for (const t of old) t.destroy()
    this.decorTexts.delete(slug)

    const resident = this.residents.find((r) => r.slug === slug)
    const bounds = resident?.home_location_id ? HOUSING_BOUNDS[resident.home_location_id] : undefined
    if (!bounds || !this.decorLayer) return

    const texts: Phaser.GameObjects.Text[] = []
    for (const item of items) {
      const px = (bounds[0] + item.x) * TILE_SIZE + TILE_SIZE / 2
      const py = (bounds[1] + item.y) * TILE_SIZE + TILE_SIZE / 2
      const t = this.add.text(px, py, decorEmoji(item.item_code), { fontSize: '22px' })
        .setOrigin(0.5)
        .setAngle(item.rot)
      this.decorLayer.add(t)
      texts.push(t)
    }
    if (texts.length > 0) this.decorTexts.set(slug, texts)
  }

  private _handleDecorUpdated(msg: { resident_slug: string; home_location_id?: string | null; decor: DecorItem[] }): void {
    // The residents snapshot predates a lazy home claim — refresh it from the
    // broadcast so the renderer can resolve the bbox.
    const resident = this.residents.find((r) => r.slug === msg.resident_slug)
    if (resident && msg.home_location_id) resident.home_location_id = msg.home_location_id
    this.decorLoaded.add(msg.resident_slug)
    this._renderDecor(msg.resident_slug, msg.decor ?? [])
  }

  private _handleResidentStatusUpdate(msg: {
    resident_slug: string
    status: string
    mood_label?: string
  }): void {
    const idx = this.residents.findIndex(r => r.slug === msg.resident_slug)
    if (idx < 0) return
    const sprite = this.npcSprites[idx]
    if (!sprite) return

    this.residents[idx].status = msg.status
    // __residentData points at the same object, so the tooltip's npc:nearby
    // emit picks the fresh mood up on the next proximity pass.
    if (msg.mood_label) this.residents[idx].mood_label = msg.mood_label
    clearStatusVisuals(this, sprite)
    applyStatusVisuals(this, sprite, msg.status, sprite.x, sprite.y)
  }

  // ── Market-day caravan projection ─────────────────────────────────

  private _setupCaravanAnimations(): void {
    const directions: CaravanPose['direction'][] = ['down', 'left', 'right', 'up']
    for (const direction of directions) {
      const merchantFrames = Array.from({ length: 4 }, (_, frame) =>
        `${direction}-walk.${String(frame).padStart(3, '0')}`)
      const merchantAnim = `${CARAVAN_MERCHANT_TEXTURE}-${direction}-walk`
      if (this.textures.exists(CARAVAN_MERCHANT_TEXTURE)
        && merchantFrames.every((frame) => this.textures.get(CARAVAN_MERCHANT_TEXTURE).has(frame))
        && !this.anims.exists(merchantAnim)) {
        this.anims.create({
          key: merchantAnim,
          frames: merchantFrames.map((frame) => ({ key: CARAVAN_MERCHANT_TEXTURE, frame })),
          frameRate: 8,
          repeat: -1,
        })
      }

      const convoyFrames = Array.from({ length: 4 }, (_, frame) =>
        `${direction}-roll.${String(frame).padStart(3, '0')}`)
      const convoyAnim = `${CARAVAN_CONVOY_TEXTURE}-${direction}-roll`
      if (this.textures.exists(CARAVAN_CONVOY_TEXTURE)
        && convoyFrames.every((frame) => this.textures.get(CARAVAN_CONVOY_TEXTURE).has(frame))
        && !this.anims.exists(convoyAnim)) {
        this.anims.create({
          key: convoyAnim,
          frames: convoyFrames.map((frame) => ({ key: CARAVAN_CONVOY_TEXTURE, frame })),
          frameRate: 6,
          repeat: -1,
        })
      }
    }
  }

  private _ensureCaravanView(): CaravanView | null {
    if (this.caravanView) return this.caravanView
    if (!this.textures.exists(CARAVAN_MERCHANT_TEXTURE)
      || !this.textures.exists(CARAVAN_CONVOY_TEXTURE)
      || !this.textures.exists(CARAVAN_STALL_TEXTURE)) return null

    const convoy = this.add.sprite(0, 0, CARAVAN_CONVOY_TEXTURE, 'down')
      .setOrigin(0.5, 0.75)
      .setDepth(0)
    const stall = this.add.sprite(0, 0, CARAVAN_STALL_TEXTURE)
      // Match the convoy atlas' 0.75 pivot so opening the side panel does not
      // make the parked wagon jump half a tile downward.
      .setOrigin(0.5, 0.75).setDepth(0).setVisible(false)
    const merchant = this.add.sprite(-24, -6, CARAVAN_MERCHANT_TEXTURE, 'down')
      .setOrigin(0.5, 0.5)
      .setDepth(1)
    const label = this.add.text(0, -44, '靛篷商队', {
      fontSize: '12px',
      color: '#ffffff',
      backgroundColor: '#172f4fcc',
      padding: { x: 5, y: 2 },
    }).setOrigin(0.5).setDepth(2).setAlpha(0.8)
    const container = this.add.container(0, 0, [convoy, stall, merchant]).setDepth(1)
    this.caravanView = { container, convoy, merchant, stall, label }
    return this.caravanView
  }

  private _destroyCaravanView(): void {
    this.caravanView?.container.destroy(true)
    this.caravanView?.label.destroy()
    this.caravanView = null
  }

  private _setCaravanDirectionalFrame(
    sprite: Phaser.GameObjects.Sprite,
    animation: string,
    idleFrame: string,
    moving: boolean,
  ): void {
    if (moving && this.anims.exists(animation)) {
      sprite.anims.play(animation, true)
      return
    }
    sprite.anims.stop()
    if (this.textures.get(sprite.texture.key).has(idleFrame)) sprite.setFrame(idleFrame)
  }

  private _updateCaravanProjection(): void {
    const projection = this.caravanProjection
    const snapshot = projection?.snapshot ?? null
    this.marketHallRuntime?.update(snapshot, this.time.now)
    this.marketPurchaseRuntime?.update(snapshot, this.time.now)
    this.marketAmbience?.setPhase(snapshot?.visible ? snapshot.phase : null)
    if (this.player) {
      this.marketAmbience?.setListenerTile(
        this.player.x / TILE_SIZE,
        this.player.y / TILE_SIZE,
      )
    }
    const mode = caravanRenderMode(snapshot)
    if (!projection || mode === 'hidden') {
      this._destroyCaravanView()
      return
    }
    const pose = projectCaravanPose(projection)
    if (!pose) {
      this._destroyCaravanView()
      return
    }
    const view = this._ensureCaravanView()
    if (!view) return

    const placement = resolveCaravanWorldPlacement(pose, mode, this.caravanBlockedTiles)
    view.container.setPosition(placement.pixelX, placement.pixelY)
    view.container.setDepth(placement.depth)
    view.label.setPosition(placement.pixelX, placement.pixelY - 44).setDepth(3)
    view.convoy.setVisible(mode === 'convoy')
    view.stall.setVisible(mode === 'stall')

    if (mode === 'stall') {
      view.merchant.setPosition(27, 6)
      this._setCaravanDirectionalFrame(
        view.merchant,
        `${CARAVAN_MERCHANT_TEXTURE}-down-walk`,
        'down',
        false,
      )
      return
    }

    const merchantOffsets: Record<CaravanPose['direction'], { x: number; y: number }> = {
      down: { x: -24, y: -6 },
      up: { x: 24, y: 6 },
      left: { x: 19, y: 14 },
      right: { x: -19, y: 14 },
    }
    view.merchant.setPosition(merchantOffsets[pose.direction].x, merchantOffsets[pose.direction].y)
    this._setCaravanDirectionalFrame(
      view.convoy,
      `${CARAVAN_CONVOY_TEXTURE}-${pose.direction}-roll`,
      pose.direction,
      pose.moving,
    )
    this._setCaravanDirectionalFrame(
      view.merchant,
      `${CARAVAN_MERCHANT_TEXTURE}-${pose.direction}-walk`,
      pose.direction,
      pose.moving,
    )
  }

  // ── E6 weather rendering ─────────────────────────────────────────

  private _handleWorldEvent(msg: { event?: { type?: string; payload_json?: Record<string, unknown> | null }; phase?: string }): void {
    const ev = msg.event
    if (!ev || ev.type !== 'weather') return
    if (msg.phase === 'end') {
      // Segment ended; the next one's `start` broadcast re-dresses the sky.
      this.applyWeather('sunny', 0)
      return
    }
    this._applyWeatherFromPayload(ev.payload_json ?? null)
  }

  private _applyWeatherFromPayload(payload: Record<string, unknown> | null | undefined): void {
    const kindRaw = payload ? payload['kind'] : undefined
    const intensityRaw = payload ? payload['intensity'] : undefined
    this.applyWeather(
      typeof kindRaw === 'string' ? kindRaw : 'sunny',
      typeof intensityRaw === 'number' ? intensityRaw : 0.5,
    )
  }

  /** Swap the ambient weather layer: rain/snow particles (≤200 alive), cloudy tint, storm flashes. */
  applyWeather(kind: string, intensity: number): void {
    if (this.isShutdown) return
    this._clearWeather()
    const t = Math.max(0, Math.min(1, intensity))
    const w = this.scale.width
    const h = this.scale.height

    if (kind === 'cloudy' || kind === 'rain' || kind === 'storm') {
      const alpha = kind === 'cloudy' ? 0.1 + 0.15 * t : kind === 'rain' ? 0.15 : 0.28
      this.weatherOverlay = this.add.rectangle(0, 0, w, h, 0x1e293b, alpha)
        .setOrigin(0, 0).setScrollFactor(0).setDepth(19)
    }

    if (kind === 'rain' || kind === 'storm') {
      // Alive ≈ lifespan / frequency; targets stay well under the 200 budget.
      const aliveTarget = kind === 'storm' ? 180 : Math.round(90 + 80 * t)
      this.weatherEmitter = this.add.particles(0, 0, this._weatherTexture('rain'), {
        x: { min: -40, max: w + 40 },
        y: -12,
        lifespan: 2200,
        speedY: { min: 340 + 160 * t, max: 460 + 200 * t },
        speedX: { min: -60, max: -15 },
        alpha: { min: 0.35, max: 0.7 },
        scaleY: { min: 0.8, max: 1.5 },
        quantity: 1,
        frequency: 2200 / aliveTarget,
        maxAliveParticles: 200,
      })
      this.weatherEmitter.setScrollFactor(0).setDepth(20)
    } else if (kind === 'snow') {
      const aliveTarget = Math.round(50 + 90 * t)
      this.weatherEmitter = this.add.particles(0, 0, this._weatherTexture('snow'), {
        x: { min: -20, max: w + 20 },
        y: -8,
        lifespan: 9000,
        speedY: { min: 50 + 40 * t, max: 90 + 70 * t },
        speedX: { min: -25, max: 25 },
        alpha: { start: 0.85, end: 0.1 },
        scale: { min: 0.5, max: 1.1 },
        quantity: 1,
        frequency: 9000 / aliveTarget,
        maxAliveParticles: 200,
      })
      this.weatherEmitter.setScrollFactor(0).setDepth(20)
    }

    if (kind === 'storm') {
      this.stormFlashTimer = this.time.addEvent({
        delay: 3800 + Math.random() * 2400,
        loop: true,
        callback: () => this.cameras.main.flash(140, 255, 255, 255),
      })
    }
  }

  private _clearWeather(): void {
    this.weatherEmitter?.destroy()
    this.weatherEmitter = null
    this.weatherOverlay?.destroy()
    this.weatherOverlay = null
    this.stormFlashTimer?.remove()
    this.stormFlashTimer = null
  }

  /** Lazily generate the tiny white particle textures (2x9 rain streak / 3x3 snow flake). */
  private _weatherTexture(kind: 'rain' | 'snow'): string {
    const key = `weather-${kind}-px`
    if (!this.textures.exists(key)) {
      const g = this.make.graphics({}, false)
      g.fillStyle(0xdbeafe, 1)
      if (kind === 'rain') {
        g.fillRect(0, 0, 2, 9)
        g.generateTexture(key, 2, 9)
      } else {
        g.fillRect(0, 0, 3, 3)
        g.generateTexture(key, 3, 3)
      }
      g.destroy()
    }
    return key
  }
}
