import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Suspense, lazy } from 'react'
import { useGameStore } from './stores/gameStore'
import { LoginPage } from './pages/LoginPage'
import { AuthCallbackPage } from './pages/AuthCallbackPage'
import { AchievementToast } from './components/AchievementToast'
import { EncounterCard } from './components/EncounterCard'

// Heavy pages are code-split so the login/first-load bundle stays lean:
// GamePage pulls in Phaser (~1.4MB), ProfilePage pulls in @uiw/react-md-editor
// (via ResidentEditor), AdminPage pulls in the whole admin panel tree. These
// pages use named exports, so adapt them to the default export React.lazy wants.
const GamePage = lazy(() => import('./pages/GamePage').then((m) => ({ default: m.GamePage })))
const ForgePage = lazy(() => import('./pages/ForgePage').then((m) => ({ default: m.ForgePage })))
const ProfilePage = lazy(() => import('./pages/ProfilePage').then((m) => ({ default: m.ProfilePage })))
const OnboardingPage = lazy(() => import('./pages/OnboardingPage').then((m) => ({ default: m.OnboardingPage })))
const AdminPage = lazy(() => import('./pages/AdminPage').then((m) => ({ default: m.AdminPage })))

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const token = useGameStore((s) => s.token)
  if (!token) return <Navigate to="/login" replace />
  return <>{children}</>
}

function PageFallback() {
  return (
    <div
      style={{
        height: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'var(--text-muted)',
        background: 'var(--bg-page)',
      }}
    >
      加载中…
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AchievementToast />
      <EncounterCard />
      <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/auth/callback" element={<AuthCallbackPage />} />
          <Route path="/onboarding" element={<ProtectedRoute><OnboardingPage /></ProtectedRoute>} />
          <Route path="/" element={<ProtectedRoute><GamePage /></ProtectedRoute>} />
          <Route path="/forge" element={<ProtectedRoute><ForgePage /></ProtectedRoute>} />
          <Route path="/profile" element={<ProtectedRoute><ProfilePage /></ProtectedRoute>} />
          <Route path="/admin" element={<ProtectedRoute><AdminPage /></ProtectedRoute>} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
