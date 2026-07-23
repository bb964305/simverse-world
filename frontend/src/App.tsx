import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { Suspense, lazy } from 'react'
import { useGameStore } from './stores/gameStore'
import { LoginPage } from './pages/LoginPage'
import { AuthCallbackPage } from './pages/AuthCallbackPage'
import { AchievementToast } from './components/AchievementToast'
import { ConnectionBanner } from './components/ConnectionBanner'
import { EncounterCard } from './components/EncounterCard'
import { ErrorBoundary } from './components/ErrorBoundary'

// Heavy pages are code-split so the login/first-load bundle stays lean:
// GamePage pulls in Phaser (~1.4MB), ProfilePage pulls in @uiw/react-md-editor
// (via ResidentEditor), AdminPage pulls in the whole admin panel tree. These
// pages use named exports, so adapt them to the default export React.lazy wants.
const GamePage = lazy(() => import('./pages/GamePage').then((m) => ({ default: m.GamePage })))
const LandingPage = lazy(() => import('./pages/LandingPage').then((m) => ({ default: m.LandingPage })))
const ForgePage = lazy(() => import('./pages/ForgePage').then((m) => ({ default: m.ForgePage })))
const ProfilePage = lazy(() => import('./pages/ProfilePage').then((m) => ({ default: m.ProfilePage })))
const OnboardingPage = lazy(() => import('./pages/OnboardingPage').then((m) => ({ default: m.OnboardingPage })))
const AdminPage = lazy(() => import('./pages/AdminPage').then((m) => ({ default: m.AdminPage })))
const GraphPage = lazy(() => import('./pages/GraphPage').then((m) => ({ default: m.GraphPage })))
const SeasonsPage = lazy(() => import('./pages/SeasonsPage').then((m) => ({ default: m.SeasonsPage })))
const DebatesPage = lazy(() => import('./pages/DebatesPage').then((m) => ({ default: m.DebatesPage })))
const CapsulesPage = lazy(() => import('./pages/CapsulesPage').then((m) => ({ default: m.CapsulesPage })))

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const token = useGameStore((s) => s.token)
  if (!token) return <Navigate to="/login" replace />
  return <>{children}</>
}

function HomeRoute() {
  const token = useGameStore((s) => s.token)
  return token ? <GamePage /> : <LandingPage />
}

function LoginRoute() {
  const token = useGameStore((s) => s.token)
  return token ? <Navigate to="/" replace /> : <LoginPage />
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

function AuthenticatedOverlays() {
  const token = useGameStore((state) => state.token)
  const { pathname } = useLocation()
  if (!token || pathname === '/login' || pathname === '/auth/callback') return null

  return (
    <>
      <ConnectionBanner />
      <AchievementToast />
      {(pathname === '/' || pathname === '/play') && <EncounterCard />}
    </>
  )
}

export function AppRoutes() {
  return (
    <>
      <AuthenticatedOverlays />
      <ErrorBoundary>
        <Suspense fallback={<PageFallback />}>
          <Routes>
            <Route path="/login" element={<LoginRoute />} />
            <Route path="/auth/callback" element={<AuthCallbackPage />} />
            <Route path="/onboarding" element={<ProtectedRoute><OnboardingPage /></ProtectedRoute>} />
            <Route path="/" element={<HomeRoute />} />
            {/* The game also lives at /play — many entry points (landing CTA,
                onboarding redirect, Admin/Forge/ErrorBoundary) navigate here.
                Without this route React Router matches nothing → blank screen. */}
            <Route path="/play" element={<ProtectedRoute><GamePage /></ProtectedRoute>} />
            <Route path="/forge" element={<ProtectedRoute><ForgePage /></ProtectedRoute>} />
            <Route path="/profile" element={<ProtectedRoute><ProfilePage /></ProtectedRoute>} />
            <Route path="/admin" element={<ProtectedRoute><AdminPage /></ProtectedRoute>} />
            <Route path="/graph" element={<ProtectedRoute><GraphPage /></ProtectedRoute>} />
            <Route path="/seasons" element={<ProtectedRoute><SeasonsPage /></ProtectedRoute>} />
            <Route path="/debates" element={<ProtectedRoute><DebatesPage /></ProtectedRoute>} />
            <Route path="/capsules" element={<ProtectedRoute><CapsulesPage /></ProtectedRoute>} />
          </Routes>
        </Suspense>
      </ErrorBoundary>
    </>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  )
}
