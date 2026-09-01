import { useEffect, useState } from 'react'
import { bridge } from '../game/phaserBridge'
import { useLocale } from '../services/locale'

interface NearbyBuilding {
  key: 'experiment' | 'market'
  name: string
  icon: string
}

export function BuildingTooltip() {
  const locale = useLocale((state) => state.locale)
  const [building, setBuilding] = useState<NearbyBuilding | null>(null)

  useEffect(() => bridge.on('building:nearby', (value) => {
    setBuilding((value as NearbyBuilding | null) ?? null)
  }), [])

  if (!building) return null
  return (
    <div style={{
      position: 'fixed', left: '50%', bottom: 82, transform: 'translateX(-50%)',
      zIndex: 35, pointerEvents: 'none', border: '1px solid rgba(255,255,255,.15)',
      background: 'rgba(15,23,42,.88)', color: '#f8fafc', borderRadius: 8,
      padding: '7px 11px', fontSize: 12, boxShadow: '0 8px 24px rgba(0,0,0,.25)',
    }}>
      {building.icon} {locale === 'en'
        ? (building.key === 'experiment' ? 'Experiment Lab' : 'Market Hall')
        : building.name} · {locale === 'en' ? <>Press <b>E</b> to enter</> : <>按 <b>E</b> 进入</>}
    </div>
  )
}
