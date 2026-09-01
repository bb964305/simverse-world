import { lazy, Suspense, useState } from 'react'
import { TopNav } from '../components/TopNav'
import { ProfileSidebar } from '../components/profile/ProfileSidebar'
import { useGameStore } from '../stores/gameStore'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLocale } from '../services/locale'

const ResidentList = lazy(() => import('../components/profile/ResidentList').then((module) => ({ default: module.ResidentList })))
const ConversationHistory = lazy(() => import('../components/profile/ConversationHistory').then((module) => ({ default: module.ConversationHistory })))
const TransactionHistory = lazy(() => import('../components/profile/TransactionHistory').then((module) => ({ default: module.TransactionHistory })))
const ResidentEditor = lazy(() => import('../components/profile/ResidentEditor').then((module) => ({ default: module.ResidentEditor })))
const SettingsPanel = lazy(() => import('../components/profile/SettingsPanel').then((module) => ({ default: module.SettingsPanel })))
const AchievementsPanel = lazy(() => import('../components/profile/AchievementsPanel').then((module) => ({ default: module.AchievementsPanel })))
const FeedList = lazy(() => import('../components/profile/FeedList').then((module) => ({ default: module.FeedList })))
const WeeklyRecap = lazy(() => import('../components/profile/WeeklyRecap').then((module) => ({ default: module.WeeklyRecap })))
const ExplorationCodex = lazy(() => import('../components/profile/ExplorationCodex').then((module) => ({ default: module.ExplorationCodex })))
const CreatorDashboard = lazy(() => import('../components/profile/CreatorDashboard').then((module) => ({ default: module.CreatorDashboard })))

function ProfileLoading() {
  const locale = useLocale((state) => state.locale)
  return <div style={{ color: 'var(--text-muted)', padding: 24 }}>{locale === 'en' ? 'Loading…' : '加载中…'}</div>
}

export function ProfilePage() {
  const profileTab = useGameStore((s) => s.profileTab)
  const [residentCount, setResidentCount] = useState(0)
  const [editingSlug, setEditingSlug] = useState<string | null>(null)
  const isMobile = useIsMobile()

  if (editingSlug) {
    return (
      <>
        <TopNav />
        <div style={{ marginTop: 'var(--nav-height)' }}>
          <Suspense fallback={<ProfileLoading />}><ResidentEditor slug={editingSlug} onBack={() => setEditingSlug(null)} /></Suspense>
        </div>
      </>
    )
  }

  return (
    <>
      <TopNav />
      <div
        data-testid="profile-layout"
        style={{
          marginTop: 'var(--nav-height)',
          display: 'flex',
          flexDirection: isMobile ? 'column' : 'row',
          height: 'calc(100vh - var(--nav-height))',
          overflow: 'hidden',
        }}
      >
        <ProfileSidebar residentCount={residentCount} />
        <div style={{ flex: 1, padding: isMobile ? 16 : 32, overflowY: 'auto', minWidth: isMobile ? 0 : undefined, minHeight: isMobile ? 0 : undefined }}>
          <Suspense fallback={<ProfileLoading />}>
          {profileTab === 'residents' && (
            <ResidentList
              onResidentCountChange={setResidentCount}
              onEditResident={setEditingSlug}
            />
          )}
          {profileTab === 'creator' && <CreatorDashboard />}
          {profileTab === 'conversations' && <ConversationHistory />}
          {profileTab === 'transactions' && <TransactionHistory />}
          {profileTab === 'achievements' && <AchievementsPanel />}
          {profileTab === 'feed' && <FeedList />}
          {profileTab === 'recap' && <WeeklyRecap />}
          {profileTab === 'codex' && <ExplorationCodex />}
          {profileTab === 'settings' && <SettingsPanel />}
          </Suspense>
        </div>
      </div>
    </>
  )
}
