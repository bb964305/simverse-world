import { useEffect, useRef } from 'react'
import { TopNav } from '../components/TopNav'
import { ChatDrawer } from '../components/ChatDrawer'
import { NpcTooltip } from '../components/NpcTooltip'
import { CoinNotification } from '../components/CoinNotification'
import { PhotoBooth } from '../components/PhotoBooth'
import { DecorEditor } from '../components/DecorEditor'
import { MinimapOverlay } from '../components/minimap/MinimapOverlay'
import { BuildingTooltip } from '../components/BuildingTooltip'
import { WorldWeb3Dock } from '../components/WorldWeb3Dock'
import { WorldWelcomeGuide } from '../components/WorldWelcomeGuide'
import { useGameStore } from '../stores/gameStore'
import { connectWS } from '../services/ws'
import { getSettings } from '../services/api'
import { bridge } from '../game/phaserBridge'
import '../styles/game-shell.css'

export function GamePage() {
  const containerRef = useRef<HTMLDivElement>(null)
  const chatOpen = useGameStore((s) => s.chatOpen)

  useEffect(() => {
    const gameOwner = Symbol('game-page')
    let destroyed = false
    connectWS()

    const startGame = async () => {
      // Fetch player sprite key before initialising the Phaser game so that
      // GameScene.preload() can read it synchronously from the store.
      try {
        const settings = await getSettings()
        if (settings.character?.sprite_key) {
          useGameStore.getState().setPlayerSpriteKey(settings.character.sprite_key)
        }
      } catch {
        // Keep default sprite key on failure
      }

      const { initGame } = await import('../game/GameScene')
      if (!destroyed && containerRef.current) {
        initGame(containerRef.current, gameOwner)
      }
    }

    startGame()

    // Listen for player interact events from the Phaser game scene
    const unsubPlayerInteract = bridge.on('player:interact', (data) => {
      const { userId, name } = data as { userId: string; name: string; x: number; y: number }
      useGameStore.getState().setChatTarget({ type: 'player', userId, name })
    })

    return () => {
      destroyed = true
      unsubPlayerInteract()
      useGameStore.getState().closeChat()
      // Keep the authenticated socket alive across route changes. Other pages
      // consume live frames too, and logout remains the deliberate disconnect.
      import('../game/GameScene').then(({ destroyGame }) => {
        destroyGame(gameOwner)
      })
    }
  }, [])

  return (
    <>
      <TopNav />
      <MinimapOverlay />
      <div
        ref={containerRef}
        id="game-container"
        className={`game-shell__canvas${chatOpen ? ' game-shell__canvas--chat-open' : ''}`}
      />
      <NpcTooltip />
      <BuildingTooltip />
      <ChatDrawer />
      <CoinNotification />
      <PhotoBooth />
      <DecorEditor />
      <WorldWeb3Dock />
      <WorldWelcomeGuide />
    </>
  )
}
