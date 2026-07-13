import Phaser from 'phaser'
import { bridge } from './phaserBridge'
import { applyStatusVisuals, clearStatusVisuals, releaseAllStatusVisuals, STATUS_CONFIG } from './StatusVisuals'
import { useGameStore } from '../stores/gameStore'
import { sendPosition, sendWS, onWSMessage } from '../services/ws'
import { updatePlayerPosition, getHomeDecor, decorEmoji, HOUSING_BOUNDS, type DecorItem } from '../services/api'

const TILE_SIZE = 32
const PLAYER_SPEED = 160
const NPC_INTERACT_DISTANCE = 60
const PLAYER_INTERACT_DISTANCE = 80

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
  slug: string
  name: string
  status: string
  sprite_key: string
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

let gameInstance: Phaser.Game | null = null

export function destroyGame(): void {
  if (gameInstance) {
    gameInstance.destroy(true)
    gameInstance = null
  }
}

export function initGame(container: HTMLElement): void {
  if (gameInstance) return
  const zoom = Math.max(1, window.innerWidth / 4400)
  gameInstance = new Phaser.Game({
    type: Phaser.AUTO,
    width: container.clientWidth / zoom,
    height: container.clientHeight / zoom,
    parent: container,
    pixelArt: true,
    physics: { default: 'arcade', arcade: { gravity: { x: 0, y: 0 } } },
    scene: [MainScene],
    scale: { zoom },
  })
}

class MainScene extends Phaser.Scene {
  private player!: Phaser.Physics.Arcade.Sprite
  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys
  private wasd!: { up: Phaser.Input.Keyboard.Key; down: Phaser.Input.Keyboard.Key; left: Phaser.Input.Keyboard.Key; right: Phaser.Input.Keyboard.Key }
  private eKey!: Phaser.Input.Keyboard.Key
  private npcSprites: Phaser.Physics.Arcade.Sprite[] = []
  private npcLabels: Phaser.GameObjects.Text[] = []
  private residents: ResidentData[] = []
  private mapReady = false
  private isTeleporting = false
  private otherPlayerSprites: Map<string, { sprite: Phaser.Physics.Arcade.Sprite; label: Phaser.GameObjects.Text }> = new Map()
  // B3 home decor: emoji objects per resident slug, rendered below characters.
  private decorLayer: Phaser.GameObjects.Layer | null = null
  private decorTexts: Map<string, Phaser.GameObjects.Text[]> = new Map()
  private decorLoaded: Set<string> = new Set()
  private lastDecorScan = 0
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
    // Decor objects die with the scene; drop the references so a late
    // getHomeDecor() resolution can't touch dead text objects.
    this.decorTexts.clear()
    this.decorLoaded.clear()
    this.decorLayer = null
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

    const spriteKey = useGameStore.getState().playerSpriteKey
    this.load.atlas(
      'player_atlas',
      `/assets/village/agents/${spriteKey}/texture.png`,
      '/assets/village/agents/sprite.json',
    )
  }

  async create() {
    const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'
    try {
      const resp = await fetch(`${API}/residents`)
      this.residents = resp.ok ? (await resp.json() as ResidentData[]) : []
    } catch {
      this.residents = []
    }

    // The scene can be destroyed while the fetch above is in flight (React
    // StrictMode mounts, then immediately unmounts → destroyGame). Touching
    // this.textures / this.load on a dead scene throws, so bail out here.
    if (this.isShutdown) return

    // Load resident sprites
    const spritesToLoad: string[] = []
    for (const r of this.residents) {
      if (!this.textures.exists(r.sprite_key)) {
        spritesToLoad.push(r.sprite_key)
        this.load.atlas(
          r.sprite_key,
          `/assets/village/agents/${r.sprite_key}/texture.png`,
          '/assets/village/agents/sprite.json',
        )
      }
    }

    if (spritesToLoad.length > 0 && !this.load.isLoading()) {
      this.load.start()
      await new Promise<void>((resolve) => this.load.once('complete', resolve))
    }

    this.setupWorld()
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

    // B3 decor layer: above ground tiles (depth 0), below characters (depth 1).
    this.decorLayer = this.add.layer().setDepth(0.5)

    // Player — use spawn position from store (set by backend spawn_position message)
    const { spawnX, spawnY } = useGameStore.getState()
    this.player = this.physics.add.sprite(spawnX, spawnY, 'player_atlas', 'down')
      .setSize(24, 24).setOffset(4, 8).setDepth(1)
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

    // NPCs
    for (const r of this.residents) {
      const x = r.tile_x * TILE_SIZE + TILE_SIZE / 2
      const y = r.tile_y * TILE_SIZE + TILE_SIZE

      const sprite = this.physics.add.sprite(x, y, r.sprite_key, 'down')
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
      if (msg.type === 'resident_move') {
        this._handleResidentMove(msg as { resident_slug: string; tile_x: number; tile_y: number; status: string })
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
    }))

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
    if (!this.mapReady || !this.player?.body) return
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
      },
    })
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
}
