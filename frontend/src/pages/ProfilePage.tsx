import { useState } from 'react'
import { TopNav } from '../components/TopNav'
import { ProfileSidebar } from '../components/profile/ProfileSidebar'
import { ResidentList } from '../components/profile/ResidentList'
import { ConversationHistory } from '../components/profile/ConversationHistory'
import { TransactionHistory } from '../components/profile/TransactionHistory'
import { ResidentEditor } from '../components/profile/ResidentEditor'
import { SettingsPanel } from '../components/profile/SettingsPanel'
import { AchievementsPanel } from '../components/profile/AchievementsPanel'
import { FeedList } from '../components/profile/FeedList'
import { WeeklyRecap } from '../components/profile/WeeklyRecap'
import { ExplorationCodex } from '../components/profile/ExplorationCodex'
import { useGameStore } from '../stores/gameStore'

export function ProfilePage() {
  const profileTab = useGameStore((s) => s.profileTab)
  const [residentCount, setResidentCount] = useState(0)
  const [editingSlug, setEditingSlug] = useState<string | null>(null)

  if (editingSlug) {
    return (
      <>
        <TopNav />
        <div style={{ marginTop: 'var(--nav-height)' }}>
          <ResidentEditor slug={editingSlug} onBack={() => setEditingSlug(null)} />
        </div>
      </>
    )
  }

  return (
    <>
      <TopNav />
      <div style={{ marginTop: 'var(--nav-height)', display: 'flex', height: 'calc(100vh - var(--nav-height))', overflow: 'hidden' }}>
        <ProfileSidebar residentCount={residentCount} />
        <div style={{ flex: 1, padding: 32, overflowY: 'auto' }}>
          {profileTab === 'residents' && (
            <ResidentList
              onResidentCountChange={setResidentCount}
              onEditResident={setEditingSlug}
            />
          )}
          {profileTab === 'conversations' && <ConversationHistory />}
          {profileTab === 'transactions' && <TransactionHistory />}
          {profileTab === 'achievements' && <AchievementsPanel />}
          {profileTab === 'feed' && <FeedList />}
          {profileTab === 'recap' && <WeeklyRecap />}
          {profileTab === 'codex' && <ExplorationCodex />}
          {profileTab === 'settings' && <SettingsPanel />}
        </div>
      </div>
    </>
  )
}
